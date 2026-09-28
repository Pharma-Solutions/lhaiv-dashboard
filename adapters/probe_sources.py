#!/usr/bin/env python3
"""
LHAIV source pressure-tester.

Given the worklist (state, agency, URL), it visits each source and classifies HOW the
full-population data can be obtained — WITHOUT ever purchasing, submitting a form, or
solving a CAPTCHA. It is read-only reconnaissance whose output is a tier verdict per source:

    Tier 1  bulk download / open-data / API .............. automate
    Tier 2  purchase, records request, or roster/list req  buy (human gate)
    Tier 3  enumerable no-CAPTCHA lookup portal .......... scrape (one adapter per platform)
    Tier 4  CAPTCHA / single-record only ................. Hermai candidate (last resort)

    downloadable/extractable = Y (Tier 1) | Purchase/Records (Tier 2) |
                               Maybe-scrape (Tier 3) | N-without-Hermai (Tier 4)

    ┌─────────────────────────  WHERE THIS RUNS  ─────────────────────────┐
    │ Must run where the network can reach the state portals — YOUR machine │
    │ or a GitHub Actions runner. It will NOT work from the Cowork cloud     │
    │ sandbox (those hosts are blocked there, same as the Vermont adapter).  │
    └────────────────────────────────────────────────────────────────────┘

It NEVER: solves/bypasses CAPTCHAs, logs in, submits a form, pays a fee, or downloads a
paid file. Those are human-gated actions. The tool only reads public pages and reports.

Setup (on the machine that runs it):
    pip install playwright pandas openpyxl requests beautifulsoup4
    python -m playwright install chromium

Usage:
    python probe_sources.py --in LHAIV_Source_PressureTest_Worklist.xlsx --out probe_results.xlsx
    python probe_sources.py --in worklist.xlsx --only "E,F"      # only probe buckets E & F
    python probe_sources.py --url https://example.gov/lookup     # one-off single URL
    python probe_sources.py --in worklist.xlsx --no-browser      # static fetch only (fast, no JS)
"""
import os, re, sys, json, time, argparse, urllib.parse as _url

import requests
import pandas as pd
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) LHAIV-source-recon/1.0 "
      "(read-only licensing-data feasibility check)")
TIMEOUT = 25
POLITE_DELAY = 1.5  # seconds between hosts — be a good citizen

# ---- signal vocabularies (evidence, not proof — a human confirms the verdict) ----
DL_EXT   = re.compile(r"\.(csv|xlsx?|zip|txt|tsv|json)(\?|$)", re.I)
DL_WORDS = re.compile(r"\b(download|data ?set|open ?data|export|bulk|full list|entire list|"
                      r"data ?list|licensee list|roster download|csv|excel|spreadsheet)\b", re.I)
REC_WORDS= re.compile(r"\b(public records|records request|open records|freedom of information|"
                      r"foia|foil|cpra|ipra|rtkl|opra|data request|list request|request form|"
                      r"records officer|records custodian)\b", re.I)
FEE_WORDS= re.compile(r"(\$\s?\d|fee|cost|per (list|record|file)|payment|purchase)", re.I)
CAPTCHA  = re.compile(r"(recaptcha|g-recaptcha|grecaptcha|hcaptcha|captcha|cf-turnstile|"
                      r"are you (a )?human|bot detection)", re.I)
ONE_REC  = re.compile(r"(single record|one record at a time|enter a (name|license)|"
                      r"maximum.{0,12}results|results.{0,6}limited|not.{0,20}bulk)", re.I)
SEARCHY  = re.compile(r"(search|look ?up|verify a? ?licen|find a (professional|licensee))", re.I)

# platform fingerprints -> reusable adapter family
PLATFORMS = [
    ("Socrata (open-data API)", re.compile(r"(/resource/|/api/views|socrata|data\.\w+\.gov)", re.I)),
    ("Thentia",       re.compile(r"thentiacloud|thentia", re.I)),
    ("MyLicense (ASP.NET)", re.compile(r"mylicense|licensee?verif|\.aspx", re.I)),
    ("igovSolution",  re.compile(r"igovsolution|/online/", re.I)),
    ("Tyler/eLicense",re.compile(r"elicense|tyler ?tech|licenseeconnect", re.I)),
    ("Pega",          re.compile(r"/prweb/|pega", re.I)),
    ("PA PALS",       re.compile(r"pals\.pa\.gov", re.I)),
    ("LLR Online",    re.compile(r"llronline", re.I)),
]

def _platform(html, url):
    blob = (url + " " + html[:4000]).lower()
    for name, rx in PLATFORMS:
        if rx.search(blob):
            return name
    return ""

def classify(html, url, captcha_seen=False):
    """Return dict: tier, downloadable, platform, signals[]. Heuristic — a human confirms."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True) if html else ""
    hay = (html or "") + " " + text
    sig = []
    # download links in the actual anchors
    dl_links = []
    try:
        for a in BeautifulSoup(html or "", "html.parser").find_all("a", href=True):
            if DL_EXT.search(a["href"]):
                dl_links.append(a["href"])
    except Exception:
        pass
    has_captcha = captcha_seen or bool(CAPTCHA.search(hay))
    has_dl   = bool(dl_links) or bool(DL_WORDS.search(hay))
    has_rec  = bool(REC_WORDS.search(hay))
    has_fee  = bool(FEE_WORDS.search(hay))
    has_one  = bool(ONE_REC.search(hay))
    has_srch = bool(SEARCHY.search(hay))
    plat     = _platform(html or "", url)

    if dl_links: sig.append(f"download-links: {dl_links[:3]}")
    if DL_WORDS.search(hay):  sig.append("bulk/download language")
    if has_rec:  sig.append("records/list-request language")
    if has_fee:  sig.append("fee/purchase language")
    if has_captcha: sig.append("CAPTCHA present")
    if has_one:  sig.append("single-record/limit language")
    if has_srch: sig.append("search/lookup interface")
    if plat:     sig.append(f"platform: {plat}")

    # verdict priority — most favorable achievable path first
    if has_dl and dl_links:
        tier, dl = "1", "Y (direct file)"
    elif "Socrata" in plat:
        tier, dl = "1", "Y (open-data API)"
    elif has_rec or (has_fee and has_dl):
        tier, dl = "2", "Purchase/Records"
    elif has_captcha:
        tier, dl = "4", "N without Hermai (CAPTCHA)"
    elif has_one and has_srch:
        tier, dl = "4", "N without Hermai (single-record)"
    elif has_srch:
        tier, dl = "3", "Maybe (scrape enumerable portal)"
    else:
        tier, dl = "?", "Undetermined — human review"
    return {"tier": tier, "downloadable": dl, "platform": plat, "signals": "; ".join(sig)}

def probe_static(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.text

def probe_browser(url):
    """Render JS/SPA pages (MyLicense, Thentia, Pega, PALS). Import lazily so --no-browser needs no Playwright."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        launch = {}
        cp = os.environ.get("CHROMIUM_PATH")
        if cp: launch["executable_path"] = cp
        b = p.chromium.launch(headless=True, **launch)
        pg = b.new_page(user_agent=UA)
        captcha = False
        try:
            pg.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            try: pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception: pass
        pg.wait_for_timeout(2500)
        html = ""
        try: html = pg.content()
        except Exception: pass
        # DOM-level captcha check (iframes/scripts the static HTML may miss)
        try:
            if pg.query_selector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .g-recaptcha, #captcha"):
                captcha = True
        except Exception:
            pass
        b.close()
        return html, captcha

def probe_one(url, use_browser=True):
    verdict = {"tier": "?", "downloadable": "unreachable", "platform": "", "signals": "", "error": ""}
    try:
        html = probe_static(url)
        verdict = classify(html, url)
        # escalate to a real browser if the static page looks like an empty JS shell or a portal
        thin = len(BeautifulSoup(html, "html.parser").get_text(strip=True)) < 400
        if use_browser and (thin or verdict["tier"] in ("3", "4", "?")):
            bhtml, cap = probe_browser(url)
            if bhtml:
                verdict = classify(bhtml, url, captcha_seen=cap)
                verdict["signals"] = "[browser] " + verdict["signals"]
    except Exception as e:
        if use_browser:
            try:
                bhtml, cap = probe_browser(url)
                verdict = classify(bhtml, url, captcha_seen=cap)
                verdict["signals"] = "[browser-only] " + verdict["signals"]
            except Exception as e2:
                verdict["error"] = f"static:{e} | browser:{e2}"
        else:
            verdict["error"] = str(e)
    return verdict

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", help="worklist .xlsx (cols State/Agency/URL)")
    ap.add_argument("--out", default="probe_results.xlsx")
    ap.add_argument("--url", help="probe a single URL and print the verdict")
    ap.add_argument("--only", default="", help="comma list of bucket letters to probe, e.g. E,F")
    ap.add_argument("--no-browser", action="store_true", help="static fetch only (no Playwright)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    use_browser = not args.no_browser

    if args.url:
        print(json.dumps(probe_one(args.url, use_browser), indent=2)); return

    df = pd.read_excel(args.inp, sheet_name="Source Pressure-Test", header=5, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    def col(sub):
        for c in df.columns:
            if sub.lower() in c.lower(): return c
    cS, cA, cU, cB = col("State"), col("Agency"), col("URL"), col("Bucket")
    df = df[df[cS].notna() & (df[cS].astype(str).str.strip() != "")]
    if args.only:
        letters = tuple(x.strip().upper() for x in args.only.split(","))
        df = df[df[cB].astype(str).str.strip().str.startswith(letters)]
    if args.limit:
        df = df.head(args.limit)

    out = []
    total = len(df)
    for n, (_, r) in enumerate(df.iterrows(), 1):
        url = str(r.get(cU, "")).strip()
        st, ag = str(r.get(cS, "")).strip(), str(r.get(cA, "")).strip()
        if not url or not url.lower().startswith("http"):
            v = {"tier": "?", "downloadable": "no-URL", "platform": "", "signals": "", "error": "no url in row"}
        else:
            print(f"[{n}/{total}] {st} · {ag}\n         {url}", flush=True)
            v = probe_one(url, use_browser)
            print(f"         -> Tier {v['tier']} | {v['downloadable']} | {v.get('platform','')}", flush=True)
            time.sleep(POLITE_DELAY)
        out.append({"State": st, "Agency": ag, "URL": url,
                    "Probe Tier": v["tier"], "Downloadable/Extractable": v["downloadable"],
                    "Platform": v.get("platform", ""), "Signals": v.get("signals", ""),
                    "Probe error": v.get("error", "")})
    res = pd.DataFrame(out)
    res.to_excel(args.out, index=False)
    print(f"\nwrote {args.out}: {len(res)} sources")
    print("\nTier tally:")
    print(res["Probe Tier"].value_counts().to_string())
    print("\nReminder: verdicts are heuristic evidence. A human confirms each before it drives a purchase or an adapter build.")

if __name__ == "__main__":
    main()
