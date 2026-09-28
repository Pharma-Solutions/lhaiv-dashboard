#!/usr/bin/env python3
"""MD BOP - diagnose WHICH detail-fetch method survives past the first few records.

The in-session crawl fetched 4 details and then froze while the grid kept advancing,
so `page.request.get()` (a background HTTP call sharing the cookie) stops working almost
immediately. Hypothesis: the token is tied to server-side grid state that a background
call does not carry or that the grid's own paging invalidates. A real user CLICKS the
row link - a full navigation with a Referer and the current grid state.

Four methods, same grid, N records each, recording WHERE each one breaks rather than
just whether it worked once:

  1. request.get                 - the baseline that failed
  2. request.get + Referer       - is the missing Referer the whole story?
  3. page.goto(detail) + back    - a real navigation
  4. click the row link + back   - exactly what a human does

Between methods the grid is re-read, which mints fresh tokens, so one method's damage is
not charged to the next. One captcha solve, nothing written, no crawling.
"""
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from md_bop_details import parse, strip_tags
from md_bop_sortunion import ROWS_JS, URL, dom_read, settle

N = 12          # records probed per method
TYPE = "Distributor"
DETAIL = "https://mdbop.mylicense.com/verification/Details.aspx?result=%s"


def log(m):
    print("[diag] " + str(m), flush=True)


def looks_real(body):
    t = strip_tags(body)
    if len(t) < 400:
        return False, "short(%d)" % len(t)
    if re.search(r"(did not complete successfully|no record|not found|expired|invalid)",
                 t, re.I):
        return False, "error-page"
    return (True, "ok") if parse(body) else (False, "unparsed")


def grid_rows(pg):
    settle(pg)
    return dom_read(pg, ROWS_JS, default=[])


def ensure_grid(pg, tries=10):
    for _ in range(tries):
        if pg.locator("a[href*='Details.aspx']").count():
            return True
        pg.wait_for_timeout(1000)
    return False


def method_request(pg, rows, referer=None):
    out = []
    for r in rows:
        try:
            hdr = {"Referer": referer} if referer else None
            resp = pg.request.get(DETAIL % r["guid"],
                                  headers=hdr, timeout=40000) if hdr else \
                   pg.request.get(DETAIL % r["guid"], timeout=40000)
            ok, why = looks_real(resp.text())
        except Exception as e:
            ok, why = False, "exc:%s" % type(e).__name__
        out.append((ok, why))
        time.sleep(0.2)
    return out


def method_goto(pg, rows):
    out = []
    for r in rows:
        try:
            pg.goto(DETAIL % r["guid"], wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(600)
            ok, why = looks_real(pg.content())
        except Exception as e:
            ok, why = False, "exc:%s" % type(e).__name__
        out.append((ok, why))
        try:
            pg.go_back(wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(500)
        except Exception:
            pass
    return out


def method_click(pg, rows_count):
    out = []
    for i in range(rows_count):
        try:
            links = pg.locator("#datagrid_results a[href*='Details.aspx']")
            if links.count() <= i:
                out.append((False, "no-link"))
                continue
            links.nth(i).click()
            pg.wait_for_timeout(1200)
            ok, why = looks_real(pg.content())
        except Exception as e:
            ok, why = False, "exc:%s" % type(e).__name__
        out.append((ok, why))
        try:
            pg.go_back(wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(700)
            ensure_grid(pg)
        except Exception:
            pass
    return out


def summarise(name, results):
    ok = sum(1 for o, _ in results if o)
    first_fail = next((i for i, (o, _) in enumerate(results) if not o), None)
    whys = {}
    for o, w in results:
        if not o:
            whys[w] = whys.get(w, 0) + 1
    log("  %-28s %2d/%2d ok   first failure at #%s   %s"
        % (name, ok, len(results),
           "none" if first_fail is None else first_fail + 1, whys or ""))
    return ok


with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--start-maximized"])
    ctx = b.new_context(viewport=None)
    pg = ctx.new_page()
    pg.set_default_timeout(120000)
    pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(2500)
    for nm, val in (("t_web_lookup__profession_name", "All"),
                    ("t_web_lookup__license_type_name", TYPE),
                    ("t_web_lookup__license_status_name", "All")):
        try:
            pg.select_option("select[name='%s']" % nm, label=val)
        except Exception:
            pass
    log("=" * 66)
    log("  SOLVE THE CAPTCHA AND CLICK SEARCH (type = %s)" % TYPE)
    log("  Click Search IMMEDIATELY after ticking (~2 min token life).")
    log("=" * 66)
    deadline = time.time() + 20 * 60
    while time.time() < deadline and not pg.locator("a[href*='Details.aspx']").count():
        time.sleep(2)
    if not pg.locator("a[href*='Details.aspx']").count():
        log("!! timed out waiting for a solved grid")
        b.close()
        sys.exit(2)
    results_url = pg.url
    log("grid up at %s" % results_url[:90])
    log("")
    log("probing %d records per method" % N)
    scores = {}

    rows = grid_rows(pg)[:N]
    scores["1 request.get"] = summarise("1 request.get (baseline)",
                                        method_request(pg, rows))

    ensure_grid(pg)
    rows = grid_rows(pg)[:N]
    scores["2 request.get+Referer"] = summarise("2 request.get + Referer",
                                                method_request(pg, rows, results_url))

    ensure_grid(pg)
    rows = grid_rows(pg)[:N]
    scores["3 goto+back"] = summarise("3 page.goto + back", method_goto(pg, rows))

    ensure_grid(pg)
    scores["4 click+back"] = summarise("4 click link + back", method_click(pg, N))

    log("")
    log("=" * 66)
    best = max(scores, key=lambda k: scores[k])
    for k, v in scores.items():
        log("  %-26s %2d/%d" % (k, v, N))
    log("")
    if scores[best] >= N - 1:
        log("VERDICT: %r survives -> full crawl is viable with it" % best)
    elif scores[best] > 4:
        log("VERDICT: %r is best (%d/%d) but still degrades - partial coverage only"
            % (best, scores[best], N))
    else:
        log("VERDICT: NO method survives past a handful. Detail pages are not")
        log("         harvestable at scale; fall back to the grid-level roster")
        log("         or a records request.")
    b.close()
