#!/usr/bin/env python3
"""MD Board of Pharmacy - per-permit-type sort-union harvest for FULL coverage.

WHY PER-TYPE
  An All/All/All search caps at 3,200 (80 pages x 40). A sort-union of that search gave
  ASC 3,200 + DESC 3,200 with ZERO overlap, proving the roster exceeds 6,400 - the two
  windows never met. So the population must be partitioned, and permit type is the only
  partition the search form offers that is honoured.

EVERY TYPE IS SORT-UNIONED, NOT MERELY WALKED
  Distributor alone is >= 4,585 in the 6,400 sample, i.e. already past the 3,200 cap. A
  plain per-type walk would truncate it silently and look healthy. Each type therefore
  gets ASC 80 pages + a re-sort + DESC 80 pages, unioned by GUID, with the overlap test
  as the completeness proof for that type.

THE HIDDEN TYPES
  The dropdown offers 5 types; the data carries 8. Prescription Drug Drop-Off (73),
  Prescription Drug Repository (20) and Technician Training Program (2) cannot be
  selected, so they can only come from the unfiltered halves already captured in
  md_halves.json. They are small and well inside 6,400, but that is a LOWER BOUND, not
  proof - the script says so rather than implying completeness it cannot demonstrate.

CAPTCHA
  This script never touches the captcha. Mark solves it. After the first search we run a
  SESSION TEST - a second search with no re-solve - to learn whether MyLicense validates
  once per session or per submit, and adapt: fully automatic if once, otherwise pausing
  for a solve per type in the SAME browser (never relaunching, so no solve is wasted).
"""
import argparse
import json
import sys
import time
from collections import Counter

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from md_bop_sortunion import (PAGE_SIZE, PAGER_JS, ROWS_JS, URL, click_label, dom_read,
                              settle, walk)

SELECTABLE = ["Distributor", "Pharmacy", "Pharmacy Waiver", "Corporation",
              "Drug Therapy Management"]
HIDDEN = ["Prescription Drug Drop-Off", "Prescription Drug Repository",
          "Technician Training Program"]
HALVES = "md_halves.json"
OUT = "md_guids_full.json"
SORT_HEADER = "License #"          # the only sortable column, confirmed live


def log(m):
    print("[mdT] " + str(m), flush=True)


def run_search(pg, lic_type):
    """Fill the facility form for one permit type and submit. Captcha untouched."""
    pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(2500)
    for nm, val in (("t_web_lookup__profession_name", "All"),
                    ("t_web_lookup__license_type_name", lic_type),
                    ("t_web_lookup__license_status_name", "All")):
        try:
            pg.select_option("select[name='%s']" % nm, label=val)
        except Exception as e:
            log("   could not set %s=%s (%s)" % (nm, val, type(e).__name__))
    pg.wait_for_timeout(600)
    try:
        pg.locator("input[name='sch_button']").first.click()
    except Exception as e:
        log("   submit click failed: %s" % type(e).__name__)
    pg.wait_for_timeout(6000)


def have_grid(pg):
    try:
        return bool(pg.locator("a[href*='Details.aspx']").count())
    except Exception:
        return False


def captcha_error(pg):
    try:
        return "solve the captcha" in pg.inner_text("body").lower()
    except Exception:
        return False


def wait_for_solve(pg, lic_type, minutes):
    log("")
    log("  " + "=" * 62)
    log("  SOLVE THE CAPTCHA AND CLICK SEARCH for permit type: %s" % lic_type)
    log("  (Profession/Status are already All; type is already selected.)")
    log("  Click Search IMMEDIATELY after ticking - v2 tokens expire ~2 min.")
    log("  " + "=" * 62)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if have_grid(pg):
            return True
        time.sleep(2)
    return False


def sort_union(pg, tag):
    """ASC walk + re-sort + DESC walk, unioned by GUID, with the overlap proof."""
    asc, asc_pages = walk(pg, tag + ":ASC")
    first_before = next(iter(asc.values()))["name"] if asc else None
    changed = False
    for attempt in (1, 2):
        if not click_label(pg, SORT_HEADER):
            break
        settle(pg)
        pg.wait_for_timeout(2000)
        head = dom_read(pg, ROWS_JS, default=[])
        after = head[0]["name"] if head else None
        if after and after != first_before:
            changed = True
            break
    if not changed:
        log("  [%s] re-sort did not change order - DESC half unavailable" % tag)
        return asc, len(asc), 0, None
    desc, _ = walk(pg, tag + ":DESC")
    union = dict(asc)
    union.update(desc)
    overlap = len(set(asc) & set(desc))
    return union, len(asc), len(desc), overlap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=30, help="minutes to wait per solve")
    a = ap.parse_args()

    per_type, proofs, records = {}, {}, {}
    session_validates_once = None
    solved_any = False          # has ANY search been captcha-validated yet?

    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = b.new_context(viewport=None)
        pg = ctx.new_page()
        pg.set_default_timeout(120000)

        for n, t in enumerate(SELECTABLE):
            log("")
            log("#" * 70)
            log("PERMIT TYPE %d/%d: %s" % (n + 1, len(SELECTABLE), t))
            run_search(pg, t)

            if have_grid(pg):
                if n == 0:
                    log("  grid present without a solve (unexpected on the first search)")
                else:
                    if session_validates_once is None:
                        session_validates_once = True
                        log("  SESSION TEST: second search succeeded with NO re-solve")
                        log("  -> MyLicense validates the captcha ONCE PER SESSION")
            else:
                # The session test is ONLY meaningful if the PREVIOUS search was actually
                # captcha-validated. If nobody solved the first one, "re-challenged" tells
                # us nothing - an unvalidated session was never going to work.
                if n > 0 and session_validates_once is None and solved_any:
                    session_validates_once = False
                    log("  SESSION TEST: second search was re-challenged (%s)"
                        % ("captcha error shown" if captcha_error(pg) else "no grid"))
                    log("  -> MyLicense validates PER SUBMIT; one solve per type")
                elif n > 0 and not solved_any:
                    log("  SESSION TEST: INVALID - no earlier search was ever solved")
                if not wait_for_solve(pg, t, a.wait):
                    log("  !! timed out waiting for a solve on %r" % t)
                    if not solved_any:
                        log("  !! nobody is at the browser - ABORTING rather than waiting")
                        log("     %d x %d min on the remaining types."
                            % (len(SELECTABLE) - n - 1, a.wait))
                        break
                    continue

            solved_any = True
            union, na, nd, ov = sort_union(pg, t)
            per_type[t] = len(union)
            proofs[t] = (na, nd, ov)
            capped = "" if ov else "  <-- NO OVERLAP: may exceed 6,400"
            log("  %s: ASC=%s DESC=%s UNION=%s overlap=%s%s"
                % (t, format(na, ","), format(nd, ","), format(len(union), ","),
                   ov if ov is not None else "n/a", capped))
            for g, r in union.items():
                records[g] = r
            json.dump(list(records.values()), open(OUT, "w", encoding="utf-8"), indent=1)
        b.close()

    # ---- fold in the hidden types from the preserved unfiltered halves
    log("")
    log("#" * 70)
    log("HIDDEN TYPES - not selectable in the dropdown, recovered from %s" % HALVES)
    try:
        h = json.load(open(HALVES, encoding="utf-8"))
        pool = {r["guid"]: r for r in h["asc"]}
        pool.update({r["guid"]: r for r in h["desc"]})
        log("  pool: %s distinct GUIDs from the unfiltered ASC/DESC halves"
            % format(len(pool), ","))
        for t in HIDDEN:
            rows = {g: r for g, r in pool.items() if r["license_type"] == t}
            added = sum(1 for g in rows if g not in records)
            records.update(rows)
            per_type[t] = len(rows)
            log("  %-34s %4d row(s) (%d new) - LOWER BOUND, not proven complete"
                % (t, len(rows), added))
    except FileNotFoundError:
        log("  !! %s missing - hidden types cannot be recovered" % HALVES)

    if not records:
        raise RuntimeError("REFUSING TO WRITE: zero rows")
    json.dump(list(records.values()), open(OUT, "w", encoding="utf-8"), indent=1)

    log("")
    log("=" * 70)
    log("TOTAL distinct GUIDs: %s" % format(len(records), ","))
    log("wrote %s" % OUT)
    log("")
    log("session captcha behaviour: %s"
        % ("VALIDATES ONCE PER SESSION" if session_validates_once
           else "RE-CHALLENGES PER SUBMIT" if session_validates_once is False
           else "not determined"))
    log("")
    log("per-type:")
    for t, c in sorted(per_type.items(), key=lambda kv: -kv[1]):
        pr = proofs.get(t)
        note = ""
        if pr:
            note = "  (ASC %s / DESC %s / overlap %s)" % (
                format(pr[0], ","), format(pr[1], ","), pr[2])
        log("    %-34s %6s%s" % (t, format(c, ","), note))
    log("    %-34s %6s" % ("TOTAL", format(len(records), ",")))
    types = Counter(r["license_type"] for r in records.values())
    log("")
    log("observed types in the final set: %d" % len(types))
    drift = set(types) - set(SELECTABLE) - set(HIDDEN)
    log("ENUM DRIFT (new, previously unseen types): %s" % (sorted(drift) or "none"))
    blank = [r for r in records.values() if not r["license_no"].strip()]
    log("blank licence numbers: %d (kept verbatim) statuses=%s"
        % (len(blank), dict(Counter(r["status"] for r in blank))))
    st = Counter(r["status"] for r in records.values())
    log("")
    log("per-status:")
    for k, v in st.most_common():
        log("    %-34s %6d" % (k or "(blank)", v))


if __name__ == "__main__":
    main()
