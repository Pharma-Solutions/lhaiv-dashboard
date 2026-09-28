#!/usr/bin/env python3
"""ENG-345 recon - Oregon Pharmaceutical Representative licensing (DCBS / DFR).

NOT the Oregon Board of Pharmacy (already held). Different entity type: individual
pharmaceutical SALES REPS, ~913 active. Oregon began licensing pharma reps under
HB 4005 / the drug price transparency programme, administered by the Division of
Financial Regulation inside DCBS - a financial regulator, not a health board,
which is why the credential does not live in the Board of Pharmacy roster.

QUESTIONS
  1. Where is the public licensee list/search for this credential?
  2. Enumerable (bulk/paged/downloadable) or single-record lookup only?
  3. What fields come back (name, licence no, issue/expiry, employer location)?
  4. Is there an explicit status flag, or is active-only implied?
  5. Any CAPTCHA / login / fee gate? -> STOP and flag, never work around.

Fallback if not enumerable: records/email request to
DFR.PharmaSalesRep@dcbs.oregon.gov / 503-947-7981 - NOTE IT, DO NOT SEND.

GUARDRAIL: read-only. No login, no fee, no CAPTCHA, nothing submitted or emailed.
"""
import re
import sys
import time

from playwright.sync_api import sync_playwright

# Windows consoles default to cp1252 and these .gov pages carry private-use
# glyphs; force UTF-8 rather than losing the whole run to an encode error.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def clean(t):
    return re.sub(r"[^ -~]", " ", t or "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

# Candidate entry points, cheapest/most-likely first.
CANDIDATES = [
    "https://dfr.oregon.gov/drugtransparency/Pages/pharma-reps.aspx",
    "https://dfr.oregon.gov/drugtransparency/Pages/index.aspx",
    "https://dfr.oregon.gov/licensing/Pages/index.aspx",
    "https://dfr.oregon.gov/pharmacy/Pages/index.aspx",
]
DOWNLOAD_RE = re.compile(r"\.(csv|xlsx?|json)(\?|$)", re.I)
LIST_WORDS = re.compile(r"(licensee list|list of licens|registered representative|"
                        r"pharmaceutical represent|licensee search|search for a licen|"
                        r"download|roster|directory)", re.I)

net = []
T0 = time.time()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, accept_downloads=True,
                        viewport={"width": 1500, "height": 1100})
    pg = ctx.new_page()
    pg.set_default_timeout(60000)
    ctx.on("response", lambda r: net.append((r.status, r.url[:180]))
           if re.search(r"(api|search|licens|json|csv|xls)", r.url, re.I) else None)

    for url in CANDIDATES:
        print("\n" + "=" * 88)
        print("[or] %s" % url)
        try:
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(3500)
        except Exception as e:
            print("   !! %s: %s" % (type(e).__name__, str(e)[:120]))
            continue
        print("   status : %s" % (resp.status if resp else "?"))
        print("   landed : %s" % pg.url)
        print("   title  : %r" % clean(pg.title()))
        if resp and resp.status >= 400:
            continue

        try:
            body = re.sub(r"\s+", " ", pg.inner_text("body"))
        except Exception:
            body = ""
        print("   copy   : %s" % clean(body)[:420])

        # gates
        cap = pg.locator("iframe[src*='recaptcha'], .g-recaptcha, [id*='captcha' i]").count()
        gate = re.search(r"(log ?in required|sign in|payment|fee of|\$\s?\d)", body, re.I)
        print("   gates  : captcha-elements=%d  fee/login-text=%s"
              % (cap, gate.group(0)[:40] if gate else "none"))

        # anything that smells like a list or a file
        hits = []
        links = pg.locator("a")
        for i in range(min(links.count(), 400)):
            a = links.nth(i)
            try:
                href = a.get_attribute("href") or ""
                text = (a.inner_text() or "").strip()
            except Exception:
                continue
            if not href:
                continue
            if DOWNLOAD_RE.search(href) or LIST_WORDS.search(text) or LIST_WORDS.search(href):
                hits.append((text[:70], href[:170]))
        if hits:
            print("   -- candidate list/download links --")
            for t, h in hits[:25]:
                print("      %-52r %s" % (clean(t), clean(h)))
        else:
            print("   (no list/download link matched on this page)")

    print("\n" + "=" * 88)
    print("[or] network worth noting:")
    for s, u in net[:30]:
        print("   %s %s" % (s, u))
    b.close()
