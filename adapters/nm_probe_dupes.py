#!/usr/bin/env python3
"""ENG-346 - prove (or refute) the 9-row de-dupe loss in Contact Lens Distributor.

A de-dupe that removes rows is a claim that those rows were redundant. Colorado
taught us that claim is often FALSE - there, colliding license numbers were
distinct businesses and distinct disciplinary episodes. So: pull the one type
that lost rows and print every collision group in full, field by field.
"""
import json
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from nm_adapter import (BOARD, SEED_TYPE, UA, URL, extract_rows, swap_type)

TYPE = sys.argv[1] if len(sys.argv) > 1 else "Contact Lens Distributor"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)
    seed = {}
    import urllib.parse

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

    resp = pg.request.post(seed["url"], data=swap_type(seed["body"], TYPE),
                           headers={"content-type": "application/x-www-form-urlencoded"},
                           timeout=180000)
    rows = extract_rows(resp.text())
    b.close()

print("type: %s   rows returned: %d" % (TYPE, len(rows)))
groups = {}
for r in rows:
    groups.setdefault(r.get("Name"), []).append(r)
coll = {k: v for k, v in groups.items() if len(v) > 1}
print("colliding license numbers: %d  (rows involved: %d, excess: %d)"
      % (len(coll), sum(len(v) for v in coll.values()),
         sum(len(v) - 1 for v in coll.values())))

FIELDS = ["Id", "Name", "Status", "License_Holder_Name__c", "License_Holder_Street__c",
          "License_Holder_City__c", "License_Holder_State__c", "License_Holder_Postal_Code__c",
          "periodStartDate__c", "periodEnd__c", "AccountId", "ContactId",
          "Regulatory_Authorization_Type__c"]
for k, v in coll.items():
    print("\n=== license_number %r : %d records ===" % (k, len(v)))
    for i, r in enumerate(v):
        print("  --- record %d ---" % i)
        for f in FIELDS:
            print("     %-40s %r" % (f, r.get(f)))
    ident = len({json.dumps(r, sort_keys=True, default=str) for r in v}) == 1
    accts = {r.get("AccountId") for r in v}
    names = {r.get("License_Holder_Name__c") for r in v}
    print("  >> byte-identical records: %s" % ident)
    print("  >> distinct AccountIds   : %d %s" % (len(accts), sorted(map(str, accts))))
    print("  >> distinct holder names : %d %s" % (len(names), sorted(map(str, names))))
    print("  >> VERDICT: %s" % ("redundant - safe to collapse" if ident or
                                (len(accts) == 1 and len(names) == 1)
                                else "*** DISTINCT ENTITIES - COLLAPSING LOSES DATA ***"))
