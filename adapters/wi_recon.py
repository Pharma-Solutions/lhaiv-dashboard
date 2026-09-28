#!/usr/bin/env python3
"""Wisconsin DSPS - PHASE 1 RECON of the live credential lookup. No writes.

The endpoint banked from earlier recon (licensesearch.wi.gov/OrganizationLicense/...)
has had NO DNS A record for days - dead from two public resolvers. The working door is
the older DSPS app:

    https://online.drl.wi.gov/licenselookup/MultipleCredentialSearch.aspx

ASP.NET WebForms (__VIEWSTATE/__EVENTVALIDATION), so locators are resolved at action
time and postbacks are driven, never replayed.

Reports rather than assumes:
  1. every credential/profession option (label + value); which are ESTABLISHMENT types
     (Wholesale Distributor of Prescription Drugs, pharmacies) and which are individual
  2. gates - captcha / login / fee (guardrail: stop and flag, never work around)
  3. what a search returns: grid columns, whether address/dates/status are present,
     page size and pagination control
  4. wi_recon.png
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "https://online.drl.wi.gov/licenselookup/MultipleCredentialSearch.aspx"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

EST = re.compile(r"(wholesale|distribut|pharmac|manufactur|outsourc|logistic|3pl|"
                 r"repackag|facility|establishment|firm|company|corporation|clinic|"
                 r"hospital|warehouse|drug)", re.I)
INDIV = re.compile(r"(pharmacist|technician|intern|student|assistant|practitioner|"
                   r"nurse|physician|therapist|counselor|operator|barber|cosmetolog)", re.I)


def clean(t):
    return re.sub(r"[^\x20-\x7e]", " ", t or "")


def log(m):
    print("[wi] " + str(m), flush=True)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1300})
    pg = ctx.new_page()
    pg.set_default_timeout(90000)
    r = pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_timeout(3500)
    log("status: %s | landed: %s" % (r.status if r else "?", clean(pg.url)[:110]))
    log("title : %r" % clean(pg.title()))

    # ---- 2. gates first
    caps = pg.locator("iframe[src*='recaptcha'], .g-recaptcha, [id*='captcha' i], "
                      "[name*='captcha' i]").count()
    body = clean(re.sub(r"\s+", " ", pg.inner_text("body")))
    gate = re.search(r"(log ?in|sign in|password|payment|purchase|fee of|\$\s?\d|"
                     r"i agree|terms and conditions)", body, re.I)
    incaptcha = pg.evaluate("""() => [...document.querySelectorAll('form')]
        .some(f => [...f.querySelectorAll('input,textarea')]
        .some(e => /captcha/i.test(e.name||'') || /recaptcha/i.test(e.id||'')))""")
    log("")
    log("GATES: captcha-elements=%d  captcha-field-in-form=%s  gate-text=%r"
        % (caps, incaptcha, gate.group(0)[:40] if gate else "none"))
    if caps or incaptcha:
        log("!! CAPTCHA present - STOPPING per guardrails")
        pg.screenshot(path="wi_recon.png", full_page=True)
        b.close()
        sys.exit(3)
    log("copy: %s" % body[:360])

    # ---- 1. enumerate dropdowns
    log("")
    log("=" * 74)
    log("1. DROPDOWNS")
    log("=" * 74)
    dump = {}
    S = pg.locator("select")
    for i in range(S.count()):
        e = S.nth(i)
        try:
            nm = e.get_attribute("name") or e.get_attribute("id") or "select#%d" % i
            o = e.locator("option")
            opts = [(o.nth(j).get_attribute("value"), clean(o.nth(j).inner_text()).strip())
                    for j in range(o.count())]
            dump[nm] = opts
            log("")
            log("SELECT %r  (%d options, visible=%s)" % (nm, len(opts), e.is_visible()))
            for v, t in opts:
                tag = ""
                if EST.search(t) and not INDIV.search(t):
                    tag = "   <== ESTABLISHMENT"
                elif INDIV.search(t):
                    tag = "   (individual)"
                log("   %-8r %-56r%s" % (str(v)[:8], t[:56], tag))
        except Exception as ex:
            log("  select %d: %s" % (i, type(ex).__name__))
    json.dump(dump, open("wi_credential_types.json", "w", encoding="utf-8"), indent=1)
    log("")
    log("-> wrote wi_credential_types.json (enum-drift baseline)")

    # ---- visible inputs / buttons
    log("")
    log("visible inputs and buttons:")
    for sel in ("input", "a[href*='javascript']"):
        L = pg.locator(sel)
        for i in range(min(L.count(), 40)):
            e = L.nth(i)
            try:
                if not e.is_visible():
                    continue
                if sel == "input":
                    ty = e.get_attribute("type")
                    if ty == "hidden":
                        continue
                    log("   input type=%-9s name=%-46r value=%r"
                        % (ty, e.get_attribute("name"),
                           clean(e.get_attribute("value"))[:24]))
                else:
                    t = clean(e.inner_text()).strip()
                    if t and len(t) < 34:
                        log("   link %r" % t)
            except Exception:
                pass

    pg.screenshot(path="wi_recon.png", full_page=True)
    log("")
    log("-> wrote wi_recon.png")
    log("RECON 1 complete: structure + credential enumeration. Grid shape and")
    log("pagination follow once the establishment types are confirmed.")
    b.close()
