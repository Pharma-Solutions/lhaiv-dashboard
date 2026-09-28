#!/usr/bin/env python3
"""ENG-344 recon pass 2 - drive the real PALS search and capture the API contract.

Pass 1 established: AngularJS SPA, REST API at /api/, hashbang routing (the plain
`#/page/search` silently lands on `#!/page/default`), and reCAPTCHA v3 loaded
SITE-WIDE with no visible widget.

The decisive question this pass answers: does the SEARCH call itself carry a
recaptcha token? Site-wide v3 script != gated endpoint. If the search payload
carries a token we must STOP and flag rather than work around it.

Also captures: profession / license-type picklists, the search request/response
shape, pagination mechanism, and the reported total.

GUARDRAIL: read-only public licence search. No login, no fee, no CAPTCHA solved.
"""
import json
import re
import time

from playwright.sync_api import sync_playwright

URL = "https://www.pals.pa.gov/#!/page/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

api = []
resps = []
T0 = time.time()
TOKEN_HINT = re.compile(r"recaptcha|captcha|g-recaptcha-response|token", re.I)


def on_request(r):
    if "/api/" in r.url:
        pd = r.post_data or ""
        api.append({"t": round(time.time() - T0, 1), "m": r.method,
                    "url": r.url[:200], "post": pd[:2000],
                    "captcha_in_payload": bool(TOKEN_HINT.search(pd)),
                    "captcha_in_headers": sorted(
                        k for k in (r.headers or {}) if TOKEN_HINT.search(k))})


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1200})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)
    ctx.on("request", on_request)
    ctx.on("response", lambda r: resps.append(r) if "/api/" in r.url else None)

    print("[pals2] loading %s" % URL)
    try:
        pg.goto(URL, wait_until="networkidle", timeout=90000)
    except Exception:
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(10000)
    print("[pals2] landed url: %s" % pg.url)

    # --- what does the search form actually offer?
    print("\n=== visible search controls ===")
    sl = pg.locator("select")
    print("  native <select>: %d" % sl.count())
    picks = {}
    for i in range(sl.count()):
        e = sl.nth(i)
        try:
            if not e.is_visible():
                continue
            nm = (e.get_attribute("name") or e.get_attribute("id")
                  or e.get_attribute("ng-model") or "select#%d" % i)
            o = e.locator("option")
            opts = [(o.nth(j).get_attribute("value"), (o.nth(j).inner_text() or "").strip())
                    for j in range(o.count())]
            picks[nm] = opts
            print("\n  -- %r  (%d options)" % (nm, len(opts)))
            for v, t in opts[:80]:
                print("       %-28r %r" % (str(v)[:26], t[:60]))
            if len(opts) > 80:
                print("       ... +%d more" % (len(opts) - 80))
        except Exception as ex:
            print("  select %d: %s" % (i, type(ex).__name__))

    ip = pg.locator("input")
    for i in range(min(ip.count(), 40)):
        e = ip.nth(i)
        try:
            if not e.is_visible():
                continue
            print("  input type=%s name=%r placeholder=%r"
                  % (e.get_attribute("type"), e.get_attribute("name"),
                     e.get_attribute("placeholder")))
        except Exception:
            pass
    for i in range(min(pg.locator("button").count(), 30)):
        e = pg.locator("button").nth(i)
        try:
            if e.is_visible():
                t = (e.inner_text() or "").strip()
                if t and len(t) < 40:
                    print("  [button] %r" % t)
        except Exception:
            pass

    # --- is there a visible captcha on THIS page?
    n = pg.locator("iframe[src*='recaptcha'], .g-recaptcha, [id*='captcha' i]").count()
    print("\n  visible captcha elements on search page: %d" % n)

    json.dump(picks, open("pals_picklists.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="pals_search.png", full_page=True)

    # --- drive one real search: Profession = Pharmacy, no other filter
    print("\n=== attempting a Profession-only search ===")
    target = None
    for nm, opts in picks.items():
        for v, t in opts:
            if t.strip().lower() == "pharmacy":
                target = (nm, v, t)
                break
        if target:
            break
    if target:
        nm, v, t = target
        print("  selecting %r = %r on %r" % (t, v, nm))
        sel = pg.locator("select").filter(has=pg.locator("option:has-text('Pharmacy')")).first
        sel.select_option(value=v)
        pg.wait_for_timeout(3000)
    else:
        print("  !! no 'Pharmacy' option found in any visible select")

    before = len(api)
    try:
        pg.locator("button:has-text('Search')").first.click()
        pg.wait_for_timeout(15000)
    except Exception as e:
        print("  search click failed: %s" % type(e).__name__)

    print("\n=== API CALLS (search phase) ===")
    for c in api[before:]:
        print("  [%ss] %s %s" % (c["t"], c["m"], c["url"]))
        print("     captcha_in_payload=%s captcha_in_headers=%s"
              % (c["captcha_in_payload"], c["captcha_in_headers"]))
        if c["post"]:
            print("     POST %s" % c["post"][:700])

    print("\n=== SEARCH RESPONSES ===")
    for r in resps:
        if not re.search(r"(search|licen)", r.url, re.I):
            continue
        try:
            t = r.text()
        except Exception as e:
            t = "<unreadable %s>" % type(e).__name__
        print("  %s %s len=%d" % (r.status, r.url[:120], len(t)))
        print("     %s" % t[:1500].replace("\n", " "))

    print("\n=== rendered results ===")
    for sel in ("table", "[role=grid]", ".table"):
        c = pg.locator(sel).count()
        if c:
            print("  %s: %d element(s), %d row(s)" % (sel, c, pg.locator("%s tr" % sel).count()))
    body = re.sub(r"\s+", " ", pg.inner_text("body"))
    m = re.findall(r"([\d,]+)\s*(?:results?|records?|licen\w+)", body, re.I)
    print("  count-ish text: %s" % m[:5])
    print("  tail: ...%s" % body[-600:])

    json.dump({"api": api}, open("pals_recon2.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="pals_results.png", full_page=True)
    print("\n[pals2] -> pals_recon2.json / pals_picklists.json / pals_results.png")
    b.close()
