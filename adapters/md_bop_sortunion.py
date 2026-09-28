#!/usr/bin/env python3
"""MD Board of Pharmacy - sort-union harvest to clear the 80-page / 3,200-row cap.

THE CAP (measured, 2026-09-25)
  A facility search for All/All/All yields exactly 3,200 distinct GUIDs over 80
  pages, the final page FULL, and the pager window then oscillates between 1-39 and
  41-80 without ever advancing. total == pages * page_size with a full last page is
  the cap signature; a genuine end-of-results has a partial last page.

THE TECHNIQUE
  The reCAPTCHA gates only the SEARCH SUBMIT. Re-sorting the results grid is a
  __doPostBack on the existing result set, NOT a new search, so it costs no solve.
  Walk the first 3,200 ascending, re-sort descending, walk the first 3,200 of THAT,
  and union: up to 6,400 distinct rows from one solve.

THE PROOF
  Completeness is not assumed from the counts. If the ascending and descending
  halves SHARE GUIDs, the two windows met in the middle and the union covers the
  whole set. If they are disjoint, the true total exceeds 6,400 and there is an
  unseen band between them - we STOP and keep both halves rather than ship a file
  with a hole we cannot size.

  This script never touches the captcha. It waits for a human-solved results grid.
  (All DOM reads below use Playwright's page.evaluate - browser-side DOM access,
  not Python eval.)
"""
import argparse
import json
import sys
import time
from collections import Counter

from playwright.sync_api import sync_playwright

URL = "https://mdbop.mylicense.com/Verification/Search.aspx?facility=Y"
PAGE_SIZE = 40
MAX_PAGES = 85
ASC_FILE = "md_guids.json"          # preserved: the earlier ascending half
OUT = "md_guids_union.json"


def log(m):
    print("[mdU] " + str(m), flush=True)


def settle(pg, tries=15):
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    for _ in range(tries):
        try:
            if pg.locator("a[href*='Details.aspx']").count():
                return True
        except Exception:
            pass
        pg.wait_for_timeout(1000)
    return False


def dom_read(pg, fn, arg=None, default=None):
    """One Playwright DOM read, settling and retrying once across a postback."""
    for attempt in (0, 1):
        try:
            return pg.evaluate(fn, arg) if arg is not None else pg.evaluate(fn)
        except Exception:
            if attempt == 0:
                settle(pg)
                continue
            return default
    return default


ROWS_JS = """() => {
  let g = document.querySelector('#datagrid_results');
  if (!g) { const a=document.querySelector("a[href*='Details.aspx']");
            if(a){let e=a; while(e&&e.tagName!=='TABLE')e=e.parentElement; g=e;} }
  if (!g) return [];
  const out=[];
  for (const r of [...g.rows].slice(1)) {
    const c=[...r.cells].map(x=>x.innerText.replace(/\\u00a0/g,' ').trim());
    if (c.length < 4) continue;
    const a=r.querySelector("a[href*='Details.aspx']");
    if (!a) continue;
    const m=(a.getAttribute('href')||'').match(/result=([^&"']+)/i);
    if (!m) continue;
    out.push({guid:m[1], name:c[0], license_no:c[1], license_type:c[2], status:c[3]});
  }
  return out;
}"""

PAGER_JS = """() => {
  const links=[...document.querySelectorAll('a')];
  const nums=links.map(a=>a.innerText.trim()).filter(t=>/^\\d+$/.test(t)).map(Number);
  return {max:nums.length?Math.max(...nums):null,
          min:nums.length?Math.min(...nums):null,
          hasEllipsis:links.some(a=>a.innerText.trim()==='...')};
}"""

HEADERS_JS = """() => {
  let g=document.querySelector('#datagrid_results');
  if(!g){const a=document.querySelector("a[href*='Details.aspx']");
         if(a){let e=a;while(e&&e.tagName!=='TABLE')e=e.parentElement;g=e;}}
  if(!g||!g.rows.length) return [];
  return [...g.rows[0].cells].map((c,i)=>{
    const a=c.querySelector('a');
    return {idx:i, text:c.innerText.trim(),
            sortable:!!a, href:a?(a.getAttribute('href')||'').slice(0,80):''};
  });
}"""

CLICK_JS = """(want)=>{
  const t=[...document.querySelectorAll('a')].find(a=>a.innerText.trim()===String(want));
  if(!t) return false; t.click(); return true;}"""


def click_label(pg, label):
    ok = dom_read(pg, CLICK_JS, str(label), default=False)
    if ok:
        settle(pg)
    return ok


def walk(pg, tag):
    """Walk the pager from the current page-1 state; return ({guid: row}, pages)."""
    rows, page, last_max, batch = {}, 0, -1, []
    while page < MAX_PAGES:
        settle(pg)
        batch = dom_read(pg, ROWS_JS, default=[])
        if len(batch) < PAGE_SIZE:
            pg.wait_for_timeout(2500)
            retry = dom_read(pg, ROWS_JS, default=[])
            if len(retry) > len(batch):
                batch = retry
        for r in batch:
            rows[r["guid"]] = r
        page += 1
        st = dom_read(pg, PAGER_JS, default={"max": None, "hasEllipsis": False})
        if page % 20 == 0 or page <= 2:
            log("  [%s] page %-3d rows=%-3d distinct=%-6d pager[max=%s]"
                % (tag, page, len(batch), len(rows), st["max"]))
        if len(batch) < PAGE_SIZE and not click_label(pg, page + 1):
            log("  [%s] final page %d returned %d row(s)" % (tag, page, len(batch)))
            break
        if click_label(pg, page + 1):
            continue
        if st.get("hasEllipsis") and click_label(pg, "..."):
            new = dom_read(pg, PAGER_JS, default={"max": None})
            if new["max"] is not None and new["max"] <= last_max:
                log("  [%s] '...' stopped advancing (max=%s) - window exhausted"
                    % (tag, new["max"]))
                break
            last_max = new["max"]
            continue
        log("  [%s] no further pager control" % tag)
        break
    if len(rows) == page * PAGE_SIZE and len(batch) == PAGE_SIZE:
        log("  [%s] !! CAP SIGNATURE: %d == %d x %d with a FULL final page"
            % (tag, len(rows), page, PAGE_SIZE))
    return rows, page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=25)
    a = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = b.new_context(viewport=None)
        pg = ctx.new_page()
        pg.set_default_timeout(120000)
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(3000)
        for nm in ("t_web_lookup__profession_name", "t_web_lookup__license_type_name",
                   "t_web_lookup__license_status_name"):
            try:
                pg.select_option("select[name='%s']" % nm, label="All")
            except Exception:
                pass
        log("=" * 68)
        log("  SOLVE THE CAPTCHA AND CLICK SEARCH IN THE OPEN WINDOW.")
        log("  Dropdowns are preset to All. This script never touches the captcha.")
        log("  Tip: click Search IMMEDIATELY after ticking - v2 tokens expire ~2 min.")
        log("=" * 68)

        deadline = time.time() + a.wait * 60
        results = None
        while time.time() < deadline:
            for cand in list(ctx.pages):
                try:
                    if cand.is_closed():
                        continue
                    if cand.locator("a[href*='Details.aspx']").count():
                        results = cand
                        break
                except Exception:
                    continue
            if results:
                break
            time.sleep(2)
        if not results:
            log("!! timed out waiting for a human-solved results grid")
            b.close()
            sys.exit(2)
        pg = results
        log("results grid detected on %s" % (pg.url or "")[:100])

        # ---- STEP 1: confirm a header is genuinely sortable (a re-sort postback,
        #      NOT a new search, which would re-trigger the captcha)
        hdrs = dom_read(pg, HEADERS_JS, default=[])
        log("column headers:")
        for h in hdrs:
            log("   [%d] %-16r sortable=%s %s"
                % (h["idx"], h["text"], h["sortable"], h["href"][:60]))
        sortable = [h for h in hdrs if h["sortable"] and "__doPostBack" in h["href"]]
        if not sortable:
            log("!! NO sortable header found - sort-union is impossible.")
            log("   STOPPING. Fall back to per-Permit-Type partitioning.")
            json.dump({"headers": hdrs}, open("md_headers.json", "w"), indent=1)
            b.close()
            sys.exit(3)
        target = sortable[0]
        log("using header [%d] %r for the re-sort" % (target["idx"], target["text"]))

        # ---- STEP 2: ascending walk
        asc, asc_pages = walk(pg, "ASC")
        log("ASC half: %s distinct over %d pages" % (format(len(asc), ","), asc_pages))

        # ---- STEP 3: re-sort, confirm the order really changed, walk again
        first_before = next(iter(asc.values()))["name"] if asc else None
        changed = False
        for attempt in (1, 2):
            if not click_label(pg, target["text"]):
                break
            settle(pg)
            pg.wait_for_timeout(2000)
            head = dom_read(pg, ROWS_JS, default=[])
            first_after = head[0]["name"] if head else None
            log("  re-sort click %d -> first row %r (was %r)"
                % (attempt, first_after, first_before))
            if first_after and first_after != first_before:
                changed = True
                break
        if not changed:
            log("!! re-sort did not change the ordering - cannot build a second half.")
            log("   STOPPING. Fall back to per-Permit-Type partitioning.")
            b.close()
            sys.exit(4)
        desc, desc_pages = walk(pg, "DESC")
        log("DESC half: %s distinct over %d pages" % (format(len(desc), ","), desc_pages))
        b.close()

    # ---- STEP 4: union + overlap proof
    union = dict(asc)
    union.update(desc)
    overlap = set(asc) & set(desc)
    log("")
    log("=" * 68)
    log("ASC   : %s" % format(len(asc), ","))
    log("DESC  : %s" % format(len(desc), ","))
    log("UNION : %s" % format(len(union), ","))
    log("OVERLAP (shared GUIDs): %s" % format(len(overlap), ","))
    if overlap:
        log("PROOF: the halves MEET in the middle -> union is COMPLETE coverage.")
    else:
        log("!! NO OVERLAP - the true total exceeds %d and an unseen band sits"
            % (len(asc) + len(desc)))
        log("   between the halves. STOPPING; keeping both halves.")
        json.dump({"asc": list(asc.values()), "desc": list(desc.values())},
                  open("md_halves.json", "w", encoding="utf-8"), indent=1)
        sys.exit(5)
    log("=" * 68)

    if not union:
        raise RuntimeError("REFUSING TO WRITE: zero rows")

    # cross-check against the preserved earlier ascending half
    try:
        prev = {r["guid"] for r in json.load(open(ASC_FILE, encoding="utf-8"))}
        log("earlier %s held %s GUIDs; %s of them are in this union"
            % (ASC_FILE, format(len(prev), ","), format(len(prev & set(union)), ",")))
        missing = prev - set(union)
        if missing:
            log("!! %d GUID(s) from the earlier half are ABSENT from the union"
                % len(missing))
    except FileNotFoundError:
        pass

    json.dump(list(union.values()), open(OUT, "w", encoding="utf-8"), indent=1)
    log("wrote %s: %s GUIDs" % (OUT, format(len(union), ",")))

    KNOWN = {"Pharmacy", "Distributor", "Pharmacy Waiver", "Prescription Drug Drop-Off",
             "Prescription Drug Repository", "Corporation", "Drug Therapy Management"}
    types = Counter(r["license_type"] for r in union.values())
    status = Counter(r["status"] for r in union.values())
    log("")
    log("per-type:")
    for k, v in types.most_common():
        log("    %-34s %6d" % (k or "(blank)", v))
    drift = set(t for t in types if t) - KNOWN
    log("ENUM DRIFT vs known 7: %s" % (sorted(drift) or "none"))
    log("per-status:")
    for k, v in status.most_common():
        log("    %-34s %6d" % (k or "(blank)", v))
    blank = [r for r in union.values() if not r["license_no"].strip()]
    log("blank licence numbers: %d (kept verbatim) statuses=%s"
        % (len(blank), dict(Counter(r["status"] for r in blank))))


if __name__ == "__main__":
    main()
