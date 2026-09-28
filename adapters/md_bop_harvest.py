#!/usr/bin/env python3
"""MD Board of Pharmacy - PHASE A: harvest GUIDs from the live facility grid.

ACCESS MODEL (Mark, 2026-09-25)
  The reCAPTCHA v2 gates ONLY the search submit. MARK solves it, once, by hand in
  the window this script opens. This script NEVER touches the captcha - it does not
  tick it, solve it, forge a token, or read its response. It simply waits until the
  results grid exists, which only happens after a human has satisfied the gate.

  After that the session is open, so we drive the site's own pager in the real form
  rather than replaying pagination through a credentialed background fetch.

WHAT IT DOES
  1. Opens Search.aspx?facility=Y and sets Profession/Permit Type/Permit Status = All.
  2. Waits (up to --wait minutes) for YOU to solve the captcha and press Search.
  3. Walks the pager - __doPostBack('datagrid_results$_ctl44$_ctlN',''), 40 rows/page,
     windowed with a trailing '...' - to the TRUE end. Per the access note there is
     no 80-page cap here (unlike NJ), so we stop only when the forward '...' stops
     advancing the maximum page number.
  4. Records, per row: the Details.aspx GUID plus Name / License # / License Type /
     Status exactly as rendered. Blank License #s are kept verbatim.

COMPLETENESS
  Every page except the last must return 40 rows. A short page mid-walk is a symptom,
  not an outcome: it is retried, and if it stays short the run records a hole rather
  than quietly banking fewer rows. Progress is checkpointed so a broken session does
  not cost the whole walk.
"""
import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

URL = "https://mdbop.mylicense.com/Verification/Search.aspx?facility=Y"
OUT = "md_guids.json"
PAGE_SIZE = 40


def log(m):
    print("[mdA] " + str(m), flush=True)


def settle(pg, tries=15):
    """Wait for the grid after a postback.

    ASP.NET __doPostBack triggers a full navigation, so an evaluate() issued while
    it is in flight dies with "Execution context was destroyed". Every DOM read
    below goes through safe_eval, which settles and retries once. (Ported from
    nj_cds.py - the same race, which I should have carried over from the start.)
    """
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    for _ in range(tries):
        try:
            if pg.locator("#datagrid_results").count() or                pg.locator("a[href*='Details.aspx']").count():
                return True
        except Exception:
            pass
        pg.wait_for_timeout(1000)
    return False


def safe_eval(pg, fn, arg=None, default=None):
    for attempt in (0, 1):
        try:
            return pg.evaluate(fn, arg) if arg is not None else pg.evaluate(fn)
        except Exception:
            if attempt == 0:
                settle(pg)
                continue
            return default
    return default


def page_rows(pg):
    """Rows currently rendered: guid + the four grid columns, verbatim."""
    return safe_eval(pg, """() => {
      let g = document.querySelector('#datagrid_results');
      if (!g) {
        const a = document.querySelector("a[href*='Details.aspx']");
        if (a) { let e=a; while (e && e.tagName!=='TABLE') e=e.parentElement; g=e; }
      }
      if (!g) return [];
      const out = [];
      for (const r of [...g.rows].slice(1)) {
        const cells = [...r.cells].map(c => c.innerText.replace(/\\u00a0/g,' ').trim());
        if (cells.length < 4) continue;
        const a = r.querySelector("a[href*='Details.aspx']");
        if (!a) continue;
        const m = (a.getAttribute('href') || '').match(/result=([^&"']+)/i);
        if (!m) continue;
        out.push({guid: m[1], name: cells[0], license_no: cells[1],
                  license_type: cells[2], status: cells[3]});
      }
      return out;
    }""", default=[])


def pager_state(pg):
    return safe_eval(pg, """() => {
      const links = [...document.querySelectorAll('a')];
      const nums = links.map(a => a.innerText.trim()).filter(t => /^\\d+$/.test(t)).map(Number);
      return {max: nums.length ? Math.max(...nums) : null,
              min: nums.length ? Math.min(...nums) : null,
              hasEllipsis: links.some(a => a.innerText.trim() === '...')};
    }""", default={"max": None, "min": None, "hasEllipsis": False})


def click_page(pg, label):
    ok = safe_eval(pg, """(want) => {
      const t = [...document.querySelectorAll('a')].find(a => a.innerText.trim() === String(want));
      if (!t) return false; t.click(); return true;
    }""", str(label), default=False)
    if ok:
        settle(pg)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=15, help="minutes to wait for the human solve")
    ap.add_argument("--max-pages", type=int, default=1000)
    a = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = b.new_context(viewport=None)
        pg = ctx.new_page()
        pg.set_default_timeout(120000)
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(3000)

        # Pre-set the three dropdowns so Mark only has to solve + click Search.
        for name, val in (("t_web_lookup__profession_name", "All"),
                          ("t_web_lookup__license_type_name", "All"),
                          ("t_web_lookup__license_status_name", "All")):
            try:
                pg.select_option("select[name='%s']" % name, label=val)
            except Exception as e:
                log("could not preset %s (%s)" % (name, type(e).__name__))
        log("")
        log("=" * 68)
        log("  ACTION NEEDED IN THE BROWSER WINDOW THAT JUST OPENED:")
        log("    1. Solve the 'I'm not a robot' checkbox")
        log("    2. Click Search")
        log("  Profession / Permit Type / Permit Status are already set to All.")
        log("  This script will NOT touch the captcha. Waiting up to %d min..." % a.wait)
        log("=" * 68)

        # Detection must not assume (a) NJ's grid id or (b) that results land in the
        # SAME tab - MyLicense can open SearchResults in a new window. Scan every page
        # in the context and accept any of: a Details.aspx link, a SearchResults URL,
        # or a sizeable table. Whichever page matches becomes the one we drive.
        def find_results():
            for cand in list(ctx.pages):
                try:
                    if cand.is_closed():
                        continue
                    # SPECIFIC signals only. A "table with >5 rows" heuristic is
                    # useless here: the search FORM page satisfies it, so detection
                    # fired on Search.aspx before any search had run and harvested
                    # nothing. Require real result markup.
                    if cand.locator("a[href*='Details.aspx']").count():
                        return cand
                    if ("searchresults" in (cand.url or "").lower()
                            and cand.locator("table").count()):
                        return cand
                except Exception:
                    continue
            return None

        deadline = time.time() + a.wait * 60
        found = None
        while time.time() < deadline:
            found = find_results()
            if found:
                break
            time.sleep(2)
        if not found:
            log("!! timed out waiting for results - nothing harvested")
            b.close()
            sys.exit(2)
        pg = found
        try:
            pg.bring_to_front()
        except Exception:
            pass
        gid = pg.evaluate("""() => {
          const a=document.querySelector("a[href*='Details.aspx']");
          if(!a) return null;
          let e=a; while(e && e.tagName!=='TABLE') e=e.parentElement;
          return e ? (e.id || '(table without id)') : null;
        }""")
        log("results detected on %s" % (pg.url or "?")[:110])
        log("results table id: %r" % gid)
        # Never start a walk on an empty grid - that is how a false-positive
        # detection turns into a silent zero-row "success".
        for _ in range(15):
            if page_rows(pg):
                break
            time.sleep(2)
        else:
            log("!! bound page yields no rows - detection was wrong, not harvesting")
            b.close()
            sys.exit(3)
        rows, holes = {}, []
        page, last_max = 0, -1
        while page < a.max_pages:
            if page and page % 25 == 0:
                json.dump(list(rows.values()), open(OUT, "w", encoding="utf-8"), indent=1)
            settle(pg)
            batch = page_rows(pg)
            if len(batch) < PAGE_SIZE:
                # short page: retry once before believing it
                pg.wait_for_timeout(2500)
                retry = page_rows(pg)
                if len(retry) > len(batch):
                    batch = retry
            for r in batch:
                rows[r["guid"]] = r
            page += 1
            st = pager_state(pg)
            short = len(batch) < PAGE_SIZE
            if page % 10 == 0 or page <= 3:
                log("  page %-4d rows=%-3d distinct=%-6d pager[min=%s max=%s ell=%s]"
                    % (page, len(batch), len(rows), st["min"], st["max"], st["hasEllipsis"]))
            if short:
                # a short page is only acceptable as the FINAL page
                if click_page(pg, page + 1) or (st["hasEllipsis"] and click_page(pg, "...")):
                    holes.append((page, len(batch)))
                    log("  !! page %d returned %d (<%d) but is NOT last" % (page, len(batch), PAGE_SIZE))
                    pg.wait_for_timeout(2500)
                    continue
                log("  final page %d returned %d row(s)" % (page, len(batch)))
                break
            if click_page(pg, page + 1):
                pg.wait_for_timeout(2200)
                continue
            # window exhausted - advance it with '...'
            if st["hasEllipsis"] and click_page(pg, "..."):
                pg.wait_for_timeout(2600)
                new = pager_state(pg)
                if new["max"] is not None and last_max is not None and new["max"] <= last_max:
                    log("  '...' no longer advances (max stuck at %s) - end of results" % new["max"])
                    break
                last_max = new["max"]
                continue
            log("  no further pager control - end of results")
            break

        if not rows:
            log("!! REFUSING TO WRITE: zero rows harvested")
            b.close()
            sys.exit(4)
        json.dump(list(rows.values()), open(OUT, "w", encoding="utf-8"), indent=1)
        log("")
        log("pages walked : %d" % page)
        log("distinct GUIDs: %s" % format(len(rows), ","))
        expected_min = (page - 1) * PAGE_SIZE
        log("expected >= %s (pages-1 x 40)" % format(expected_min, ","))
        if len(rows) < expected_min:
            log("!! FEWER rows than pages imply - investigate before trusting")
        if holes:
            log("!! %d short page(s) mid-walk: %s" % (len(holes), holes[:8]))
        # CAP SIGNATURE. The old check (total % 1000) was wrong and did not fire on
        # 3,200. A server cap looks like: total == pages * page_size AND the final
        # page was FULL. A genuine end-of-results almost always has a partial last
        # page, so a full final page plus an exact multiple is the tell.
        if len(rows) == page * PAGE_SIZE:
            log("!! CAP SIGNATURE: %d == %d pages x %d, final page FULL - this is a"
                % (len(rows), page, PAGE_SIZE))
            log("   server cap, NOT end-of-results. Do not treat as complete.")
        log("wrote %s" % OUT)
        # Details.aspx is a public ungated GET, so Phase B needs no session and the
        # browser can close here. No blocking prompt: this may run unattended.
        b.close()


if __name__ == "__main__":
    main()
