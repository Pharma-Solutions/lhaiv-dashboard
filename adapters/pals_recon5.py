#!/usr/bin/env python3
"""ENG-344 recon pass 5 - can we PARTITION under the 500 cap?

Pass 4 proved the blocker: PALS caps every query at ~500 rows (TotalRecords came
back 497/499/500 for four wildly different queries). Partitioning by license type
is NOT enough - "Pharmacy" (499) and "Nonresident Pharmacy" (500) are both pinned
at the cap.

So the adapter needs a second partition axis that splits those two types into
buckets that each land UNDER 500. Candidates visible in the search form:
    County  (in-state pharmacies - PA has 67 counties)
    State   (nonresident pharmacies - one bucket per US state)
    City / Zip / LicenseNo / name prefix

This pass tests whether those filters are actually honoured by the API, and
whether the resulting buckets clear the cap.

GUARDRAIL: read-only. No login, no fee, no CAPTCHA solved.
"""
import json
import time

from playwright.sync_api import sync_playwright

URL = "https://www.pals.pa.gov/#!/page/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
SEARCH = "https://www.pals.pa.gov/api/Search/SearchForPersonOrFacilty"
PHARM = 8
T_PHARMACY = 113        # in-state Pharmacy, reported 499 (capped)
T_NONRESIDENT = 4       # Nonresident Pharmacy, reported 500 (capped)

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

    def search(lic_type, page_no=1, **extra):
        pl = {"OptPersonFacility": "Facility", "IsFacility": 1, "ProfessionID": PHARM,
              "LicenseTypeId": lic_type, "State": "", "Country": "ALL",
              "County": None, "PersonId": None, "PageNo": page_no}
        pl.update(extra)
        r = pg.request.post(SEARCH, data=json.dumps(pl),
                            headers={"content-type": "application/json"}, timeout=180000)
        try:
            rows = json.loads(r.text())
        except Exception:
            return r.status, [], None
        rows = rows if isinstance(rows, list) else []
        return r.status, rows, (rows[0].get("TotalRecords") if rows else 0)

    counties = json.load(open("pals_lookup.json", encoding="utf-8"))["Facility"]["Counties"]
    pa_counties = [c for c in counties if c.get("StateCode") == "PA"]
    print("PA counties available: %d" % len(pa_counties))

    print("\n=== 1. does County filter work? (in-state Pharmacy, type 113) ===")
    tot_by_county = {}
    for c in pa_counties[:8]:
        time.sleep(1.2)
        st, rows, tot = search(T_PHARMACY, County=c["CountyId"])
        tot_by_county[c["CountyName"]] = tot
        cityset = {r.get("County") for r in rows}
        print("    %-18s id=%-6s total=%-5s  rows=%-3d  County values returned=%s"
              % (c["CountyName"], c["CountyId"], tot, len(rows),
                 sorted(filter(None, cityset))[:3]))
    distinct = len(set(tot_by_county.values()))
    print("    >> County filter %s (%d distinct totals across %d counties)"
          % ("WORKS" if distinct > 1 else "*** IGNORED ***", distinct, len(tot_by_county)))

    print("\n=== 2. does State filter work? (Nonresident Pharmacy, type 4) ===")
    for stc in ("OH", "NY", "CA", "TX", "FL"):
        time.sleep(1.2)
        st, rows, tot = search(T_NONRESIDENT, OtherFacState=stc)
        vals = {r.get("State") for r in rows}
        print("    OtherFacState=%-3s total=%-5s rows=%-3d  State values returned=%s"
              % (stc, tot, len(rows), sorted(filter(None, vals))[:3]))

    print("\n=== 3. alternative: does the STATE field (not OtherFacState) filter? ===")
    for stc in ("OH", "NY", "CA"):
        time.sleep(1.2)
        st, rows, tot = search(T_NONRESIDENT, State=stc)
        vals = {r.get("State") for r in rows}
        print("    State=%-3s total=%-5s rows=%-3d  State values returned=%s"
              % (stc, tot, len(rows), sorted(filter(None, vals))[:3]))

    print("\n=== 4. baseline for comparison (no partition) ===")
    for t, nm in ((T_PHARMACY, "Pharmacy"), (T_NONRESIDENT, "Nonresident Pharmacy")):
        time.sleep(1.2)
        st, rows, tot = search(t)
        print("    %-22s total=%s" % (nm, tot))

    b.close()
