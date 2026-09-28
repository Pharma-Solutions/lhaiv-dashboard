#!/usr/bin/env python3
"""ENG-344 recon pass 3 - class enumeration + pagination contract.

Established so far:
  POST /api/Search/SearchForPersonOrFacilty
       {"OptPersonFacility":"Person|Facility","IsFacility":0|1,"PageNo":n, ...}
  GET  /api/Search/FetchLookupData  -> ProfessionList (+ license types)
  Search is NOT captcha-gated; every row embeds TotalRecords.

This pass answers:
  A. What license types exist under State Board of Pharmacy (ProfessionID 8)?
     Which are FACILITY classes vs INDIVIDUAL classes?
  B. What is the page size, and does PageNo actually walk (page 2 != page 1)?
  C. What does the Board-of-Pharmacy facility search report as TotalRecords?
  D. Is there a server-side cap (the blank search reported exactly 499)?
  E. SEPARATION: do the held PA DDC professions ("Device",
     "Wholesaler/Distributor") live under this board, i.e. is DDC a slice of PALS?

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
PHARM_BOARD_ID = 8


def post(pg, url, payload):
    r = pg.request.post(url, data=json.dumps(payload),
                        headers={"content-type": "application/json"}, timeout=180000)
    return r.status, r.text()


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

    # ---------- A. lookup data
    st, txt = post(pg, LOOKUP, {})
    print("[A] FetchLookupData -> %s, %d bytes" % (st, len(txt)))
    look = json.loads(txt)
    print("    top-level keys: %s" % list(look.keys()))
    json.dump(look, open("pals_lookup.json", "w", encoding="utf-8"), indent=1)

    for k, v in look.items():
        if isinstance(v, list):
            print("    %-26s %d entries   sample=%s"
                  % (k, len(v), json.dumps(v[0])[:150] if v else "-"))

    # license types under the pharmacy board
    lt_key = next((k for k in look
                   if isinstance(look[k], list) and look[k]
                   and any("LicenseType" in str(f) for f in look[k][0])), None)
    print("\n[A] license-type list key: %r" % lt_key)
    if lt_key:
        lts = look[lt_key]
        f0 = list(lts[0].keys())
        print("    fields: %s" % f0)
        idf = next((f for f in f0 if f.lower() in
                    ("professionid", "profession_id", "professionids")), None)
        pharm = [x for x in lts if str(x.get(idf)) == str(PHARM_BOARD_ID)] if idf else []
        print("    State Board of Pharmacy (%s=%d): %d license type(s)"
              % (idf, PHARM_BOARD_ID, len(pharm)))
        for x in pharm:
            print("       %s" % json.dumps(x))
        json.dump(pharm, open("pals_pharmacy_license_types.json", "w",
                              encoding="utf-8"), indent=1)

    # ---------- B/C/D. facility search under the pharmacy board
    def search(page_no, is_facility=1, prof=PHARM_BOARD_ID, lic_type=None):
        pl = {"OptPersonFacility": "Facility" if is_facility else "Person",
              "IsFacility": is_facility, "ProfessionID": prof,
              "State": "", "Country": "ALL", "County": None,
              "PersonId": None, "PageNo": page_no}
        if lic_type is not None:
            pl["LicenseTypeId"] = lic_type
        st, t = post(pg, SEARCH, pl)
        try:
            rows = json.loads(t)
        except Exception:
            print("    !! non-JSON response: %s" % t[:300])
            return st, []
        return st, rows if isinstance(rows, list) else []

    print("\n[B/C] Board-of-Pharmacy FACILITY search")
    st, p1 = search(1)
    print("    page 1 -> status=%s rows=%d" % (st, len(p1)))
    if p1:
        tot = p1[0].get("TotalRecords")
        print("    TotalRecords reported: %s" % tot)
        print("    fields: %s" % list(p1[0].keys()))
        print("    sample: %s" % json.dumps(
            {k: p1[0][k] for k in list(p1[0])[:14]})[:400])
        print("    LicenceType spread on page 1: %s"
              % dict(Counter(r.get("LicenceType") for r in p1)))
        print("    ProfessionType spread      : %s"
              % dict(Counter(r.get("ProfessionType") for r in p1)))
        print("    IsFacility spread          : %s"
              % dict(Counter(r.get("IsFacility") for r in p1)))

    time.sleep(1.5)
    st2, p2 = search(2)
    print("\n    page 2 -> status=%s rows=%d" % (st2, len(p2)))
    if p1 and p2:
        k1 = {r.get("LicenseId") for r in p1}
        k2 = {r.get("LicenseId") for r in p2}
        print("    page1 ids: %d, page2 ids: %d, overlap: %d"
              % (len(k1), len(k2), len(k1 & k2)))
        print("    >> PAGINATION %s"
              % ("WORKS (disjoint pages)" if not (k1 & k2) else "*** BROKEN / repeats ***"))
        print("    page size appears to be: %d" % len(p1))

    # ---------- D. cap probe: walk far out
    print("\n[D] cap probe - request a deep page")
    for pn in (5, 10, 25, 50):
        time.sleep(1.2)
        stx, px = search(pn)
        print("    PageNo=%-3d -> rows=%-4d total=%s"
              % (pn, len(px), px[0].get("TotalRecords") if px else "-"))
        if not px:
            print("    (empty at PageNo=%d)" % pn)
            break

    # ---------- E. separation check vs the held PA DDC file
    print("\n[E] SEPARATION vs held PA DDC ('Device', 'Wholesaler/Distributor')")
    seen_types, seen_profs = set(), set()
    for pn in (1, 2, 3):
        time.sleep(1.2)
        _, px = search(pn)
        seen_types |= {r.get("LicenceType") for r in px}
        seen_profs |= {r.get("ProfessionType") for r in px}
    print("    LicenceType values seen under board 8: %s" % sorted(filter(None, seen_types)))
    print("    ProfessionType values seen           : %s" % sorted(filter(None, seen_profs)))
    ddc = {"Device", "Wholesaler/Distributor"}
    print("    DDC professions present here? %s"
          % (sorted(ddc & (seen_types | seen_profs)) or "NO - disjoint so far"))

    b.close()
print("\n[pals3] -> pals_lookup.json / pals_pharmacy_license_types.json")
