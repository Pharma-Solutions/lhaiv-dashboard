#!/usr/bin/env python3
"""ENG-344 recon pass 4 - class enumeration + THE 500-RECORD CAP.

Pass 3 found the decisive constraint: a Board-of-Pharmacy facility search reports
TotalRecords=500 and pages stop dead after PageNo=10 (10 x 50). A blank
all-boards search reported 499. That is a SERVER-SIDE CAP, not a real count -
PA publishes ~106,419 licences. Any adapter that trusts a single query silently
truncates.

This pass:
  A. FetchLookupData with the correct payload shape (an ARRAY of lookup
     descriptors, Filter = "Person" | "Facility") -> ProfessionList +
     LicenseTypeList. Enumerate the Board of Pharmacy classes.
  B. Prove the 500 cap: does a query KNOWN to exceed 500 still report exactly 500?
  C. Test whether partitioning by LicenseTypeId brings partitions under the cap,
     and whether per-type totals look real.
  D. Resolve the PA DDC separation question properly (do NOT assume).

GUARDRAIL: read-only. No login, no fee, no CAPTCHA solved.
"""
import json
import time
from collections import Counter

from playwright.sync_api import sync_playwright

URL = "https://www.pals.pa.gov/#!/page/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
SEARCH = "https://www.pals.pa.gov/api/Search/SearchForPersonOrFacilty"
LOOKUP = "https://www.pals.pa.gov/api/Search/FetchLookupData"
PHARM = 8


def lookup_payload(filt):
    return [{"LookupType": "COUNTRY", "LookupID": 1, "Filter": ""},
            {"LookupType": "STATE", "LookupID": 2, "Filter": ""},
            {"LookupType": "CountyList", "LookupID": 3, "Filter": ""},
            {"LookupType": "ProfessionList", "LookupID": 4, "Filter": filt},
            {"LookupType": "LicenseTypeList", "LookupID": 5, "Filter": filt},
            {"LookupType": "DisciplinaryActionTypeList", "LookupID": 6, "Filter": ""}]


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

    def post(url, payload):
        r = pg.request.post(url, data=json.dumps(payload),
                            headers={"content-type": "application/json"}, timeout=180000)
        return r.status, r.text()

    # ---------- A. lookups
    store = {}
    for filt in ("Person", "Facility"):
        st, txt = post(LOOKUP, lookup_payload(filt))
        print("[A] FetchLookupData(Filter=%s) -> %s, %d bytes" % (filt, st, len(txt)))
        if st != 200:
            print("    body: %s" % txt[:300])
            continue
        d = json.loads(txt)
        store[filt] = d
        print("    keys: %s" % list(d.keys()))
        for k, v in d.items():
            if isinstance(v, list) and v:
                print("      %-28s %5d  sample=%s" % (k, len(v), json.dumps(v[0])[:130]))
    json.dump(store, open("pals_lookup.json", "w", encoding="utf-8"), indent=1)

    # pharmacy license types
    print("\n[A] License types under State Board of Pharmacy (ProfessionID=%d)" % PHARM)
    pharm_types = {}
    for filt, d in store.items():
        lts = d.get("LicenseTypeList") or []
        if not lts:
            continue
        idf = next((f for f in lts[0] if f.lower().startswith("profession")), None)
        sub = [x for x in lts if str(x.get(idf)) == str(PHARM)]
        pharm_types[filt] = sub
        print("    %-9s %d type(s)  (field %r)" % (filt, len(sub), idf))
        for x in sub:
            print("       %s" % json.dumps(x))
    json.dump(pharm_types, open("pals_pharmacy_license_types.json", "w",
                                encoding="utf-8"), indent=1)

    # ---------- B/C. the cap, and partitioning
    def search(page_no, is_facility, prof=PHARM, lic_type=None, extra=None):
        pl = {"OptPersonFacility": "Facility" if is_facility else "Person",
              "IsFacility": is_facility, "ProfessionID": prof,
              "State": "", "Country": "ALL", "County": None,
              "PersonId": None, "PageNo": page_no}
        if lic_type is not None:
            pl["LicenseTypeId"] = lic_type
        if extra:
            pl.update(extra)
        st, t = post(SEARCH, pl)
        try:
            rows = json.loads(t)
        except Exception:
            return st, [], t[:200]
        return st, (rows if isinstance(rows, list) else []), ""

    print("\n[B] CAP PROOF - the same query under different filters")
    for label, kw in (("board 8, facility", dict(is_facility=1)),
                      ("board 8, person", dict(is_facility=0)),
                      ("ALL boards, person", dict(is_facility=0, prof=None)),
                      ("ALL boards, facility", dict(is_facility=1, prof=None))):
        time.sleep(1.2)
        st, rows, err = search(1, **kw)
        tot = rows[0].get("TotalRecords") if rows else None
        print("    %-22s status=%s rows=%-3d TotalRecords=%s %s"
              % (label, st, len(rows), tot, err))
    print("    >> a constant 499/500 across wildly different filters == a CAP,")
    print("       not a count. PA publishes ~106,419 licences.")

    print("\n[C] partition by LicenseTypeId - do partitions come in under the cap?")
    fac = pharm_types.get("Facility") or []
    idk = next((f for f in (fac[0] if fac else {}) if "licensetype" in f.lower()
                and "id" in f.lower()), None)
    namek = next((f for f in (fac[0] if fac else {}) if "name" in f.lower()
                  or f.lower() == "licensetype"), None)
    print("    id field=%r name field=%r" % (idk, namek))
    per_type = {}
    for x in fac:
        tid, tname = x.get(idk), x.get(namek)
        time.sleep(1.2)
        st, rows, err = search(1, 1, PHARM, tid)
        tot = rows[0].get("TotalRecords") if rows else 0
        per_type[tname] = tot
        flag = "  <-- AT CAP, truncated" if tot and int(tot) >= 499 else ""
        print("    %-44s id=%-6s total=%-6s%s" % (str(tname)[:44], tid, tot, flag))
    json.dump(per_type, open("pals_facility_type_counts.json", "w",
                             encoding="utf-8"), indent=1)
    print("    sum of facility per-type totals: %s"
          % format(sum(int(v or 0) for v in per_type.values()), ","))

    # ---------- D. DDC separation
    print("\n[D] SEPARATION - where do 'Device' / 'Wholesaler/Distributor' live?")
    for filt, d in store.items():
        for k in ("ProfessionList", "LicenseTypeList"):
            for x in d.get(k) or []:
                s = json.dumps(x)
                if "Device" in s or "Wholesal" in s:
                    print("    [%s/%s] %s" % (filt, k, s[:200]))

    b.close()
print("\n[pals4] -> pals_lookup.json / pals_pharmacy_license_types.json / "
      "pals_facility_type_counts.json")
