#!/usr/bin/env python3
"""WI DSPS (license.wi.gov, Salesforce) - RECON 3: capture the real search contract.

Established so far:
  * license.wi.gov/s/license-lookup is live, guest-accessible, NO captcha and NO login.
    Cloudflare blocks plain HTTP clients but passes a real browser with no challenge.
  * Salesforce Experience Cloud; Apex controller DSPS_LicensesLookupController
    (fetchProfessions seen on load - that is only the picklist).
  * Search By offers: Credential/License Number | Individual Name | Organization Name |
    License Type.  "License Type" is the enumeration axis; Organization vs Individual is
    the establishment separator.

This pass drives ONE real search and records BOTH the request and the response, so the
adapter can replay the Apex call rather than guess it. Response bodies are read AFTER the
handler returns - reading inside a sync_playwright handler re-enters the event loop.

Read-only: a public licence search. No login, no fee, no captcha.
"""
import json
import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "https://license.wi.gov/s/license-lookup"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

reqs, resps = [], []


def clean(t):
    return re.sub(r"[^\x20-\x7e]", " ", t or "")


def log(m):
    print("[wi3] " + str(m), flush=True)


def on_request(r):
    if "/aura" not in r.url or r.method != "POST":
        return
    m = re.search(r"message=([^&]*)", r.post_data or "")
    if not m:
        return
    try:
        msg = json.loads(urllib.parse.unquote_plus(m.group(1)))
    except Exception:
        return
    for a in msg.get("actions", []):
        if "ApexAction" in (a.get("descriptor") or ""):
            p = a.get("params", {})
            reqs.append({"class": p.get("classname"), "method": p.get("method"),
                         "params": p.get("params"), "url": r.url[:120],
                         "raw": r.post_data[:4000]})


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1400},
                        locale="en-US")
    pg = ctx.new_page()
    pg.set_default_timeout(90000)
    ctx.on("request", on_request)
    ctx.on("response", lambda r: resps.append(r)
           if ("/aura" in r.url and r.request.method == "POST") else None)

    try:
        pg.goto(URL, wait_until="networkidle", timeout=90000)
    except Exception:
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(10000)
    log("loaded: %s" % clean(pg.title()))

    # ---- choose Search By = License Type
    log("")
    log("selecting Search By = 'License Type'")
    try:
        pg.locator("button[role=combobox], [role=combobox]").first.click()
        pg.wait_for_timeout(1500)
        pg.locator("[role=option]:has-text('License Type')").first.click()
        pg.wait_for_timeout(3500)
    except Exception as e:
        log("  !! %s" % type(e).__name__)

    # ---- what appeared after choosing that mode?
    log("")
    log("controls now visible:")
    for sel in ("[role=combobox]", "input", "button"):
        L = pg.locator(sel)
        for i in range(min(L.count(), 14)):
            e = L.nth(i)
            try:
                if not e.is_visible():
                    continue
                if sel == "input":
                    log("   input type=%s ph=%r" % (e.get_attribute("type"),
                                                    clean(e.get_attribute("placeholder"))[:34]))
                else:
                    t = clean(e.inner_text()).strip()
                    if t and len(t) < 44:
                        log("   %s %r" % (sel, t[:44]))
            except Exception:
                pass

    # ---- the License Type vocabulary (WI's own, not the retired app's)
    log("")
    log("License Type options:")
    types = []
    try:
        combos = pg.locator("[role=combobox]")
        combos.nth(min(1, combos.count() - 1)).click()
        pg.wait_for_timeout(2000)
        o = pg.locator("[role=option]")
        for i in range(o.count()):
            t = clean(o.nth(i).inner_text()).strip()
            if t:
                types.append(t)
        for t in types[:80]:
            mark = ""
            if re.search(r"(wholesale|distribut|pharmac|manufactur|logistic|3pl|outsourc)",
                         t, re.I):
                mark = "   <== SUPPLY CHAIN"
            log("   %-58r%s" % (t[:58], mark))
        if len(types) > 80:
            log("   ... +%d more" % (len(types) - 80))
    except Exception as e:
        log("  !! %s" % type(e).__name__)
    json.dump(types, open("wi_license_types.json", "w", encoding="utf-8"), indent=1)
    log("-> wrote wi_license_types.json (%d)" % len(types))

    # ---- drive ONE search on a supply-chain type
    target = next((t for t in types if re.search(r"wholesale distributor", t, re.I)), None)
    if not target:
        target = next((t for t in types if re.search(r"pharmacy", t, re.I)), None)
    log("")
    log("driving one search for: %r" % target)
    before = len(reqs)
    try:
        if target:
            pg.locator("[role=option]:has-text('%s')" % target[:28]).first.click()
            pg.wait_for_timeout(2500)
        for lbl in ("Search", "Apply", "Go"):
            btn = pg.locator("button:has-text('%s')" % lbl)
            if btn.count():
                btn.first.click()
                break
        pg.wait_for_timeout(12000)
    except Exception as e:
        log("  !! %s: %s" % (type(e).__name__, str(e)[:110]))

    log("")
    log("=" * 74)
    log("APEX REQUESTS (search phase)")
    log("=" * 74)
    for c in reqs[before:]:
        log("  %s.%s" % (c["class"], c["method"]))
        log("    params=%s" % json.dumps(c["params"])[:500])

    log("")
    log("APEX RESPONSES")
    log("=" * 74)
    bodies = []
    for r in resps:
        try:
            t = r.text()
        except Exception:
            continue
        if "returnValue" not in t:
            continue
        bodies.append({"url": r.url[:110], "len": len(t), "body": t[:6000]})
    for x in bodies[-3:]:
        log("  %s  len=%d" % (x["url"], x["len"]))
        log("    %s" % x["body"][:900].replace("\n", " "))
        log("")

    # ---- grid shape after the search
    log("grid after search:")
    log(json.dumps(pg.evaluate("""() => {
      const t=document.querySelector('table');
      if(!t) return {table:false};
      return {table:true,
              headers:[...t.querySelectorAll('thead th')].map(h=>h.innerText.trim()).filter(Boolean),
              rows:t.querySelectorAll('tbody tr').length,
              sample:[...t.querySelectorAll('tbody tr')].slice(0,3).map(r=>[...r.cells].map(c=>c.innerText.trim()))};
    }"""), indent=1)[:1200])
    body = clean(re.sub(r"\s+", " ", pg.inner_text("body")))
    m = re.search(r"([\d,]+)\s*(?:results?|records?|items?|rows?)", body, re.I)
    log("count text: %s" % (m.group(0) if m else "none"))

    json.dump({"requests": reqs, "responses": bodies},
              open("wi_recon3.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="wi_recon3.png", full_page=True)
    log("")
    log("-> wi_recon3.json / wi_recon3.png")
    b.close()
