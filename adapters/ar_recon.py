#!/usr/bin/env python3
"""Arkansas Board of Pharmacy (GLSuite) - PHASE 1 RECON. No writes, no harvesting.

Answers, by measurement rather than assumption:
  1. every License Type option (label + value); which are ESTABLISHMENT/supplier types
     and which are individual credentials to exclude
  2. does the results GRID carry street address + issue/expiration + status, or do those
     require each record's detail page?
  3. search mechanics - does a blank (or * / %) query return everything for a type?
     what is the page size, and what drives pagination?
  4. ar_recon.png screenshot

GUARDRAILS: read-only. No login, no payment, no terms accepted, no CAPTCHA touched. If a
gate appears this script reports it and stops.

GLSuite is ASP.NET WebForms - __VIEWSTATE/__EVENTVALIDATION, postbacks - so locators are
re-resolved at action time rather than cached.
"""
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = ("https://arbopharmv7prod.glsuite.us/glsuiteweb/clients/arbopharm/public/"
       "verification/search.aspx")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

# Establishment / supply-chain keywords to FLAG (not an allowlist yet - recon reports,
# the human confirms the scope before Phase 2).
EST_HINTS = re.compile(
    r"(wholesale|outsourc|manufactur|logistic|3pl|third.?party|reverse|distribut|"
    r"repackag|pharmacy|drug ?room|hospital|clinic|facility|establishment|"
    r"controlled substance|cds|warehouse|supplier|retail|mail ?order|nuclear|"
    r"telepharm|remote)", re.I)
INDIV_HINTS = re.compile(
    r"(pharmacist|intern|technician|tech\b|student|preceptor|practitioner|"
    r"individual|person)", re.I)


def clean(t):
    return re.sub(r"[^\x20-\x7e]", " ", t or "")


def log(m):
    print("[ar] " + str(m), flush=True)


def dump_controls(pg, label):
    log("")
    log("--- %s ---" % label)
    for sel in ("select", "input", "a[href*='javascript']"):
        els = pg.locator(sel)
        n = els.count()
        for i in range(min(n, 40)):
            e = els.nth(i)
            try:
                if not e.is_visible():
                    continue
                if sel == "select":
                    nm = e.get_attribute("name") or e.get_attribute("id") or "select#%d" % i
                    log("  SELECT %r (%d options)" % (nm, e.locator("option").count()))
                elif sel == "input":
                    ty = e.get_attribute("type")
                    if ty == "hidden":
                        continue
                    log("  input type=%-9s name=%-40r value=%r"
                        % (ty, e.get_attribute("name"), clean(e.get_attribute("value"))[:26]))
                else:
                    t = clean(e.inner_text()).strip()
                    if t and len(t) < 30:
                        log("  link %r" % t)
            except Exception:
                pass


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1300})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)

    log("loading %s" % URL)
    try:
        r = pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(4000)
    except Exception as e:
        log("!! %s: %s" % (type(e).__name__, str(e)[:160]))
        b.close()
        sys.exit(2)
    log("status: %s | landed: %s" % (r.status if r else "?", clean(pg.url)[:120]))
    log("title : %r" % clean(pg.title()))

    # ---- GATE CHECK first: anything requiring login/payment/terms/captcha stops us
    caps = pg.locator("iframe[src*='recaptcha'], .g-recaptcha, [id*='captcha' i], "
                      "[name*='captcha' i]").count()
    body = clean(re.sub(r"\s+", " ", pg.inner_text("body")))
    gate = re.search(r"(log ?in|sign in|password|payment|purchase|fee of|\$\s?\d|"
                     r"i agree|terms and conditions|accept the terms)", body, re.I)
    log("")
    log("GATES: captcha-elements=%d  gate-text=%r"
        % (caps, gate.group(0)[:40] if gate else "none"))
    has_captcha_field = pg.evaluate("""() => [...document.querySelectorAll('form')]
        .some(f => [...f.querySelectorAll('input,textarea')]
        .some(e => /captcha/i.test(e.name||'') || /recaptcha/i.test(e.id||'')))""")
    log("       captcha field inside a form: %s" % has_captcha_field)
    if caps or has_captcha_field:
        log("!! CAPTCHA present - reporting and STOPPING per guardrails")
        pg.screenshot(path="ar_recon.png", full_page=True)
        b.close()
        sys.exit(3)
    log("visible copy: %s" % body[:420])

    dump_controls(pg, "controls on the search page")

    # ---- 1. enumerate every dropdown, in full
    log("")
    log("=" * 74)
    log("1. DROPDOWN OPTIONS (label + value), in full")
    log("=" * 74)
    selects = {}
    S = pg.locator("select")
    for i in range(S.count()):
        e = S.nth(i)
        try:
            nm = e.get_attribute("name") or e.get_attribute("id") or "select#%d" % i
            o = e.locator("option")
            opts = [(o.nth(j).get_attribute("value"), clean(o.nth(j).inner_text()).strip())
                    for j in range(o.count())]
            selects[nm] = opts
            log("")
            log("SELECT %r  (%d options, visible=%s)" % (nm, len(opts), e.is_visible()))
            for v, t in opts:
                tag = ""
                if EST_HINTS.search(t) and not INDIV_HINTS.search(t):
                    tag = "   <== ESTABLISHMENT/SUPPLIER"
                elif INDIV_HINTS.search(t):
                    tag = "   (individual - exclude)"
                log("    %-10r %-52r%s" % (str(v)[:10], t[:52], tag))
        except Exception as ex:
            log("  select %d: %s" % (i, type(ex).__name__))
    json.dump({k: v for k, v in selects.items()},
              open("ar_license_types.json", "w", encoding="utf-8"), indent=1)
    log("")
    log("-> wrote ar_license_types.json (enum-drift baseline)")

    pg.screenshot(path="ar_recon.png", full_page=True)
    log("-> wrote ar_recon.png")
    log("")
    log("RECON PHASE 1a complete (page structure + option enumeration).")
    log("Grid-shape and pagination probing follows in ar_recon2.py so this pass")
    log("stays strictly non-submitting.")
    b.close()
