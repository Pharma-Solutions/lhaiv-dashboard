#!/usr/bin/env python3
"""ENG-344 recon pass 6 - find a working sub-partition for IN-STATE Pharmacy.

Pass 5: `State` is honoured (OH=130, NY=243, CA=124 - all correctly filtered and
under the cap). `OtherFacState` and `County` (by CountyId) are SILENTLY IGNORED -
they return the unfiltered capped 500, which is the dangerous failure mode: a
filter that looks applied but isn't.

Remaining problem: in-state `Pharmacy` (type 113). If State=PA still pins at the
cap, we need a sub-partition inside PA. This pass hunts for one and, critically,
tests each candidate for SILENT IGNORING rather than trusting the total.

GUARDRAIL: read-only. No login, no fee, no CAPTCHA solved.
"""
import json
import string
import time

from playwright.sync_api import sync_playwright

URL = "https://www.pals.pa.gov/#!/page/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
SEARCH = "https://www.pals.pa.gov/api/Search/SearchForPersonOrFacilty"
PHARM, T_PHARMACY = 8, 113

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

    def search(page_no=1, lic=T_PHARMACY, **extra):
        pl = {"OptPersonFacility": "Facility", "IsFacility": 1, "ProfessionID": PHARM,
              "LicenseTypeId": lic, "State": "", "Country": "ALL",
              "County": None, "PersonId": None, "PageNo": page_no}
        pl.update(extra)
        r = pg.request.post(SEARCH, data=json.dumps(pl),
                            headers={"content-type": "application/json"}, timeout=180000)
        try:
            rows = json.loads(r.text())
        except Exception:
            return [], 0
        rows = rows if isinstance(rows, list) else []
        return rows, (rows[0].get("TotalRecords") if rows else 0)

    print("=== 1. in-state Pharmacy with State=PA ===")
    rows, tot = search(State="PA")
    print("    total=%s  states returned=%s"
          % (tot, sorted({r.get("State") for r in rows})[:4]))
    capped = int(tot or 0) >= 499
    print("    >> %s" % ("STILL AT CAP - needs a sub-partition" if capped else "under cap"))

    print("\n=== 2. hunt a working sub-partition inside PA ===")
    # Every candidate is tested for SILENT IGNORING: if two different values give
    # the identical total, the field is not being applied.
    probes = {
        "County (name)":      [dict(State="PA", County="Allegheny"),
                               dict(State="PA", County="Philadelphia")],
        "FaclityCounty":      [dict(State="PA", FaclityCounty="Allegheny"),
                               dict(State="PA", FaclityCounty="Philadelphia")],
        "City":               [dict(State="PA", City="PITTSBURGH"),
                               dict(State="PA", City="PHILADELPHIA")],
        "Zip":                [dict(State="PA", Zip="15213"),
                               dict(State="PA", Zip="19104")],
        "LicenseNo prefix":   [dict(State="PA", LicenseNo="PP"),
                               dict(State="PA", LicenseNo="PG")],
    }
    for name, variants in probes.items():
        outs = []
        for v in variants:
            time.sleep(1.2)
            rows, tot = search(**v)
            city = sorted({(r.get("City") or "").upper() for r in rows})[:2]
            outs.append((tot, len(rows), city))
        same = len({o[0] for o in outs}) == 1
        verdict = "*** IGNORED (identical totals) ***" if same else "WORKS"
        print("    %-18s %s" % (name, verdict))
        for v, o in zip(variants, outs):
            arg = {k: x for k, x in v.items() if k != "State"}
            print("        %-34s total=%-5s rows=%-3d sample_city=%s"
                  % (json.dumps(arg), o[0], o[1], o[2]))

    print("\n=== 3. fallback axis: does a name prefix split the set? ===")
    tots = {}
    for ch in list(string.ascii_uppercase)[:6]:
        time.sleep(1.2)
        rows, tot = search(State="PA", FacilityName=ch)
        tots[ch] = tot
        names = [(r.get("FacilityName") or "")[:22] for r in rows[:2]]
        print("    FacilityName=%s total=%-5s sample=%s" % (ch, tot, names))
    print("    >> %s" % ("WORKS" if len(set(tots.values())) > 1
                         else "*** IGNORED ***"))

    b.close()
