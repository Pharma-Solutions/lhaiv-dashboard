#!/usr/bin/env python3
"""
LHAIV Tier-1 direct-fetch adapters: Colorado (Socrata API), New York BNE,
Virgin Islands, West Virginia (two daily roster CSVs).

These four sources are a file or an API — no browser, no form, no postback, no CAPTCHA —
so they are small HTTP fetchers rather than Playwright portals like vt_adapter.py /
nj_adapter.py. One shared shape, four thin implementations writing five files:

    fetch -> validate -> normalize -> write CSV (fail-safe)

The shared scaffolding (stamping, natural-key de-dupe, refuse-empty, count regression,
no-silent-truncation) is the reusable part: ~22 of the Tier-1 "free bulk file" agencies
are the same download-and-validate shape.

    NOTE ON EXECUTION LOCATION
    These public files/APIs are reachable from most networks, but still run from your
    machine or a CI runner — the Cowork cloud sandbox blocks state hosts, same as the
    Vermont and New Jersey adapters.

Modes:
    python tier1_fetch.py --recon                 # inspect every source; NO files written
    python tier1_fetch.py                         # fetch them all
    python tier1_fetch.py --source co             # one source (co | ny | vi | wv | all)
    python tier1_fetch.py --source wv            # both WV rosters -> wv_individual.csv + wv_facility.csv
    python tier1_fetch.py --source co --scope dscsa   # CO: facilities only (~8.5k)
    python tier1_fetch.py --source co --scope all      # CO: entire 1.6M-row dataset
    python tier1_fetch.py --recon --source co     # recon one source

First-run protocol (SAME AS VT/NJ): run --recon FIRST and read what it prints. It
confirms the Colorado resource id + the live prefix counts, discovers the NY/VI download
anchors rather than trusting a hard-coded path, and reports whether each WV roster key is
actually unique before anything is de-duped on it.

Setup:
    pip install requests pandas openpyxl beautifulsoup4

WHY THE DEFAULTS LOOK LIKE THEY DO (confirmed at recon 2026-08-27/28, not assumed):
  * Colorado's `licensetype` field is really `licensePrefix` — opaque CODES (PDO, WHO,
    MFR, TPLP), not words. A `like '%PHARM%'` filter matches ZERO rows. `subcategory` is
    empty for every pharmacy row. So scope is an explicit, discovered prefix allowlist.
  * www.health.ny.gov returns 403 to a bot User-Agent; it needs a browser-style UA.
  * The NY workbook's real header is the SECOND row (the first is a "valid as of" banner),
    and LICENSEE NAME appears TWICE — the 2nd column is a name CONTINUATION, not a
    duplicate. Dropping it truncates 2,068 names.
  * VI has license numbers shared by DIFFERENT people (398, 437), so de-duping on the
    number alone would delete real licensees. VI keys on number + name.
  * WV serves two daily CSV rosters whose KEY COLUMN NAMES DIFFER ('License Number#' vs
    'License Number'); both are fully unique, but the '#' variant is matched by neither
    pick_key() nor DEDUPE_KEYS, so both are passed explicitly. WV does NOT 403 a bot UA.
    Both rosters are ACTIVE-ONLY.
"""
import argparse
import datetime as _dt
import io
import json
import os
import re
import sys
import urllib.parse as _urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

# --- shared config ---------------------------------------------------------
# Browser-style UA: www.health.ny.gov 403s anything that looks like a bot (confirmed).
UA = os.environ.get("LHAIV_UA", (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36 LHAIV-tier1-adapter/1.0 (licensing-data ingestion)"))
HEADERS = {"User-Agent": UA}
TIMEOUT = 120

# Natural-key candidates, checked in order (same idea as vt/nj DEDUPE_KEYS).
DEDUPE_KEYS = ["licensenumber", "License Number", "LicenseNumber", "License #",
               "License No", "License_No", "Registration Number", "Credential Number",
               "Number"]

RECON_ONLY = False


def log(src, m):
    print(f"[{src}] {m}", flush=True)


def _today():
    # a normal CLI run may stamp today's date
    return _dt.date.today().isoformat()


def fetch(url, params=None, expect="bytes", src="net"):
    """GET with a loud failure. Never returns a partial body as if whole."""
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT,
                     allow_redirects=True)
    # Guardrail: these three sources have no login/fee/CAPTCHA. If one appears, stop.
    final = (r.url or "").lower()
    if any(w in final for w in ("login", "signin", "payment", "checkout", "captcha")):
        raise RuntimeError(
            f"{src}: URL redirected to what looks like a login/payment/CAPTCHA "
            f"({r.url}) — STOPPING per guardrail. Do not work around this; flag it.")
    if r.status_code != 200:
        raise RuntimeError(f"{src}: HTTP {r.status_code} for {r.url} "
                           f"— failing loudly rather than writing a partial file. "
                           f"body[:200]={r.text[:200]!r}")
    return r.content if expect == "bytes" else r.text


# --- shared scaffolding ----------------------------------------------------
def stamp(df, state, source, extract_date=None, valid_as_of=None):
    """Every row carries where it came from and when."""
    df = df.copy()
    df["State"] = state
    df["__source"] = source
    df["__extract_date"] = extract_date or _today()
    if valid_as_of:
        df["__valid_as_of"] = valid_as_of
    return df


def pick_key(df, extra=()):
    for k in list(extra) + DEDUPE_KEYS:
        if k in df.columns:
            return k
    return None


def dedupe(df, src, key_cols=None, max_loss_pct=0.10):
    """De-dupe on the natural key, and REFUSE a key that destroys the population.

    key_cols=[...]  use exactly these columns
    key_cols="row"  de-dupe on the whole row (lossless; use where the source's row
                    grain is finer than one-row-per-licence, e.g. discipline history)
    key_cols=None   auto-pick from DEDUPE_KEYS

    The max_loss_pct guard exists because a WRONG key looks exactly like a clean run.
    Colorado taught us this the hard way: `licensenumber` restarts from 1 within each
    licence prefix, so de-duping on it alone silently deleted 53% of rows and merged
    unrelated businesses (Kuehne + Nagel vs Joy Pharmacy both had number 100). A key
    that drops more than max_loss_pct of the rows is treated as a bug, not a result.
    """
    before = len(df)
    if before == 0:
        return df

    if key_cols == "row":
        out = df.drop_duplicates()
        log(src, f"de-duped on WHOLE ROWS (row grain is finer than one-per-licence): "
                 f"{before} -> {len(out)}")
        return out

    if key_cols:
        missing = [c for c in key_cols if c not in df.columns]
        if missing:
            raise RuntimeError(f"{src}: expected key column(s) {missing} absent — "
                               f"header may have changed; refusing to guess.")
        used = list(key_cols)
    else:
        k = pick_key(df)
        if not k:
            out = df.drop_duplicates()
            log(src, f"NO known license-number column found "
                     f"(columns={list(df.columns)[:10]}) — de-duped on WHOLE ROWS: "
                     f"{before} -> {len(out)}. CONFIRM headers.")
            return out
        used = [k]

    out = df.drop_duplicates(subset=used)
    lost = before - len(out)
    pct = lost / before
    if pct > max_loss_pct:
        raise RuntimeError(
            f"{src}: de-duping on {used} would remove {lost:,} of {before:,} rows "
            f"({pct:.1%}) — that exceeds the {max_loss_pct:.0%} guard, which almost "
            f"always means the KEY IS WRONG rather than the data being duplicated. "
            f"Investigate the row grain (is the number unique only WITHIN a type? does "
            f"the source carry history rows?) before relaxing this.")
    log(src, f"de-duped on {used}: {before} -> {len(out)} (removed {lost}, {pct:.2%})")
    return out


def licence_grain_report(df, key_cols, src):
    """Report how many DISTINCT licences the rows represent, without dropping anything.
    Where a source carries history (Colorado's disciplinary actions), rows > licences
    is correct and worth stating rather than silently collapsing."""
    if any(c not in df.columns for c in key_cols):
        return
    n = df.drop_duplicates(subset=key_cols).shape[0]
    if n != len(df):
        extra = len(df) - n
        log(src, f"grain: {len(df):,} rows represent {n:,} distinct licences on "
                 f"{key_cols} — {extra:,} extra row(s) are additional history records "
                 f"(kept deliberately, NOT duplicates).")


def failsafe(df, src, state, out_path, abs_floor=None, drop_pct=0.20, variant=None):
    """Refuse to write nothing, and shout if the population fell off a cliff.

    abs_floor is for tiny populations (VI has ~150 rows, where a percentage band is
    meaningless noise); drop_pct is the relative band for the larger sources.

    variant keeps the baseline PER-SCOPE. Without it, running Colorado at --scope dscsa
    after --scope pharmacy fires a bogus 85% "drop" (the scope changed on purpose) and
    then overwrites the baseline, so the next pharmacy run compares against the wrong
    number. A count check that cries wolf is a count check people learn to ignore.
    """
    n = len(df)
    if n == 0:
        raise RuntimeError(f"{src}: 0 rows — refusing to write an empty file.")
    if abs_floor is not None and n < abs_floor:
        raise RuntimeError(f"{src}: {n} rows is below the absolute floor of {abs_floor} "
                           f"— refusing to write a suspiciously short file.")
    tag = f"{state.lower()}_{variant}" if variant else state.lower()
    cf = f"{tag}_last_count.json"
    prev = None
    if os.path.exists(cf):
        try:
            prev = json.load(open(cf)).get("rows")
        except Exception:
            prev = None
    if isinstance(prev, int) and prev > 0:
        if n < prev * (1 - drop_pct):
            log(src, f"WARNING — row count fell from {prev} to {n} "
                     f"({100 * (1 - n / prev):.1f}% drop, band={drop_pct:.0%}). "
                     f"Inspect before trusting this file.")
        else:
            log(src, f"count check OK: {prev} -> {n}")
    else:
        log(src, f"no previous count on file; baselining at {n} rows")
    json.dump({"rows": n, "at": _today(), "variant": variant,
               "out": os.path.basename(out_path)}, open(cf, "w"), indent=2)
    return df


def write_csv(df, out_path, src):
    if RECON_ONLY:
        log(src, f"--recon: would write {len(df)} rows -> {out_path} (nothing written)")
        return
    df.to_csv(out_path, index=False, encoding="utf-8")
    log(src, f"wrote {out_path}: {len(df)} rows x {len(df.columns)} cols")


def leading_zero_report(df, cols, src):
    """The PRD's leading-zero requirement, checked rather than assumed."""
    for c in cols:
        if c not in df.columns:
            continue
        s = df[c].dropna().astype(str)
        nz = sum(x.startswith("0") for x in s)
        if nz:
            log(src, f"leading zeros preserved in {c!r}: {nz}/{len(s)} values "
                     f"(e.g. {[x for x in s if x.startswith('0')][:3]})")


# =========================================================================
# 1. COLORADO — Socrata Open Data API
# =========================================================================
# Resource id taken from the tracker URL
# data.colorado.gov/Regulations/Professional-and-Occupational-Licenses-in-Colorado/7s5z-vewr
# and re-confirmed live at recon (name + row count). Overridable by env.
CO_RESOURCE = os.environ.get("CO_RESOURCE_ID", "7s5z-vewr")
CO_BASE = os.environ.get("CO_BASE", "https://data.colorado.gov")
CO_PAGE = int(os.environ.get("CO_PAGE_SIZE", "50000"))

# The DSCSA trading-partner classes: entity/facility licence prefixes. Every one of these
# was identified at recon by sampling entityname, NOT guessed from the code letters.
CO_DSCSA_PREFIXES = {
    "PDO":  "Prescription Drug Outlet (retail pharmacy)",
    "SPDO": "Specialized Prescription Drug Outlet",
    "TPDO": "Third-Party Prescription Drug Outlet",
    "OSP":  "Out-of-State Pharmacy",
    "WHO":  "Wholesaler (out-of-state)",
    "WHI":  "Wholesaler (in-state)",
    "MFR":  "Manufacturer",
    "TPLP": "Third-Party Logistics Provider (3PL)",
    "NOF":  "Nonresident Outsourcing Facility (503B)",
}
# Individual pharmacy credentials — part of the pharmacy population, but NOT DSCSA
# trading partners. Included in the default 'pharmacy' scope, excluded from 'dscsa'.
CO_INDIV_PREFIXES = {
    "PHA":   "Pharmacist",
    "PHAT":  "Pharmacy Technician",
    "PHATP": "Pharmacy Technician (provisional)",
    "PHACS": "Pharmacist (clinical specialist)",
}
# Deliberately EXCLUDED though they look adjacent: NMTP is Colorado's Natural Medicine
# Training Program (not drug supply chain); FRM is accountancy firms; NTTB is
# non-transplant tissue banks. Recorded so the omission is a choice, not an oversight.


def co_where(scope):
    if scope == "all":
        return None
    pfx = list(CO_DSCSA_PREFIXES)
    if scope == "pharmacy":
        pfx += list(CO_INDIV_PREFIXES)
    return "licensetype in(" + ",".join(f"'{p}'" for p in pfx) + ")"


def co_count(where):
    p = {"$select": "count(*)"}
    if where:
        p["$where"] = where
    txt = fetch(f"{CO_BASE}/resource/{CO_RESOURCE}.json", p, expect="text", src="co")
    return int(json.loads(txt)[0]["count"])


def co_recon():
    src = "co"
    meta = json.loads(fetch(f"{CO_BASE}/api/views/{CO_RESOURCE}.json", expect="text", src=src))
    log(src, f"resource id {CO_RESOURCE} -> {meta.get('name')!r}")
    log(src, f"columns: {[c.get('fieldName') for c in meta.get('columns', [])]}")
    log(src, f"NOTE the field named 'licensetype' is really {[c.get('name') for c in meta.get('columns', []) if c.get('fieldName') == 'licensetype']} "
             f"— a PREFIX CODE, so a text LIKE filter on it matches nothing.")
    log(src, f"total rows (unfiltered): {co_count(None):,}")
    for scope in ("dscsa", "pharmacy"):
        log(src, f"scope {scope!r} -> {co_count(co_where(scope)):,} rows")
    # live per-prefix counts, so a vanished/renamed prefix is visible
    txt = fetch(f"{CO_BASE}/resource/{CO_RESOURCE}.json",
                {"$select": "licensetype,count(*)", "$group": "licensetype",
                 "$where": co_where("pharmacy"), "$limit": 100}, expect="text", src=src)
    live = {r["licensetype"]: int(r["count"]) for r in json.loads(txt)}
    known = {**CO_DSCSA_PREFIXES, **CO_INDIV_PREFIXES}
    for p, desc in known.items():
        n = live.get(p)
        flag = "" if n else "   <-- MISSING FROM DATASET, investigate"
        log(src, f"   {p:<6} {str(n if n is not None else 0):>7}  {desc}{flag}")
    _co_enum_guard(live)


def _co_enum_guard(live):
    """Same drift guard as vt/nj: surface prefix changes instead of silently dropping."""
    f = "co_prefixes.json"
    prev = json.load(open(f)) if os.path.exists(f) else {}
    added = [p for p in live if p not in prev]
    removed = [p for p in prev if p not in live]
    if prev and (added or removed):
        log("co", f"ENUM DRIFT — added={added} removed={removed} (review; continuing)")
    json.dump(live, open(f, "w"), indent=2)


def fetch_co(out_dir, scope="pharmacy"):
    src = "co"
    where = co_where(scope)
    expected = co_count(where)
    log(src, f"scope={scope!r} -> API reports {expected:,} rows"
             + (f" (filter: {where[:70]}...)" if where else " (no filter — the whole dataset)"))
    if expected == 0:
        raise RuntimeError(f"{src}: filter matched 0 rows — the prefix allowlist is "
                           f"probably stale. Run --recon.")

    frames, offset = [], 0
    while True:
        p = {"$limit": CO_PAGE, "$offset": offset, "$order": ":id"}
        if where:
            p["$where"] = where
        body = fetch(f"{CO_BASE}/resource/{CO_RESOURCE}.csv", p, expect="text", src=src)
        page = pd.read_csv(io.StringIO(body), dtype=str, keep_default_na=False)
        frames.append(page)
        got = len(page)
        log(src, f"  page offset={offset:>8} rows={got:>6}  (running {sum(len(f) for f in frames):,})")
        offset += CO_PAGE
        if got < CO_PAGE:
            break
        if offset > expected + CO_PAGE * 2:
            raise RuntimeError(f"{src}: paging ran past the reported total "
                               f"({offset} > {expected}) — aborting rather than looping.")

    df = pd.concat(frames, ignore_index=True)
    # No silent truncation: the fetched total must match what the API reported.
    if len(df) != expected:
        raise RuntimeError(
            f"{src}: fetched {len(df):,} rows but the API reported {expected:,} "
            f"— paging stopped short or over-ran. Refusing to write a partial file "
            f"as if it were whole.")
    log(src, f"paging validated: fetched {len(df):,} == API count {expected:,}")

    if "licensetype" in df.columns:
        seen = df["licensetype"].value_counts().to_dict()
        log(src, f"prefixes present: { {k: seen[k] for k in sorted(seen)} }")

    df = stamp(df, "CO", f"CO DORA Socrata {CO_RESOURCE} (scope={scope})")
    # The row grain here is one row per (licence x disciplinary action), NOT one per
    # licence: 839 licences carry 2-3 rows differing only in casenumber / programaction /
    # discipline dates, and there are ZERO exactly-identical rows. So whole-row de-dupe is
    # both lossless and correct -- and those discipline records are exactly the risk
    # signal a trading-partner registry wants. De-duping on licensenumber alone would
    # delete 53% of the file (see the dedupe() docstring).
    df = dedupe(df, src, key_cols="row")
    licence_grain_report(df, ["licensetype", "licensenumber"], src)
    leading_zero_report(df, ["licensenumber", "licensetype"], src)
    out = os.path.join(out_dir, "co_licenses.csv")
    df = failsafe(df, src, "co", out, variant=scope)
    write_csv(df, out, src)
    return df


# =========================================================================
# 2. NEW YORK — Bureau of Narcotic Enforcement, licensed_entities.xlsx
# =========================================================================
NY_PAGE = os.environ.get(
    "NY_BNE_PAGE",
    "https://www.health.ny.gov/professionals/narcotic/licensing_and_certification/")
NY_FILE_FALLBACK = os.environ.get(
    "NY_BNE_FILE",
    "https://www.health.ny.gov/professionals/narcotic/licensing_and_certification/"
    "docs/licensed_entities.xlsx")
NY_REQUIRED = ["PREFIX/CLASS", "LICENSE #", "LICENSEE NAME"]


def ny_discover():
    """Find the workbook anchor on the BNE page (the filename/path can rotate)."""
    src = "ny"
    try:
        html = fetch(NY_PAGE, expect="text", src=src)
    except Exception as e:
        log(src, f"page fetch failed ({e}) — falling back to the known file URL")
        return NY_FILE_FALLBACK
    soup = BeautifulSoup(html, "html.parser")
    hits = [a["href"] for a in soup.find_all("a", href=True)
            if re.search(r"\.xlsx?($|\?)", a["href"], re.I)]
    if not hits:
        log(src, "no .xlsx anchor found on the page — falling back to the known file URL")
        return NY_FILE_FALLBACK
    url = _urlparse.urljoin(NY_PAGE, hits[0])
    if len(hits) > 1:
        log(src, f"{len(hits)} spreadsheet anchors found; using the first: {url}")
    log(src, f"discovered download: {url}")
    return url


def _ny_valid_as_of(raw):
    """The banner in the first row reads 'ALL LICENSES VALID AS OF 8/7/2026'."""
    banner = " ".join(str(v) for v in raw.iloc[0].tolist() if str(v) not in ("nan", "None"))
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", banner)
    return (m.group(1) if m else ""), banner.strip()


def ny_recon():
    src = "ny"
    url = ny_discover()
    b = fetch(url, src=src)
    raw = pd.read_excel(io.BytesIO(b), dtype=str, header=None, nrows=3)
    date, banner = _ny_valid_as_of(raw)
    log(src, f"row 1 banner: {banner!r}  -> valid_as_of={date!r}")
    log(src, f"row 2 (the REAL header): {[str(v) for v in raw.iloc[1].tolist()]}")
    hdr = [str(v) for v in raw.iloc[1].tolist()]
    dups = {h for h in hdr if hdr.count(h) > 1}
    if dups:
        log(src, f"DUPLICATE header name(s) {sorted(dups)} — in this file the 2nd "
                 f"'LICENSEE NAME' is a name CONTINUATION, not a copy; it must be JOINED.")
    df = pd.read_excel(io.BytesIO(b), dtype=str, header=1)
    log(src, f"shape={df.shape} cols={list(df.columns)}")
    leading_zero_report(df, ["LICENSE #", "PREFIX/CLASS"], src)
    log(src, f"PREFIX/CLASS distinct: {sorted(set(df['PREFIX/CLASS'].dropna()))[:18]}")


def fetch_ny(out_dir):
    src = "ny"
    url = ny_discover()
    b = fetch(url, src=src)
    raw = pd.read_excel(io.BytesIO(b), dtype=str, header=None, nrows=2)
    valid_as_of, banner = _ny_valid_as_of(raw)
    log(src, f"banner: {banner!r} -> __valid_as_of={valid_as_of!r}")
    if not valid_as_of:
        log(src, "WARNING — no 'valid as of' date parsed from the banner; the file layout "
                 "may have changed. Inspect row 1.")

    # header is the SECOND row; the first is the banner
    df = pd.read_excel(io.BytesIO(b), dtype=str, header=1)
    log(src, f"read {len(df)} rows x {len(df.columns)} cols (header=row 2)")

    missing = [c for c in NY_REQUIRED if c not in df.columns]
    if missing:
        raise RuntimeError(f"{src}: required column(s) {missing} missing — the re-posted "
                           f"file changed shape. Got {list(df.columns)}. Refusing to guess.")

    # Join the split licensee-name columns rather than dropping the continuation.
    cont = [c for c in df.columns if str(c).startswith("LICENSEE NAME.")]
    if cont:
        parts = ["LICENSEE NAME"] + cont
        joined = (df[parts].fillna("")
                  .apply(lambda r: " ".join(str(v).strip() for v in r if str(v).strip()), axis=1))
        n_real = sum(1 for c in cont for v in df[c].fillna("") if str(v).strip())
        df["LICENSEE NAME FULL"] = joined.str.replace(r"\s+", " ", regex=True).str.strip()
        log(src, f"joined split name columns {parts} -> 'LICENSEE NAME FULL' "
                 f"({n_real} continuation values were non-blank; dropping them would "
                 f"have truncated those names)")

    df = stamp(df, "NY", "NY DOH BNE licensed_entities", valid_as_of=valid_as_of)
    # NY CS licence CLASSES matter — key on class + number so classes are not collapsed.
    df = dedupe(df, src, key_cols=["PREFIX/CLASS", "LICENSE #"])
    leading_zero_report(df, ["LICENSE #", "PREFIX/CLASS"], src)
    out = os.path.join(out_dir, "ny_bne_licenses.csv")
    df = failsafe(df, src, "ny", out, abs_floor=500)
    write_csv(df, out, src)
    return df


# =========================================================================
# 3. VIRGIN ISLANDS — Board of Pharmacy roster
# =========================================================================
VI_PAGE = os.environ.get(
    "VI_BOP_PAGE",
    "https://doh.vi.gov/office-of-professional-licensure-and-health-planning/board-of-pharmacy/")
VI_FILE_FALLBACK = os.environ.get(
    "VI_BOP_FILE",
    "https://doh.vi.gov/wp-content/uploads/2025/11/PHARMACY-LIST-10312025-fnl.xlsx")


def vi_discover():
    """The roster filename is date-stamped (.../2025/11/PHARMACY-LIST-10312025-fnl.xlsx),
    so it WILL rotate — always take the anchor rather than hard-coding."""
    src = "vi"
    try:
        html = fetch(VI_PAGE, expect="text", src=src)
    except Exception as e:
        log(src, f"page fetch failed ({e}) — falling back to the known file URL")
        return VI_FILE_FALLBACK, ""
    soup = BeautifulSoup(html, "html.parser")
    cands = [a for a in soup.find_all("a", href=True)
             if re.search(r"\.xlsx?($|\?)", a["href"], re.I)]
    # prefer an anchor that looks like the pharmacy roster
    pref = [a for a in cands if re.search(r"pharmac", a["href"] + a.get_text(" "), re.I)]
    pick = (pref or cands)
    txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    m = re.search(r"UPDATED\s+(\d{1,2}/\d{1,2}/\d{2,4})", txt, re.I)
    page_date = m.group(1) if m else ""
    if not pick:
        log(src, "no .xlsx anchor found — falling back to the known file URL")
        return VI_FILE_FALLBACK, page_date
    url = _urlparse.urljoin(VI_PAGE, pick[0]["href"])
    log(src, f"discovered download: {url}" + (f"   page says UPDATED {page_date}" if page_date else ""))
    return url, page_date


def vi_recon():
    src = "vi"
    url, page_date = vi_discover()
    b = fetch(url, src=src)
    df = pd.read_excel(io.BytesIO(b), dtype=str)
    log(src, f"shape={df.shape} cols={list(df.columns)}")
    log(src, f"License Type values: {sorted(set(df['License Type'].dropna()))}")
    log(src, f"Credential values  : {sorted(set(df['Credential'].dropna()))}")
    log(src, "SCOPE: this roster is PHARMACISTS ONLY (RPh) — it contains no pharmacies, "
             "wholesalers or manufacturers. The VI facility/trading-partner population "
             "needs a separate channel.")
    k = df["License #"].astype(str).str.strip()
    log(src, f"License #: n={len(k)} unique={k.nunique()} "
             f"whitespace-padded={sum(x != x.strip() for x in df['License #'].astype(str))}")
    _vi_collisions(df, src)


def _vi_collisions(df, src):
    """License # is NOT reliably unique here: two pairs are different people sharing a
    number. Report them so a human sees the source anomaly."""
    t = df.copy()
    t["_k"] = t["License #"].astype(str).str.strip()
    t["_n"] = (t["Last Name"].fillna("").str.strip() + "|"
               + t["First Name"].fillna("").str.strip()).str.upper()
    g = t.groupby("_k")["_n"].nunique()
    clash = g[g > 1]
    if len(clash):
        log(src, f"SOURCE ANOMALY — {len(clash)} licence number(s) shared by DIFFERENT "
                 f"people: {list(clash.index)}. De-duping on the number alone would "
                 f"DELETE real licensees, so the key is number + name.")
        for k in clash.index:
            who = t[t["_k"] == k][["Last Name", "First Name"]].values.tolist()
            log(src, f"     {k}: {who}")
    exact = len(t) - t.drop_duplicates(subset=["_k", "_n"]).shape[0]
    log(src, f"exact duplicate rows (same number AND same name): {exact}")


def fetch_vi(out_dir):
    src = "vi"
    url, page_date = vi_discover()
    b = fetch(url, src=src)
    df = pd.read_excel(io.BytesIO(b), dtype=str)
    log(src, f"read {len(df)} rows x {len(df.columns)} cols")

    for c in ["Last Name", "First Name", "Credential", "License Type", "License #"]:
        if c not in df.columns:
            raise RuntimeError(f"{src}: required column {c!r} missing — roster changed "
                               f"shape. Got {list(df.columns)}.")
    # 23 of 150 licence numbers arrive whitespace-padded; strip before keying.
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip().replace({"nan": None, "": None})

    _vi_collisions(df, src)
    df = stamp(df, "VI", "VI Board of Pharmacy roster",
               valid_as_of=page_date or "")
    # key on number + name, NOT number alone (see _vi_collisions)
    df = dedupe(df, src, key_cols=["License #", "Last Name", "First Name"])
    leading_zero_report(df, ["License #"], src)
    types = sorted({t for t in df["License Type"].dropna()})
    log(src, f"License Type present: {types} — pharmacists only; VI facilities/wholesalers "
             f"are NOT in this file and need a separate channel.")
    out = os.path.join(out_dir, "vi_licenses.csv")
    # tiny population: an absolute floor is meaningful where a percentage band is not
    df = failsafe(df, src, "vi", out, abs_floor=100)
    write_csv(df, out, src)
    return df


# =========================================================================
# 4. WEST VIRGINIA — Board of Pharmacy daily roster CSVs (two files)
# =========================================================================
# Two plain, daily-refreshed CSV endpoints. No login, no fee, no CAPTCHA.
#
# THIS SUPERSEDES THE PRESSURE-TEST VERDICT. West Virginia was classified Tier 4
# ("true Hermai candidate") because wvbop.com/public/verify/index.asp carries a real
# reCAPTCHA and the only paid option was a $10 single certified copy. That was true of the
# VERIFY TOOL and false of the agency: these roster CSVs sit on the same host, one
# directory up, and hand over the whole active population. The lesson is the same one
# Vermont taught -- a CAPTCHA on the lookup form says nothing about whether a bulk file
# exists elsewhere on the site.
WV_INDIV_URL = os.environ.get("WV_INDIV_URL", "https://www.wvbop.com/public/roster.csv")
WV_FAC_URL = os.environ.get("WV_FAC_URL", "https://www.wvbop.com/public/rosterF.csv")

# Per-file config. The key column name DIFFERS between the two files -- individuals use
# 'License Number#' (trailing '#'), facilities use 'License Number' -- and neither
# pick_key() nor DEDUPE_KEYS matches the '#' variant, so both are passed explicitly
# rather than auto-detected.
WV_FILES = {
    "individual": {
        "url": WV_INDIV_URL,
        "out": "wv_individual.csv",
        "key": "License Number#",
        "type_col": "License Type",
        "required": ["License Number#", "License Type", "Status", "Last Name"],
        "floor": 5000,
        # confirmed at recon 2026-08-28: pharmacists, techs, trainees, interns
        "expect_types": {"Registered Pharmacist", "Pharmacy Technician",
                         "Pharmacy Technician Trainee", "Nuclear Pharmacy Technician",
                         "Intern"},
        "zip_cols": ["Mailing Address Zip Code"],
    },
    "facility": {
        "url": WV_FAC_URL,
        "out": "wv_facility.csv",
        "key": "License Number",
        "type_col": "Type",
        "required": ["License Number", "Type", "Status", "Name"],
        "floor": 2000,
        # the DSCSA-critical types; asserted present rather than assumed
        "expect_types": {"Wholesale Distributor", "Third-Party Logistics Provider",
                         "Manufacturer", "Mail-Order Pharmacy",
                         "Single-Site Community Pharmacy"},
        "zip_cols": ["Physical Zip Code", "Mailing Address Zip Code"],
    },
}


def _wv_type_guard(kind, live_types):
    """Same drift guard as CO prefixes / VT+NJ enums: a new or vanished licence type is
    surfaced, never silently absorbed."""
    f = f"wv_{kind}_types.json"
    prev = set(json.load(open(f))) if os.path.exists(f) else set()
    added = sorted(live_types - prev)
    removed = sorted(prev - live_types)
    if prev and (added or removed):
        log("wv", f"ENUM DRIFT in {kind} types — added={added} removed={removed} "
                 f"(review; continuing)")
    json.dump(sorted(live_types), open(f, "w"), indent=2)


def _wv_load(cfg, kind):
    src = f"wv:{kind}"
    body = fetch(cfg["url"], expect="text", src=src)
    # A roster endpoint that starts serving HTML is a redirect/outage, not data.
    if body.lstrip()[:1] == "<":
        raise RuntimeError(f"{src}: response looks like HTML, not CSV "
                           f"(starts {body.lstrip()[:60]!r}) — refusing to parse it as a "
                           f"roster.")
    df = pd.read_csv(io.StringIO(body), dtype=str, keep_default_na=False)
    log(src, f"fetched {cfg['url']} -> {len(df)} rows x {len(df.columns)} cols")
    missing = [c for c in cfg["required"] if c not in df.columns]
    if missing:
        raise RuntimeError(f"{src}: required column(s) {missing} missing — the roster "
                           f"changed shape. Got {list(df.columns)}. Refusing to guess.")
    return df, src


def wv_recon():
    for kind, cfg in WV_FILES.items():
        df, src = _wv_load(cfg, kind)
        k = df[cfg["key"]].astype(str).str.strip()
        log(src, f"key {cfg['key']!r}: n={len(k)} unique={k.nunique()} "
                 f"blank={(k == '').sum()} -> "
                 f"{'UNIQUE, number-only key is safe' if k.nunique() == len(k) else 'NOT UNIQUE, needs a composite key'}")
        types = sorted(set(df[cfg["type_col"]].astype(str).str.strip()))
        log(src, f"{cfg['type_col']} ({len(types)} distinct): {types}")
        st = sorted(set(df["Status"].astype(str).str.strip()))
        log(src, f"Status ({len(st)}): {st}"
                 + ("   <-- ACTIVE-ONLY roster: no expired/inactive records"
                    if st == ["Active"] else ""))
        leading_zero_report(df, [cfg["key"]] + cfg["zip_cols"], src)
        missing_t = cfg["expect_types"] - set(types)
        if missing_t:
            log(src, f"WARNING — expected type(s) absent: {sorted(missing_t)}")


def fetch_wv(out_dir):
    """Fetch both WV rosters. Returns the combined row count via a dict of frames."""
    out = {}
    for kind, cfg in WV_FILES.items():
        df, src = _wv_load(cfg, kind)

        types = set(df[cfg["type_col"]].astype(str).str.strip())
        _wv_type_guard(kind, types)
        log(src, f"{cfg['type_col']} covered ({len(types)}): {sorted(types)}")
        missing_t = cfg["expect_types"] - types
        if missing_t:
            log(src, f"WARNING — expected {cfg['type_col']} value(s) absent: "
                     f"{sorted(missing_t)} — coverage may have narrowed.")

        statuses = sorted(set(df["Status"].astype(str).str.strip()))
        if statuses == ["Active"]:
            log(src, "SCOPE: this roster is ACTIVE-ONLY (Status is uniformly 'Active') — "
                     "expired/inactive licences are NOT present. Recorded in __scope.")
        df["__scope"] = ("active-only" if statuses == ["Active"]
                         else "statuses:" + "|".join(statuses))

        df = stamp(df, "WV", f"WV Board of Pharmacy {kind} roster ({os.path.basename(cfg['url'])})")

        # Run the number through the >10% guard as instructed. It passes at 0% loss:
        # both files are fully unique on the number. For INDIVIDUALS that is also
        # structural -- the number embeds the credential type 1:1 (RP/PT/TT/NT/IN), so
        # cross-type collisions cannot occur. For FACILITIES the prefix->type map is NOT
        # 1:1 (EP covers two types; 'SP' and 'sp' differ only by case), so uniqueness
        # there is verified empirically and enforced by the guard, not assumed. If a
        # future refresh makes the number non-unique, the guard raises and the fix is
        # key_cols=[key, type_col] -- not relaxing the threshold.
        df = dedupe(df, src, key_cols=[cfg["key"]])
        licence_grain_report(df, [cfg["key"], cfg["type_col"]], src)
        leading_zero_report(df, [cfg["key"]] + cfg["zip_cols"], src)

        out_path = os.path.join(out_dir, cfg["out"])
        df = failsafe(df, src, "wv", out_path, abs_floor=cfg["floor"], variant=kind)
        write_csv(df, out_path, src)
        out[kind] = df
    return out


# =========================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all", choices=["all", "co", "ny", "vi", "wv"])
    ap.add_argument("--scope", default="pharmacy", choices=["dscsa", "pharmacy", "all"],
                    help="Colorado only: dscsa=facilities/trading partners (~8.5k); "
                         "pharmacy=+pharmacists & techs (~57k, default); all=1.6M")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--recon", action="store_true",
                    help="inspect the sources and write nothing")
    args = ap.parse_args()

    global RECON_ONLY
    RECON_ONLY = args.recon
    os.makedirs(args.out_dir, exist_ok=True)

    want = ["co", "ny", "vi", "wv"] if args.source == "all" else [args.source]
    if args.recon:
        for s in want:
            print("=" * 78)
            {"co": co_recon, "ny": ny_recon, "vi": vi_recon,
             "wv": wv_recon}[s]()
        print("=" * 78)
        print("recon complete — nothing written. Re-run without --recon to fetch.")
        return

    results, failed = {}, {}
    for s in want:
        print("=" * 78)
        try:
            fn = {"co": lambda: fetch_co(args.out_dir, args.scope),
                  "ny": lambda: fetch_ny(args.out_dir),
                  "vi": lambda: fetch_vi(args.out_dir),
                  "wv": lambda: fetch_wv(args.out_dir)}[s]
            results[s] = fn()
        except Exception as e:
            failed[s] = str(e)
            log(s, f"FAILED — {e}")

    print("=" * 78)
    for s, res in results.items():
        if isinstance(res, dict):          # WV writes two files
            for kind, df in res.items():
                print(f"  {s.upper():<3} OK      {len(df):>9,} rows  ({kind})")
        else:
            print(f"  {s.upper():<3} OK      {len(res):>9,} rows")
    for s, e in failed.items():
        print(f"  {s.upper():<3} FAILED  {e[:100]}")

    print()
    print("VERIFY before trusting (a clean run is NOT verification):")
    print("  1. row counts plausible — CO matches the API count printed above;")
    print("     NY ~3.8k; VI ~150 (pharmacists only).")
    print("  2. leading zeros intact — grep the CSV: NY 'LICENSE #' should show values")
    print("     like 0177 (2,066 of them), VI like 078. If they read 177/78, dtype broke.")
    print("  3. a known registrant present — e.g. CO 'Kuehne + Nagel' (TPLP),")
    print("     NY 'POPULATION COUNCIL INC', VI 'ABDALLAH'.")
    print("     WV ~11.8k individuals + ~4.3k facilities.")
    print("  3b. WV leading zeros: zip codes like 07646 (not 7646); licence numbers are")
    print("      alphanumeric (RP0014664), so also check the embedded zeros survived.")
    print("  4. both active and expired statuses present where the source carries them —")
    print("     NOTE WV rosters are ACTIVE-ONLY by design; see the __scope column.")
    print("  5. CO: the per-prefix counts above match the --recon table.")
    print("  6. WV: licence types cover pharmacists/techs/interns (individual file) and")
    print("     Wholesale Distributor / 3PL / Manufacturer (facility file).")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
