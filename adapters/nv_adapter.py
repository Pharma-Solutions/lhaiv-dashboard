#!/usr/bin/env python3
"""
Nevada State Board of Pharmacy — facility licence adapter.

    Portal : https://online.nvbop.org/#/verifylicense  (Inlumon Angular SPA)
    API    : https://ws.nvbop.org/api/                 (from app.web-api-url.constant.js)

The handoff planned to drive "Export to Excel" through the browser. Recon (2026-09-18)
found the SPA is backed by a plain JSON API, which is both simpler and STRICTLY BETTER
than the export, because the export/grid drops fields the API returns:

    POST /api/Provider/IndividualProviderVerifySearchWithPage
         ?PageNumber=1&NoOfRecords=<n>&ShowAllRecords=true
    body {"IsIndividualSearch": false, "IsFirmSearch": true,
          "ProviderTypeId": "<id>", ...all other filters empty}

THREE HANDOFF CAVEATS THAT DO NOT APPLY TO THE API PATH:
  * "no street address (city/state only)" -> the API returns Address1, Address2, Zip.
  * "no issue date"                       -> the API returns LicenseEffectiveDate AND
                                             OriginalLicenseDate.
  * the public dropdown lists 11 facility types; the API's own reference table lists 14
    (it adds Dispensing Site, Medi-Spa, Pseudo School License). We enumerate the API's
    list at runtime, so the extra three are not silently missed.

ShowAllRecords=true returns the whole type in ONE call (no pagination), which we still
cross-check by re-requesting with paging off and comparing counts.

    NOTE ON EXECUTION LOCATION
    Runs where the network can reach ws.nvbop.org - this machine or CI.

GUARDRAILS: read-only. No login, no fee, no CAPTCHA (recon confirmed none). If any
appears, the run STOPS and flags rather than working around it.

Usage:
    python nv_adapter.py --recon      # enumerate types + per-type counts, write nothing
    python nv_adapter.py              # full pull -> data\\NV\\board-of-pharmacy\\...
    python nv_adapter.py --limit 2    # first N types (debug)
"""
import argparse
import datetime as _dt
import json
import os
import re
import sys
import time

import pandas as pd
import requests

API = os.environ.get("NV_API_BASE", "https://ws.nvbop.org/api")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://online.nvbop.org/",
           "Origin": "https://online.nvbop.org",
           "Content-Type": "application/json",
           "Accept": "application/json, text/plain, */*"}

OUT_DIR = os.environ.get("NV_OUT_DIR", r"C:\Verified\data\NV\board-of-pharmacy")
SLUG = "nv-board-of-pharmacy"
TYPES_JSON = "nv_provider_types.json"
COUNTLOG = "nv_counts.csv"
POLITE = 1.0
BIG = 100000          # ShowAllRecords still wants a NoOfRecords ceiling

STOP_RX = re.compile(r"(recaptcha|hcaptcha|turnstile|please log ?in|sign in to continue|"
                     r"payment required|Access Denied)", re.I)


def log(m):
    print(f"[nv] {m}", flush=True)


def _today():
    return _dt.date.today().isoformat()


def _stamp_date():
    return _dt.date.today().strftime("%Y%m%d")


def _check(resp, what):
    if STOP_RX.search(resp.text[:3000]):
        raise RuntimeError(f"STOPPING per guardrail: login/CAPTCHA/paywall content in "
                           f"{what} ({resp.url}). Never work around it - flag it.")
    if resp.status_code != 200:
        raise RuntimeError(f"{what}: HTTP {resp.status_code} for {resp.url} - failing "
                           f"loudly rather than writing a partial file. "
                           f"body[:200]={resp.text[:200]!r}")


def get_types(s):
    """Enumerate the facility types from the API's OWN reference table (runtime discovery,
    like vt_adapter's dropdown walk) rather than hard-coding the 11 the dropdown shows."""
    r = s.get(f"{API}/TypeValues/RefProviderLicenseTypeGetAll/null", timeout=60)
    _check(r, "type list")
    lst = r.json().get("RefProviderLicenseTypeList") or []
    types = [{"id": str(t["ProviderLicenseTypeId"]),
              "code": str(t.get("ProviderLicenseTypeCode", "")),
              "name": t["ProviderLicenseTypeName"]}
             for t in lst if t.get("IsActive") and not t.get("IsDeleted")]
    if not types:
        raise RuntimeError("no active facility types returned - the reference table "
                           "changed shape. Re-run recon.")
    # enum-drift guard (same pattern as vt/nj/wv)
    prev = json.load(open(TYPES_JSON)) if os.path.exists(TYPES_JSON) else {}
    cur = {t["id"]: t["name"] for t in types}
    added = [cur[k] for k in cur if k not in prev]
    removed = [prev[k] for k in prev if k not in cur]
    if prev and (added or removed):
        log(f"ENUM DRIFT - added={added} removed={removed} (review; continuing)")
    json.dump(cur, open(TYPES_JSON, "w"), indent=2)
    return types


def search(s, type_id, show_all=True, n=BIG, page=1):
    body = {"LastName": "", "FirstName": "", "LicenseNumber": "",
            "IsIndividualSearch": False, "IsFirmSearch": True,
            "State": "", "Zip": "", "LicenseTypeId": "", "CorporationName": "",
            "City": "", "StreetAddress": "", "ProviderTypeId": str(type_id)}
    params = {"PageNumber": page, "NoOfRecords": n,
              "ShowAllRecords": "true" if show_all else "false"}
    r = s.post(f"{API}/Provider/IndividualProviderVerifySearchWithPage",
               params=params, json=body, timeout=180)
    _check(r, f"search type={type_id}")
    return r.json().get("IndividualProviderVerifySearchList") or []


# --- shared scaffolding (dedupe_util.py is referenced in the handoff but is NOT in the
# repo; this mirrors the guard used by tier1_fetch.py / nj_adapter.py) -----------------
def dedupe_guarded(df, key, tiebreak, max_loss=0.10):
    """Refuse a key that destroys the population, and prefer the key that PRESERVES most.

    Two lessons already paid for on this project: Colorado's licence numbers restart per
    category (number-only de-dupe silently deleted 53%), and NJ carries status history
    that a bare number key discards. So: blank keys are never collapsed, and we pick the
    key with the lowest loss rather than the first one under the threshold.
    """
    before = len(df)
    if key not in df.columns:
        out = df.drop_duplicates()
        log(f"no {key!r} column - whole-row de-dupe {before} -> {len(out)}; CONFIRM headers")
        return out
    blank = df[key].fillna("").astype(str).str.strip() == ""
    keyed, blanks = df[~blank], df[blank]
    if len(blanks):
        log(f"  {len(blanks):,} row(s) have a blank {key!r} - de-duped whole-row, "
            f"NOT collapsed into one")
    ladder, results = [], []
    for i in range(len(tiebreak) + 1):
        cand = list(dict.fromkeys([key] + [c for c in tiebreak[:i] if c in keyed.columns]))
        if cand not in ladder:
            ladder.append(cand)
    for cand in ladder:
        d = keyed.drop_duplicates(subset=cand)
        loss = 1 - len(d) / len(keyed) if len(keyed) else 0
        results.append((cand, d, loss))
        log(f"  candidate key {cand} -> {len(d):,} of {len(keyed):,} (loss {loss:.3%})")
    best = min(r[2] for r in results)
    chosen, kept, loss = next((c, d, l) for c, d, l in results if l <= best + 0.0005)
    if loss > max_loss:
        log(f"  GUARD TRIPPED: best key {chosen} still loses {loss:.1%} - KEEPING ALL ROWS")
        kept, chosen = keyed, ["(none - all rows kept)"]
    out = pd.concat([kept, blanks.drop_duplicates()], ignore_index=True)
    log(f"de-duped on {chosen}: {before:,} -> {len(out):,}")
    return out


def recon(s):
    types = get_types(s)
    log(f"{len(types)} active facility type(s) from the API reference table:")
    total = 0
    rows = []
    for t in types:
        recs = search(s, t["id"])
        n = len(recs)
        total += n
        rows.append({"type_id": t["id"], "type": t["name"], "rows": n})
        log(f"  id={t['id']:<4} {t['name'][:42]:<44} {n:>6,}")
        time.sleep(POLITE)
    log(f"  {'TOTAL':<49} {total:>6,}")
    log("")
    log("field availability (the handoff expected these to be MISSING):")
    sample = search(s, types[0]["id"])[:1]
    if sample:
        r = sample[0]
        for f in ("Address1", "Address2", "Zip", "LicenseEffectiveDate",
                  "OriginalLicenseDate", "LicenseExpirationDate", "Contact",
                  "ContactEmail", "Discipline", "LicenseStatusTypeName"):
            log(f"  {f:<24} {'PRESENT' if f in r else 'absent':<8} e.g. {str(r.get(f))[:40]}")
    pd.DataFrame(rows).to_csv(COUNTLOG, index=False)
    log(f"\nwrote {COUNTLOG}. RECON ONLY - no roster written.")


def full(s, limit=0):
    types = get_types(s)
    if limit:
        types = types[:limit]
        log(f"--limit active: first {len(types)} type(s)")
    frames, counts = [], []
    for i, t in enumerate(types, 1):
        recs = search(s, t["id"])
        if not recs:
            log(f"  [{i}/{len(types)}] {t['name']}: 0 rows")
            counts.append({"type_id": t["id"], "type": t["name"], "rows": 0})
            time.sleep(POLITE)
            continue
        # everything as string so leading zeros / alphanumeric licence numbers survive
        df = pd.DataFrame(recs).astype(str).replace({"None": "", "nan": ""})
        df["__facility_type"] = t["name"]
        df["__provider_type_id"] = t["id"]
        # cross-check: paged request must agree with the ShowAllRecords pull
        paged = search(s, t["id"], show_all=False, n=BIG)
        if len(paged) != len(recs):
            log(f"    NOTE {t['name']}: ShowAllRecords={len(recs)} vs paged={len(paged)} "
                f"- using ShowAllRecords (the larger/complete set)")
        frames.append(df)
        counts.append({"type_id": t["id"], "type": t["name"], "rows": len(df)})
        log(f"  [{i}/{len(types)}] {t['name'][:42]:<44} {len(df):>6,} rows")
        time.sleep(POLITE)

    if not frames:
        raise RuntimeError("no rows from any facility type - refusing to write an "
                           "empty roster.")
    allrows = pd.concat(frames, ignore_index=True)
    log(f"stacked: {len(allrows):,} rows")

    allrows["State"] = "NV"
    allrows["__source"] = "NV State Board of Pharmacy (ws.nvbop.org facility verify API)"
    allrows["__extract_date"] = _today()

    # keep the facility type in the key so one number under two types is not collapsed
    allrows = dedupe_guarded(allrows, "LicenseNumber",
                             ["__facility_type", "LicenseStatusTypeName",
                              "LicenseExpirationDate"])
    if allrows.empty:
        raise RuntimeError("0 rows after de-dupe - refusing to write an empty roster.")

    log("")
    log("per-type row counts (post-dedupe):")
    for k, v in allrows["__facility_type"].value_counts().items():
        log(f"  {str(k)[:44]:<46} {v:>6,}")
    log("")
    log("licence-status spread:")
    for k, v in allrows["LicenseStatusTypeName"].replace("", "(blank)").value_counts().items():
        log(f"  {str(k)[:44]:<46} {v:>6,}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{SLUG} - Complete - {_stamp_date()}.csv")
    allrows.to_csv(out, index=False, encoding="utf-8")
    pd.DataFrame(counts).to_csv(COUNTLOG, index=False)
    log(f"\nwrote {out}: {len(allrows):,} rows x {len(allrows.columns)} cols")
    log(f"wrote {COUNTLOG}: per-type count log")
    log("\nVERIFY (a clean run is not verification):")
    log("  1. per-type counts above match a --recon run;")
    log("  2. street address populated (Address1) - the grid/Excel export omits it;")
    log("  3. a known NV wholesaler present (e.g. AMERICAN REGENT / APOTEX);")
    log("  4. leading zeros intact in the raw bytes;")
    log("  5. both active and non-active statuses present.")
    return allrows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    s = requests.Session()
    s.headers.update(HEADERS)
    if args.recon:
        recon(s)
    else:
        full(s, args.limit)


if __name__ == "__main__":
    main()
