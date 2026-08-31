#!/usr/bin/env python3
"""
Vermont license-data adapter (first Tier-1 ingestion adapter).

Vermont OPR publishes a FREE roster download, but the Profession Type dropdown is
single-select with no "select all" — so the full pharmacy population requires one
download per profession type, stacked and de-duplicated.

This adapter drives the live Pega portal with Playwright, DISCOVERS the profession
types at runtime (so nothing is hard-coded and enum drift is detectable), downloads
each, stacks + de-dupes, and writes one combined CSV.

    NOTE ON EXECUTION LOCATION
    This must run where the network can reach secure.professionals.vermont.gov —
    i.e., your machine or the GitHub Actions runner. It will NOT run from the
    Cowork cloud sandbox (that host is blocked there).

Modes:
    python vt_adapter.py --recon        # load page, dump dropdowns/buttons + screenshot; NO downloads
    python vt_adapter.py                # full run: loop types, stack, dedupe -> vt_licenses.csv
    python vt_adapter.py --headed       # run with a visible browser (debugging)

First-run protocol: run --recon FIRST and eyeball vt_recon.png + the printed
dropdown labels. If the heuristic selectors below don't match, adjust the small
LOCATORS block — everything else is generic.

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

import pandas as pd
from playwright.sync_api import sync_playwright

VT_URL = os.environ.get(
    "VT_URL",
    "https://secure.professionals.vermont.gov/prweb/PRServletCustom/"
    "V9csDxL3sXkkjMC_FR2HrA%5B%5B*/!STANDARD?UserIdentifier=LicenseLookupGuestUser",
)

# --- Heuristic locators (confirmed against live --recon, 2026-08) -----------
# The roster-download form is under the "PROFESSION ROSTER DOWNLOAD" tab (NOT
# "Licensee Lookup"). Profession Type is empty until Profession = Pharmacy is set.
LOCATORS = {
    "panel_entry_texts": ["PROFESSION ROSTER DOWNLOAD", "Profession Roster Download"],
    "profession_value": "Pharmacy",
    "download_texts": ["DOWNLOAD", "Download"],
}
import re as _re
LIMIT = 0  # set by --limit; 0 = all types
# Columns to de-dupe on if present (VT's exact headers confirmed on first real run).
DEDUPE_KEYS = ["LicenseNumber", "License Number", "License #", "Number", "Credential Number"]


def log(m): print(f"[vt] {m}", flush=True)


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


def _reveal_form(page):
    """Open the PROFESSION ROSTER DOWNLOAD tab (the bulk-download form)."""
    for t in LOCATORS["panel_entry_texts"]:
        try:
            el = page.get_by_text(t, exact=False).first
            if el and el.is_visible():
                el.click(); page.wait_for_timeout(4000); return
        except Exception:
            pass


def _first_opt(s):
    opts = _options(s)
    return opts[0]["text"].strip().upper() if opts else ""


def _select_profession(page):
    """Set Pharmacy on every visible Profession select (the portal renders it more than once)."""
    hits = []
    for s in _visible_selects(page):
        if _first_opt(s) == "SELECT PROFESSION":  # profession list, NOT "SELECT PROFESSION TYPE"
            try:
                s.select_option(label=LOCATORS["profession_value"]); hits.append(s)
            except Exception:
                pass
    return hits


def _get_type_select(page, timeout_ms=20000):
    """Poll for the Profession Type select (placeholder 'SELECT PROFESSION TYPE') to populate (>1 option)."""
    waited = 0
    while waited < timeout_ms:
        for s in _visible_selects(page):
            if _first_opt(s) == "SELECT PROFESSION TYPE" and len(_options(s)) > 1:
                return s
        page.wait_for_timeout(1000); waited += 1000
    return None


def _check_all_status(page):
    """Check every visible checkbox on the roster panel (the 4 Status boxes)."""
    for cb in page.query_selector_all("input[type=checkbox]"):
        try:
            if cb.is_visible() and not cb.is_checked():
                cb.check()
        except Exception:
            pass


def _click_download(page):
    """Click the Download button (its label may carry an icon glyph); exclude the tab 'PROFESSION ROSTER DOWNLOAD'."""
    for el in page.query_selector_all("button, a, input[type=button], input[type=submit], [role=button]"):
        try:
            t = (el.inner_text() or el.get_attribute("value") or "").strip().lower()
            if el.is_visible() and "download" in t and "roster" not in t and "profession" not in t:
                el.click(); return True
        except Exception:
            pass
    return False


def recon(page):
    _reveal_form(page)
    prof = _select_profession(page)
    log(f"Profession=Pharmacy set on {len(prof)} select(s)")
    ptype = _get_type_select(page) if prof else None
    if ptype:
        opts = [o["text"] for o in _options(ptype) if "PROFESSION TYPE" not in o["text"].upper() and o["text"]]
        log(f"Profession Type populated: {len(opts)} options -> {opts[:8]}{'...' if len(opts) > 8 else ''}")
    else:
        log("Profession Type did NOT populate — check tab/selectors")
    labels = []
    for x in page.query_selector_all("button, a, input[type=button], input[type=submit]"):
        t = (x.inner_text() or x.get_attribute("value") or "").strip()
        if x.is_visible() and t:
            labels.append(t)
    log("visible buttons/links: " + ", ".join(list(dict.fromkeys(labels))[:25]))
    page.screenshot(path="vt_recon.png", full_page=True)
    log("screenshot -> vt_recon.png")


def full(page, out):
    _reveal_form(page)
    prof = _select_profession(page)
    if not prof:
        raise RuntimeError("Profession select not found — run --recon.")
    page.wait_for_timeout(2500)
    ptype = _get_type_select(page)
    if not ptype:
        raise RuntimeError("Profession Type dropdown did not populate — run --recon and adjust LOCATORS.")
    types = [o for o in _options(ptype) if o["value"] and o["text"].upper() not in ("", "SELECT PROFESSION TYPE")]
    # dedupe by TEXT (labels repeat; data is de-duped later on the natural key anyway)
    seen, uniq = set(), []
    for o in types:
        if o["text"] not in seen:
            seen.add(o["text"]); uniq.append(o)
    log(f"discovered {len(uniq)} profession types")
    if LIMIT:
        uniq = uniq[:LIMIT]; log(f"--limit active: only first {len(uniq)} types this run")

    # --- enum guard: surface drift instead of silently dropping types ---
    prev = {}
    if os.path.exists("vt_types.json"):
        prev = json.load(open("vt_types.json"))
    cur = {o["value"]: o["text"] for o in uniq}
    added = [v for v in cur if v not in prev]
    removed = [v for v in prev if v not in cur]
    if prev and (added or removed):
        log(f"ENUM DRIFT — added={[cur[v] for v in added]} removed={[prev[v] for v in removed]} (review; continuing)")
    json.dump(cur, open("vt_types.json", "w"), indent=2)

    tmp = tempfile.mkdtemp(prefix="vt_")
    frames = []
    # Auto-waiting LOCATORS (re-resolve at action time — immune to Pega's partial re-renders):
    type_loc = page.locator("select").filter(has=page.locator("option", has_text="SELECT PROFESSION TYPE"))
    dl_name = _re.compile(r"^\W*download\W*$", _re.I)  # anchored: excludes the "Profession Roster Download" tab
    def _dl_locator():
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=dl_name)
            if loc.count():
                return loc.first
        return page.get_by_text(dl_name).first
    for i, o in enumerate(uniq, 1):
        name = o["text"]
        try:
            type_loc.first.select_option(label=name); page.wait_for_timeout(1200)
            _check_all_status(page); page.wait_for_timeout(600)
            with page.expect_download(timeout=60000) as dl:
                _dl_locator().click()
            fn = dl.value.suggested_filename or f"{i}.xlsx"
            ext = ".csv" if fn.lower().endswith(".csv") else ".xlsx"
            dest = os.path.join(tmp, f"{i:02d}{ext}")
            dl.value.save_as(dest)
            df = pd.read_csv(dest, dtype=str) if ext == ".csv" else pd.read_excel(dest, dtype=str)
            df["__profession_type"] = name
            frames.append(df)
            log(f"  [{i}/{len(uniq)}] {name}: {len(df)} rows")
        except Exception as e:
            log(f"  [{i}/{len(uniq)}] {name}: FAILED ({e}) — continuing")

    if not frames:
        raise RuntimeError("No downloads succeeded — refusing to write an empty roster.")
    allrows = pd.concat(frames, ignore_index=True)
    key = next((k for k in DEDUPE_KEYS if k in allrows.columns), None)
    before = len(allrows)
    # DEDUPE GUARD (added 2026-08 after a CC run found license numbers that RESTART per
    # category — deduping on the number alone silently deleted 53% of Colorado's rows and
    # still "succeeded"). A key that drops >10% is treated as wrong, not clean.
    if key:
        d1 = allrows.drop_duplicates(subset=[key]); loss1 = 1 - len(d1) / before if before else 0
        if loss1 > 0.10 and "__profession_type" in allrows.columns:
            d2 = allrows.drop_duplicates(subset=[key, "__profession_type"])
            loss2 = 1 - len(d2) / before if before else 0
            log(f"DEDUPE GUARD: {key!r} alone dropped {loss1:.0%} — numbers likely restart per "
                f"profession type. Composite [{key}, __profession_type] drops {loss2:.0%}.")
            if loss2 <= 0.10:
                allrows = d2
            else:
                log("  composite still drops >10% — KEEPING ALL ROWS; review the key before trusting.")
        elif loss1 > 0.10:
            log(f"DEDUPE GUARD: {key!r} dropped {loss1:.0%} — suspicious; KEEPING ALL ROWS, review.")
        else:
            allrows = d1
    else:
        allrows = allrows.drop_duplicates()
        log("no known license-number column found; de-duped on whole rows (confirm headers)")
    allrows.to_csv(out, index=False)
    log(f"wrote {out}: {len(allrows)} rows (stacked {before}, deduped on {key or 'all columns'})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon", action="store_true", help="Inspect the page; no downloads.")
    ap.add_argument("--headed", action="store_true", help="Show the browser.")
    ap.add_argument("--out", default="vt_licenses.csv")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N profession types (debug).")
    args = ap.parse_args()
    global LIMIT; LIMIT = args.limit
    with sync_playwright() as p:
        launch = {}
        cp = os.environ.get("CHROMIUM_PATH")
        if cp: launch["executable_path"] = cp
        b = p.chromium.launch(headless=not args.headed, **launch)
        page = b.new_page(accept_downloads=True)
        page.set_default_timeout(45000)
        log(f"navigating to VT portal")
        page.goto(VT_URL, wait_until="domcontentloaded"); page.wait_for_timeout(6000)
        try:
            (recon if args.recon else lambda pg: full(pg, args.out))(page)
        finally:
            b.close()


if __name__ == "__main__":
    main()
