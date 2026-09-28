#!/usr/bin/env python3
"""MD BOP - full harvest WITH detail pages, fetched in-session while tokens are live.

WHY THIS SHAPE
  Details.aspx?result=<guid> is a per-request, session-scoped token. A sample of 40
  preserved GUIDs came back 100% dead ("Verification Search Error"), so details CANNOT
  be fetched after the fact. They must be pulled during the same browser session that
  produced them, through page.request so the session cookies travel with the call, and
  as soon as possible after the page that minted them.

  So: fetch each page's ~40 details immediately after reading that page, before paging
  on. Token age at fetch time is then seconds, not minutes.

  A record already held (by NATURAL KEY, not GUID) is never re-fetched, so the ~13.4k
  raw grid rows cost only ~9.9k detail fetches.

KEY
  State(MD) + License# + type; blank licence numbers fall back to (name, type).
  GUIDs are transient fetch handles and are never used for identity.

BEFORE SPENDING SOLVES
  After the first grid appears, ONE detail fetch is probed. If it does not return a real
  record the run stops immediately and reports - rather than burning five captcha solves
  to discover the same dead end.

CHECKPOINTING
  Progress is written after every page, so a lost session costs the remaining types, not
  the whole run.
"""
import argparse
import collections
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from md_bop_details import FIELD_MAP, parse, strip_tags
from md_bop_sortunion import ROWS_JS, URL, click_label, dom_read, settle, walk

SELECTABLE = ["Distributor", "Pharmacy", "Pharmacy Waiver", "Corporation",
              "Drug Therapy Management"]
SORT_HEADER = "License #"
DETAIL = "https://mdbop.mylicense.com/verification/Details.aspx?result=%s"
CKPT = "md_full_checkpoint.json"
OUT = "md_records_detailed.json"
PAGE_SIZE = 40


def log(m):
    print("[mdF] " + str(m), flush=True)


def nkey(r):
    lic = (r.get("license_no") or "").strip()
    typ = (r.get("license_type") or "").strip()
    if lic:
        return "MD|%s|%s" % (lic, typ)
    return "MD||%s|%s" % ((r.get("name") or "").strip().upper(), typ)


def fetch_detail(pg, guid, tries=2):
    """One detail page, through the browser session so cookies travel with it."""
    for attempt in range(tries):
        try:
            resp = pg.request.get(DETAIL % guid, timeout=45000)
            body = resp.text()
            txt = strip_tags(body)
            if len(txt) < 400 or re.search(r"(did not complete successfully|no record|"
                                           r"not found|expired|invalid)", txt, re.I):
                return None, "dead"
            d = parse(body)
            return (d, "ok") if d else (None, "unparsed")
        except Exception as e:
            if attempt + 1 == tries:
                return None, "err:%s" % type(e).__name__
            time.sleep(1.0)
    return None, "err"


def harvest_page(pg, store, details, stats, delay):
    """Read the current grid page, then immediately fetch details for its new records."""
    rows = dom_read(pg, ROWS_JS, default=[])
    fresh = 0
    for r in rows:
        k = nkey(r)
        if k not in store:
            store[k] = r
        if k in details:
            continue
        d, why = fetch_detail(pg, r["guid"])
        stats[why] += 1
        if d:
            details[k] = d
            fresh += 1
        time.sleep(delay)
    return len(rows), fresh


def walk_with_details(pg, tag, store, details, stats, delay, max_pages=85):
    page, last_max, batch = 0, -1, []
    while page < max_pages:
        settle(pg)
        n, fresh = harvest_page(pg, store, details, stats, delay)
        batch = [1] * n
        page += 1
        st = dom_read(pg, """() => {
          const l=[...document.querySelectorAll('a')];
          const nums=l.map(a=>a.innerText.trim()).filter(t=>/^\\d+$/.test(t)).map(Number);
          return {max:nums.length?Math.max(...nums):null,
                  hasEllipsis:l.some(a=>a.innerText.trim()==='...')};}""",
                      default={"max": None, "hasEllipsis": False})
        if page % 10 == 0 or page <= 2:
            log("    [%s] page %-3d rows=%-3d records=%-6d details=%-6d"
                % (tag, page, n, len(store), len(details)))
        json.dump({"store": store, "details": details}, open(CKPT, "w", encoding="utf-8"))
        if n < PAGE_SIZE and not click_label(pg, page + 1):
            break
        if click_label(pg, page + 1):
            continue
        if st.get("hasEllipsis") and click_label(pg, "..."):
            new = dom_read(pg, """() => {const l=[...document.querySelectorAll('a')];
              const n=l.map(a=>a.innerText.trim()).filter(t=>/^\\d+$/.test(t)).map(Number);
              return n.length?Math.max(...n):null;}""", default=None)
            if new is not None and new <= last_max:
                break
            last_max = new
            continue
        break
    return page


def run_search(pg, lic_type):
    pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(2500)
    for nm, val in (("t_web_lookup__profession_name", "All"),
                    ("t_web_lookup__license_type_name", lic_type),
                    ("t_web_lookup__license_status_name", "All")):
        try:
            pg.select_option("select[name='%s']" % nm, label=val)
        except Exception:
            pass
    pg.wait_for_timeout(600)
    try:
        pg.locator("input[name='sch_button']").first.click()
    except Exception:
        pass
    pg.wait_for_timeout(6000)


def have_grid(pg):
    try:
        return bool(pg.locator("a[href*='Details.aspx']").count())
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=20)
    ap.add_argument("--delay", type=float, default=0.12)
    a = ap.parse_args()

    store, details = {}, {}
    if os.path.exists(CKPT):
        try:
            ck = json.load(open(CKPT, encoding="utf-8"))
            store, details = ck.get("store", {}), ck.get("details", {})
            log("resumed checkpoint: %s records, %s details"
                % (format(len(store), ","), format(len(details), ",")))
        except Exception:
            pass
    stats = collections.Counter()
    solved_any = False

    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = b.new_context(viewport=None)
        pg = ctx.new_page()
        pg.set_default_timeout(120000)

        for n, t in enumerate(SELECTABLE):
            log("")
            log("#" * 72)
            log("PERMIT TYPE %d/%d: %s" % (n + 1, len(SELECTABLE), t))
            run_search(pg, t)
            if not have_grid(pg):
                log("")
                log("  " + "=" * 64)
                log("  SOLVE THE CAPTCHA AND CLICK SEARCH for: %s" % t)
                log("  Click Search IMMEDIATELY after ticking (~2 min token life).")
                log("  " + "=" * 64)
                deadline = time.time() + a.wait * 60
                while time.time() < deadline and not have_grid(pg):
                    time.sleep(2)
                if not have_grid(pg):
                    log("  !! timed out on %r" % t)
                    if not solved_any:
                        log("  !! nobody at the browser - ABORTING rather than waiting on"
                            " the remaining %d type(s)." % (len(SELECTABLE) - n - 1))
                        break
                    continue
            solved_any = True

            # --- PROBE one detail before committing to the crawl
            if not details:
                first = dom_read(pg, ROWS_JS, default=[])
                if first:
                    d, why = fetch_detail(pg, first[0]["guid"])
                    log("  detail probe: %s" % why)
                    if not d:
                        log("  !! in-session detail fetch FAILED (%s)." % why)
                        log("     Stopping before spending further solves.")
                        b.close()
                        raise SystemExit(4)
                    log("     sample fields: %s" % json.dumps(d)[:200])

            pages = walk_with_details(pg, t + ":ASC", store, details, stats, a.delay)
            log("  %s ASC: %d page(s); records=%s details=%s"
                % (t, pages, format(len(store), ","), format(len(details), ",")))

            before = next(iter(dom_read(pg, ROWS_JS, default=[{}])), {}).get("name")
            changed = False
            for _ in (1, 2):
                if not click_label(pg, SORT_HEADER):
                    break
                settle(pg)
                pg.wait_for_timeout(1800)
                head = dom_read(pg, ROWS_JS, default=[])
                if head and head[0]["name"] != before:
                    changed = True
                    break
            if changed:
                pages = walk_with_details(pg, t + ":DESC", store, details, stats, a.delay)
                log("  %s DESC: %d page(s); records=%s details=%s"
                    % (t, pages, format(len(store), ","), format(len(details), ",")))
            else:
                log("  %s: re-sort did not change order - ASC only" % t)
        b.close()

    if not store:
        raise RuntimeError("REFUSING TO WRITE: zero records")

    merged = []
    for k, r in store.items():
        d = details.get(k, {})
        row = {"license_number": r["license_no"], "license_type": r["license_type"],
               "status": r["status"], "grid_name": r["name"], "jurisdiction": "MD",
               "__nkey": k,
               "__source": ("MD Board of Pharmacy facility verification - "
                            "mdbop.mylicense.com/Verification (facility=Y)"),
               "__retrieved": time.strftime("%Y-%m-%d")}
        for _, col in FIELD_MAP:
            row[col] = d.get(col, "")
        merged.append(row)
    json.dump(merged, open(OUT, "w", encoding="utf-8"), indent=1)

    withaddr = sum(1 for r in merged if r.get("address_city", "").strip())
    log("")
    log("=" * 72)
    log("records (natural key): %s" % format(len(merged), ","))
    log("with detail/address  : %s (%.1f%%)"
        % (format(withaddr, ","), 100.0 * withaddr / max(len(merged), 1)))
    log("detail fetch outcomes: %s" % dict(stats))
    log("wrote %s" % OUT)
    log("")
    for k, v in collections.Counter(r["license_type"] for r in merged).most_common():
        log("    %-34s %6s" % (k or "(blank)", format(v, ",")))


if __name__ == "__main__":
    main()
