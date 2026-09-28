#!/usr/bin/env python3
"""ENG-344 recon - PA PALS (Pennsylvania Licensing System) public licensee search.

QUESTIONS THIS PASS MUST ANSWER (report before pulling anything)
  1. What is the mechanism? SPA over a JSON API, or server-rendered pages?
  2. Is it enumerable - browse-all / paged - or single-record lookup only?
  3. Is there a profession / license-type picklist, and what is in it?
  4. Which classes are ESTABLISHMENTS vs INDIVIDUALS?
  5. Any CAPTCHA / login / fee gate? -> if yes: STOP and flag, never work around.

SEPARATION CHECK (specific to this ticket)
  The held `PA - pa-ddc` file has a PALS-shaped schema (practitionerName /
  profession / licenseNumber / licenseStatus / address) and contains only
  profession = "Device" (2,645) and "Wholesaler/Distributor" (155). If those
  professions appear in the PALS picklist, DDC is a SLICE of PALS, and ENG-344
  must take the Board-of-Pharmacy professions instead so the two never overlap.

GUARDRAIL: read-only. No login, no fee, no CAPTCHA touched.
"""
import json
import re
import time
import urllib.parse

from playwright.sync_api import sync_playwright

URL = "https://www.pals.pa.gov/#/page/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
CAPSEL = ("iframe[src*='recaptcha'], .g-recaptcha, iframe[src*='hcaptcha'], .cf-turnstile, "
          "#captcha, [id*='captcha' i], [name*='captcha' i]")

net = []
T0 = time.time()
INTERESTING = re.compile(r"(api|search|lookup|licen|profession|board|json|odata)", re.I)


def cap_report(pg, where):
    n = pg.locator(CAPSEL).count()
    html = pg.content()
    words = sorted(set(re.findall(r"recaptcha|hcaptcha|turnstile|captcha", html, re.I)))
    try:
        body = pg.inner_text("body")
    except Exception:
        body = ""
    gate = re.search(r"(please log ?in|sign in to continue|payment required|purchase|"
                     r"\$\s?\d[\d,]*)", body, re.I)
    print("  [gate @ %s] captcha-elements=%d source-words=%s fee/login-text=%s"
          % (where, n, words, gate.group(0)[:40] if gate else "none"))
    return n > 0


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, accept_downloads=True,
                        viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)

    ctx.on("request", lambda r: net.append(
        {"t": round(time.time() - T0, 1), "m": r.method, "url": r.url[:220],
         "post": (r.post_data or "")[:800]}) if INTERESTING.search(r.url) else None)
    resps = []
    ctx.on("response", lambda r: resps.append(r)
           if INTERESTING.search(r.url) and "text/html" not in (r.headers or {}).get(
               "content-type", "") else None)
    pg.on("download", lambda d: net.append({"DOWNLOAD": d.suggested_filename}))

    print("[pals] loading %s" % URL)
    try:
        pg.goto(URL, wait_until="networkidle", timeout=90000)
    except Exception as e:
        print("[pals] networkidle timeout (%s); falling back" % type(e).__name__)
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(9000)

    print("[pals] url   : %s" % pg.url)
    print("[pals] title : %r" % pg.title())
    print("\n=== 5. gates ===")
    cap_report(pg, "search page load")

    try:
        txt = re.sub(r"\s+", " ", pg.inner_text("body"))
    except Exception:
        txt = ""
    print("\n=== visible copy (first 900) ===\n%s" % txt[:900])

    print("\n=== 3. picklists ===")
    sl = pg.locator("select")
    print("  native <select> count: %d" % sl.count())
    dump = {}
    for i in range(sl.count()):
        e = sl.nth(i)
        try:
            o = e.locator("option")
            opts = [(o.nth(j).get_attribute("value"), (o.nth(j).inner_text() or "").strip())
                    for j in range(o.count())]
            nm = (e.get_attribute("name") or e.get_attribute("id")
                  or e.get_attribute("formcontrolname") or "select#%d" % i)
            dump[nm] = opts
            print("  %r visible=%s n=%d" % (nm, e.is_visible(), len(opts)))
            for v, t in opts[:60]:
                print("     %-34r %r" % (str(v)[:32], t[:56]))
            if len(opts) > 60:
                print("     ... +%d more" % (len(opts) - 60))
        except Exception as ex:
            print("  select %d: %s" % (i, type(ex).__name__))

    for sel in ("mat-select", "[role=combobox]", "[role=listbox]", "ng-select", ".dropdown"):
        c = pg.locator(sel).count()
        if c:
            print("  %s: %d" % (sel, c))

    print("\n=== inputs / buttons ===")
    ip = pg.locator("input")
    for i in range(min(ip.count(), 30)):
        e = ip.nth(i)
        try:
            if not e.is_visible():
                continue
            print("  input type=%s name=%r placeholder=%r"
                  % (e.get_attribute("type"), e.get_attribute("name"),
                     e.get_attribute("placeholder")))
        except Exception:
            pass
    for sel in ("button", "a[role=button]"):
        l = pg.locator(sel)
        for i in range(min(l.count(), 30)):
            e = l.nth(i)
            try:
                if not e.is_visible():
                    continue
                t = (e.inner_text() or "").strip()
                if t and len(t) < 46:
                    print("  [%s] %r" % (sel, t))
            except Exception:
                pass

    print("\n=== 1/2. API traffic seen on load ===")
    seen = set()
    for n in net:
        u = n.get("url", "")
        key = u.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        print("  %s %s" % (n.get("m", ""), u[:170]))
        if n.get("post"):
            print("     POST %s" % urllib.parse.unquote_plus(n["post"])[:400])

    print("\n=== API response bodies ===")
    bodies = []
    for r in resps[:25]:
        try:
            t = r.text()
        except Exception as e:
            t = "<unreadable: %s>" % type(e).__name__
        bodies.append({"url": r.url[:200], "status": r.status, "body": t[:4000]})
        print("  %s %s  len=%d" % (r.status, r.url[:110], len(t)))
        print("     %s" % t[:500].replace("\n", " "))

    json.dump({"selects": dump, "net": net, "bodies": bodies},
              open("pals_recon.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="pals_recon.png", full_page=True)
    print("\n[pals] -> pals_recon.json / pals_recon.png  (NOTHING submitted)")
    b.close()
