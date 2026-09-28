#!/usr/bin/env python3
"""ENG-346 — New Mexico Board of Pharmacy recon.

Portal: https://nmrldlpi.my.site.com/bcd/s/rld-public-search  (Salesforce Experience Cloud,
RLD Boards & Commissions Division — the division that owns Pharmacy).

CONTEXT / TENSION THIS RECON RESOLVES
  * The 2026-08 pressure test filed NM as Tier 2 — a paid bulk list request (~$1,500) at
    nmrldlpi.my.site.com/bcd/s/license-list-request.
  * ENG-346 says "Public — no purchase", ~42,997 across 33 populated classes.
  Both are true: they are DIFFERENT channels. This recon characterises the FREE public
  search and asks whether it is enumerable enough to replace the purchase.

QUESTIONS (spec DoD for #16)
  1. Is the search enumerable (browse-all / wildcard / class-driven) or single-record only?
  2. Which license classes does the Board of Pharmacy expose, and which are populated?
  3. Is it a bulk export, or per-query pagination? What page size / total is reported?
  4. What field names come back?
  5. Any CAPTCHA / login / fee gate? -> if yes: STOP and flag, never work around.

GUARDRAIL: read-only. Nothing purchased, no account, no CAPTCHA touched.
"""
import json
import re
import time

from playwright.sync_api import sync_playwright

URL = "https://nmrldlpi.my.site.com/bcd/s/rld-public-search?language=en_US"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

net = []
T0 = time.time()
CAPSEL = ("iframe[src*='recaptcha'], .g-recaptcha, iframe[src*='hcaptcha'], .cf-turnstile, "
          "#captcha, [id*='captcha' i], [name*='captcha' i]")


def cap_report(pg, where):
    n = pg.locator(CAPSEL).count()
    html = pg.content()
    words = sorted(set(re.findall(r"recaptcha|hcaptcha|turnstile|captcha", html, re.I)))
    gate = re.search(r"(please log ?in|sign in to continue|payment required|purchase|"
                     r"\$\s?\d[\d,]*)", pg.inner_text("body"), re.I)
    print(f"  [gate @ {where}] captcha-elements={n} source-words={words} "
          f"fee/login-text={gate.group(0)[:40] if gate else 'none'}")
    return n > 0


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, accept_downloads=True,
                        viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)

    ctx.on("request", lambda r: net.append(
        {"t": round(time.time() - T0, 1), "m": r.method, "url": r.url[:200],
         "post": (r.post_data or "")[:700]}) if re.search(r"(apex|aura|webruntime|api|query|search)",
                                                          r.url, re.I) else None)
    ctx.on("response", lambda r: net.append(
        {"t": round(time.time() - T0, 1), "resp": r.status, "url": r.url[:200],
         "ct": (r.headers or {}).get("content-type", "")[:40],
         "cd": (r.headers or {}).get("content-disposition", "")[:70]})
        if re.search(r"(apex|aura|webruntime|api|query|search)", r.url, re.I) else None)
    pg.on("download", lambda d: net.append({"DOWNLOAD": d.suggested_filename,
                                            "url": (d.url or "")[:200]}))

    print(f"[nm] loading {URL}")
    try:
        pg.goto(URL, wait_until="networkidle", timeout=90000)
    except Exception as e:
        print(f"[nm] networkidle timeout ({type(e).__name__}); falling back")
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(9000)          # Salesforce Aura is slow to hydrate

    print(f"[nm] url   : {pg.url}")
    print(f"[nm] title : {pg.title()!r}")
    print("\n=== 5. gates ===")
    cap_report(pg, "search page load")

    txt = re.sub(r"\s+", " ", pg.inner_text("body"))
    print(f"\n=== visible copy (first 1100) ===\n{txt[:1100]}")

    print("\n=== 2. selects / class axis ===")
    dump = {}
    sl = pg.locator("select")
    print(f"  native <select> count: {sl.count()}")
    for i in range(sl.count()):
        e = sl.nth(i)
        try:
            o = e.locator("option")
            opts = [(o.nth(j).get_attribute("value"), (o.nth(j).inner_text() or "").strip())
                    for j in range(o.count())]
            nm = e.get_attribute("name") or e.get_attribute("id") or f"select#{i}"
            dump[nm] = opts
            print(f"  {nm!r} visible={e.is_visible()} n={len(opts)}")
            for v, t in opts[:45]:
                print(f"     {str(v)[:38]!r:<40} {t[:52]!r}")
            if len(opts) > 45:
                print(f"     ... +{len(opts)-45} more")
        except Exception as ex:
            print(f"  select {i}: {type(ex).__name__}")

    # Salesforce often uses lightning comboboxes, not native <select>
    print("\n  lightning combobox / listbox candidates:")
    for sel in ("lightning-combobox", "[role=combobox]", "[role=listbox]", ".slds-combobox"):
        c = pg.locator(sel).count()
        if c:
            print(f"     {sel}: {c}")

    print("\n=== 3/4. inputs + buttons ===")
    ip = pg.locator("input")
    for i in range(min(ip.count(), 30)):
        e = ip.nth(i)
        try:
            if not e.is_visible():
                continue
            print(f"  input type={e.get_attribute('type')} name={e.get_attribute('name')!r} "
                  f"placeholder={e.get_attribute('placeholder')!r} "
                  f"label={(e.get_attribute('aria-label') or '')[:40]!r}")
        except Exception:
            pass
    for sel in ("button", "a[role=button]", "lightning-button"):
        l = pg.locator(sel)
        for i in range(min(l.count(), 40)):
            e = l.nth(i)
            try:
                if not e.is_visible():
                    continue
                t = (e.inner_text() or "").strip()
                if t and len(t) < 50:
                    print(f"  [{sel}] {t!r}")
            except Exception:
                pass

    print("\n=== any table already rendered (browse-all?) ===")
    for sel in ("table", "lightning-datatable", "[role=grid]", ".slds-table"):
        c = pg.locator(sel).count()
        if c:
            rows = pg.locator(f"{sel} tr").count()
            print(f"  {sel}: {c} element(s), {rows} row(s)")

    print("\n=== network: Aura/Apex endpoints seen on load ===")
    for n in net[:25]:
        print(f"  {n}")

    json.dump({"selects": dump, "net": net}, open("nm_recon.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="nm_recon.png", full_page=True)
    print("\n[nm] -> nm_recon.png / nm_recon.json   (NOTHING submitted, NOTHING purchased)")
    b.close()
