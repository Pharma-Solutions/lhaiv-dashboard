#!/usr/bin/env python3
"""Fail-safe tests for verified_enrich.py residency derivation.

Covers the three defects fixed on 2026-09-25 AND the no-regression cases that
constrain each fix. Run: python test_failsafes.py
"""
import sys

import pandas as pd

sys.path.insert(0, ".")
from verified_enrich import (addr_state_strong, addr_state_weak, derive_resnon,
                             looks_foreign, normalize_state, pick_state_col)

FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print("  [%s] %-62s got=%r want=%r" % ("PASS" if ok else "**FAIL**", label, got, want))


def frame(rows, cols):
    return pd.DataFrame(rows, columns=cols).astype(str)


print("=" * 96)
print("DEFECT 3 - comma-delimited directional quadrant must not read as a state")
# NM: Albuquerque quadrant with a trailing unit token
check("NM '4431 Anaheim Ave., NE, Ste. A' -> not NE",
      addr_state_strong("4431 Anaheim Ave., NE, Ste. A"), "")
check("NM '4201 Yale Blvd., NE, Ste E' -> not NE",
      addr_state_strong("4201 Yale Blvd., NE, Ste E"), "")
# DC: quadrant at END of string, nothing after it - a following-unit rule would miss these
check("DC '4414 BENNING ROAD, NE' -> not NE",
      addr_state_strong("4414 BENNING ROAD, NE"), "")
check("DC '223 7TH STREET, NE' -> not NE",
      addr_state_strong("223 7TH STREET, NE"), "")
check("DC '1100 K STREET, NW' -> not NW",
      addr_state_strong("1100 K STREET, NW"), "")
# genuine Nebraska must still parse
check("genuine 'Omaha, NE' -> NE", addr_state_strong("Omaha, NE"), "NE")
check("genuine '123 Main St, Omaha, NE 68101' -> NE",
      addr_state_strong("123 Main St, Omaha, NE 68101"), "NE")
check("genuine 'Lincoln, NE 68508' -> NE", addr_state_strong("Lincoln, NE 68508"), "NE")
# inline (non-comma-delimited) quadrant was never the bug and must still behave
check("inline '5501 Wilshire Ave NE, STE B' -> '' (unchanged)",
      addr_state_strong("5501 Wilshire Ave NE, STE B"), "")
# control case: parsed state agreed with the column and must stay correct
check("control '5 Great Valley Pkwy STE 100, Malvern PA 19355' -> PA",
      addr_state_strong("5 Great Valley Pkwy STE 100, Malvern PA 19355"), "PA")
check("control 'Memphis, TN' -> TN", addr_state_strong("Memphis, TN"), "TN")

print("\nDEFECT 2 - constant state column must not force Resident")
COLS = ["name", "license_type", "state", "address"]
# constant issuing stamp + varying real addresses -> address must lead
rows = [["A Co", "Wholesaler", "NM", "100 Main St, Plattsburgh, NY 12901"],
        ["B Co", "Wholesaler", "NM", "200 Oak Ave, Albuquerque, NM 87109"],
        ["C Co", "Wholesaler", "NM", "300 Elm Rd, Dallas, TX 75201"]]
rn, basis = derive_resnon(frame(rows, COLS), "license_type", "state", "address", "NM")
check("constant col + varying addresses -> address wins",
      rn, ["Nonresident", "Resident", "Nonresident"])
check("  basis is the address, not the column",
      sorted(set(basis)), ["facility state (address)"])
# varying column -> column leads and a mid-street parse cannot override it
rows2 = [["D Co", "Wholesaler", "PA", "5 Great Valley Pkwy STE 100, Malvern PA 19355"],
         ["E Co", "Wholesaler", "NY", "1 Broadway, New York, NY 10004"]]
rn2, basis2 = derive_resnon(frame(rows2, COLS), "license_type", "state", "address", "PA")
check("varying col leads", rn2, ["Resident", "Nonresident"])
check("  basis is the column", sorted(set(basis2)), ["facility state (column)"])

print("\nNO-REGRESSION - single-jurisdiction street-only rosters keep the column")
# KY Manufacturer (65), AZ (1,313), DC (131): constant column IS the true state and the
# addresses are street-only, so the column fallback must still fire.
ky = [["KY Co %d" % i, "Manufacturer", "KY", "%d Industrial Way" % (100 + i)] for i in range(5)]
rn3, basis3 = derive_resnon(frame(ky, COLS), "license_type", "state", "address", "KY")
check("KY street-only + constant KY column -> all Resident", set(rn3), {"Resident"})
check("  basis falls back to the column", set(basis3), {"facility state (column)"})
az = [["AZ Co %d" % i, "Pharmacy", "AZ", "%d W CAMELBACK RD" % (200 + i)] for i in range(5)]
rn4, _ = derive_resnon(frame(az, COLS), "license_type", "state", "address", "AZ")
check("AZ street-only + constant AZ column -> all Resident", set(rn4), {"Resident"})
dc = [["DC Co 1", "Pharmacy", "DC", "4414 BENNING ROAD, NE"],
      ["DC Co 2", "Pharmacy", "DC", "223 7TH STREET, NE"],
      ["DC Co 3", "Pharmacy", "DC", "1100 K STREET, NW"]]
rn5, _ = derive_resnon(frame(dc, COLS), "license_type", "state", "address", "DC")
check("DC quadrant rows -> Resident (was Nonresident: the live defect)",
      rn5, ["Resident", "Resident", "Resident"])

print("\nDEFECT 1 - foreign addresses must not inherit a constant stamp")
check("looks_foreign ON/Canada", looks_foreign("447 March Road", "ON"), True)
check("looks_foreign QC", looks_foreign("16751 TransCanada Highway", "QC"), True)
check("looks_foreign BC", looks_foreign("100 Granville St", "BC"), True)
check("looks_foreign IT", looks_foreign("Via Roma 1", "IT"), True)
check("looks_foreign UK", looks_foreign("10 Downing St", "UK"), True)
check("looks_foreign by country word", looks_foreign("12 King St, Toronto, Canada", ""), True)
check("looks_foreign by CA postal", looks_foreign("447 March Road, K2K 1X8", ""), True)
# US codes that collide with country abbreviations must NEVER be foreign
for code in ("DE", "IN", "LA", "MS", "OK", "OR", "PA", "VA", "MD", "ME", "PR"):
    check("US code %r not foreign" % code, looks_foreign("1 Main St", code), False)
check("malformed US state not foreign", looks_foreign("23 ORCHARD RD", "NJ New Jersey"), False)
# end-to-end: foreign row under a constant stamp
rows6 = [["F Co", "Wholesaler", "ON", "447 March Road"],
         ["G Co", "Wholesaler", "NM", "200 Oak Ave, Albuquerque, NM 87109"]]
rn6, basis6 = derive_resnon(frame(rows6, COLS), "license_type", "state", "address", "NM")
check("foreign row -> Nonresident (not Resident)", rn6[0], "Nonresident")
check("  basis names the foreign address", basis6[0], "foreign address")

print("\nLICENCE-TYPE markers still win outright")
rows7 = [["H Co", "Non Resident Pharmacy", "NM", "200 Oak Ave, Albuquerque, NM 87109"],
         ["I Co", "Resident Outsourcing Facility", "NM", "1 Foreign Rd, Toronto, Canada"]]
rn7, basis7 = derive_resnon(frame(rows7, COLS), "license_type", "state", "address", "NM")
check("'Non Resident' type -> Nonresident even with an NM address", rn7[0], "Nonresident")
check("'Resident' type -> Resident even with a foreign address", rn7[1], "Resident")
check("  basis is the licence type", set(basis7), {"license type"})

print("\n" + "=" * 96)
print("ALL TESTS PASSED" if not FAILS else "%d FAILURE(S): %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
