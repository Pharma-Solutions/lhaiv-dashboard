#!/usr/bin/env python3
"""ENG-346 recon pass 2 — capture the NM RLD search Apex call end-to-end.

Pass 1 found the controller (RLDLicenseSearchController) but only ever saw
getPicklistValues, because it never submitted. This pass drives the real UI
once and records BOTH the request body and the response body of every
ApexAction, so the adapter can replay the call directly instead of guessing.

Read-only: a public license search. Nothing purchased, no account, no CAPTCHA.
"""
import json
import re
import time
import urllib.parse

from playwright.sync_api import sync_playwright

URL = "https://nmrldlpi.my.site.com/bcd/s/rld-public-search?language=en_US"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

reqs = []      # decoded ApexAction request messages
resps = []     # (url, Response) kept for later body read — reading inside the
               # handler re-enters the sync event loop and deadlocks.
T0 = time.time()


def decode_msg(post):
    m = re.search(r"message=([^&]*)", post or "")
    if not m:
        return None
    try:
        return json.loads(urllib.parse.unquote_plus(m.group(1)))
    except Exception:
        return urllib.parse.unquote_plus(m.group(1))[:900]


def on_request(r):
    if "aura" in r.url and r.method == "POST":
        msg = decode_msg(r.post_data)
        acts = (msg or {}).get("actions", []) if isinstance(msg, dict) else []
        for a in acts:
            if "ApexAction" in (a.get("descriptor") or ""):
                p = a.get("params", {})
                reqs.append({"t": round(time.time() - T0, 1),
                             "classname": p.get("classname"),
                             "method": p.get("method"),
                             "params": p.get("params"),
                             "url": r.url[:140]})


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(60000)
    ctx.on("request", on_request)
    ctx.on("response", lambda r: resps.append(r) if "aura" in r.url and "ApexAction" in r.url else None)

    print(f"[nm2] loading {URL}")
    try:
        pg.goto(URL, wait_until="networkidle", timeout=90000)
    except Exception:
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(9000)

    # ---- capture the aura envelope the adapter will need to replay calls
    env = pg.evaluate("""() => {
        const c = window.$A && window.$A.getContext ? window.$A.getContext() : null;
        return c ? {fwuid: c.fwuid, app: c.app, mode: c.mode,
                    loaded: c.loaded, token: (window.$A.get('$Global') ? '' : ''),
                    auraToken: (window.aura && window.aura.token) || null} : null;
    }""")
    print(f"[nm2] aura context: {json.dumps(env)[:400] if env else 'unavailable'}")

    def open_combo(label):
        btn = pg.locator(f"button[aria-label='{label}'], button:has-text('{label}')").first
        btn.click()
        pg.wait_for_timeout(1200)
        return btn

    print("\n[nm2] --- Profession combobox ---")
    open_combo("Select a Profession")
    opts = pg.locator("[role=option]")
    n = opts.count()
    vals = []
    for i in range(n):
        try:
            t = (opts.nth(i).inner_text() or "").strip()
            if t:
                vals.append(t)
        except Exception:
            pass
    print(f"[nm2] {n} option node(s); pharmacy-ish: {[v for v in vals if 'harmac' in v]}")
    json.dump(vals, open("nm_professions_raw.json", "w", encoding="utf-8"), indent=1)

    target = next((v for v in vals if "harmac" in v), None)
    if not target:
        print("[nm2] !! no Pharmacy profession option found — dumping and stopping")
        pg.screenshot(path="nm_recon2_fail.png", full_page=True)
        b.close()
        raise SystemExit(1)
    print(f"[nm2] selecting profession: {target!r}")
    pg.locator(f"[role=option]:has-text('{target}')").first.click()
    pg.wait_for_timeout(2500)

    print("\n[nm2] --- License Type combobox (after profession) ---")
    open_combo("Select a License Type")
    o2 = pg.locator("[role=option]")
    lt = []
    for i in range(o2.count()):
        try:
            t = (o2.nth(i).inner_text() or "").strip()
            if t:
                lt.append(t)
        except Exception:
            pass
    print(f"[nm2] {len(lt)} option node(s) visible after profession select")
    json.dump(lt, open("nm_licensetypes_raw.json", "w", encoding="utf-8"), indent=1)

    probe = next((v for v in lt if v.strip() == "Wholesale Drug Distributor"), None)
    if probe:
        print(f"[nm2] selecting license type: {probe!r}")
        pg.locator(f"[role=option]:has-text('{probe}')").first.click()
        pg.wait_for_timeout(1500)
    else:
        print("[nm2] 'Wholesale Drug Distributor' not in list; closing combo, searching profession-wide")
        pg.keyboard.press("Escape")

    print("\n[nm2] --- Search ---")
    pg.locator("button:has-text('Search'), lightning-button:has-text('Search')").last.click()
    pg.wait_for_timeout(12000)

    print("\n=== ApexAction REQUESTS ===")
    for r in reqs:
        print(f"  [{r['t']}s] {r['classname']}.{r['method']}  params={json.dumps(r['params'])[:500]}")

    print("\n=== ApexAction RESPONSES ===")
    bodies = []
    for r in resps:
        try:
            txt = r.text()
        except Exception as e:
            txt = f"<unreadable: {type(e).__name__}>"
        bodies.append({"url": r.url[:140], "status": r.status, "body": txt})
        print(f"  {r.status} {r.url[:90]}  len={len(txt)}")
        print(f"     {txt[:900]}")

    print("\n=== rendered result area ===")
    for sel in ("table", "lightning-datatable", "[role=grid]", ".slds-table"):
        c = pg.locator(sel).count()
        if c:
            print(f"  {sel}: {c} element(s), {pg.locator(f'{sel} tr').count()} row(s)")
    body_txt = re.sub(r"\s+", " ", pg.inner_text("body"))
    m = re.search(r"(\d[\d,]*)\s*(results?|records?|licen)", body_txt, re.I)
    print(f"  result-count text: {m.group(0) if m else 'none'}")
    print(f"  tail of page: ...{body_txt[-700:]}")

    json.dump({"requests": reqs, "responses": bodies},
              open("nm_recon2.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="nm_recon2.png", full_page=True)
    print("\n[nm2] -> nm_recon2.json / nm_recon2.png")
    b.close()
