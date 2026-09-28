#!/usr/bin/env python3
"""
NJ Department of Health — Drug & Medical Device Registration adapter.

    Spec target (DEAD):  https://healthapps.nj.gov/fooddrug/fdSearch.aspx
    Live source (2026-08-28): https://njgov.healthinspections.us/  (Tyler Technologies)

THIS IS NOT THE MYLICENSE PORTAL. NJ has two separate licensing worlds:
  * NJ Division of Consumer Affairs -> newjersey.mylicense.com (nj_adapter.py)
    Professional / CDS credentials. Has a bulk DOWNLOAD module.
  * NJ Department of Health -> THIS FILE
    Drug manufacturers, wholesale distributors and device registrants — the actual
    DSCSA trading partners, which MyLicense does NOT contain.

=========================== WHAT RECON FOUND ===============================
THE SOURCE HAS MIGRATED. fdSearch.aspx is now a 379-byte JavaScript stub:

    location.href = "https://njgov.healthinspections.us/";

plus an Incapsula/Imperva WAF beacon. fdResults.aspx and fdDetail.aspx are 404. NJ DOH's
own programme page (nj.gov/health/ceohs/phfpp/dmd) now links "Verify a Registration" to
the Tyler host. So the spec's premise — ASP.NET WebForms, ~4 dropdowns, __doPostBack
pagination, table scrape — is obsolete. None of the postback discipline is needed here.

The replacement is BETTER than a table scrape: a plain JSON API.

    GET /API/index.cfm/filters                     -> the filter vocabulary
    GET /API/index.cfm/clientData                  -> portal config (incl. recaptchaActive)
    GET /API/index.cfm/search/{json}/{page}        -> results, 5 per page
    GET /_templates/636/1/certificate/_report_full.cfm?permitID=N&...   -> per-record detail

  * Filter VALUES are base64-encoded: {"permitType":"RHJ1Zy9NZWRpY2Fs"} is
    base64("Drug/Medical"). Verified.
  * clientData reports "recaptchaActive": "no". No CAPTCHA, no login, no fee.
  * PAGE SIZE IS 5 and NO TOTAL IS REPORTED. The end signal is an empty page. Bounded by
    exponential probe + binary search: last non-empty page 462, first empty 463
    => ~2,314 Drug/Medical records.
  * The SEARCH payload carries only name / permit number / address / county / permit type
    / permit status. It does NOT carry the registration sub-type or expiry.
  * The CERTIFICATE page does: "Registered As" is the sub-type, plus Original/Current
    Issue Date, Expiration Date, Disciplines, DBA and any Additional Registered Addresses.

  SCOPE ANSWER: the permitType axis has only five values — Youth Camp, Frozen Desserts,
  Drug/Medical, Cottage Food, Tanning. There is no finer manufacturer/wholesaler/device
  split on that axis; the whole "Drug/Medical" type is the drug/device population. The
  finer split lives in "Registered As" on the certificate, sampled as:
        Distributor 57% | Manufacturer,Distributor 28% | Manufacturer 13% | blank 2%
  All of those are DSCSA trading-partner classes, so NOTHING is filtered out. The
  sub-type is captured as a column instead of used as a scope filter.
===========================================================================

    NOTE ON EXECUTION LOCATION
    Must run where the network can reach njgov.healthinspections.us — this machine or a
    CI runner, not the Cowork cloud sandbox.

Modes:
    python nj_drug_device.py --recon        # enumerate filters, bound the set, sample
                                            # sub-types, dump njdd_options.json; NO bulk pull
    python nj_drug_device.py                # full run -> nj_drug_device.csv (list + detail)
    python nj_drug_device.py --no-detail    # list pass only (fast, no sub-type/expiry)
    python nj_drug_device.py --limit 20     # first N pages (debug)
    python nj_drug_device.py --resume       # continue an interrupted run from the cache
    python nj_drug_device.py --permit-type "Tanning"   # a different permitType axis value

HARD RULES: read-only public search. No login, no fee, no CAPTCHA expected — every fetch
is checked, and if any appears the run STOPS and reports. Never worked around.

Setup:
    pip install requests pandas beautifulsoup4
"""
import argparse
import base64
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.parse as U

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = os.environ.get("NJ_DMD_BASE", "https://njgov.healthinspections.us")
LEGACY_URL = "https://healthapps.nj.gov/fooddrug/fdSearch.aspx"   # dead; kept for the record
PROGRAM_PAGE = "https://www.nj.gov/health/ceohs/phfpp/dmd/"

UA = os.environ.get("LHAIV_UA", (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"))

PERMIT_TYPE = "Drug/Medical"
PAGE_SIZE = 5              # confirmed at recon
PAGE_GUARD = 4000          # hard ceiling: cannot spin
LIST_DELAY = float(os.environ.get("NJ_DMD_LIST_DELAY", "0.8"))
DETAIL_DELAY = float(os.environ.get("NJ_DMD_DETAIL_DELAY", "0.45"))

OPTIONS_JSON = "njdd_options.json"
CACHE_JSON = "njdd_cache.json"
COUNTLOG = "njdd_counts.csv"
CERT_TMPL = (BASE + "/_templates/636/1/certificate/_report_full.cfm"
                    "?permitID={pid}&parentTableName=tblPermit&dsn=dhd_636_1&domainid=636")

# Certificate field labels. "Information Recorded in the System as of ..." is an
# UNLABELLED trailing sentence and "Additional Registered Addresses" is a block; both must
# act as boundaries or a value silently swallows the next field (this bit us in recon:
# "Registered As" came back as "Distributor Information Recorded in the System as of...").
CERT_LABELS = ["Registration Number", "Registered As", "Name", "DBA", "Address",
               "Original Issue Date", "Expiration Date", "Current Issue Date",
               "Disciplines", "Permit Status", "Additional Registered Addresses"]
CERT_SENTINEL = r"Information Recorded in the System as of\s*[\d/]*"
_CERT_PAT = "|".join(re.escape(x) for x in CERT_LABELS)

# Search-payload column positions, from /API/index.cfm/filters columnposition + observation
LIST_COLS = {"0": "Name", "1": "Permit Number", "2": "Address",
             "3": "County", "4": "Permit Type", "5": "Permit Status"}

STOP_WORDS = re.compile(
    r"(recaptcha/api\.js|hcaptcha\.com/1|challenges\.cloudflare\.com/turnstile|"
    r"g-recaptcha|please log ?in|sign in to continue|payment required|add to cart|"
    r"Request unsuccessful|Incapsula incident|Pardon Our Interruption)", re.I)


def log(m):
    print(f"[njdd] {m}", flush=True)


def _today():
    return _dt.date.today().isoformat()


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "en-US,en;q=0.9",
                      "Referer": BASE + "/",
                      "X-Requested-With": "XMLHttpRequest"})
    return s


def get(s, url, want_json=False, tries=3):
    """GET with a loud failure and the hard-rule guard on every response."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = s.get(url, timeout=90)
        except Exception as e:
            last = e
            time.sleep(1.5 * attempt)
            continue
        final = (r.url or "").lower()
        if any(w in final for w in ("login", "signin", "payment", "checkout", "captcha")):
            raise RuntimeError(f"STOPPING per hard rule: URL redirected to what looks like "
                               f"a login/payment/CAPTCHA ({r.url}). Do not work around it.")
        if r.status_code == 200 and STOP_WORDS.search(r.text[:4000]):
            raise RuntimeError(f"STOPPING per hard rule: CAPTCHA / WAF-challenge / login "
                               f"content detected at {r.url}. Flag for a human; never "
                               f"work around it.")
        if r.status_code == 200:
            if want_json:
                try:
                    return r.json()
                except Exception as e:
                    raise RuntimeError(f"expected JSON from {r.url} but parsing failed "
                                       f"({e}); body[:200]={r.text[:200]!r}")
            return r.text
        if r.status_code in (429, 500, 502, 503, 504) and attempt < tries:
            log(f"  HTTP {r.status_code} on {url[:70]} — backing off (attempt {attempt})")
            time.sleep(2.5 * attempt)
            last = RuntimeError(f"HTTP {r.status_code}")
            continue
        raise RuntimeError(f"HTTP {r.status_code} for {r.url} — failing loudly rather than "
                           f"writing a partial file. body[:200]={r.text[:200]!r}")
    raise RuntimeError(f"giving up on {url} after {tries} attempts: {last}")


def b64(v):
    return base64.b64encode(v.encode()).decode()


def search_url(permit_type, page, extra=None):
    f = {"permitType": b64(permit_type), "keyword": ""}
    if extra:
        f.update(extra)
    blob = json.dumps(f, separators=(",", ":"))
    return f"{BASE}/API/index.cfm/search/{U.quote(blob, safe='')}/{page}"


def parse_cert(html):
    """Flatten the certificate into fields, using every known label AND the trailing
    unlabelled sentence as boundaries."""
    t = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    out = {}
    for m in re.finditer(
            rf"({_CERT_PAT})\s*:\s*(.*?)(?=\s*(?:{_CERT_PAT})\s*:|\s*{CERT_SENTINEL}|$)", t):
        out[m.group(1)] = m.group(2).strip().strip(",").strip()
    m = re.search(r"as of\s*([\d/]+)", t)
    out["Recorded As Of"] = m.group(1) if m else ""
    return out


def flatten(rec):
    """One search record -> flat dict, preserving everything we were given."""
    row = {}
    cols = rec.get("columns") or {}
    for k, name in LIST_COLS.items():
        v = str(cols.get(k, "") or "")
        # the payload prefixes several columns with their own label
        v = re.sub(rf"^{re.escape(name)}\s*:\s*", "", v).strip()
        row[name] = v
    row["Address"] = re.sub(r"\s*\r?\n\s*", ", ", row.get("Address", "")).strip()
    row["__record_id"] = rec.get("id", "")
    pid = re.search(r"permitID=(\d+)", rec.get("PrintablePath", "") or "")
    row["__permit_id"] = pid.group(1) if pid else ""
    return row


# --------------------------------------------------------------------------
def recon(s):
    log(f"legacy spec URL : {LEGACY_URL}")
    legacy = requests.get(LEGACY_URL, headers={"User-Agent": UA}, timeout=45)
    stub = re.search(r'location\.href\s*=\s*"([^"]+)"', legacy.text)
    log(f"  -> HTTP {legacy.status_code}, {len(legacy.content)} bytes, "
        f"{'JS redirect stub -> ' + stub.group(1) if stub else 'no redirect stub'}")
    log(f"  -> the ASP.NET app is GONE; the spec's postback/table-scrape plan is obsolete")
    log(f"programme page  : {PROGRAM_PAGE}")
    log(f"live source     : {BASE}")

    cd = get(s, f"{BASE}/API/index.cfm/clientData", want_json=True)
    cfg = cd[0] if isinstance(cd, list) and cd else {}
    log(f"\nclientData.recaptchaActive = {cfg.get('recaptchaActive')!r}  "
        f"(anything but 'no' means STOP)")
    if str(cfg.get("recaptchaActive", "")).lower() not in ("no", "0", "false", ""):
        raise RuntimeError("STOPPING per hard rule: the portal reports reCAPTCHA active.")

    filters = get(s, f"{BASE}/API/index.cfm/filters", want_json=True)
    log(f"\n--- {len(filters)} filter axis(es) ---")
    for f in filters:
        vals = f.get("values") or []
        log(f"  {f.get('className')!r} (id={f.get('id')}, {f.get('type')}, "
            f"col={f.get('columnposition')}): {len(vals)} values")
        for v in vals[:26]:
            mark = "   <== drug/device scope" if v == PERMIT_TYPE else ""
            log(f"        - {v}{mark}")
        if len(vals) > 26:
            log(f"        ... +{len(vals) - 26} more")

    ptype = next((f for f in filters if f.get("className") == "permitType"), None)
    if not ptype or PERMIT_TYPE not in (ptype.get("values") or []):
        raise RuntimeError(f"permitType axis no longer offers {PERMIT_TYPE!r} — "
                           f"got {ptype.get('values') if ptype else None}. Re-scope.")
    log(f"\nSCOPE: the permitType axis is the registration-type axis, and "
        f"{PERMIT_TYPE!r} is the whole drug/device population. No finer "
        f"manufacturer/wholesaler/device split exists on this axis.")

    # bound the set: exponential probe then binary search (politer than walking 463 pages)
    log("\n--- bounding the result set (exponential probe + binary search) ---")
    hi = 1
    while hi <= PAGE_GUARD:
        j = get(s, search_url(PERMIT_TYPE, hi), want_json=True)
        n = len(j) if isinstance(j, list) else 0
        log(f"  page {hi:>5}: {n} records")
        if n == 0:
            break
        hi *= 2
        time.sleep(LIST_DELAY)
    lo = hi // 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        j = get(s, search_url(PERMIT_TYPE, mid), want_json=True)
        n = len(j) if isinstance(j, list) else 0
        log(f"  page {mid:>5}: {n} records")
        (lo, hi) = (mid, hi) if n > 0 else (lo, mid)
        time.sleep(LIST_DELAY)
    tail = get(s, search_url(PERMIT_TYPE, lo), want_json=True)
    est = lo * PAGE_SIZE + len(tail)
    log(f"  last non-empty page={lo}, first empty={hi}; page size {PAGE_SIZE}")
    log(f"  => ~{est:,} records over ~{hi} pages "
        f"(~{hi * LIST_DELAY / 60:.0f} min for the list pass)")

    # sample certificates for the sub-type vocabulary
    log("\n--- sampling certificates for the 'Registered As' sub-type ---")
    from collections import Counter
    sub, sample = Counter(), []
    for pn in {0, lo // 4, lo // 2, (3 * lo) // 4, lo}:
        for rec in get(s, search_url(PERMIT_TYPE, pn), want_json=True):
            row = flatten(rec)
            if not row["__permit_id"]:
                continue
            d = parse_cert(get(s, CERT_TMPL.format(pid=row["__permit_id"])))
            sub[d.get("Registered As", "") or "(blank)"] += 1
            sample.append((row["Name"][:34], d.get("Registration Number", ""),
                           d.get("Registered As", ""), d.get("Expiration Date", "")))
            time.sleep(DETAIL_DELAY)
        time.sleep(LIST_DELAY)
    tot = sum(sub.values()) or 1
    for k, v in sub.most_common():
        log(f"  {k[:42]:<44} {v:>3}  {100 * v / tot:.0f}%")
    log("  ALL of these are DSCSA trading-partner classes -> nothing is filtered out; "
        "the sub-type is kept as a COLUMN.")
    log("\n  sample:")
    for n_, r_, a_, e_ in sample[:8]:
        log(f"    {n_:<36} {r_:<10} {a_[:26]:<28} exp={e_}")
    log("\n  certificate adds: Registered As, Original/Current Issue Date, Expiration "
        "Date, Disciplines, DBA, Additional Registered Addresses "
        f"(+{est:,} requests, ~{est * DETAIL_DELAY / 60:.0f} min; skip with --no-detail)")

    json.dump({"recon_date": _today(), "legacy_url": LEGACY_URL,
               "legacy_status": legacy.status_code,
               "legacy_is_stub": bool(stub),
               "legacy_redirects_to": stub.group(1) if stub else None,
               "live_base": BASE, "program_page": PROGRAM_PAGE,
               "clientData": cfg, "filters": filters,
               "permit_type_in_scope": PERMIT_TYPE,
               "page_size": PAGE_SIZE, "last_page": lo, "first_empty_page": hi,
               "estimated_records": est,
               "registered_as_sample": dict(sub),
               "list_columns": LIST_COLS, "certificate_labels": CERT_LABELS},
              open(OPTIONS_JSON, "w", encoding="utf-8"), indent=2)
    log(f"\nwrote {OPTIONS_JSON}")
    log("RECON ONLY — no bulk pull performed.")


# --------------------------------------------------------------------------
def dedupe_guarded(df, key_candidates, fallback_cols, max_loss=0.10):
    """vt_adapter's guard: a key that destroys the population is a bug, not a result.
    number -> composite -> keep everything and warn."""
    before = len(df)
    key = next((k for k in key_candidates if k in df.columns and df[k].notna().any()), None)
    if key:
        out = df.drop_duplicates(subset=[key])
        pct = (before - len(out)) / before
        if pct <= max_loss:
            log(f"de-duped on {key!r}: {before} -> {len(out)} "
                f"(removed {before - len(out)}, {pct:.2%})")
            return out
        log(f"GUARD TRIPPED — de-duping on {key!r} would remove {before - len(out)} of "
            f"{before} rows ({pct:.1%} > {max_loss:.0%}); the key is probably not unique. "
            f"Trying a composite.")
        comp = [key] + [c for c in fallback_cols if c in df.columns]
        out = df.drop_duplicates(subset=comp)
        pct2 = (before - len(out)) / before
        if pct2 <= max_loss:
            log(f"de-duped on composite {comp}: {before} -> {len(out)} ({pct2:.2%})")
            return out
        log(f"GUARD TRIPPED AGAIN — composite {comp} still removes {pct2:.1%}. "
            f"KEEPING ALL {before} ROWS. Registration numbers may restart per type "
            f"(as in Colorado); inspect before assuming duplicates.")
        return df
    log(f"no registration-number column found (cols={list(df.columns)[:8]}) — "
        f"de-duping whole rows only. CONFIRM headers.")
    out = df.drop_duplicates()
    log(f"whole-row de-dupe: {before} -> {len(out)}")
    return out


def full(s, out_path, want_detail=True, limit=0, resume=False):
    cd = get(s, f"{BASE}/API/index.cfm/clientData", want_json=True)
    cfg = cd[0] if isinstance(cd, list) and cd else {}
    if str(cfg.get("recaptchaActive", "")).lower() not in ("no", "0", "false", ""):
        raise RuntimeError("STOPPING per hard rule: portal reports reCAPTCHA active.")

    filters = get(s, f"{BASE}/API/index.cfm/filters", want_json=True)
    ptype = next((f for f in filters if f.get("className") == "permitType"), None)
    if not ptype or PERMIT_TYPE not in (ptype.get("values") or []):
        raise RuntimeError(f"permitType no longer offers {PERMIT_TYPE!r} "
                           f"(got {ptype.get('values') if ptype else None}) — run --recon.")

    cache = {"rows": [], "pages": [], "details": {}}
    if resume and os.path.exists(CACHE_JSON):
        cache = json.load(open(CACHE_JSON, encoding="utf-8"))
        log(f"resuming: {len(cache['rows'])} rows, {len(cache['details'])} details cached")

    # ---- list pass
    page_log = list(cache["pages"])
    rows = list(cache["rows"])
    seen = {r["__record_id"] for r in rows if r.get("__record_id")}
    start = len(page_log)
    if start:
        log(f"list pass resuming at page {start}")
    n = start
    while n < PAGE_GUARD:
        if limit and n >= start + limit:
            log(f"--limit {limit}: stopping the list pass at page {n}")
            break
        j = get(s, search_url(PERMIT_TYPE, n), want_json=True)
        got = len(j) if isinstance(j, list) else 0
        new = 0
        for rec in (j or []):
            row = flatten(rec)
            if row["__record_id"] and row["__record_id"] in seen:
                continue
            seen.add(row["__record_id"])
            rows.append(row)
            new += 1
        page_log.append({"page": n, "records": got, "new": new})
        if n % 25 == 0 or got == 0:
            log(f"  page {n:>4}: {got} records ({new} new)   running total {len(rows):,}")
        if got == 0:
            log(f"  empty page at {n} -> end of results")
            break
        n += 1
        cache.update({"rows": rows, "pages": page_log})
        if n % 20 == 0:
            json.dump(cache, open(CACHE_JSON, "w", encoding="utf-8"))
        time.sleep(LIST_DELAY)
    else:
        raise RuntimeError(f"page guard {PAGE_GUARD} hit without an empty page — refusing "
                           f"to spin. Investigate the end-of-results signal.")

    log(f"list pass done: {len(rows):,} records over {len(page_log)} pages")
    if not rows:
        raise RuntimeError("0 records from the list pass — refusing to write an empty file.")
    cache.update({"rows": rows, "pages": page_log})
    json.dump(cache, open(CACHE_JSON, "w", encoding="utf-8"))

    # ---- detail pass
    if want_detail:
        todo = [r for r in rows if r["__permit_id"]
                and r["__permit_id"] not in cache["details"]]
        log(f"detail pass: {len(todo):,} certificate(s) to fetch "
            f"({len(cache['details']):,} already cached), "
            f"~{len(todo) * DETAIL_DELAY / 60:.0f} min")
        failed = 0
        for i, r in enumerate(todo, 1):
            try:
                d = parse_cert(get(s, CERT_TMPL.format(pid=r["__permit_id"])))
                cache["details"][r["__permit_id"]] = d
            except RuntimeError as e:
                if "hard rule" in str(e).lower():
                    raise
                failed += 1
                if failed <= 5:
                    log(f"  certificate {r['__permit_id']} failed: {str(e)[:90]}")
            if i % 200 == 0:
                log(f"  detail {i:,}/{len(todo):,} ({failed} failed)")
                json.dump(cache, open(CACHE_JSON, "w", encoding="utf-8"))
            time.sleep(DETAIL_DELAY)
        json.dump(cache, open(CACHE_JSON, "w", encoding="utf-8"))
        log(f"detail pass done: {len(cache['details']):,} certificates, {failed} failed")
        if failed and failed > 0.10 * max(len(todo), 1):
            log(f"WARNING — {failed} of {len(todo)} certificates failed (>10%). "
                f"Sub-type/expiry coverage is incomplete; inspect before trusting.")

        for r in rows:
            d = cache["details"].get(r["__permit_id"], {})
            r["Registration Number"] = d.get("Registration Number", "")
            r["Registered As"] = d.get("Registered As", "")
            r["DBA"] = d.get("DBA", "")
            r["Certificate Address"] = d.get("Address", "")
            r["Original Issue Date"] = d.get("Original Issue Date", "")
            r["Current Issue Date"] = d.get("Current Issue Date", "")
            r["Expiration Date"] = d.get("Expiration Date", "")
            r["Disciplines"] = d.get("Disciplines", "")
            r["Additional Registered Addresses"] = d.get("Additional Registered Addresses", "")
            r["Certificate Permit Status"] = d.get("Permit Status", "")
            r["Recorded As Of"] = d.get("Recorded As Of", "")
    else:
        log("--no-detail: skipping certificates. The output will have NO registration "
            "sub-type ('Registered As') and NO expiry.")

    # everything as string, so leading zeros cannot be coerced away
    df = pd.DataFrame(rows).astype(str).replace({"nan": "", "None": ""})
    df["State"] = "NJ"
    df["__source"] = f"NJ DOH Drug & Medical Device Registration ({BASE}, permitType={PERMIT_TYPE})"
    df["__extract_date"] = _today()

    df = dedupe_guarded(df,
                        key_candidates=["Registration Number", "Permit Number"],
                        fallback_cols=["Registered As", "Name"])

    if df.empty:
        raise RuntimeError("0 rows after de-dupe — refusing to write an empty file.")

    # per-type / per-status counts
    if "Registered As" in df.columns:
        log("\nper-'Registered As' counts:")
        for k, v in df["Registered As"].replace("", "(blank)").value_counts().items():
            log(f"  {str(k)[:44]:<46} {v:>6,}")
    log("\nper-'Permit Status' counts:")
    for k, v in df["Permit Status"].replace("", "(blank)").value_counts().items():
        log(f"  {str(k)[:44]:<46} {v:>6,}")

    pd.DataFrame(page_log).to_csv(COUNTLOG, index=False)
    log(f"\nwrote {COUNTLOG}: per-page count log ({len(page_log)} pages)")
    df.to_csv(out_path, index=False, encoding="utf-8")
    log(f"wrote {out_path}: {len(df):,} rows x {len(df.columns)} cols")

    log("\nVERIFY before trusting (a clean run is NOT verification):")
    log("  1. pagination reached the end — the count log's last page must be EMPTY.")
    log("  2. row count ~= page_count * 5 (no silently dropped page).")
    log("  3. a known NJ wholesaler present (e.g. CARDINAL HEALTH / ABBVIE / APOTEX).")
    log("  4. leading zeros intact in the RAW BYTES, not just via pandas.")
    log("  5. 'Registered As' populated for ~all rows (Distributor/Manufacturer mix).")
    return df


# --------------------------------------------------------------------------
def main():
    global PERMIT_TYPE
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon", action="store_true")
    ap.add_argument("--no-detail", action="store_true",
                    help="list pass only — no sub-type or expiry")
    ap.add_argument("--limit", type=int, default=0, help="first N list pages (debug)")
    ap.add_argument("--resume", action="store_true", help="continue from njdd_cache.json")
    ap.add_argument("--permit-type", default=PERMIT_TYPE)
    ap.add_argument("--out", default="nj_drug_device.csv")
    args = ap.parse_args()

    PERMIT_TYPE = args.permit_type

    s = session()
    if args.recon:
        recon(s)
    else:
        full(s, args.out, want_detail=not args.no_detail,
             limit=args.limit, resume=args.resume)


if __name__ == "__main__":
    main()
