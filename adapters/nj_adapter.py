#!/usr/bin/env python3
"""
New Jersey license-data adapter (2nd Tier-1 ingestion adapter, after Vermont).

Target: the NJ Division of Consumer Affairs MyLicense **bulk verification** module,
which serves the NJ Drug Control Unit (CDS) registrations (and the Board of Pharmacy
population) as a DOWNLOADABLE FILE.

    https://newjersey.mylicense.com/verification_bulk/Search.aspx

Verified live (2026-08): three <select>s — Profession, License Type, License Status —
each with an "All" option; optional text filters; NO CAPTCHA, NO login, NO fee; and the
page states "The download may take a few minutes" (i.e. it returns a FILE, not a grid).

Strategy (mirrors vt_adapter.py): the License-Type dropdown is the precise axis, so we
DISCOVER its options at runtime, keep only the LHAIV-relevant ones (CDS*, wholesaler,
manufacturer, device), and download each with Status="All" — stacking + de-duping on the
license/registration number. An "All"-scoped query is also run as a completeness
cross-check. Nothing is hard-coded that can drift silently.

    NOTE ON EXECUTION LOCATION
    Must run where the network can reach newjersey.mylicense.com — your machine or a
    GitHub Actions runner. It will NOT run from the Cowork cloud sandbox (blocked there),
    exactly like the Vermont adapter.

Modes:
    python nj_adapter.py --recon     # dump the 3 selects' options + inputs + submit; screenshot; NO download
    python nj_adapter.py             # full run: loop in-scope license types -> nj_licenses.csv
    python nj_adapter.py --headed    # visible browser (debugging)
    python nj_adapter.py --all-query # single "All" license-type query + post-filter (alt strategy)
    python nj_adapter.py --types "cds,wholesale"   # override the scope keyword allowlist
    python nj_adapter.py --limit 2   # only first N in-scope types (debug)

First-run protocol (SAME AS VT): run --recon FIRST, eyeball nj_recon.png + the printed
dropdown option lists, and confirm (a) the exact submit control, (b) whether Profession
must match the License Type, (c) whether submit yields a direct download or a results
page with a download link. Adjust the small LOCATORS block if the heuristics miss.

Setup (one time, on the machine that runs it):
    pip install playwright pandas openpyxl
    python -m playwright install chromium
"""
import os
import sys
import glob
import json
import argparse
import tempfile
import datetime as _dt

import pandas as pd
from playwright.sync_api import sync_playwright

NJ_URL = os.environ.get(
    "NJ_BULK_URL",
    "https://newjersey.mylicense.com/verification_bulk/Search.aspx",
)

# --- Heuristic locators (confirm against --recon) ---------------------------
# MyLicense usually names these controls t_web_lookup__<field>_name. We target by that
# name first, then fall back to identifying each <select> by the CONTENT of its options
# (the only drift-proof signal), the way vt_adapter keys on placeholder text.
LOCATORS = {
    "profession_name_hints":   ["profession_name", "profession"],
    "license_type_name_hints": ["license_type_name", "licensetype", "license_type"],
    "status_name_hints":       ["license_status_name", "status"],
    "submit_hints":            ["search", "submit", "sch_button", "lookup"],
    "download_texts":          ["download", "export", "download results", "download list"],
    "all_option":              "all",
}

# License-Type keywords that define LHAIV scope (matched case-insensitively against the
# discovered option labels). Pharmacy is OFF by default (NJ BoP already obtained).
DEFAULT_TYPE_KEYWORDS = ["cds", "wholesale", "manufacturer", "distributor", "device"]

DEDUPE_KEYS = ["License Number", "LicenseNumber", "License #", "Number",
               "Credential Number", "Registration Number", "License No", "License_No"]

LIMIT = 0
TYPE_KEYWORDS = list(DEFAULT_TYPE_KEYWORDS)
ALL_QUERY = False


def log(m): print(f"[nj] {m}", flush=True)


def _today():
    # a normal CLI run may stamp the date (unlike a workflow context)
    return _dt.date.today().isoformat()


def _options(select_el):
    return [
        {"text": (o.inner_text() or "").strip(), "value": o.get_attribute("value") or ""}
        for o in select_el.query_selector_all("option")
    ]


def _visible_selects(page):
    out = []
    for s in page.query_selector_all("select"):
        try:
            if s.is_visible():
                out.append(s)
        except Exception:
            pass
    return out


def _name_of(el):
    return ((el.get_attribute("name") or "") + " " + (el.get_attribute("id") or "")).lower()


def _find_select(page, name_hints, content_test=None):
    """Locate a <select> by name/id hint first, then by an option-content predicate."""
    sels = _visible_selects(page)
    for s in sels:
        n = _name_of(s)
        if any(h in n for h in name_hints):
            return s
    if content_test:
        for s in sels:
            labels = [o["text"].lower() for o in _options(s)]
            if content_test(labels):
                return s
    return None


def _has_all(labels):
    return any(l.strip() == LOCATORS["all_option"] or l.strip().startswith("all")
               for l in labels)


def _select_by_label_contains(select_el, needle):
    """Select the option whose visible text == needle (case-insensitive), else contains it."""
    opts = _options(select_el)
    lower = needle.strip().lower()
    for o in opts:
        if o["text"].strip().lower() == lower:
            select_el.select_option(value=o["value"]); return o["text"]
    for o in opts:
        if lower in o["text"].strip().lower():
            select_el.select_option(value=o["value"]); return o["text"]
    return None


def _set_all(select_el):
    for o in _options(select_el):
        if o["text"].strip().lower().startswith("all"):
            select_el.select_option(value=o["value"]); return True
    return False


def _click_submit(page):
    for el in page.query_selector_all(
            "input[type=submit], input[type=button], button, [role=button], a"):
        try:
            t = ((el.inner_text() or "") + " " + (el.get_attribute("value") or "")
                 + " " + _name_of(el)).strip().lower()
            if el.is_visible() and any(h in t for h in LOCATORS["submit_hints"]) \
                    and "reset" not in t and "clear" not in t:
                el.click(); return True
        except Exception:
            pass
    return False


def _try_download_link(page):
    for el in page.query_selector_all("a, button, input[type=submit], input[type=button]"):
        try:
            t = ((el.inner_text() or "") + " " + (el.get_attribute("value") or "")).strip().lower()
            if el.is_visible() and any(d in t for d in LOCATORS["download_texts"]):
                return el
        except Exception:
            pass
    return None


def _read_download(page, click_target, dest_dir, idx):
    """Click something that triggers a file download; save + return the local path (or None)."""
    with page.expect_download(timeout=300_000) as dl:   # "may take a few minutes"
        click_target()
    d = dl.value
    fn = d.suggested_filename or f"{idx}.csv"
    ext = ".csv" if fn.lower().endswith((".csv", ".txt", ".tsv")) else ".xlsx"
    dest = os.path.join(dest_dir, f"{idx:02d}{ext}")
    d.save_as(dest)
    return dest, ext


def _frame_from_file(path, ext, label):
    df = pd.read_csv(path, dtype=str) if ext == ".csv" else pd.read_excel(path, dtype=str)
    df["__license_type"] = label
    df["State"] = "NJ"
    df["__source"] = "NJ DCA MyLicense Verification_Bulk"
    df["__extract_date"] = _today()
    return df


def _load_form(page):
    log(f"navigating to {NJ_URL}")
    page.goto(NJ_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    prof = _find_select(page, LOCATORS["profession_name_hints"], _has_all)
    ltype = _find_select(page, LOCATORS["license_type_name_hints"], _has_all)
    status = _find_select(page, LOCATORS["status_name_hints"], _has_all)
    return prof, ltype, status


def recon(page):
    prof, ltype, status = _load_form(page)
    for nm, s in [("Profession", prof), ("License Type", ltype), ("License Status", status)]:
        if s is None:
            log(f"{nm}: NOT FOUND — inspect nj_recon.png and adjust LOCATORS hints")
            continue
        opts = [o["text"] for o in _options(s) if o["text"]]
        log(f"{nm}: name={_name_of(s).strip()!r} — {len(opts)} options -> {opts[:12]}{'...' if len(opts) > 12 else ''}")
        if s is ltype:
            hits = [o for o in opts if any(k in o.lower() for k in TYPE_KEYWORDS)]
            log(f"  License Type in-scope matches ({len(hits)}): {hits}")
    inputs = []
    for i in page.query_selector_all("input[type=text]"):
        if i.is_visible():
            inputs.append(_name_of(i).strip())
    log(f"visible text inputs: {inputs}")
    btns = []
    for b in page.query_selector_all("input[type=submit], input[type=button], button, a"):
        try:
            t = ((b.inner_text() or "") + (b.get_attribute("value") or "")).strip()
            if b.is_visible() and t:
                btns.append(t)
        except Exception:
            pass
    log("visible buttons/links: " + ", ".join(list(dict.fromkeys(btns))[:25]))
    page.screenshot(path="nj_recon.png", full_page=True)
    log("screenshot -> nj_recon.png  (confirm submit control + download-vs-results-page before a full run)")


def _in_scope_types(ltype):
    opts = [o for o in _options(ltype)
            if o["value"] and o["text"].strip() and not o["text"].strip().lower().startswith("all")]
    scoped = [o for o in opts if any(k in o["text"].lower() for k in TYPE_KEYWORDS)]
    # dedupe by text
    seen, uniq = set(), []
    for o in scoped:
        if o["text"] not in seen:
            seen.add(o["text"]); uniq.append(o)
    return uniq


def _enum_guard(uniq):
    prev = {}
    if os.path.exists("nj_license_types.json"):
        prev = json.load(open("nj_license_types.json"))
    cur = {o["value"]: o["text"] for o in uniq}
    added = [cur[v] for v in cur if v not in prev]
    removed = [prev[v] for v in prev if v not in cur]
    if prev and (added or removed):
        log(f"ENUM DRIFT — added={added} removed={removed} (review; continuing)")
    json.dump(cur, open("nj_license_types.json", "w"), indent=2)


def _download_current(page, dest_dir, idx, label):
    """Submit the current form state and capture the resulting file (direct or via a link)."""
    try:
        return _read_download(page, lambda: _click_submit(page), dest_dir, idx)
    except Exception:
        # no direct download — maybe a results page with a Download link
        link = _try_download_link(page)
        if link:
            return _read_download(page, lambda: link.click(), dest_dir, idx)
        raise


def full(page, out):
    prof, ltype, status = _load_form(page)
    if ltype is None:
        raise RuntimeError("License Type select not found — run --recon and adjust LOCATORS.")
    if status is not None:
        _set_all(status)                       # capture every status; keep the column
    if prof is not None:
        _set_all(prof)                         # let License Type drive the filter
    dest = tempfile.mkdtemp(prefix="nj_")
    frames = []

    if ALL_QUERY:
        # alternate strategy: one "All" license-type query, then post-filter by scope
        _set_all(ltype); page.wait_for_timeout(1000)
        try:
            path, ext = _download_current(page, dest, 1, "ALL")
            df = _frame_from_file(path, ext, "ALL")
            tcol = next((c for c in df.columns if "type" in c.lower()), None)
            if tcol:
                mask = df[tcol].fillna("").str.lower().apply(
                    lambda v: any(k in v for k in TYPE_KEYWORDS))
                log(f"ALL query: {len(df)} rows -> {int(mask.sum())} in-scope after post-filter")
                df = df[mask]
            frames.append(df)
        except Exception as e:
            raise RuntimeError(f"--all-query download failed: {e}")
    else:
        uniq = _in_scope_types(ltype)
        _enum_guard(uniq)
        if not uniq:
            raise RuntimeError("No in-scope license types matched the keyword allowlist — "
                               "run --recon and check --types.")
        if LIMIT:
            uniq = uniq[:LIMIT]; log(f"--limit active: first {len(uniq)} types")
        log(f"in-scope license types: {[o['text'] for o in uniq]}")
        for i, o in enumerate(uniq, 1):
            name = o["text"]
            try:
                # re-resolve the select each iteration (MyLicense re-renders on postback)
                _, ltype2, status2 = _load_form(page)
                if status2 is not None: _set_all(status2)
                if _select_by_label_contains(ltype2, name) is None:
                    log(f"  [{i}/{len(uniq)}] {name}: option vanished — skipping"); continue
                page.wait_for_timeout(800)
                path, ext = _download_current(page, dest, i, name)
                df = _frame_from_file(path, ext, name)
                frames.append(df)
                log(f"  [{i}/{len(uniq)}] {name}: {len(df)} rows")
            except Exception as e:
                log(f"  [{i}/{len(uniq)}] {name}: FAILED ({e}) — continuing")

    if not frames:
        raise RuntimeError("No downloads succeeded — refusing to write an empty roster.")
    allrows = pd.concat(frames, ignore_index=True)
    key = next((k for k in DEDUPE_KEYS if k in allrows.columns), None)
    before = len(allrows)
    allrows = allrows.drop_duplicates(subset=[key]) if key else allrows.drop_duplicates()
    if not key:
        log("no known license-number column found; de-duped on whole rows (CONFIRM headers)")
    allrows.to_csv(out, index=False)
    log(f"wrote {out}: {len(allrows)} rows (stacked {before}, deduped on {key or 'all columns'})")
    log("VERIFY before trusting: (1) non-empty & plausible; (2) per-type counts vs an --all-query count; "
        "(3) leading zeros preserved (values are strings); (4) a known CDS registrant present; "
        "(5) both active and expired present.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--all-query", action="store_true", help="single All query + post-filter")
    ap.add_argument("--types", default="", help="comma keywords overriding the scope allowlist")
    ap.add_argument("--out", default="nj_licenses.csv")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    global LIMIT, TYPE_KEYWORDS, ALL_QUERY
    LIMIT = args.limit
    ALL_QUERY = args.all_query
    if args.types.strip():
        TYPE_KEYWORDS = [k.strip().lower() for k in args.types.split(",") if k.strip()]
    with sync_playwright() as p:
        launch = {}
        cp = os.environ.get("CHROMIUM_PATH")
        if cp: launch["executable_path"] = cp
        b = p.chromium.launch(headless=not args.headed, **launch)
        page = b.new_page(accept_downloads=True)
        page.set_default_timeout(45000)
        try:
            (recon if args.recon else (lambda pg: full(pg, args.out)))(page)
        finally:
            b.close()


if __name__ == "__main__":
    main()
