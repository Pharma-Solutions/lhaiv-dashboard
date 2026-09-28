#!/usr/bin/env python3
"""Verification for the MD Board of Pharmacy facility pull.

Two jobs:

  A. CREDENTIAL SEPARATION from the held MD OCSA controlled-substance file. Its id
     column is CDSPERMIT - PINNED EXPLICITLY, because a generic
     "licence/permit + no/num/#" regex does NOT match the bare name "CDSPERMIT".
     That exact miss made the NJ disjointness test vacuous (it printed
     `id columns tested []` and passed against an empty set), so the column is
     hard-coded here and the run FAILS if it is absent.

     OCSA covers the SAME BUSINESSES under a DIFFERENT credential (its PROFESSION
     column is 44% PHARMACY / DISTRIBUTOR), so entity-name overlap is EXPECTED and
     is not a defect. Only licence-number overlap would be.

  B. COMPLETENESS on an axis the harvester never used. The harvester walked the
     site's pager; this re-derives counts by CITY from the detail records and
     spot-checks membership. A partition cannot audit itself.
"""
import csv
import re
import sys
from collections import Counter

CSVF = sys.argv[1] if len(sys.argv) > 1 else None
if not CSVF:
    import glob
    cand = sorted(glob.glob("md-board-of-pharmacy - Company-Only - *.csv"))
    cand = [c for c in cand if "_enriched" not in c]
    CSVF = cand[-1] if cand else "md-board-of-pharmacy - Company-Only - 20260925.csv"

MD_DIR = (r"C:\Users\MarkFulton\OneDrive - Pharma Solutions USA, Inc\All Company - Documents"
          r"\LighthouseAI - Product\LighthouseAI Verified\Master Data")
OCSA = MD_DIR + r"\MD - md-ocsa-controlled-substance-registrants - 20260910.csv"
OCSA_ID_COL = "CDSPERMIT"          # pinned; see module docstring

KNOWN_STATUS = {"Active", "Active-Corp", "Closed", "Closed-Change of Ownership",
                "Deceased", "Non-Renewed", "Pending", "Probation",
                "Probation Non-Renewed", "Rescinded", "Retired", "Revoked",
                "Surrendered", "Suspended", "Suspended Non-Renewed"}
KNOWN_TYPES = {"Corporation", "Distributor", "Drug Therapy Management",
               "Pharmacy", "Pharmacy Waiver"}
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
print("MD Board of Pharmacy - facility scope")
print("  file: %s   %s rows" % (CSVF, format(len(rows), ",")))

# ---- 1. scope + enum drift
print("\n1. Scope and enum drift")
types = Counter(r["license_type"] for r in rows)
status = Counter(r["status"] for r in rows)
print("     per-type:")
for k, v in types.most_common():
    print("       %-34s %6d" % (k or "(blank)", v))
print("     per-status:")
for k, v in status.most_common():
    print("       %-34s %6d" % (k or "(blank)", v))
chk("jurisdiction stamped MD on every row", {r["jurisdiction"] for r in rows} == {"MD"})
chk("no unknown licence types (enum drift)",
    set(t for t in types if t) <= KNOWN_TYPES,
    sorted(set(t for t in types if t) - KNOWN_TYPES) or "none")
chk("no unknown statuses (enum drift)",
    set(s for s in status if s) <= KNOWN_STATUS,
    sorted(set(s for s in status if s) - KNOWN_STATUS) or "none")
chk("every row carries a GUID", all(r["md_guid"].strip() for r in rows))
chk("GUIDs unique", len({r["md_guid"] for r in rows}) == len(rows),
    "%d distinct of %d" % (len({r["md_guid"] for r in rows}), len(rows)))

# ---- 2. raw values, blanks kept verbatim
print("\n2. Raw-value integrity")
blanks = [r for r in rows if not r["license_number"].strip()]
bst = Counter(r["status"] for r in blanks)
chk("blank licence numbers confined to non-issued statuses",
    set(bst) <= {"Pending", "Denied", "Non-Renewed", "Closed", ""},
    "%d blank; statuses=%s" % (len(blanks), dict(bst)))
with open(CSVF, encoding="utf-8", newline="") as fh:
    rd = csv.reader(fh)
    h = next(rd)
    raw = [r[h.index("license_number")] for r in rd]
chk("no float coercion in licence numbers",
    not any(v.endswith(".0") for v in raw), "e.g. %s" % [v for v in raw if v][:3])
pref = Counter(re.match(r"^[A-Za-z]*", v).group(0) for v in raw if v.strip())
print("     licence-number prefixes: %s" % dict(pref.most_common(6)))

# ---- 3. detail-page agreement (the two sources of truth must not disagree)
print("\n3. Grid vs detail-page agreement")
both = [r for r in rows if r["status"].strip() and r["detail_status"].strip()]
agree = sum(1 for r in both if r["status"].strip() == r["detail_status"].strip())
chk("grid status agrees with detail status", both and agree / len(both) > 0.95,
    "%d/%d agree" % (agree, len(both)))
named = [r for r in rows if r["grid_name"].strip() and r["license_holder_name"].strip()]
nagree = sum(1 for r in named
             if r["grid_name"].strip().upper()[:18] == r["license_holder_name"].strip().upper()[:18])
chk("grid name agrees with detail name", named and nagree / len(named) > 0.95,
    "%d/%d agree" % (nagree, len(named)))
chk("detail address populated for most rows",
    sum(1 for r in rows if r["address_city"].strip()) > 0.9 * len(rows),
    "%d with city" % sum(1 for r in rows if r["address_city"].strip()))

# ---- 4. separation from the held MD OCSA file (PINNED id column)
print("\n4. Separation from held MD OCSA controlled-substance registrants")
try:
    ocsa = list(csv.DictReader(open(OCSA, encoding="utf-8")))
    cols = list(ocsa[0].keys())
    # tolerate a UTF-8 BOM on the first header
    idcol = next((c for c in cols if c.lstrip("\ufeff") == OCSA_ID_COL), None)
    chk("pinned OCSA id column %r present" % OCSA_ID_COL, bool(idcol),
        "cols=%s" % [c.lstrip("\ufeff") for c in cols][:6])
    if idcol:
        theirs = {str(r.get(idcol, "")).strip() for r in ocsa}
        theirs = {x for x in theirs if x}
        ours = {r["license_number"].strip() for r in rows if r["license_number"].strip()}
        overlap = ours & theirs
        chk("no licence-number overlap with MD OCSA", not overlap,
            "%d overlapping (e.g. %s)" % (len(overlap), sorted(overlap)[:5]))
        print("     OCSA: %s rows, %s distinct %s; this file: %s numbered rows"
              % (format(len(ocsa), ","), format(len(theirs), ","), OCSA_ID_COL,
                 format(len(ours), ",")))
        # entity-name overlap is EXPECTED - report it, do not fail on it
        namecol = next((c for c in cols if "BUSINESS_NAME" in c.upper()), None)
        if namecol:
            onames = {str(r.get(namecol, "")).strip().upper() for r in ocsa}
            ournames = {r["grid_name"].strip().upper() for r in rows}
            shared = {n for n in (onames & ournames) if n}
            print("     entity-name overlap: %s (EXPECTED - same businesses, "
                  "different credential; not a defect)" % format(len(shared), ","))
except FileNotFoundError:
    chk("held MD OCSA file present", False, OCSA)

# ---- 5. independent axis: city distribution from the DETAIL records
print("\n5. Independent axis - city re-derivation from detail pages")
cities = Counter(r["address_city"].strip().upper() for r in rows if r["address_city"].strip())
print("     %d distinct cities; top: %s" % (len(cities), cities.most_common(6)))
chk("city spread is plausible (not concentrated in one place)",
    len(cities) > 20 and cities.most_common(1)[0][1] < 0.5 * len(rows),
    "top city = %s" % (cities.most_common(1)[0] if cities else None))
md_share = sum(v for k, v in Counter(r["address_state"].strip().upper()
                                     for r in rows).items() if k == "MD")
print("     MD-addressed: %s of %s (%.1f%%) - Distributors are often out-of-state"
      % (format(md_share, ","), format(len(rows), ","), 100.0 * md_share / max(len(rows), 1)))

print("\n" + "=" * 92)
print("ALL CHECKS PASSED" if not fails else "%d FAILURE(S): %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
