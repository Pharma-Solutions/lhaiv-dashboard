#!/usr/bin/env python3
"""NJ Drug Control Unit / CDS - BUSINESS (establishment) scope adapter.

CREDENTIAL SEPARATION - this is the THIRD distinct NJ credential we hold:
    1. NJ Board of Pharmacy            (held: NJ - nj-board-of-pharmacy - Complete)
    2. NJ Drug & Medical Device Reg.   (held: NJ DOH Tyler, njgov.healthinspections.us)
    3. NJ Drug Control Unit / CDS      (THIS ONE)
`verify_nj_cds.py` proves licence-number disjointness from both.

CHANNEL (recon 2026-09-25)
  newjersey.mylicense.com MyLicense. Two modules, NEITHER captcha-gated - an earlier
  note in this repo claiming /verification/ was captcha-gated is WRONG
  (hasCaptchaField=False, 0 captcha elements on /verification/, /verification_bulk/
  and the facility variant).

  `?facility=Y` is the establishment/individual separator and it is first class:
  it drops the licence-type picklist from 286 options to 88, shedding exactly the
  INDIVIDUAL CDS credentials (CDS Physician, CDS Dentist, CDS Veterinarian, ...)
  which are explicitly out of scope.

  The BULK DOWNLOAD path is broken for business scope: `btnBulkDownLoad` silently
  bounces back to Verification_BULK/ with no file, no error page and no charge.
  It works for person scope, which is why nj_adapter.py can use it. So this adapter
  scrapes the paged grid instead.

  Results live in the ASP.NET DataGrid `#datagrid_results`, 40 rows/page, paged via
  __doPostBack on a WINDOWED pager (numbered links plus '...'). Probed across 80
  pages: every page returned 40 NEW distinct rows, zero duplicates - NJ paging is
  stable, unlike PALS. We still verify on an independent axis, because a partition
  cannot audit itself.

GUARDS
  - enum-drift on the 4 CDS business types and on licence status
  - distinct-key accounting (never count a list; duplicates would hide a short page)
  - `if not frames: raise`; refuse to overwrite a good file with an empty/short one
  - every source value preserved verbatim (leading zeros, punctuation)
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter

from playwright.sync_api import sync_playwright

BASE = "https://newjersey.mylicense.com/verification"
SEARCH = BASE + "/Search.aspx?facility=Y"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

CDS_BUSINESS_TYPES = [
    "CDS Pharmacy",
    "CDS Out of State Pharmacy",
    "CDS ADS Branch",
    "CDS Automated Dispensing Sys",
]
TYPE_SELECT = "select[name='t_web_lookup__license_type_name']"
CITY_INPUT = "input[name='t_web_lookup__addr_city']"
SUBMIT = "input[name='sch_button']"

COLS = ["full_name", "license_number", "profession", "license_type",
        "license_status", "address_city", "address_state"]

# THE CAP. The MyLicense DataGrid pager stops dead at 80 pages x 40 rows = 3,200.
# Past page 80 the '...' link merely oscillates the window between 1-39 and 42-80,
# so a query with more than 3,200 matches is SILENTLY TRUNCATED. CDS Pharmacy came
# back at exactly 3,200 on the first build - a round number that is the tell.
# Beaten by recursive full-name PREFIX partitioning. Matching is a literal prefix
# (probed: "ALGREEN" -> 0 while "WALGREEN" -> 299), including spaces and
# punctuation, so unlike PALS there is no normalisation to design around.
PAGE_CAP = 80
ROW_CAP = PAGE_CAP * 40
CAP_SUSPECT = ROW_CAP - 10
NAME_INPUT = "input[name='t_web_lookup__full_name']"
ALPHABET = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") + list(" '&.,-#/()")
MAX_DEPTH = 12

stats = Counter()


def log(m):
    print("[njcds] " + str(m), flush=True)


def grid_rows(pg):
    """Rows currently rendered in the DataGrid, verbatim."""
    return pg.evaluate("""() => {
      const g = document.querySelector('#datagrid_results');
      if (!g) return [];
      return [...g.rows].slice(1)
        .map(r => [...r.cells].map(c => c.innerText.replace(/\\u00a0/g,' ').trim()))
        .filter(r => r.length >= 7 && (r[1] || r[0]));
    }""")


def advance(pg, want):
    """Click the pager link for `want`, else '...' to shift the window. False = end."""
    return pg.evaluate("""(want) => {
      const links = [...document.querySelectorAll('a')];
      let t = links.find(a => a.innerText.trim() === String(want));
      if (!t) t = links.find(a => a.innerText.trim() === '...');
      if (!t) return false;
      t.click();
      return true;
    }""", want)


def settle(pg, tries=12):
    """Wait for the grid; postbacks can destroy the execution context mid-read."""
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    for _ in range(tries):
        try:
            if pg.locator("#datagrid_results").count():
                return True
        except Exception:
            pass
        pg.wait_for_timeout(1000)
    return False


def run_search(pg, lic_type, city=None, name=None):
    pg.goto(SEARCH, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(1500)
    # enum-drift: the type must still exist in the live picklist
    opts = pg.evaluate("""(sel) => [...document.querySelectorAll(sel+' option')]
                                   .map(o => o.innerText.trim())""", TYPE_SELECT)
    if lic_type not in opts:
        raise RuntimeError("ENUM DRIFT: %r absent from the live business picklist" % lic_type)
    pg.select_option(TYPE_SELECT, label=lic_type)
    if city:
        pg.fill(CITY_INPUT, city)
    if name:
        pg.fill(NAME_INPUT, name)
    pg.wait_for_timeout(700)
    # Click the submit BY NAME. A comma selector returns document order, and the
    # person/business cross-link sits earlier in the document - that is the bug that
    # silently flipped nj_adapter.py back to person scope.
    pg.locator(SUBMIT).first.click()
    pg.wait_for_timeout(6000)
    stats["searches"] += 1


def walk(pg, lic_type, city=None, name=None, delay=0.4):
    """All distinct rows for one query, keyed on (license_number, full_name)."""
    run_search(pg, lic_type, city, name)
    settle(pg)
    body = pg.inner_text("body")
    if re.search(r"(Server Error|Runtime Error)", body, re.I):
        raise RuntimeError("server error on %r/%r/%r" % (lic_type, city, name))
    seen, page, stale = {}, 0, 0
    while page < PAGE_CAP + 2:
        try:
            rows = grid_rows(pg)
        except Exception:
            settle(pg)
            rows = grid_rows(pg)
        before = len(seen)
        for r in rows:
            seen[(r[1], r[0])] = r
        page += 1
        stats["pages"] += 1
        if len(seen) == before:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        if not advance(pg, page + 1):
            break
        time.sleep(delay)
        pg.wait_for_timeout(1600)
        settle(pg)
    return seen


def crawl(pg, lic_type, prefix, depth, holes, delay):
    """Recursive prefix partitioning; splits any bucket pinned at the 3,200 cap."""
    got = walk(pg, lic_type, name=prefix or None, delay=delay)
    label = "%s|prefix=%r" % (lic_type, prefix or "")
    if len(got) < CAP_SUSPECT:
        return got
    stats["capped_buckets"] += 1
    log("    capped at %d: %s - splitting" % (len(got), label))
    if depth >= MAX_DEPTH:
        holes.append((label, len(got), "MAX_DEPTH while still capped"))
        return got
    pos = len(prefix or "")
    observed = {(r[0] or "")[pos:pos + 1].upper() for r in got.values()}
    observed.discard("")
    merged = {}
    for ch in sorted(set(ALPHABET) | observed):
        cand = (prefix or "") + ch
        # STRUCTURAL guard, applied BEFORE recursing. NJ trims the name filter, so a
        # prefix of leading whitespace matches everything and stays pinned at the cap
        # forever. The post-hoc "child == parent" test below cannot save us here: by
        # the time it runs, the child has already recursed and burned the requests.
        # (Same failure mode as the PALS crawler; caught here by the same rule.)
        if cand != cand.lstrip() or "  " in cand:
            stats["skipped_degenerate_prefix"] += 1
            continue
        sub = crawl(pg, lic_type, cand, depth + 1, holes, delay)
        # safety net: a child reproducing the parent exactly means the filter no-oped
        if len(sub) == len(got) and len(sub) >= CAP_SUSPECT:
            stats["filter_not_applied"] += 1
            continue
        merged.update(sub)
    # self-check: parent rows must reappear below; recover any that do not
    missing = set(got) - set(merged)
    unsplittable = {k for k in missing if len((got[k][0] or "").strip()) <= pos}
    merged.update({k: got[k] for k in unsplittable})
    missing -= unsplittable
    if missing:
        log("    ~ %s: %d parent row(s) not under any child - recovered" % (label, len(missing)))
        merged.update({k: got[k] for k in missing})
        stats["recovered"] += len(missing)
    return merged


def flatten(r, lic_type):
    out = dict(zip(COLS, [r[0], r[1], r[2], r[3], r[4], r[5], r[6]]))
    out["jurisdiction"] = "NJ"          # native, so enrich Defect #2 cannot bite
    out["__license_type_queried"] = lic_type
    out["__source"] = ("NJ Division of Consumer Affairs - Drug Control Unit / CDS, "
                       "business scope. newjersey.mylicense.com/verification "
                       "Search.aspx?facility=Y (MyLicense DataGrid)")
    out["__retrieved"] = time.strftime("%Y-%m-%d")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--types", default=None, help="comma-separated subset, for probing")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    targets = ([t.strip() for t in a.types.split(",")] if a.types else CDS_BUSINESS_TYPES)

    records, per_type = {}, {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=not a.headed)
        ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1400})
        pg = ctx.new_page()
        pg.set_default_timeout(90000)
        holes = []
        for t in targets:
            log("")
            log("%s ..." % t)
            found = crawl(pg, t, "", 0, holes, a.delay)
            per_type[t] = len(found)
            flag = "  <-- STILL AT CAP" if len(found) >= CAP_SUSPECT else ""
            log("  %-30s %6d row(s)%s" % (t, len(found), flag))
            for k, r in found.items():
                records[k] = (r, t)
        b.close()

    if holes:
        for lab, n, why in holes:
            log("  HOLE: %s (%d) - %s" % (lab, n, why))
        raise RuntimeError("REFUSING TO WRITE: %d coverage hole(s)" % len(holes))

    if not records:
        raise RuntimeError("REFUSING TO WRITE: zero rows collected")

    frames = [flatten(r, t) for r, t in records.values()]

    # whole-row de-dupe (lossless); natural-key collisions reported, never collapsed
    seen, deduped, dups = set(), [], 0
    for r in frames:
        sig = tuple(sorted(r.items()))
        if sig in seen:
            dups += 1
            continue
        seen.add(sig)
        deduped.append(r)
    log("")
    log("de-dupe (whole row, lossless): %d -> %d (removed %d)"
        % (len(frames), len(deduped), dups))

    keyc = Counter((r["jurisdiction"], r["license_number"], r["license_type"])
                   for r in deduped)
    multi = {k: v for k, v in keyc.items() if v > 1}
    if multi:
        log("NATURAL-KEY COLLISIONS RETAINED (not collapsed): %d key(s)" % len(multi))
        for k, v in list(multi.items())[:8]:
            log("    %s / %s x%d" % (k[1], k[2], v))

    # enum-drift on status
    statuses = Counter(r["license_status"] or "(blank)" for r in deduped)
    prof = Counter(r["profession"] for r in deduped)
    if set(prof) - {"CDS"}:
        log("!! non-CDS profession values present: %s" % dict(prof))

    out = a.out or ("nj_cds_business_%s.csv" % time.strftime("%Y%m%d"))
    cols = COLS + ["jurisdiction", "__license_type_queried", "__source", "__retrieved"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(deduped)
    log("wrote %s: %d rows x %d cols" % (out, len(deduped), len(cols)))

    json.dump(per_type, open("nj_cds_type_counts.json", "w", encoding="utf-8"), indent=1)
    log("")
    log("per-type counts:")
    for t, c in sorted(per_type.items(), key=lambda kv: -kv[1]):
        log("    %-32s %6d" % (t, c))
    log("    %-32s %6d" % ("TOTAL", sum(per_type.values())))
    log("")
    log("per-status counts:")
    for k, v in statuses.most_common():
        log("    %-32s %6d" % (k, v))
    log("")
    log("profession values: %s" % dict(prof))
    log("searches=%d pages=%d" % (stats["searches"], stats["pages"]))


if __name__ == "__main__":
    main()
