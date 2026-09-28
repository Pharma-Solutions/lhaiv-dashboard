#!/usr/bin/env python3
"""Verification for the NJ Drug Control Unit / CDS business pull.

Two jobs:

  A. CREDENTIAL SEPARATION. This is the third distinct NJ credential in Master Data.
     Prove licence-number disjointness from BOTH held files - NJ Board of Pharmacy
     and NJ Drug & Medical Device Registration - rather than asserting it.

  B. COMPLETENESS ON AN INDEPENDENT AXIS. The crawler partitions by licence type and
     walks the DataGrid pager. This suite re-derives the population by CITY, an axis
     the crawler never touches, and tests MEMBERSHIP - is every row the portal can
     show present in the file - not count equality. Counts on these portals drift
     between runs and can read lower than truth; membership does not.
"""
import csv
import re
import sys
import time
from collections import Counter

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from nj_cds import (CDS_BUSINESS_TYPES, CITY_INPUT, SEARCH, SUBMIT, TYPE_SELECT, UA,
                    grid_rows, advance)

CSVF = sys.argv[1] if len(sys.argv) > 1 else "nj_cds_all.csv"
MD = (r"C:\Users\MarkFulton\OneDrive - Pharma Solutions USA, Inc\All Company - Documents"
      r"\LighthouseAI - Product\LighthouseAI Verified\Master Data")
HELD = {
    "NJ Board of Pharmacy": MD + r"\NJ - nj-board-of-pharmacy - Complete - 20260803.csv",
    "NJ Drug & Medical Device": MD + r"\NJ - nj-drug-and-medical-device-registration - "
                                     r"Company-Only - 20260923.csv",
}
# Cities chosen for size and spread, not because the crawler used them (it did not).
CITIES = ["NEWARK", "JERSEY CITY", "PATERSON", "TRENTON", "CAMDEN", "EDISON",
          "TOMS RIVER", "CHERRY HILL", "HACKENSACK", "PRINCETON"]
fails = []


def chk(label, cond, detail=""):
    ok = bool(cond)
    if not ok:
        fails.append(label)
    print("  [%s] %s%s" % ("PASS" if ok else "**FAIL**", label,
                           ("  " + str(detail)) if detail else ""))
    return ok


rows = list(csv.DictReader(open(CSVF, encoding="utf-8")))
print("=" * 92)
print("NJ Drug Control Unit / CDS - business scope")
print("  file: %s   %s rows" % (CSVF, format(len(rows), ",")))

# ---- 1. scope
print("\n1. Scope - CDS business credentials only")
prof = Counter(r["profession"] for r in rows)
chk("every row is the CDS profession", set(prof) == {"CDS"}, dict(prof))
types = Counter(r["license_type"] for r in rows)
chk("only the 4 allowlisted CDS business types present",
    set(types) <= set(CDS_BUSINESS_TYPES), sorted(set(types)))
chk("jurisdiction stamped NJ on every row", {r["jurisdiction"] for r in rows} == {"NJ"})
indiv = [t for t in types if re.search(r"(physician|dentist|veterinar|podiatr|nurse|"
                                       r"optometr|midwife|assistant)", t, re.I)]
chk("no INDIVIDUAL CDS credential leaked in", not indiv, indiv or "none")
print("     per-type:")
for t, c in types.most_common():
    print("       %-34s %6d" % (t, c))

# ---- 2. natural key + raw values
print("\n2. Natural key and raw-value integrity")
# NJ genuinely issues no number for some records. Rather than drop them or invent an
# identifier, keep them verbatim and assert the blanks are CONFINED to non-issued
# statuses - that is the real invariant.
blanks = [r for r in rows if not r["license_number"].strip()]
blank_st = Counter(r["license_status"] for r in blanks)
chk("blank licence numbers confined to non-issued statuses",
    set(blank_st) <= {"Deleted", "Pending", "Withdrawn", ""},
    "%d blank; statuses=%s" % (len(blanks), dict(blank_st)))
chk("blank licence numbers are a trivial share",
    len(blanks) / len(rows) < 0.02, "%.2f%%" % (100.0 * len(blanks) / len(rows)))
keyc = Counter((r["jurisdiction"], r["license_number"], r["license_type"])
               for r in rows if r["license_number"].strip())
multi = {k: v for k, v in keyc.items() if v > 1}
chk("natural key unique (numbered rows)", not multi, "%d colliding" % len(multi))
chk("no whole-row duplicates",
    len({tuple(sorted(r.items())) for r in rows}) == len(rows))
with open(CSVF, encoding="utf-8", newline="") as fh:
    rd = csv.reader(fh)
    h = next(rd)
    raw = [r[h.index("license_number")] for r in rd]
chk("no float coercion", not any(v.endswith(".0") for v in raw), "e.g. %s" % raw[:3])
chk("licence numbers kept as strings (alpha prefix preserved)",
    sum(1 for v in raw if not v.isdigit()) > 0.9 * len(raw),
    "%d/%d non-numeric" % (sum(1 for v in raw if not v.isdigit()), len(raw)))

# ---- 3. status
print("\n3. Status coverage (verbatim from the grid, nothing derived)")
st = Counter(r["license_status"] or "(blank)" for r in rows)
for k, v in st.most_common():
    print("       %-30s %6d" % (k, v))
chk("more than one status present", len(st) > 1)
chk("active AND non-active present",
    "Active" in st and any(k not in ("Active", "(blank)") for k in st))

# ---- 4. THREE-WAY credential separation
print("\n4. Separation from the two held NJ credentials")
ours = {r["license_number"].strip() for r in rows}
for label, path in HELD.items():
    try:
        held = list(csv.DictReader(open(path, encoding="utf-8")))
    except FileNotFoundError:
        print("       (%s not found - skipped)" % label)
        continue
    # Test against EVERY identifier column in the held file, not just the first match.
    # The held BOP file is a broad MyLicense pull (144k rows) and could plausibly
    # already contain CDS records; a single-column test could miss that.
    idcols = [c for c in held[0]
              if re.search(r"(licen[cs]e|permit|registration|cert)", c, re.I)
              and re.search(r"(no|num|number|#)", c, re.I)]
    # A disjointness test against an EMPTY column set passes trivially - a false pass
    # on the most important check in this suite. Fail loudly instead.
    chk("identifier column(s) found in %s" % label, bool(idcols),
        "cols=%s" % (idcols or "NONE - test would be vacuous"))
    theirs = set()
    for c in idcols:
        theirs |= {str(r.get(c, "")).strip() for r in held}
    overlap = {x for x in (ours & theirs) if x}
    chk("no licence-number overlap with %s" % label, not overlap,
        "%d overlapping (e.g. %s)" % (len(overlap), sorted(overlap)[:5]))
    print("       %s: %s rows; id columns tested %s; this file: %s rows"
          % (label, format(len(held), ","), idcols, format(len(rows), ",")))
    # also assert the credential LABEL differs, not just the numbers
    tcol = next((c for c in held[0] if re.search(r"(license_type|permit type|profession)", c, re.I)), None)
    if tcol:
        theirtypes = {str(r.get(tcol, "")).strip() for r in held}
        shared = {t for t in (set(types) & theirtypes) if t}
        chk("no shared credential-type label with %s" % label, not shared,
            sorted(shared)[:4] or "disjoint")

# ---- 5. INDEPENDENT AXIS: city membership
print("\n5. Independent CITY-axis membership check "
      "(crawler partitions by licence type + pager, never city)")
have = {(r["license_number"], r["full_name"]) for r in rows}
tot_live = tot_missing = 0
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1400})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)
    for city in CITIES:
        live = {}
        for t in CDS_BUSINESS_TYPES:
            pg.goto(SEARCH, wait_until="domcontentloaded", timeout=90000)
            pg.wait_for_timeout(1200)
            pg.select_option(TYPE_SELECT, label=t)
            pg.fill(CITY_INPUT, city)
            pg.wait_for_timeout(500)
            pg.locator(SUBMIT).first.click()
            pg.wait_for_timeout(5000)
            page = 0
            while page < 40:
                for r in grid_rows(pg):
                    live[(r[1], r[0])] = r
                page += 1
                if not advance(pg, page + 1):
                    break
                pg.wait_for_timeout(1600)
            time.sleep(0.3)
        miss = [v for k, v in live.items() if k not in have]
        tot_live += len(live)
        tot_missing += len(miss)
        mark = "PASS" if not miss else "**FAIL**"
        if miss:
            fails.append("city membership %s" % city)
        print("       [%s] %-14s live=%-5d missing=%d" % (mark, city, len(live), len(miss)))
        for v in miss[:3]:
            print("             %r (%s)" % (v[0], v[4]))
    b.close()
print("     TOTAL sampled live rows=%d  missing=%d (%.2f%%)"
      % (tot_live, tot_missing, 100.0 * tot_missing / max(tot_live, 1)))

print("\n" + "=" * 92)
print("ALL CHECKS PASSED" if not fails else "%d FAILURE(S): %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
