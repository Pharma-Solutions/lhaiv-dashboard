#!/usr/bin/env python3
"""Tests for npi_scope() - which rows NPPES should be asked about.

Every licence type and status string below is a REAL value taken from one of the
six registered sources, not an invented example. The whole failure mode this gate
addresses is that board naming does not mean what a regex assumes it means, so a
test built on made-up strings would prove nothing.

The two traps this pins:
  * "Non-Dispensing" in a type does NOT imply no NPI. SC's "Non-resident
    Non-Dispensing Pharmacy" matches at 41.8% and its "Non-Dispensing Drug Outlet"
    at 14.2% - both must still be attempted. The discriminator is the supply-chain
    ROLE (wholesaler / manufacturer / distributor), not the word "dispensing".
  * "ACTIVE" must never be caught by the INACTIVE pattern, and neither must
    "ACTIVE IN RENEWAL" or "Active - Renewal Pending".
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
    print("  [%s] %-72s got=%-5s want=%s" % ("PASS" if ok else "FAIL", label[:72], got, want))


def attempt(t, n="SOME PHARMACY", s="", skip_inactive=True):
    return ve.npi_scope(t, n, s, skip_inactive=skip_inactive)[0]


print("=" * 100)
print("ATTEMPTED - real dispensing sites (must keep)")
print("=" * 100)
for t, s, st in [
    ("Retail Pharmacy", "Active", "GA"), ("Non-Resident Pharmacy", "Active", "GA"),
    ("Low THC Pharmacy", "Active", "GA"), ("Hospital Pharmacy", "Active", "GA"),
    ("Clinic Pharmacy", "Active", "GA"), ("Nuclear Pharmacy", "Active", "GA"),
    ("Home Healthcare - Retail Pharmacy", "Active", "GA"),
    ("Pharmacy", "ACTIVE", "SC"), ("Non-Resident Pharmacy", "ACTIVE", "SC"),
    ("In-State Central Fill Pharmacy", "ACTIVE", "SC"),
    ("Pharmacy - Resident", "Active", "WY"), ("Pharmacy - Nonresident", "Active", "WY"),
    ("Pharmacy - Institutional", "Pending Inspection", "WY"),
    ("Pharmacy - Telepharmacy", "Pending Renewal", "WY"),
    ("Retail Drug Outlet", "Active", "OR"), ("Institutional Drug Outlet", "Active", "OR"),
]:
    check("%s: %r / %r" % (st, t, s), attempt(t, s=s), True)

print()
print("  the 'Non-Dispensing' trap - these DO have NPIs and must still be attempted")
check("SC: 'Non-resident Non-Dispensing Pharmacy' (41.8%% match)",
      attempt("Non-resident Non-Dispensing Pharmacy", s="ACTIVE"), True)
check("SC: 'Non-Dispensing Drug Outlet' (14.2%% match)",
      attempt("Non-Dispensing Drug Outlet", s="ACTIVE"), True)
check("SC: 'Health System Non-Dispensing Permit'",
      attempt("Health System Non-Dispensing Permit", s="ACTIVE"), True)

print()
print("  boards that ship NO status column - blank is unknown, never inactive")
check("MD: 'Pharmacy' / blank status", attempt("Pharmacy", s=""), True)
check("SD: 'Pharmacy - Resident' / blank status", attempt("Pharmacy - Resident", s=""), True)

print()
print("=" * 100)
print("EXCLUDED by supply-chain ROLE (licence type is the board's own classification)")
print("=" * 100)
for t, st in [
    ("Wholesaler Pharmacy", "GA"), ("Manufacturing Pharmacy", "GA"),
    ("Durable Medical Equipment Supplier", "GA"), ("Third Party Distributor", "GA"),
    ("Limited Chemical Wholesale Distributor", "GA"),
    ("Remote Automated Medication System", "GA"), ("PBM - Retail Pharmacy", "GA"),
    ("Reverse Distributor Pharmacy", "GA"),
    ("Non-Resident Wholesale/Distributor", "SC"), ("Wholesale/Distributor", "SC"),
    ("EMS Non-dispensing Drug Outlet", "SC"), ("Non-Resident Virtual Manufacturer", "SC"),
    ("Non Resident Manufacturer/Repackager", "SC"), ("Manufacturer/Repackager", "SC"),
    ("Non-Resident Third Party Logistics Provider", "SC"),
    ("Medical Gas/Legend Device", "SC"), ("Non-Resident Medical Gas/DME", "SC"),
    ("Non-Resident Outsourcing Facility", "SC"), ("Non-Resident Virtual Wholesale", "SC"),
    ("Distributor", "MD"), ("Wholesale / Distributor / 503B", "SD"),
    ("Wholesale Distributor - Human Use", "WY"),
    ("Wholesale Distributor - Medical Oxygen", "WY"),
    ("Third-Party Logistics Provider", "WY"),
    ("Manufacturer", "OR"), ("Wholesaler with Prescription", "OR"),
]:
    check("%s: %r" % (st, t), attempt(t, s="ACTIVE"), False)

print()
print("  the role test reads the TYPE, never the business name")
check("type 'Distributor' + name 'SMITH PHARMACY' -> excluded (board wins)",
      ve.npi_scope("Distributor", "SMITH PHARMACY", "ACTIVE")[0], False)
check("blank type + name 'SMITH PHARMACY' -> attempted (nothing contradicts it)",
      ve.npi_scope("", "SMITH PHARMACY", "ACTIVE")[0], True)
check("type 'Wholesale Drug Pharmacy' is still excluded by role",
      attempt("Wholesale Drug Pharmacy", s="ACTIVE"), False)

print()
print("=" * 100)
print("EXCLUDED by STATUS (default) - and recovered with --npi-include-inactive")
print("=" * 100)
for s, st in [
    ("CLOSED", "SC"), ("LAPSED", "SC"), ("REVOKED", "SC"), ("PERMANENTLY REVOKED", "SC"),
    ("CEASE AND DESIST", "SC"), ("SUSPENDED", "SC"), ("VOLUNTARY SURRENDERED", "SC"),
    ("RELINQUISHED", "SC"), ("CANCELED", "SC"), ("WITHDRAWN", "SC"), ("DENIED", "SC"),
    ("INACTIVE", "SC"), ("Lapsed-Late Renewal Period", "GA"), ("Cancelled", "WY"),
]:
    check("%s: 'Pharmacy' / %r" % (st, s), attempt("Pharmacy", s=s), False)
    check("    ...recovered with --npi-include-inactive",
          attempt("Pharmacy", s=s, skip_inactive=False), True)

print()
print("  statuses that must NOT be read as inactive")
for s, st in [("ACTIVE", "SC"), ("ACTIVE IN RENEWAL", "SC"), ("Active", "GA"),
              ("Active - Renewal Pending", "GA"), ("Probation", "GA"),
              ("APPROVED", "SC"), ("PENDING", "SC"),
              ("Pending Inspection", "WY"), ("Pending Renewal", "WY")]:
    check("%s: 'Pharmacy' / %r stays attempted" % (st, s), attempt("Pharmacy", s=s), True)

print()
print("=" * 100)
print("NOT A PHARMACY AT ALL")
print("=" * 100)
for t in ["Narcotic Treatment Program", "Narcotic Treatment Program Satellite"]:
    check("SC: %r (no dispensing hint in type or name)" % t,
          ve.npi_scope(t, "SOME CLINIC", "ACTIVE")[0], False)

print()
print("  the skip reason is recorded, so a scope decision is auditable")
check("role skip reason", ve.npi_scope("Distributor", "X", "ACTIVE")[1],
      "supply-chain role (no dispensing NPI expected)")
check("status skip reason", ve.npi_scope("Pharmacy", "X", "CLOSED")[1],
      "licence not current (CLOSED)")
check("no-hint skip reason", ve.npi_scope("Widget Shop", "X", "ACTIVE")[1],
      "not a pharmacy (no NPI expected)")

print()
print("=" * 100)
print("RESULT   %d passed, %d failed" % (passed, failed))
print("=" * 100)
sys.exit(1 if failed else 0)
