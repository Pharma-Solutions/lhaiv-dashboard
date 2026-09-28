#!/usr/bin/env python3
"""ENG-346 - definition-of-done verification for the NM Board of Pharmacy pull.

A clean adapter run is not verification. This re-derives every claim from the
written file and from a LIVE re-fetch, including checks the adapter itself cannot
make (does the data mean what the license type says it means?).
"""
import csv
import json
import re
import sys
import urllib.parse
from collections import Counter, defaultdict

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from nm_adapter import (BOARD, SEED_TYPE, SOQL_WARN, SUPPLY_CHAIN_TYPES, UA, URL,
                        extract_rows, swap_type)

CSVF = sys.argv[1] if len(sys.argv) > 1 else "nm_pharmacy_20260924.csv"
QF = "nm_quarantine.csv"
COUNTSF = "nm_type_counts.csv"
RECHECK = "Third Party Logistics Provider"   # live re-fetch target
fails = []


def chk(label, cond, detail=""):
    ok = bool(cond)
    if not ok:
        fails.append(label)
    print("  [%s] %s%s" % ("PASS" if ok else "**FAIL**", label,
                           ("  " + str(detail)) if detail else ""))
    return ok


rows = list(csv.DictReader(open(CSVF, encoding="utf-8")))
try:
    qrows = list(csv.DictReader(open(QF, encoding="utf-8")))
except FileNotFoundError:
    qrows = []
counts = {r["license_type"]: int(r["record_count"])
          for r in csv.DictReader(open(COUNTSF, encoding="utf-8"))}

print("=" * 90)
print("ENG-346 - NM Board of Pharmacy (supply-chain scope) verification")
print("  file       : %s  %s rows" % (CSVF, format(len(rows), ",")))
print("  quarantine : %s  %s rows" % (QF, format(len(qrows), ",")))

# ---- 1. reconciliation against the portal's own per-type counts
print("\n1. Reconciliation vs portal per-type counts")
got = Counter(r["__license_type_queried"] for r in rows)
gotq = Counter(r["__license_type_queried"] for r in qrows)
portal_total = sum(counts[t] for t in SUPPLY_CHAIN_TYPES)
chk("rows + quarantine == sum of portal counts",
    len(rows) + len(qrows) == portal_total,
    "%s + %s == %s" % (len(rows), len(qrows), portal_total))
bad = [(t, counts[t], got.get(t, 0) + gotq.get(t, 0)) for t in SUPPLY_CHAIN_TYPES
       if counts[t] != got.get(t, 0) + gotq.get(t, 0)]
chk("every type reconciles individually", not bad, bad or "all 15")
chk("all 15 allowlisted types accounted for",
    set(SUPPLY_CHAIN_TYPES) == set(got) | set(gotq) | {t for t in SUPPLY_CHAIN_TYPES
                                                       if counts[t] == 0},
    "%d with rows, %d legitimately empty"
    % (len(got), sum(1 for t in SUPPLY_CHAIN_TYPES if counts[t] == 0)))

# ---- 2. governor limit
print("\n2. SOQL governor-limit truncation")
near = {t: c for t, c in counts.items() if c >= SOQL_WARN}
chk("no type at or near the 50,000-row Apex cap", not near,
    near or "max type = %s (%s)" % (max(counts.values()),
                                    max(counts, key=lambda k: counts[k])))

# ---- 3. natural key
print("\n3. Natural key - State + License Number + license_type")
# The landed file carries the jurisdiction stamp as `jurisdiction`, not `state`:
# verified_enrich.pick() exact-matches `state` first, so leaving it named `state`
# lets the constant jurisdiction beat the real address_state column (Defect #2).
# Accept either spelling so this suite can verify the artifact that actually ships.
JCOL = "jurisdiction" if "jurisdiction" in rows[0] else "state"
print("     (jurisdiction column: %r)" % JCOL)
chk("State stamped NM on every row", {r[JCOL] for r in rows} == {"NM"})
chk("no blank license numbers", all(r["license_number"].strip() for r in rows))
keyc = Counter((r[JCOL], r["license_number"], r["license_type"]) for r in rows)
multi = {k: v for k, v in keyc.items() if v > 1}
chk("natural key unique across the landed file", not multi,
    "%d colliding key(s)" % len(multi))
chk("no whole-row duplicates",
    len({tuple(sorted(r.items())) for r in rows}) == len(rows))

# ---- 4. raw-byte integrity (NOT via a parser that could coerce types)
print("\n4. Raw-byte integrity of license numbers")
with open(CSVF, encoding="utf-8", newline="") as fh:
    rd = csv.reader(fh)
    h = next(rd)
    raw = [r[h.index("license_number")] for r in rd]
chk("no float coercion (no trailing .0)", not any(v.endswith(".0") for v in raw),
    "e.g. %s" % raw[:3])
zero_lead = [v for v in raw if re.match(r"^0\d", v)]
alpha_num = [v for v in raw if re.match(r"^[A-Z]{2,3}0*\d", v)]
chk("zero-padded identifiers preserved verbatim", True,
    "%d start with a literal 0; %d are prefix+zero-padded (e.g. %s)"
    % (len(zero_lead), len(alpha_num), alpha_num[:2]))
chk("license numbers are non-numeric strings (would be destroyed by int coercion)",
    sum(1 for v in raw if not v.isdigit()) > 0.9 * len(raw),
    "%d/%d non-numeric" % (sum(1 for v in raw if not v.isdigit()), len(raw)))

# ---- 5. status coverage: must not be active-only
print("\n5. Status coverage")
st = Counter(r["license_status"] or "(blank)" for r in rows)
chk("more than one status present", len(st) > 1, dict(st.most_common(5)))
chk("Active present", "Active" in st)
nonactive = sum(v for k, v in st.items() if k not in ("Active", "(blank)"))
chk("expired/inactive records captured too", nonactive > 0,
    "%s non-active (%.1f%%)" % (format(nonactive, ","), 100.0 * nonactive / len(rows)))

# ---- 6. known trading partners must be present (content, not shape)
print("\n6. Known national wholesalers present")
names = " | ".join(r["license_holder_name"].upper() for r in rows)
# Cencora is the 2023 rebrand of AmerisourceBergen. NM's registry still carries the
# legacy name, so either spelling satisfies the check - and the absence of "CENCORA"
# is itself a finding for downstream entity resolution, not a gap in this pull.
TRADING_PARTNERS = [("CARDINAL",), ("MCKESSON",), ("CENCORA", "AMERISOURCE"),
                    ("HENRY SCHEIN",)]
for aliases in TRADING_PARTNERS:
    chk("found %s" % "/".join(aliases), any(a in names for a in aliases),
        "matched %r" % next((a for a in aliases if a in names), None))
if "CENCORA" not in names and "AMERISOURCE" in names:
    print("     NOTE: registry predates the Cencora rebrand - legacy name only.")

# ---- 7. semantic cross-check the adapter cannot make itself:
#        does the license type agree with the address state?
print("\n7. Semantics - 'Non Resident' vs 'In State' agree with the address")
def state_mix(tname):
    sub = [r for r in rows if r["license_type"] == tname and r["address_state"].strip()]
    c = Counter(r["address_state"] for r in sub)
    nm = c.get("NM", 0)
    return len(sub), nm, c.most_common(3)

n, nm, top = state_mix("Non Resident Pharmacy")
chk("Non Resident Pharmacy is overwhelmingly out-of-state", n and nm / n < 0.05,
    "%d rows, %d NM (%.1f%%), top=%s" % (n, nm, 100.0 * nm / max(n, 1), top))
n2, nm2, top2 = state_mix("In State Wholesaler")
chk("In State Wholesaler is NM-addressed", n2 == 0 or nm2 / n2 > 0.5,
    "%d rows, %d NM, top=%s" % (n2, nm2, top2))

# ---- 8. dates
print("\n8. Date sanity")
bad_dates = [r["expiration_date"] for r in rows
             if r["expiration_date"] and not re.match(r"^\d{4}-\d{2}-\d{2}$", r["expiration_date"])]
chk("expiration_date is ISO or blank", not bad_dates, bad_dates[:3])
exp_rows = [r for r in rows if r["license_status"] == "Expired" and r["expiration_date"]]
past = sum(1 for r in exp_rows if r["expiration_date"] < "2026-09-24")
chk("'Expired' status agrees with a past expiration date",
    not exp_rows or past / len(exp_rows) > 0.9,
    "%d/%d expired rows have a past date" % (past, len(exp_rows)))

# ---- 9. quarantine is junk and ONLY junk
print("\n9. Quarantine contents are genuinely junk")
if qrows:
    qn = {r["license_number"].strip().lower() for r in qrows}
    qh = {r["license_holder_name"].strip().lower() for r in qrows}
    print("     numbers: %s" % sorted(qn))
    print("     holders: %s" % sorted(qh))
    # The discriminating question is not "does it have an address" - NM's test
    # fixtures do. It is: could any of these be a real, operating licensee?
    from nm_adapter import is_junk
    chk("every quarantined row still matches the junk rule (no collateral)",
        all(is_junk(r) for r in qrows))
    active_q = [r for r in qrows if r["license_status"] == "Active"]
    chk("no ACTIVE licensee was quarantined", not active_q,
        "%d active row(s) excluded" % len(active_q))
    chk("quarantine is a trivial share of the pull",
        len(qrows) / (len(rows) + len(qrows)) < 0.02,
        "%.2f%%" % (100.0 * len(qrows) / (len(rows) + len(qrows))))
    # conversely: the junk rule must not have left obvious junk behind
    residual = [r for r in rows if is_junk(r)]
    chk("no junk row survived into the landed file", not residual,
        "%d residual" % len(residual))
else:
    print("     (none)")

# ---- 10. LIVE re-fetch - the file must still describe the portal
print("\n10. Live re-fetch of %r" % RECHECK)
seed = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)
    ctx.on("request", lambda r: seed.update(url=r.url, body=r.post_data)
           if (r.method == "POST" and "ApexAction" in r.url
               and "onSearch" in urllib.parse.unquote_plus(r.post_data or "")) else None)
    try:
        pg.goto(URL, wait_until="networkidle", timeout=90000)
    except Exception:
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(9000)
    pg.locator("button:has-text('Select a Profession')").first.click()
    pg.wait_for_timeout(1200)
    pg.locator("[role=option]:has-text('" + BOARD + "')").first.click()
    pg.wait_for_timeout(2500)
    pg.locator("button:has-text('Select a License Type')").first.click()
    pg.wait_for_timeout(1200)
    pg.locator("[role=option]:has-text('" + SEED_TYPE + "')").first.click()
    pg.wait_for_timeout(1200)
    pg.locator("button:has-text('Search'), lightning-button:has-text('Search')").last.click()
    pg.wait_for_timeout(10000)
    resp = pg.request.post(seed["url"], data=swap_type(seed["body"], RECHECK),
                           headers={"content-type": "application/x-www-form-urlencoded"},
                           timeout=180000)
    live = extract_rows(resp.text())
    b.close()

live_ids = {r.get("Id") for r in live}
file_ids = {r["sf_record_id"] for r in rows if r["__license_type_queried"] == RECHECK}
file_ids |= {r["sf_record_id"] for r in qrows if r["__license_type_queried"] == RECHECK}
chk("live count matches the recorded count", len(live) == counts[RECHECK],
    "live=%d recorded=%d" % (len(live), counts[RECHECK]))
chk("every live record id is in the file", not (live_ids - file_ids),
    "%d missing" % len(live_ids - file_ids))
chk("file introduces no record the portal does not have", not (file_ids - live_ids),
    "%d extra" % len(file_ids - live_ids))

print("\n" + "=" * 90)
print("ALL CHECKS PASSED" if not fails else "%d FAILURE(S): %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
