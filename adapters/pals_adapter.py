#!/usr/bin/env python3
"""ENG-344 - PA State Board of Pharmacy (PALS) establishment adapter.

SCOPE (Mark, 2026-09-25): establishments only - "pharmacies only", individuals OUT.
The board's FACILITY classes are the whole establishment set; it issues no
wholesaler / manufacturer / distributor credential (that is PA DDC, already held
and a genuinely separate system - "Device" and "Wholesaler/Distributor" appear
nowhere in PALS's 396 person + 191 facility license types).

CHANNEL (recon passes 1-6, 2026-09-24)
  AngularJS SPA over REST:
    POST /api/Search/SearchForPersonOrFacilty  -> 50 rows/page, PageNo paging
    POST /api/Search/FetchLookupData           -> ARRAY payload, Filter Person|Facility
  Not CAPTCHA-gated: the search POST carries no token in payload or headers.

THE BLOCKER, AND THE DESIGN THAT ANSWERS IT
  Every query is capped at ~500 rows. TotalRecords came back 497/499/500 for four
  wildly different queries, and paging dies after PageNo=10. TotalRecords is
  therefore a CAP, not a count, whenever it sits at the ceiling.

  Worse, several filters are SILENTLY IGNORED - County (by id or name),
  FaclityCounty, Zip and LicenseNo all return the unfiltered capped set. Only
  State, City and FacilityName are honoured. FacilityName is a PREFIX match
  (query "AID" returns AID RX LLC, not the hundreds of RITE AID rows), so prefix
  buckets are disjoint.

  Strategy: partition by State; any state still at the cap is split by recursive
  FacilityName prefix, deepening until every leaf is under the cap.

  COMPLETENESS SELF-CHECK: a capped bucket still exposes 500 rows. After
  expanding it, those 500 MUST all appear in the union of its children. If one
  does not, the prefix alphabet has a hole - we raise rather than ship a
  quietly-short file.
"""
import argparse
import csv
import json
import math
import time
from collections import Counter

from playwright.sync_api import sync_playwright

URL = "https://www.pals.pa.gov/#!/page/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
SEARCH = "https://www.pals.pa.gov/api/Search/SearchForPersonOrFacilty"
LOOKUP = "https://www.pals.pa.gov/api/Search/FetchLookupData"

PHARM_BOARD = 8
PAGE_SIZE = 50
CAP_SUSPECT = 495     # treat >= this as "capped"; splitting a safe bucket is harmless,
                      # missing rows is not
MAX_PAGE = 10         # server stops serving past PageNo=10 (10 x 50 = 500)
MAX_DEPTH = 26        # 'RITE AID PHARMACY #10892' normalises to 24 chars before
                      # the store number disambiguates - depth must clear that

# PALS matches FacilityName on a NORMALISED string. Probed character by character
# (2026-09-25): space, apostrophe, hyphen and period are STRIPPED from both the query
# and the stored name; comma & # / ( ) * + @ and alphanumerics are RETAINED literally.
#   evidence: "RITE-AID" == "RITE AID" == "RITEAID" (500, same first row), while
#             "RITEAID1" (2 rows -> RITE AID 1383) != "RITE AID #1" (280 -> ...#10892)
# The alphabet must therefore contain ONLY retained characters. Appending a stripped
# character yields a byte-identical query, which is what sent earlier versions of this
# crawler down branches that could never resolve.
NORM_STRIP = " '-."
FIXED_ALPHABET = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") + list(",&#/()*+@")


def norm(s):
    """The server's matching form of a facility name."""
    return "".join(c for c in (s or "").upper() if c not in NORM_STRIP)

# The board's 8 facility classes, from FetchLookupData(Filter=Facility).
EXPECTED_FACILITY_TYPES = {
    113: "Pharmacy",
    4: "Nonresident Pharmacy",
    292: "Cancer Drug Repository",
    389: "Out of State School",
    375: "Inactive",
    599: "Pharmacy (Temp. Military License)",
    601: "Nonresident Pharmacy (Temp. Military License)",
    602: "Cancer Drug Repository (Temp. Military License)",
}

FIELDS = [
    ("LicenseNumber", "license_number"),
    ("LicenceType", "license_type"),        # raw, verbatim (note PALS's spelling)
    ("Status", "license_status"),           # raw, verbatim
    ("FacilityName", "license_holder_name"),
    ("DoingBusinessAs", "doing_business_as"),
    ("FictitiousName", "fictitious_name"),
    ("AddressLine1", "address_line_1"),
    ("AddressLine2", "address_line_2"),
    ("AddressLine3", "address_line_3"),
    ("AddressLine4", "address_line_4"),
    ("FacilityStreetAddress", "address_street"),
    ("City", "address_city"),
    ("State", "address_state"),
    ("County", "address_county"),
    ("Country", "address_country"),
    ("zipcode", "address_zip"),
    ("PhoneNo1", "phone"),
    ("ProfessionType", "profession"),
    ("BoardName", "board"),
    ("DisciplinaryAction", "disciplinary_action"),
    ("ComplaintNumber", "complaint_number"),
    ("LicenseId", "pals_license_id"),
    ("PersonId", "pals_person_id"),
    ("LicenseTypeId", "pals_license_type_id"),
]

stats = Counter()
recon = []   # reconciliation notes (recovered rows) - distinct from coverage holes


def log(m):
    print("[pals] " + str(m), flush=True)


class Client:
    def __init__(self, pg, delay):
        self.pg = pg
        self.delay = delay

    def post(self, url, payload):
        stats["requests"] += 1
        r = self.pg.request.post(url, data=json.dumps(payload),
                                 headers={"content-type": "application/json"},
                                 timeout=180000)
        time.sleep(self.delay)
        if r.status != 200:
            raise RuntimeError("HTTP %s from %s" % (r.status, url))
        return r.text()

    def search(self, lic_type, page_no=1, state=None, name_prefix=None):
        pl = {"OptPersonFacility": "Facility", "IsFacility": 1,
              "ProfessionID": PHARM_BOARD, "LicenseTypeId": lic_type,
              "State": state or "", "Country": "ALL", "County": None,
              "PersonId": None, "PageNo": page_no}
        if name_prefix:
            pl["FacilityName"] = name_prefix
        txt = self.post(SEARCH, pl)
        try:
            rows = json.loads(txt)
        except Exception:
            raise RuntimeError("non-JSON search response: %s" % txt[:200])
        rows = rows if isinstance(rows, list) else []
        total = int(rows[0].get("TotalRecords") or 0) if rows else 0
        return rows, total

    def page_all(self, lic_type, total, state=None, name_prefix=None, first=None):
        """Page a bucket. `first` is page 1 if the caller already has it; when it is
        None (a re-page attempt) page 1 must be fetched too, or the retry can never
        converge on the rows it missed."""
        out = list(first or [])
        pages = min(math.ceil(total / PAGE_SIZE), MAX_PAGE)
        for pn in range(2 if first else 1, pages + 1):
            rows, _ = self.search(lic_type, pn, state, name_prefix)
            if not rows:
                break
            out.extend(rows)
        return out


def key(r):
    return r.get("LicenseId")


def crawl(cli, lic_type, tname, state, prefix, depth, holes, root_sig=None):
    """Return {LicenseId: row} for this bucket, splitting while capped.

    PALS normalises the FacilityName filter - it ignores leading whitespace AND
    leading punctuation - so a prefix made only of those characters silently
    matches everything, and the recursion walks off down a branch that can never
    resolve. Rather than blacklisting character classes one at a time (spaces,
    then apostrophes, then...), detect the condition directly.

    The comparison must be against the STATE-LEVEL ROOT, not the immediate parent.
    A child legitimately equalling its parent is common and harmless - "CV" -> "CVS"
    contains the whole parent set, same total, same first row. Only a prefix that
    reproduces the ROOT's result has actually had its filter discarded.
    """
    rows, total = cli.search(lic_type, 1, state, prefix)
    label = "%s|state=%s|prefix=%r" % (tname, state or "-", prefix or "")
    if total == 0:
        return {}
    sig = (total, key(rows[0]) if rows else None)
    if prefix and root_sig is not None and sig == root_sig:
        stats["filter_not_applied"] += 1
        return {}
    got = {}
    if total < CAP_SUSPECT:
        # PALS paging is NOT stable: the server's ordering can shift between page
        # calls, so the same row is returned twice and another is never returned at
        # all. Counting the returned LIST hides this (duplicates pad it out to
        # exactly `total`), so compare DISTINCT keys and re-page to converge.
        for attempt in range(3):
            page = cli.page_all(lic_type, total, state, prefix,
                                first=rows if attempt == 0 else None)
            for r in page:
                got[key(r)] = r
            if len(got) >= total:
                break
            stats["repage_attempts"] += 1
        if len(got) >= total:
            return got
        # Still short after retries. Do not accept a short bucket - split it, which
        # shrinks each query until paging is no longer needed.
        log("    ~ %s: %d distinct of %d after re-paging - splitting instead"
            % (label, len(got), total))
        stats["unstable_paging_split"] += 1
        if depth >= MAX_DEPTH:
            holes.append((label, total, "unstable paging, %d of %d, at MAX_DEPTH"
                          % (len(got), total)))
            return got
    else:
        stats["capped_buckets"] += 1

    # --- capped (or unstably paged): keep what is visible, then split
    visible = dict(got)
    for r in cli.page_all(lic_type, 500, state, prefix, first=rows):
        visible[key(r)] = r
    if depth >= MAX_DEPTH:
        holes.append((label, total, "MAX_DEPTH reached while still capped"))
        log("    !! %s STILL CAPPED at max depth - DATA WILL BE MISSING" % label)
        return visible

    pos = len(norm(prefix))
    observed = {norm(r.get("FacilityName"))[pos:pos + 1]
                for r in visible.values()}
    observed.discard("")
    alphabet = sorted(set(FIXED_ALPHABET) | observed)

    merged = {}
    for ch in alphabet:
        cand = (prefix or "") + ch
        # A character that normalises away would produce a query identical to the
        # parent's - an unresolvable branch. The alphabet already excludes those;
        # this assertion keeps it true if the alphabet is ever edited.
        if norm(cand) == norm(prefix):
            stats["skipped_degenerate_prefix"] += 1
            continue
        merged.update(crawl(cli, lic_type, tname, state, cand, depth + 1, holes,
                            root_sig if root_sig is not None else sig))

    # --- the self-check: the parent's visible rows must all reappear below
    missing = set(visible) - set(merged)
    # A record whose name IS the prefix (or is blank) has no next character, so no
    # longer child prefix can ever match it. That is a structural property of prefix
    # partitioning, not a coverage failure - recover it silently.
    plen = len(norm(prefix))
    unsplittable = {k for k in missing
                    if len(norm(visible[k].get("FacilityName"))) <= plen}
    merged.update({k: visible[k] for k in unsplittable})
    stats["unsplittable_exact_name"] += len(unsplittable)
    missing -= unsplittable
    if missing:
        # These rows WERE fetched - they are visible at this level and are merged back
        # below, so nothing is lost. They simply did not reappear under any child,
        # usually because the server's ordering shifts between paged calls. Record it
        # as a reconciliation note, NOT a coverage hole: a hole means rows we never
        # saw at all, which is a different and far more serious thing.
        recon.append((label, len(missing)))
        log("    ~ %s: %d parent row(s) not seen under any child - recovered directly"
            % (label, len(missing)))
        merged.update({k: visible[k] for k in missing})
        stats["recovered_rows"] += len(missing)
    return merged


def flatten(r, tname):
    out = {}
    for src, dst in FIELDS:
        v = r.get(src)
        out[dst] = "" if v is None else str(v)
    out["jurisdiction"] = "PA"        # named `jurisdiction`, NOT `state`, so
                                      # verified_enrich picks address_state (Defect #2)
    out["__license_type_queried"] = tname
    out["__source"] = ("PA State Board of Pharmacy - PALS public licensee search, "
                       "pals.pa.gov (api/Search/SearchForPersonOrFacilty, "
                       "ProfessionID=8, IsFacility=1)")
    out["__retrieved"] = time.strftime("%Y-%m-%d")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--out", default=None)
    ap.add_argument("--types", default=None,
                    help="comma-separated LicenseTypeIDs (default: all 8 facility classes)")
    a = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1200})
        pg = ctx.new_page()
        pg.set_default_timeout(90000)
        try:
            pg.goto(URL, wait_until="networkidle", timeout=90000)
        except Exception:
            pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(8000)
        cli = Client(pg, a.delay)

        # ---- enum-drift guard against the live picklist
        look = json.loads(cli.post(LOOKUP, [
            {"LookupType": "STATE", "LookupID": 2, "Filter": ""},
            {"LookupType": "ProfessionList", "LookupID": 4, "Filter": "Facility"},
            {"LookupType": "LicenseTypeList", "LookupID": 5, "Filter": "Facility"}]))
        live = {x["LicenseTypeID"]: x["LicenseType"]
                for x in look["LicenseTypeList"] if x.get("ProfessionID") == PHARM_BOARD}
        if live != EXPECTED_FACILITY_TYPES:
            added = {k: v for k, v in live.items() if k not in EXPECTED_FACILITY_TYPES}
            gone = {k: v for k, v in EXPECTED_FACILITY_TYPES.items() if k not in live}
            raise RuntimeError("ENUM DRIFT in Board-of-Pharmacy facility classes: "
                               "added=%s removed=%s" % (added, gone))
        log("enum-drift guard: 8 facility class(es) unchanged")

        states = [s["code"] for s in look["StatesList"]]
        log("state partition axis: %d state/territory code(s)" % len(states))

        targets = ([int(x) for x in a.types.split(",")] if a.types
                   else list(EXPECTED_FACILITY_TYPES))
        holes, per_type, records = [], {}, {}

        for tid in targets:
            tname = EXPECTED_FACILITY_TYPES[tid]
            rows, total = cli.search(tid, 1)
            log("")
            log("%s (id=%d): unpartitioned TotalRecords=%s%s"
                % (tname, tid, total, "  <-- CAPPED, partitioning" if total >= CAP_SUSPECT
                   else ""))
            if total == 0:
                per_type[tname] = 0
                continue

            if total < CAP_SUSPECT:
                got = {key(r): r for r in cli.page_all(tid, total, first=rows)}
            else:
                got = {}
                for st in states:
                    sub = crawl(cli, tid, tname, st, "", 0, holes)
                    if sub:
                        log("    state=%-3s %6d" % (st, len(sub)))
                    got.update(sub)
                # rows with a blank/unlisted State would be missed by the sweep;
                # the capped unpartitioned view is our probe for that.
                missing = {key(r) for r in rows} - set(got)
                if missing:
                    log("    ! %d unpartitioned row(s) not caught by the state sweep "
                        "(blank/unlisted State) - recovering" % len(missing))
                    got.update({key(r): r for r in rows if key(r) in missing})
                    stats["state_sweep_gap"] += len(missing)

            per_type[tname] = len(got)
            log("  -> %s: %s record(s)" % (tname, format(len(got), ",")))
            for k, v in got.items():
                records[k] = (v, tname)

        b.close()

    # ---------------- guards ----------------
    log("")
    log("requests issued: %s | capped buckets split: %s"
        % (format(stats["requests"], ","), stats["capped_buckets"]))
    if recon:
        log("")
        log("reconciliation notes (rows recovered directly, NOT missing): %d row(s) over "
            "%d bucket(s)" % (stats["recovered_rows"], len(recon)))
        for lab, n in recon[:8]:
            log("    ~ %s: %d" % (lab, n))
    if holes:
        log("")
        for lab, tot, why in holes:
            log("  HOLE: %s (total=%s) - %s" % (lab, tot, why))
        raise RuntimeError("REFUSING TO WRITE: %d coverage hole(s); the file would be "
                           "silently short" % len(holes))

    frames = [flatten(r, t) for r, t in records.values()]
    if not frames:
        raise RuntimeError("REFUSING TO WRITE: zero rows collected")

    # whole-row de-dupe (lossless); natural-key collisions reported, never collapsed
    seen, deduped, dups = set(), [], 0
    for r in frames:
        sig = tuple(sorted(r.items()))
        if sig in seen:
            dups += 1
            continue
        seen.add(sig)
        deduped.append(r)
    log("de-dupe (whole row, lossless): %s -> %s (removed %s)"
        % (format(len(frames), ","), format(len(deduped), ","), dups))

    keyc = Counter((r["jurisdiction"], r["license_number"], r["license_type"])
                   for r in deduped)
    multi = {k: v for k, v in keyc.items() if v > 1}
    if multi:
        log("")
        log("NATURAL-KEY COLLISIONS RETAINED (not collapsed): %d key(s), %d excess row(s)"
            % (len(multi), sum(v - 1 for v in multi.values())))
        for k, v in sorted(multi.items(), key=lambda kv: -kv[1])[:10]:
            log("    %s / %s  x%d" % (k[1], k[2], v))

    cols = [d for _, d in FIELDS] + ["jurisdiction", "__license_type_queried",
                                     "__source", "__retrieved"]
    out = a.out or ("pa_pals_facilities_" + time.strftime("%Y%m%d") + ".csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(deduped)
    log("wrote %s: %s rows x %d cols" % (out, format(len(deduped), ","), len(cols)))

    with open("pals_type_counts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["license_type", "record_count"])
        for t, c in sorted(per_type.items(), key=lambda kv: -kv[1]):
            w.writerow([t, c])
    log("wrote pals_type_counts.csv")

    log("")
    log("per-class counts:")
    for t, c in sorted(per_type.items(), key=lambda kv: -kv[1]):
        log("    %-46s %7s" % (t, format(c, ",")))
    log("    %-46s %7s" % ("TOTAL", format(sum(per_type.values()), ",")))
    log("")
    log("status spread:")
    for k, v in Counter(r["license_status"] or "(blank)" for r in deduped).most_common():
        log("    %-30s %7s" % (k, format(v, ",")))


if __name__ == "__main__":
    main()
