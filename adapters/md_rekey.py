#!/usr/bin/env python3
"""MD BOP - re-key the preserved captures on the canonical natural key.

WHY
  Details.aspx?result=<guid> is a PER-REQUEST TOKEN, not a record id: the same record
  gets a fresh GUID on every search. So GUID-based counts double-counted, and the
  ASC/DESC overlap test could never find a match - it was measuring the token churn,
  not the data. Every GUID-derived figure reported before this is void.

KEY
  canonical: State(MD) + License# + Requirement(type)
  Blank licence numbers (Denied/Pending records MD never issued a number for) fall back
  to (name, type) so genuinely distinct businesses are not collapsed into one row. They
  are kept verbatim - never dropped, never assigned a synthetic id.

INDEPENDENT AXIS
  A partition cannot audit itself, so completeness is not judged from the per-type
  harvest's own counts. The all-types ASC/DESC halves were captured under a DIFFERENT
  partition (no type filter, ordered by licence number), so testing whether every
  all-types record appears in the per-type set is a genuine independent membership
  test - and it costs no crawl and no captcha solve.
"""
import collections
import json
import sys

PER_TYPE = "md_guids_full.json"
HALVES = "md_halves.json"
OUT = "md_records_natural.json"


def log(m):
    print("[mdK] " + str(m), flush=True)


def nkey(r):
    """State(MD) + License# + type; blank licence numbers fall back to (name, type)."""
    lic = (r.get("license_no") or "").strip()
    typ = (r.get("license_type") or "").strip()
    if lic:
        return ("MD", lic, typ)
    return ("MD", "", (r.get("name") or "").strip().upper(), typ)


def dedupe(rows):
    out = {}
    for r in rows:
        out.setdefault(nkey(r), r)
    return out


def main():
    per = json.load(open(PER_TYPE, encoding="utf-8"))
    h = json.load(open(HALVES, encoding="utf-8"))
    asc, desc = h["asc"], h["desc"]

    log("=" * 78)
    log("1. RE-KEY ON THE NATURAL KEY")
    log("=" * 78)
    per_u = dedupe(per)
    log("  per-type capture : %s raw rows -> %s distinct records (GUID count inflated by %s)"
        % (format(len(per), ","), format(len(per_u), ","),
           format(len(per) - len(per_u), ",")))

    asc_u, desc_u = dedupe(asc), dedupe(desc)
    all_u = dict(asc_u)
    all_u.update(desc_u)
    overlap = set(asc_u) & set(desc_u)
    log("  all-types halves : ASC %s / DESC %s distinct; union %s; OVERLAP %s"
        % (format(len(asc_u), ","), format(len(desc_u), ","),
           format(len(all_u), ","), format(len(overlap), ",")))
    log("    -> %s" % ("halves MEET in the middle: all-types coverage is COMPLETE"
                       if overlap else "halves DISJOINT: coverage NOT proven"))

    log("")
    log("2. PER-TYPE NATURAL-KEY COUNTS (authoritative roster)")
    log("=" * 78)
    c = collections.Counter(r["license_type"] for r in per_u.values())
    for k, v in c.most_common():
        log("    %-36s %6s" % (k or "(blank)", format(v, ",")))
    log("    %-36s %6s" % ("TOTAL", format(len(per_u), ",")))
    HIDDEN = {"Prescription Drug Drop-Off", "Prescription Drug Repository",
              "Technician Training Program"}
    log("")
    log("  hidden types (not selectable in the dropdown; recovered from the all-types")
    log("  halves, so these counts are a LOWER BOUND, not proven complete):")
    for t in sorted(HIDDEN):
        log("    %-36s %6d" % (t, c.get(t, 0)))

    blanks = [r for r in per_u.values() if not (r.get("license_no") or "").strip()]
    log("")
    log("  blank licence numbers kept verbatim: %d  statuses=%s"
        % (len(blanks), dict(collections.Counter(r["status"] for r in blanks))))
    log("    (keyed on (name, type) so distinct businesses are not collapsed)")

    log("")
    log("3. INDEPENDENT-AXIS MEMBERSHIP TEST")
    log("=" * 78)
    log("  axis: the all-types ASC/DESC capture - a DIFFERENT partition (no type")
    log("        filter, ordered by licence number) than the per-type harvest.")
    missing = set(all_u) - set(per_u)
    log("  all-types distinct records : %s" % format(len(all_u), ","))
    log("  present in the per-type set: %s" % format(len(all_u) - len(missing), ","))
    log("  MISSING from per-type      : %s" % format(len(missing), ","))
    if missing:
        log("  -> the per-type harvest does NOT contain everything the all-types")
        log("     capture saw. Sample of what is missing:")
        for k in list(missing)[:10]:
            r = all_u[k]
            log("       %-12s %-40s %-24s %s"
                % (r["license_no"] or "(blank)", r["name"][:40], r["license_type"][:24],
                   r["status"]))
        bytype = collections.Counter(all_u[k]["license_type"] for k in missing)
        log("     by type: %s" % dict(bytype))
    else:
        log("  -> 0 missing: every record the independent axis saw is in the per-type set")

    final = dict(per_u)
    added = 0
    for k, r in all_u.items():
        if k not in final:
            final[k] = r
            added += 1
    log("")
    log("  folding the independent capture in adds %s record(s) -> FINAL %s"
        % (format(added, ","), format(len(final), ",")))

    if not final:
        raise RuntimeError("REFUSING TO WRITE: zero records")
    json.dump([{**r, "__nkey": " | ".join(str(x) for x in k)}
               for k, r in final.items()], open(OUT, "w", encoding="utf-8"), indent=1)
    log("  wrote %s" % OUT)

    log("")
    log("FINAL per-type (natural key):")
    fc = collections.Counter(r["license_type"] for r in final.values())
    for k, v in fc.most_common():
        log("    %-36s %6s" % (k or "(blank)", format(v, ",")))
    log("    %-36s %6s" % ("TOTAL", format(len(final), ",")))
    log("")
    log("FINAL per-status:")
    for k, v in collections.Counter(r["status"] for r in final.values()).most_common():
        log("    %-36s %6s" % (k or "(blank)", format(v, ",")))


if __name__ == "__main__":
    main()
