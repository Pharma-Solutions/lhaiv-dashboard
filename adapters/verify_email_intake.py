#!/usr/bin/env python3
"""Verification for the email-intake engine. Generic: driven by the registry.

The outputs are transcriptions, not crawls, so there is no cap to hunt. What can
still go wrong is transcription: a dropped sheet, a coerced value, a zip "fixed"
into the wrong number, a column the enricher will not find. The independent axis
is therefore the SOURCE WORKBOOK, re-read here by a different code path than the
engine used, and the test is MEMBERSHIP in both directions. Counts alone would
pass even if two sheets had been swapped.

Check group R is a regression gate: the new generic engine must reproduce the
output of the bespoke WY/GA adapters, which were verified 32/32 before being
replaced. Refactoring that silently changes data is the failure this prevents.

Usage:  python verify_email_intake.py [WY GA MD] [--baseline DIR]
"""
import argparse
import collections
import csv
import importlib.util
import os
import re
import sys

import openpyxl

import email_sources as ES

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA = r"C:\Verified\data"
ENRICH = r"C:\Verified\lhaiv-dashboard-pipeline\verified_enrich.py"

_spec = importlib.util.spec_from_file_location("ve", ENRICH)
ve = importlib.util.module_from_spec(_spec)
sys.modules["ve"] = ve
_spec.loader.exec_module(ve)
US_OK, FOREIGN_OK = set(ve.US_STATES), set(ve.FOREIGN_CODES)

passed = failed = 0


def chk(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print("  [%s] %-56s %s" % ("PASS" if ok else "FAIL", label, detail), flush=True)


def squash(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def load_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def newest_output(state, agency):
    d = os.path.join(DATA, state, "_incoming")
    pat = "%s - %s - " % (state, agency)
    c = sorted(f for f in os.listdir(d)
               if f.startswith(pat) and f.endswith(".csv") and "_enriched" not in f)
    if not c:
        raise SystemExit("no intake output for %s in %s" % (state, d))
    return os.path.join(d, c[-1])


def raw_sheet(path, sheet):
    """Independent re-read of a worksheet: locate the header by scan, key by name."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    hdr, hdr_at = None, 0
    for i, r in enumerate(ws.iter_rows(values_only=True), start=1):
        cand = [squash(v).lower() for v in r]
        if sum(1 for c in cand if c) >= 3:
            hdr, hdr_at = cand, i
            break
    rows = []
    for i, r in enumerate(ws.iter_rows(values_only=True), start=1):
        if i <= hdr_at:                  # skip everything up to AND INCLUDING the header
            continue
        rows.append({hdr[j]: r[j] for j in range(min(len(hdr), len(r))) if hdr[j]})
    wb.close()
    return hdr, rows


def alias_of(spec, canon):
    for n in spec.aliases.get(canon, []):
        return n
    return None


def verify(spec, baseline_dir):
    print()
    print("=" * 80)
    print("%s  -  %s" % (spec.state, spec.agency))
    print("=" * 80)
    out = newest_output(spec.state, spec.agency)
    rows = load_csv(out)
    print("  output: %s   rows=%d" % (os.path.basename(out), len(rows)))

    lic_hdr = alias_of(spec, "license_number")
    name_hdr = alias_of(spec, "facility_name")
    zip_hdr = alias_of(spec, "address_zip")

    # ---- A. membership against the source workbooks, both directions
    src_rows, occ = {}, collections.Counter()
    for wbspec in spec.workbooks:
        path = os.path.join(DATA, spec.state, "_incoming", wbspec.filename)
        wb = openpyxl.load_workbook(path, read_only=True)
        names = wb.sheetnames
        wb.close()
        for sheet in names:
            hdr, raw = raw_sheet(path, sheet)
            for r in raw:
                v = r.get(lic_hdr)
                if v in (None, ""):
                    continue
                base = (wbspec.filename, sheet, str(v).strip())
                occ[base] += 1
                src_rows[base + (occ[base] - 1,)] = r
    out_rows, oocc = {}, collections.Counter()
    for r in rows:
        base = (r["__source_file"], r["__source_sheet"], r["license_number"])
        oocc[base] += 1
        out_rows[base + (oocc[base] - 1,)] = r
    src = {k[:3] for k in src_rows}
    got = {k[:3] for k in out_rows}
    chk("A1. every SOURCE (file,sheet,licence) is in the output", not (src - got),
        "missing=%d %s" % (len(src - got), sorted(src - got)[:3]))
    chk("A2. every OUTPUT (file,sheet,licence) is in the source", not (got - src),
        "extra=%d %s" % (len(got - src), sorted(got - src)[:3]))

    # ---- B. values are transcribed, not transformed
    aligned = sorted(set(src_rows) & set(out_rows))
    chk("B0. occurrence alignment is non-empty and accounts for every source row",
        bool(aligned) and len(src_rows) - len(aligned) == len(src_rows) - len(out_rows),
        "source=%d output=%d aligned=%d (difference = whole-row dups dropped)"
        % (len(src_rows), len(out_rows), len(aligned)))
    bad = [k for k in aligned
           if squash(src_rows[k].get(name_hdr)) != squash(out_rows[k]["facility_name"])]
    chk("B1. facility_name matches source (all aligned rows)", not bad,
        "compared=%d mismatches=%d" % (len(aligned), len(bad)))

    if zip_hdr:
        byk = out_rows
        padded = wrong = kept = 0
        for k in aligned:
            s = src_rows[k].get(zip_hdr)
            if s is None or str(s).strip() == "":
                continue
            raw_s, outv = str(s).strip(), byk[k]["address_zip"]
            if len(raw_s) < 5 and raw_s.isdigit():
                padded += 1
                if outv != raw_s.zfill(5) or len(outv) != 5:
                    wrong += 1
            elif outv != raw_s:
                kept += 1
        chk("B2. every padded zip == source zfill(5)", not wrong,
            "padded=%d wrong=%d" % (padded, wrong))
        chk("B3. untouched zips byte-identical to source", not kept, "changed=%d" % kept)

    # ---- C. structural invariants
    chk("C1. no blank license_number or facility_name",
        all(r["license_number"] and r["facility_name"] for r in rows))
    chk("C2. jurisdiction constant %r" % spec.state,
        {r["jurisdiction"] for r in rows} == {spec.state})
    baddate = [r["license_number"] for r in rows
               if r["issue_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["issue_date"])]
    chk("C3. issue_date is ISO yyyy-mm-dd (or empty)", not baddate,
        "non-ISO=%d %s" % (len(baddate), baddate[:3]))
    st = collections.Counter(r["address_state"] for r in rows)
    documented = getattr(spec, "documented_state_literals", set())
    unknown = {s for s in st if s and s not in US_OK and s not in FOREIGN_OK
               and s not in documented}
    seen_doc = sorted({s for s in st if s in documented})
    chk("C4. address_state is US, a known foreign code, or a documented literal",
        not unknown,
        "unknown=%s foreign=%s documented=%s blank=%d"
        % (sorted(unknown), sorted({s for s in st if s in FOREIGN_OK and s not in US_OK}),
           seen_doc, st[""]))
    # A documented literal that no longer appears is stale scaffolding - say so.
    for lit in sorted(documented - set(st)):
        print("      note: documented state literal %r no longer present in the data" % lit)
    if spec.known_status is not None:
        drift = {r["license_status"] for r in rows if r["license_status"]} - spec.known_status
        chk("C5. license_status within the registered enum", not drift, "drift=%s" % sorted(drift))
    else:
        chk("C5. license_status left blank (source has no status column)",
            not any(r["license_status"] for r in rows),
            "source carries no status; nothing inferred")

    # ---- D. duplicates
    within = collections.Counter((r["license_number"], r["license_type"]) for r in rows)
    repeats = {lic for (lic, _t), n in within.items() if n > 1}
    chk("D1. within-type repeats are exactly the documented set",
        repeats == spec.documented_near_dups,
        "unexpected=%s missing=%s" % (sorted(repeats - spec.documented_near_dups),
                                      sorted(spec.documented_near_dups - repeats)))
    bylic = collections.defaultdict(set)
    for r in rows:
        bylic[r["license_number"]].add(r["license_type"])
    multi = {k for k, v in bylic.items() if len(v) > 1}
    print("      %d licence(s) held under more than one credential type" % len(multi))

    # ---- E. enrich compatibility, measured against the real module
    cols = list(rows[0].keys())
    want = {"name": "facility_name", "type": "license_type", "state": "address_state",
            "city": "address_city", "zip": "address_zip", "addr": "address_line1"}
    for k, cand in (("name", ve.NAME_COLS), ("type", ve.TYPE_COLS), ("state", ve.STATE_COLS),
                    ("city", ve.CITY_COLS), ("zip", ve.ZIP_COLS), ("addr", ve.ADDR_COLS)):
        chk("E. enrich picks %-5s -> %s" % (k, want[k]), ve.pick(cols, cand) == want[k],
            "got %r" % ve.pick(cols, cand))

    # ---- R. regression against the previously verified bespoke output
    base = os.path.join(baseline_dir, "%s_bespoke_enriched.csv" % spec.state)
    if os.path.exists(base):
        old = load_csv(base)
        shared = [c for c in ES.CANONICAL
                  if c in old[0] and c not in ("__source_file", "__source_email")]
        # Key on fields BOTH files carry. __source_sheet is absent from the bespoke GA
        # output, and including it made the key sets disjoint - which let the field
        # comparison below iterate an empty intersection and report PASS.
        kf = [c for c in ("license_number", "license_type", "address_line1", "address_city")
              if c in old[0] and c in rows[0]]

        def mk(rs):
            d, c = {}, collections.Counter()
            for r in rs:
                b = tuple(r.get(x, "") for x in kf)
                c[b] += 1
                d[b + (c[b] - 1,)] = r
            return d

        ok, nk = mk(old), mk(rows)
        inter = set(ok) & set(nk)
        chk("R1. same record keys as the bespoke adapter", set(ok) == set(nk),
            "key=%s  only-old=%d only-new=%d" % (kf, len(set(ok) - set(nk)), len(set(nk) - set(ok))))
        diff = collections.Counter()
        for k in inter:
            for c in shared:
                if (ok[k].get(c) or "") != (nk[k].get(c) or ""):
                    diff[c] += 1
        # A comparison over an empty intersection must FAIL, not pass silently.
        chk("R2. every shared field byte-identical to the bespoke output",
            bool(inter) and not diff,
            "compared %d record(s) x %d col(s); differing: %s"
            % (len(inter), len(shared), dict(diff) or "NONE")
            + ("   <-- VACUOUS, nothing compared" if not inter else ""))
    else:
        print("      (no bespoke baseline for %s - regression gate skipped)" % spec.state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("states", nargs="*")
    ap.add_argument("--baseline", default=os.path.join(
        os.environ.get("TEMP", "."), "claude", "C--Verified",
        "b8a5b93d-f76f-471d-a0ab-65b824232b81", "scratchpad", "baseline"))
    a = ap.parse_args()
    keys = sorted(ES.ALL) if not a.states else [s.upper() for s in a.states]
    for k in keys:
        verify(ES.ALL[k], a.baseline)
    print()
    print("=" * 80)
    print("RESULT   %d passed, %d failed" % (passed, failed))
    print("=" * 80)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
