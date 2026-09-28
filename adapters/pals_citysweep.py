#!/usr/bin/env python3
"""ENG-344 - City-axis sweep to close the residual gap in the PALS in-state pull.

WHY THIS EXISTS
  The main crawler partitions on State + FacilityName prefix. Verification against
  an INDEPENDENT axis (City) showed the resulting file was missing 14 of 1,825
  sampled rows (0.77%), almost all of them Active. The misses cluster in the 'P'
  and 'S' subtrees - the largest - because PALS paging is unstable and a bucket
  that is capped only ever exposes 500 rows, so a child dropping a row deeper down
  is invisible to the crawler's own parent-vs-children check.

  No amount of internal guarding fixes that: a partition cannot audit itself. Two
  independent partitions unioned together can. This sweep re-partitions the same
  population by City and unions the result into the file.

  Neither axis alone is complete - PHILADELPHIA alone hits the 500 cap, and some
  cities are unstable - so any city that comes back at/near the cap is further
  split by name prefix, exactly as the main crawler does.

  The city list is derived from the rows already collected. That is deliberately a
  SUPPLEMENT, not a replacement: it cannot discover a city with zero rows so far,
  which is why this augments the prefix crawl instead of superseding it.
"""
import argparse
import csv
import json
import math
import sys
import time
from collections import Counter

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from pals_adapter import (CAP_SUSPECT, FIXED_ALPHABET, FIELDS, MAX_PAGE, PAGE_SIZE,
                          PHARM_BOARD, SEARCH, UA, URL, flatten, norm)

IN_STATE = 113


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="pals_all.csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--delay", type=float, default=0.55)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.inp, encoding="utf-8")))
    have = {r["pals_license_id"]: r for r in rows}
    cities = sorted({r["address_city"].strip().upper() for r in rows
                     if r["license_type"] == "Pharmacy" and r["address_city"].strip()})
    print("[sweep] file: %s  %s rows" % (a.inp, format(len(rows), ",")))
    print("[sweep] distinct in-state cities to sweep: %d" % len(cities))

    added, capped_cities, stats = {}, [], Counter()

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

        def search(page_no, city, prefix=None):
            pl = {"OptPersonFacility": "Facility", "IsFacility": 1,
                  "ProfessionID": PHARM_BOARD, "LicenseTypeId": IN_STATE,
                  "State": "PA", "Country": "ALL", "County": None,
                  "PersonId": None, "PageNo": page_no, "City": city}
            if prefix:
                pl["FacilityName"] = prefix
            stats["requests"] += 1
            r = pg.request.post(SEARCH, data=json.dumps(pl),
                                headers={"content-type": "application/json"},
                                timeout=180000)
            time.sleep(a.delay)
            if r.status != 200:
                raise RuntimeError("HTTP %s" % r.status)
            rr = json.loads(r.text())
            rr = rr if isinstance(rr, list) else []
            return rr, (int(rr[0].get("TotalRecords") or 0) if rr else 0)

        def collect(city, prefix=None, depth=0):
            """Distinct rows for (city, prefix), re-paging to converge, splitting if capped."""
            first, total = search(1, city, prefix)
            if total == 0:
                return {}
            got = {}
            if total < CAP_SUSPECT:
                for attempt in range(3):
                    page = list(first) if attempt == 0 else []
                    pages = min(math.ceil(total / PAGE_SIZE), MAX_PAGE)
                    for pn in range(2 if attempt == 0 else 1, pages + 1):
                        pr, _ = search(pn, city, prefix)
                        if not pr:
                            break
                        page.extend(pr)
                    for r in page:
                        got[str(r.get("LicenseId"))] = r
                    if len(got) >= total:
                        return got
                    stats["repage"] += 1
                return got
            # capped: split this city by name prefix
            if depth >= 6:
                capped_cities.append((city, prefix, total))
                return got
            for ch in FIXED_ALPHABET:
                cand = (prefix or "") + ch
                if norm(cand) == norm(prefix or ""):
                    continue
                got.update(collect(city, cand, depth + 1))
            return got

        for i, city in enumerate(cities, 1):
            try:
                found = collect(city)
            except Exception as e:
                print("  !! %s: %s" % (city, type(e).__name__))
                stats["city_errors"] += 1
                continue
            new = {k: v for k, v in found.items() if k not in have and k not in added}
            if new:
                added.update(new)
                print("  [%4d/%d] %-26s +%d new (city total %d)"
                      % (i, len(cities), city, len(new), len(found)))
            elif i % 100 == 0:
                print("  [%4d/%d] %-26s ..." % (i, len(cities), city))
        b.close()

    print("")
    print("[sweep] requests: %s | re-page retries: %s | city errors: %s"
          % (format(stats["requests"], ","), stats["repage"], stats["city_errors"]))
    if capped_cities:
        print("[sweep] cities still capped after prefix split (investigate):")
        for c, pre, t in capped_cities[:10]:
            print("    %s prefix=%r total=%s" % (c, pre, t))
    print("[sweep] NEW rows recovered by the City axis: %s" % format(len(added), ","))

    if not added:
        print("[sweep] nothing to add - file already complete on this axis")
        return

    merged = list(rows) + [flatten(v, "Pharmacy") for v in added.values()]
    ids = Counter(r["pals_license_id"] for r in merged)
    dup = {k: v for k, v in ids.items() if v > 1}
    if dup:
        raise RuntimeError("REFUSING: %d duplicate LicenseId after merge" % len(dup))
    if len(merged) <= len(rows):
        raise RuntimeError("REFUSING: merge did not grow the file")

    out = a.out or a.inp.replace(".csv", "_swept.csv")
    cols = [d for _, d in FIELDS] + ["jurisdiction", "__license_type_queried",
                                     "__source", "__retrieved"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(merged)
    print("[sweep] wrote %s: %s rows (was %s, +%s)"
          % (out, format(len(merged), ","), format(len(rows), ","), format(len(added), ",")))
    print("")
    for t, c in Counter(r["license_type"] for r in merged).most_common():
        print("    %-40s %7s" % (t, format(c, ",")))


if __name__ == "__main__":
    main()
