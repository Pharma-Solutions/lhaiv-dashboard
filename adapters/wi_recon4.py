#!/usr/bin/env python3
"""WI DSPS - RECON 4: capture the records-returning Apex call.

Everything up to here is settled: license.wi.gov/s/license-lookup is live and guest
accessible, Cloudflare passes a real browser with no challenge, there is no login and no
captcha, and DSPS_LicensesLookupController.fetchProfessions yields 254 credential types
across 5 categories.

What is NOT yet known, and what this pass measures rather than assumes:
  1. which Apex method actually returns RECORDS (NM's was onSearch) and its request shape
  2. whether it returns the whole result set in one response or pages, and any record cap
     (the total == pages x page_size round-number tell applies either way)
  3. the field schema - does the search payload carry address / status / issue+expiry, or
     is a per-record detail call needed?

Drives: Search By -> License Type -> Category "Health" -> License Name
"Wholesale Distributor of Prescription Drugs" -> Search.

Response bodies are read AFTER the handler returns; reading inside a sync_playwright
handler re-enters the event loop and deadlocks.

If an interactive Cloudflare/CAPTCHA challenge appears, this stops and flags it.
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
TARGET = "Wholesale Distributor of Prescription Drugs"

reqs, resps = [], []


def clean(t):
    return re.sub(r"[^\x20-\x7e]", " ", t or "")


def log(m):
    print("[wi4] " + str(m), flush=True)


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
            reqs.append({"t": round(time.time() % 10000, 1),
                         "class": p.get("classname"), "method": p.get("method"),
                         "params": p.get("params"), "raw": (r.post_data or "")[:6000]})


def pick_combo(pg, label_text):
    """Click the combobox whose current label/button text matches, re-resolved each time."""
    btns = pg.locator("button[role=combobox], [role=combobox]")
    for i in range(btns.count()):
        try:
            if label_text.lower() in clean(btns.nth(i).inner_text()).lower():
                btns.nth(i).click()
                return True
        except Exception:
            continue
    return False


def choose_option(pg, text, timeout=6000):
    pg.wait_for_timeout(1200)
    opt = pg.locator("[role=option]").filter(has_text=text)
    if opt.count():
        opt.first.click()
        pg.wait_for_timeout(2500)
        return True
    return False


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
    pg.wait_for_timeout(9000)

    body = clean(re.sub(r"\s+", " ", pg.inner_text("body")))
    if re.search(r"(attention required|verify you are human|checking your browser)", body, re.I):
        log("!! Cloudflare interactive challenge present - STOPPING per guardrails")
        pg.screenshot(path="wi_recon4_challenge.png", full_page=True)
        b.close()
        sys.exit(3)
    log("loaded: %r" % clean(pg.title()))

    # ---- cascade: Search By -> License Type
    log("")
    log("cascade step 1: Search By -> License Type")
    pick_combo(pg, "Select Search By") or pick_combo(pg, "Search By")
    if not choose_option(pg, "License Type"):
        log("  !! could not select 'License Type'")
    # ---- Category -> Health
    log("cascade step 2: Category -> Health")
    pick_combo(pg, "Select Category") or pick_combo(pg, "Category")
    if not choose_option(pg, "Health"):
        log("  !! could not select 'Health'")
    # ---- License Name -> target
    log("cascade step 3: License Name -> %r" % TARGET)
    pick_combo(pg, "Select License Name") or pick_combo(pg, "License Name")
    if not choose_option(pg, TARGET):
        log("  !! could not select %r" % TARGET)

    log("")
    log("state before search:")
    for i in range(pg.locator("[role=combobox]").count()):
        try:
            log("   combo[%d] = %r" % (i, clean(pg.locator("[role=combobox]").nth(i).inner_text())[:52]))
        except Exception:
            pass

    before = len(reqs)
    log("")
    log("clicking Search")
    for lbl in ("Search", "Apply", "Submit", "Go"):
        btn = pg.locator("button").filter(has_text=re.compile(r"^\s*%s\s*$" % lbl, re.I))
        if btn.count():
            try:
                btn.first.click()
                break
            except Exception:
                pass
    pg.wait_for_timeout(14000)

    log("")
    log("=" * 76)
    log("APEX REQUESTS during search")
    log("=" * 76)
    for c in reqs[before:]:
        log("  %s.%s" % (c["class"], c["method"]))
        log("    params = %s" % json.dumps(c["params"])[:700])
    if len(reqs) == before:
        log("  (none captured - the Search click may not have fired)")

    log("")
    log("=" * 76)
    log("APEX RESPONSES (largest last)")
    log("=" * 76)
    bodies = []
    for r in resps:
        try:
            t = r.text()
        except Exception:
            continue
        if "returnValue" in t:
            bodies.append({"url": r.url[:110], "len": len(t), "body": t})
    bodies.sort(key=lambda x: x["len"])
    for x in bodies[-2:]:
        log("  %s  len=%d" % (x["url"], x["len"]))
        log("    %s" % x["body"][:1500].replace("\n", " "))
        log("")

    # ---- what did the grid render?
    log("grid after search:")
    log(json.dumps(pg.evaluate("""() => {
      const t=document.querySelector('table');
      if(!t) return {table:false};
      return {table:true,
              headers:[...t.querySelectorAll('thead th')].map(h=>h.innerText.trim()).filter(Boolean),
              rows:t.querySelectorAll('tbody tr').length,
              sample:[...t.querySelectorAll('tbody tr')].slice(0,3).map(r=>[...r.cells].map(c=>c.innerText.trim()))};
    }"""), indent=1)[:1600])
    body = clean(re.sub(r"\s+", " ", pg.inner_text("body")))
    for pat in (r"([\d,]+)\s*(?:results?|records?|items?|rows?)", r"of\s+([\d,]+)"):
        m = re.search(pat, body, re.I)
        if m:
            log("count text: %r" % m.group(0))
            break

    json.dump({"requests": reqs, "responses": [{"url": x["url"], "len": x["len"],
                                                "body": x["body"][:20000]} for x in bodies]},
              open("wi_recon4.json", "w", encoding="utf-8"), indent=1)
    pg.screenshot(path="wi_recon4.png", full_page=True)
    log("")
    log("-> wi_recon4.json / wi_recon4.png")
    b.close()
