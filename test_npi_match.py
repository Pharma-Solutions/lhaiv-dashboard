#!/usr/bin/env python3
"""Tests for _match() - choosing ONE NPI from an NPPES result set.

The licence addresses below are REAL rows from the HI NED build (2026-10-16), the groups
the old ZIP-only rule collapsed onto a single NPI. The NPPES records are fixtures in the
API's own shape (number / taxonomies / addresses with address_purpose, address_1,
city, postal_code); their NPI numbers are placeholders, not real registrations.

The failure this pins: the old rule returned the FIRST pharmacy in the ZIP, so two QSI
stores in 96744 at different streets both received the same NPI. A licence that carries
a house number must now be matched by that number, or not at all.
"""
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location(
    "ve", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verified_enrich.py"))
ve = importlib.util.module_from_spec(spec)
sys.modules["ve"] = ve
spec.loader.exec_module(ve)

passed = failed = 0


def check(label, got, want):
    global passed, failed
    ok = got == want
    passed, failed = passed + ok, failed + (not ok)
    print("  [%s] %-72s got=%-12s want=%s" % ("PASS" if ok else "FAIL", label[:72], got, want))


def org(npi, street, city, zipc, taxonomy="3336C0003X", purpose="LOCATION", desc="Pharmacy"):
    return {"number": npi, "taxonomies": [{"code": taxonomy, "desc": desc}],
            "addresses": [{"address_purpose": "MAILING", "address_1": "PO BOX 1",
                           "city": city, "postal_code": zipc},
                          {"address_purpose": purpose, "address_1": street,
                           "city": city, "postal_code": zipc}]}


def npi(results, street, city="KANEOHE", zipc="96744"):
    return ve._match(results, city, zipc, street)[0]


QSI_KANEOHE = [org("1000000001", "45-934 KAMEHAMEHA HWY", "KANEOHE", "967443302"),
               org("1000000002", "47-388 HUI IWA ST", "KANEOHE", "96744")]

print("=" * 100)
print("STREET DECIDES between pharmacies in one ZIP (HI QSI, E03463 / E06423)")
print("=" * 100)
check("E03463 45-934 KAMEHAMEHA HWY -> its own NPI",
      npi(QSI_KANEOHE, "45-934 KAMEHAMEHA HWY"), "1000000001")
check("E06423 47-388 HUI IWA ST -> its own NPI (old rule gave ...001)",
      npi(QSI_KANEOHE, "47-388 HUI IWA ST"), "1000000002")
check("result order does not matter",
      npi(list(reversed(QSI_KANEOHE)), "45-934 KAMEHAMEHA HWY"), "1000000001")

print()
print("  a house number nobody in the ZIP has -> no match, never the first candidate")
QUEENS = [org("1000000003", "91-2141 FORT WEAVER RD", "EWA BEACH", "96706")]
check("E17720 91-2301 OLD FORT WEAVER RD vs sole 91-2141 record -> none",
      npi(QUEENS, "91-2301 OLD FORT WEAVER RD", "EWA BEACH", "96706"), "")
check("E02639 91-2141 Fort Weaver Road -> match (case/suffix differ)",
      npi(QUEENS, "91-2141 Fort Weaver Road", "EWA BEACH", "96706"), "1000000003")
check("the reason is recorded",
      ve._match(QUEENS, "EWA BEACH", "96706", "91-2301 OLD FORT WEAVER RD")[1],
      "no NPI at this street (1 in zip5)")

print()
print("  same house number on two NPIs -> street word breaks the tie, else ambiguous")
SAME_NUM = [org("1000000004", "1620 N SCHOOL ST", "HONOLULU", "96817"),
            org("1000000005", "1620 LILIHA ST", "HONOLULU", "96817")]
check("1620 N SCHOOL ST -> SCHOOL wins", npi(SAME_NUM, "1620 N SCHOOL ST", "HONOLULU", "96817"),
      "1000000004")
check("1620 KING ST -> ambiguous, none", npi(SAME_NUM, "1620 KING ST", "HONOLULU", "96817"), "")
check("ambiguous reason", ve._match(SAME_NUM, "HONOLULU", "96817", "1620 KING ST")[1],
      "ambiguous: street# 1620 on 2 different streets")

print()
print("  one site, several pharmacy NPIs -> ALL kept, sorted, each taxonomy in the basis")
print("  (shape of NPPES for Birdi, 43811 Plymouth Oaks Blvd, 48170 - E12999)")
BIRDI = [org("1000000012", "43811 PLYMOUTH OAKS BLVD", "PLYMOUTH", "48170", taxonomy="3336C0004X"),
         org("1000000011", "43811 PLYMOUTH OAKS BLVD", "PLYMOUTH", "48170", taxonomy="3336M0002X"),
         org("1000000013", "43811 PLYMOUTH OAKS BLVD STE 100", "PLYMOUTH", "48170", taxonomy="3336S0011X")]
check("all three NPIs, sorted", npi(BIRDI, "43811 PLYMOUTH OAKS BLVD", "PLYMOUTH", "48170"),
      "1000000011;1000000012;1000000013")
check("basis lists each NPI's taxonomy",
      ve._match(BIRDI, "PLYMOUTH", "48170", "43811 PLYMOUTH OAKS BLVD")[1],
      "street# + zip5 + pharmacy taxonomy; 3 NPIs at this site "
      "[1000000011=3336M0002X, 1000000012=3336C0004X, 1000000013=3336S0011X]")
check("a single NPI is never given a separator",
      ";" in npi(QSI_KANEOHE, "45-934 KAMEHAMEHA HWY"), False)
check("single-NPI basis still names its taxonomy",
      ve._match(QSI_KANEOHE, "KANEOHE", "96744", "45-934 KAMEHAMEHA HWY")[1],
      "street# + zip5 + pharmacy taxonomy [1000000001=3336C0003X]")
check("the other pharmacy in the ZIP is NOT swept in",
      "1000000002" in npi(QSI_KANEOHE, "45-934 KAMEHAMEHA HWY"), False)

print()
print("  no house number on the licence (blank, or 'ONE COMMERCE DRIVE')")
check("sole candidate in ZIP -> match", npi(QUEENS, "", "EWA BEACH", "96706"), "1000000003")
check("sole candidate basis says so", ve._match(QUEENS, "EWA BEACH", "96706", "ONE COMMERCE DRIVE")[1],
      "zip5 + pharmacy taxonomy (sole candidate, no street number) [1000000003=3336C0003X]")
check("two candidates -> none", npi(QSI_KANEOHE, ""), "")
check("one NPI listing two LOCATIONs is still one candidate",
      npi([org("1000000006", "1 MAIN ST", "HILO", "96720"),
           org("1000000006", "2 MAIN ST", "HILO", "96720")], "", "HILO", "96720"), "1000000006")

print()
print("  unchanged rules")
check("non-pharmacy taxonomy ignored",
      npi([org("1000000007", "45-934 KAMEHAMEHA HWY", "KANEOHE", "96744", taxonomy="207Q00000X",
                 desc="Family Medicine")],
          "45-934 KAMEHAMEHA HWY"), "")
check("MAILING-only address ignored",
      npi([org("1000000008", "45-934 KAMEHAMEHA HWY", "KANEOHE", "96744", purpose="MAILING")],
          "45-934 KAMEHAMEHA HWY"), "")
check("city fallback when ZIP absent",
      npi(QSI_KANEOHE, "47-388 HUI IWA ST", "Kaneohe", ""), "1000000002")
check("hyphenated HI number vs unhyphenated NPPES form",
      npi([org("1000000009", "452 KAMEHAMEHA HWY", "KANEOHE", "96744")], "45-2 KAMEHAMEHA HWY"),
      "1000000009")

print()
print("  street_key")
check("HI hyphenated", ve.street_key("91-2301 OLD FORT WEAVER RD"), ("912301", "OLD"))
check("directional skipped", ve.street_key("1620 N SCHOOL ST"), ("1620", "SCHOOL"))
check("no number", ve.street_key("ONE COMMERCE DRIVE"), ("", ""))
check("None", ve.street_key(None), ("", ""))

print()
print("=" * 100)
print("RESULT   %d passed, %d failed" % (passed, failed))
print("=" * 100)
sys.exit(1 if failed else 0)
