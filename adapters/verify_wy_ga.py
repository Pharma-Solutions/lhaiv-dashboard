#!/usr/bin/env python3
"""Real-data verification of the WY and GA adapter outputs.

The outputs are not crawls, so there is no cap to hunt and no paging to converge.
What CAN still go wrong is transcription: a dropped sheet, a silently coerced
value, a zip "fixed" into the wrong number, a column the enricher will not find.

So the independent axis here is the SOURCE WORKBOOK, re-read by a different code
path than the adapter used, and the test is MEMBERSHIP in both directions - every
source record present in the output, and no output record absent from the source.
Counts alone would pass even if two sheets had been swapped.

Check 9 is the one that would otherwise be assumed: it imports the real
verified_enrich and asks it which columns it would pick, rather than trusting my
reading of its candidate lists.
"""
import collections
import csv
import importlib.util
import os
import re
import sys

import openpyxl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WY_DIR = r"C:\Verified\data\WY\_incoming"
GA_DIR = r"C:\Verified\data\GA\_incoming"
ENRICH = r"C:\Verified\lhaiv-dashboard-pipeline\verified_enrich.py"

# Use the REAL enricher's sets rather than a hand-written copy: if ON/QC are
# acceptable to verified_enrich they must be acceptable here, and a local list
# would drift away from it silently.
_spec = importlib.util.spec_from_file_location("ve", ENRICH)
ve = importlib.util.module_from_spec(_spec)
sys.modules["ve"] = ve
_spec.loader.exec_module(ve)
US_OK = set(ve.US_STATES)
FOREIGN_OK = set(ve.FOREIGN_CODES)

# Documented in wy_adapter.py: six 3PL licences the board lists twice, the second
# copy with a blank address. Kept deliberately; this set is a tripwire for NEW ones.
WY_KNOWN_NEAR_DUPS = {"3PL0010", "3PL0029", "3PL0068", "3PL0135", "3PL0185", "3PL0227"}

passed = failed = 0


def chk(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", label, detail), flush=True)


def newest(d, pat):
    # "_enriched" sorts AFTER ".csv", so without this the verifier would silently
    # start checking the enriched twin instead of the adapter's own output.
    c = sorted(f for f in os.listdir(d)
               if re.match(pat, f) and "_enriched" not in f)
    if not c:
        raise SystemExit("no output matching %r in %s" % (pat, d))
    return os.path.join(d, c[-1])


def load_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def sheet_rows(path, sheet, lic_col="license number"):
    """Independent re-read: locate columns by name, return raw tuples."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    hdr = None
    for r in ws.iter_rows(values_only=True):
        cand = [re.sub(r"\s+", " ", str(v or "")).strip().lower() for v in r]
        if sum(1 for c in cand if c) >= 3:
            hdr = cand
            break
    out = []
    i = hdr.index(lic_col)
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r and i < len(r) and r[i] not in (None, ""):
            out.append({hdr[j]: r[j] for j in range(min(len(hdr), len(r))) if hdr[j]})
    wb.close()
    return out


# ===================================================================== WY
print("=" * 78)
print("WY  -  wy-board-of-pharmacy")
print("=" * 78)
wy = load_csv(newest(WY_DIR, r"WY - wy-board-of-pharmacy"))
PH = os.path.join(WY_DIR, "WY_Pharmacies (Res, Ins, Tel, NR) Sept 2026.xlsx")
WD = os.path.join(WY_DIR, "WY_WD & 3PL Sept 2026.xlsx")

# 1. membership, both directions, keyed on (sheet, license number)
src = set()
for path, sheets in ((PH, ["Nonresident", "Resident", "Institutional", "Telepharmacy"]),
                     (WD, ["Human Use", "Vet Use", "Med O2", "3PL"])):
    for sh in sheets:
        for r in sheet_rows(path, sh):
            src.add((sh, str(r["license number"]).strip()))
got = {(r["__source_sheet"], r["license_number"]) for r in wy}
chk("1. every SOURCE (sheet,license) is in the output", not (src - got),
    "missing=%d" % len(src - got))
chk("2. every OUTPUT (sheet,license) is in the source", not (got - src),
    "extra=%d" % len(got - src))

# 3. duplicate license numbers - cross-type is legitimate, within-type is not
bylic = collections.defaultdict(list)
for r in wy:
    bylic[r["license_number"]].append(r)
dups = {k: v for k, v in bylic.items() if len(v) > 1}
within = {k for k, v in dups.items()
          if len({x["license_type"] for x in v}) < len(v)}
chk("3. within-type repeats are exactly the 6 documented 3PL pairs",
    within == WY_KNOWN_NEAR_DUPS,
    "within=%s  unexpected=%s  missing=%s"
    % (len(within), sorted(within - WY_KNOWN_NEAR_DUPS), sorted(WY_KNOWN_NEAR_DUPS - within)))
chk("3b. cross-type repeats carry distinct license_types (legitimate)",
    all(len({x["license_type"] for x in v}) == len(v)
        for k, v in dups.items() if k not in WY_KNOWN_NEAR_DUPS),
    "%d license(s) hold more than one WY credential" % (len(dups) - len(within)))

# 4. verbatim spot-check against the workbook
smp = [r for r in wy if r["__source_sheet"] == "Human Use"][:200]
raw = {str(r["license number"]).strip(): r for r in sheet_rows(WD, "Human Use")}
bad = [r["license_number"] for r in smp
       if str(raw[r["license_number"]]["business name"] or "").strip() != r["facility_name"]]
chk("4. facility_name byte-identical to source", not bad, "mismatches=%d" % len(bad))

# 5. the board's own misspelling survived
acts = collections.Counter(r["business_activity"] for r in wy if r["business_activity"])
chk("5. 'Wholesale Distributer' preserved verbatim (not corrected)",
    "Wholesale Distributer" in acts, "activities=%s" % dict(acts))

# 6. structural
chk("6. no blank license_number or facility_name",
    all(r["license_number"] and r["facility_name"] for r in wy))
wy_st = collections.Counter(r["address_state"] for r in wy)
wy_unknown = {s for s in wy_st if s and s not in US_OK and s not in FOREIGN_OK}
chk("7. address_state is US or an enricher-recognised foreign code", not wy_unknown,
    "unknown=%s  foreign=%s  blank=%d (no address in source)"
    % (sorted(wy_unknown), sorted({s for s in wy_st if s in FOREIGN_OK and s not in US_OK}), wy_st[""]))
baddate = [r["license_number"] for r in wy
           if r["issue_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["issue_date"])]
chk("8. issue_date is ISO yyyy-mm-dd", not baddate, "bad=%d" % len(baddate))
chk("9. jurisdiction constant 'WY'", {r["jurisdiction"] for r in wy} == {"WY"})
res = collections.Counter(r["address_state"] == "WY" for r in wy)
print("      residency preview: in-state=%d  out-of-state=%d" % (res[True], res[False]))

# ===================================================================== GA
print()
print("=" * 78)
print("GA  -  ga-board-of-pharmacy")
print("=" * 78)
ga = load_csv(newest(GA_DIR, r"GA - ga-board-of-pharmacy"))
GX = os.path.join(GA_DIR, "GA_PHARMACY FACILITIES - September 2026.xlsx")
graw = sheet_rows(GX, openpyxl.load_workbook(GX, read_only=True).sheetnames[0])

chk("1. row count == source minus byte-identical duplicates",
    len(ga) == len({tuple((k, str(v)) for k, v in sorted(r.items())) for r in graw}),
    "out=%d  source rows=%d  source distinct rows=%d"
    % (len(ga), len(graw), len({tuple((k, str(v)) for k, v in sorted(r.items())) for r in graw})))
s_src = {str(r["license number"]).strip() for r in graw}
s_got = {r["license_number"] for r in ga}
chk("2. every SOURCE license is in the output", not (s_src - s_got),
    "missing=%d" % len(s_src - s_got))
chk("3. every OUTPUT license is in the source", not (s_got - s_src),
    "extra=%d" % len(s_got - s_src))

g_by = collections.defaultdict(list)
for r in ga:
    g_by[r["license_number"]].append(r)
gd = {k: v for k, v in g_by.items() if len(v) > 1}
chk("4. no duplicate license numbers survive whole-row de-dupe", not gd,
    "%d duplicated: %s" % (len(gd), {k: [x["license_type"] for x in v] for k, v in gd.items()}))

# 5. the zip reconstruction is the headline risk - verify every pad against source
zsrc = {str(r["license number"]).strip(): r["zip code"] for r in graw}
padded = [r for r in ga if zsrc.get(r["license_number"]) is not None
          and len(str(zsrc[r["license_number"]]).strip()) < 5]
wrong = [r["license_number"] for r in padded
         if r["address_zip"] != str(zsrc[r["license_number"]]).strip().zfill(5)
         or len(r["address_zip"]) != 5]
chk("5. every padded zip == source zfill(5), length 5", not wrong,
    "padded=%d wrong=%d" % (len(padded), len(wrong)))
nonpad = [r["license_number"] for r in ga
          if zsrc.get(r["license_number"]) is not None
          and len(str(zsrc[r["license_number"]]).strip()) == 5
          and r["address_zip"] != str(zsrc[r["license_number"]]).strip()]
chk("6. untouched zips byte-identical to source", not nonpad, "changed=%d" % len(nonpad))

gbad = [r["license_number"] for r in ga
        if r["issue_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["issue_date"])]
chk("7. issue_date is ISO yyyy-mm-dd", not gbad, "bad=%d" % len(gbad))
chk("8. no blank license_number or facility_name",
    all(r["license_number"] and r["facility_name"] for r in ga))
ga_st = collections.Counter(r["address_state"] for r in ga)
ga_unknown = {s for s in ga_st if s and s not in US_OK and s not in FOREIGN_OK}
chk("9. address_state is US or an enricher-recognised foreign code", not ga_unknown,
    "unknown=%s  foreign=%s  blank=%d (no address in source)"
    % (sorted(ga_unknown), sorted({s for s in ga_st if s in FOREIGN_OK and s not in US_OK}), ga_st[""]))
chk("10. jurisdiction constant 'GA'", {r["jurisdiction"] for r in ga} == {"GA"})
gres = collections.Counter(r["address_state"] == "GA" for r in ga)
print("      residency preview: in-state=%d  out-of-state=%d" % (gres[True], gres[False]))

# ============================================== enrich compatibility (measured)
print()
print("=" * 78)
print("verified_enrich column resolution  (asking the real module, not assuming)")
print("=" * 78)
for nm, rows in (("WY", wy), ("GA", ga)):
    cols = list(rows[0].keys())
    got = {
        "name":  ve.pick(cols, ve.NAME_COLS),
        "type":  ve.pick(cols, ve.TYPE_COLS),
        "state": ve.pick(cols, ve.STATE_COLS),
        "city":  ve.pick(cols, ve.CITY_COLS),
        "zip":   ve.pick(cols, ve.ZIP_COLS),
        "addr":  ve.pick(cols, ve.ADDR_COLS),
    }
    want = {"name": "facility_name", "type": "license_type", "state": "address_state",
            "city": "address_city", "zip": "address_zip", "addr": "address_line1"}
    for k in want:
        chk("%s: enrich picks %-5s -> %s" % (nm, k, want[k]), got[k] == want[k],
            "got %r" % got[k])

print()
print("=" * 78)
print("RESULT   %d passed, %d failed" % (passed, failed))
print("=" * 78)
sys.exit(1 if failed else 0)
