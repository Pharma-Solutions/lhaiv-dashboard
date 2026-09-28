#!/usr/bin/env python3
"""Montana eBiz PUBLICPORTAL recon — pin down the per-type CSV export mechanism.

Answers spec section 11, in priority order:
  1. HOW is a per-type CSV export invoked (method / URL / params)?
  2. Does the CAPTCHA gate the EXPORT, or only the interactive search?
     -> decides fully-scheduled vs human-in-the-loop.
  3. Does the export need a cookie/session the search page sets?
  4. Exact license-type option labels/values.
  5. Is there an "all types" export?

GUARDRAIL: read-only. We LOOK at the CAPTCHA, we never solve or bypass it.
"""
import json
import re
import time

from playwright.sync_api import sync_playwright

URL = "https://ebizws.mt.gov/PUBLICPORTAL/searchform?mylist=licenses"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

net = []
T0 = time.time()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, accept_downloads=True,
                        viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)

    ctx.on("request", lambda r: net.append(
        {"t": round(time.time() - T0, 1), "m": r.method, "url": r.url[:200],
         "post": (r.post_data or "")[:400]}))
    ctx.on("response", lambda r: net.append(
        {"t": round(time.time() - T0, 1), "resp": r.status, "url": r.url[:200],
         "cd": (r.headers or {}).get("content-disposition", "")[:80],
         "ct": (r.headers or {}).get("content-type", "")[:40]}))
    pg.on("download", lambda d: net.append({"DOWNLOAD": d.suggested_filename,
                                            "url": (d.url or "")[:200]}))

    print(f"[mt] loading {URL}")
    pg.goto(URL, wait_until="networkidle", timeout=90000)
    pg.wait_for_timeout(5000)
    print(f"[mt] url   : {pg.url}")
    print(f"[mt] title : {pg.title()!r}")
    html = pg.content()
    print(f"[mt] rendered len={len(html)}")

    print("\n=== 2. CAPTCHA: present on the SEARCH page at load? ===")
    capsel = ("img[src*='captcha'], img[alt*='captcha' i], .captcha, #captcha, "
              "[id*='captcha' i], [name*='captcha' i], iframe[src*='recaptcha']")
    loc = pg.locator(capsel)
    print(f"  captcha-ish elements: {loc.count()}")
    for i in range(min(loc.count(), 6)):
        e = loc.nth(i)
        try:
            print(f"    #{i} tag={e.evaluate('x=>x.tagName')} visible={e.is_visible()} "
                  f"id={e.get_attribute('id')!r} name={e.get_attribute('name')!r} "
                  f"src={(e.get_attribute('src') or '')[:70]!r}")
        except Exception:
            pass
    for m in set(re.findall(r"[\w/.?=&%-]{0,30}captcha[\w/.?=&%-]{0,30}", html, re.I)):
        print(f"    source mentions: {m[:80]}")
    txt = re.sub(r"\s+", " ", pg.inner_text("body"))
    for m in re.finditer(r".{0,80}(captcha|playing card|select the card|verify).{0,80}", txt, re.I):
        print(f"    text: ...{m.group(0)[:150]}...")

    print("\n=== 1 + 5. export controls / license-type list on this page ===")
    for sel in ("a", "button", "input[type=submit]", "input[type=button]"):
        l = pg.locator(sel)
        for i in range(min(l.count(), 120)):
            e = l.nth(i)
            try:
                if not e.is_visible():
                    continue
                t = ((e.inner_text() or "") + " " + (e.get_attribute("value") or "")).strip()
                href = e.get_attribute("href") or ""
                onclick = e.get_attribute("onclick") or ""
                if re.search(r"export|download|csv|excel|list", t + href + onclick, re.I):
                    print(f"  [{sel}] {t[:52]!r}\n        href={href[:110]!r}\n        onclick={onclick[:110]!r}")
            except Exception:
                pass

    print("\n=== 4. selects / license-type axis ===")
    sl = pg.locator("select")
    dump = {}
    for i in range(sl.count()):
        e = sl.nth(i)
        try:
            o = e.locator("option")
            opts = [{"v": o.nth(j).get_attribute("value"),
                     "t": (o.nth(j).inner_text() or "").strip()} for j in range(o.count())]
            nm = e.get_attribute("name") or e.get_attribute("id") or f"select#{i}"
            dump[nm] = opts
            print(f"  {nm!r} visible={e.is_visible()} n={len(opts)}")
            for x in opts[:30]:
                print(f"     {str(x['v'])[:30]!r:<32} {x['t'][:52]!r}")
            if len(opts) > 30:
                print(f"     ... +{len(opts)-30} more")
        except Exception as ex:
            print(f"  select {i}: {type(ex).__name__}")

    print("\n=== 3. cookies the portal set ===")
    for c in ctx.cookies():
        print(f"  {c['name']} = {str(c['value'])[:34]}...  domain={c['domain']} path={c['path']}")

    print("\n=== all links on the page (looking for the export index) ===")
    seen = set()
    la = pg.locator("a[href]")
    for i in range(min(la.count(), 200)):
        try:
            e = la.nth(i)
            h = e.get_attribute("href") or ""
            t = (e.inner_text() or "").strip()
            if h and (h, t) not in seen:
                seen.add((h, t))
                if len(seen) <= 60:
                    print(f"  {t[:40]!r:<42} -> {h[:110]}")
        except Exception:
            pass

    json.dump({"selects": dump, "net": net},
              open("mt_recon.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="mt_recon.png", full_page=True)
    print("\n[mt] -> mt_recon.png / mt_recon.json   (NOTHING submitted, NO captcha touched)")
    b.close()
