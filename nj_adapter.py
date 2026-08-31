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
import re
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

# DEFAULT SCOPE (person search) = NJ pharmacy PERSONNEL — board-licensed individuals, in scope.
# Exact labels confirmed at recon (2026-08). NOT here: pharmacy FACILITIES + facility CDS live in
# this portal's separate BUSINESS Download search (~88 types — a future --business mode); drug
# manufacturers / wholesalers / devices are the separate NJ DOH system (healthapps fdSearch).
NJ_PERSONNEL_TYPES = ["Pharmacist", "Pharmacist Graduate License", "Pharmacy Intern", "Pharmacy Technician"]
# Keyword fallback, used ONLY when --types is passed (substring match).
DEFAULT_TYPE_KEYWORDS = ["pharmacist", "pharmacy intern", "pharmacy tech"]

# The MyLicense payload uses lowercase snake_case headers ('license_no'), NOT the
# title-case names guessed earlier -- so the key was never found and every run silently
# fell through to whole-row de-duplication. Real header confirmed live 2026-08-31:
# full_name|first_name|middle_name|last_name|name_suffix|profession_name|
# license_type_name|license_no|issue_date|expiration_date|addr_line_1|addr_line_2|
# addr_city|addr_state|addr_zipcode|addr_county|addr_email|license_status_name|
DEDUPE_KEYS = ["license_no", "License Number", "LicenseNumber", "License #", "Number",
               "Credential Number", "Registration Number", "License No", "License_No"]
# Columns that legitimately distinguish two rows sharing a licence number. NJ carries
# STATUS HISTORY: one licensee appears as Expired + Reinstatement Pending + Deleted on the
# same license_no. Keying on the number alone would drop that history.
DEDUPE_TIEBREAK = ["license_status_name", "__license_type", "expiration_date"]

LIMIT = 0
TYPE_KEYWORDS = list(DEFAULT_TYPE_KEYWORDS)
EXACT_TYPES = list(NJ_PERSONNEL_TYPES)   # exact-label allowlist (default); [] => keyword mode
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
    """Click the control that triggers the file download; save + return (path, kind).

    kind is ".xlsx" for a real workbook, otherwise ".txt" meaning DELIMITED TEXT whose
    separator is sniffed at parse time. MyLicense serves this population as `Data.txt`,
    PIPE-delimited (confirmed live 2026-08-31) — calling it ".csv" and letting pandas
    assume commas parsed every row into ONE column, which looked like a successful run.
    """
    with page.expect_download(timeout=300_000) as dl:   # server takes ~30s to build the file
        click_target()
    d = dl.value
    fn = (d.suggested_filename or f"{idx}.txt")
    ext = ".xlsx" if fn.lower().endswith((".xlsx", ".xls")) else ".txt"
    dest = os.path.join(dest_dir, f"{idx:02d}{ext}")
    d.save_as(dest)
    log(f"    download captured: {fn!r} -> {dest} ({os.path.getsize(dest):,} bytes)")
    return dest, ext


def _sniff_sep(path):
    """Pick the delimiter from the header line rather than assuming one. MyLicense uses
    '|'; if it ever switches to comma or tab this keeps working instead of silently
    collapsing every row into a single column."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.readline()
    counts = {sep: head.count(sep) for sep in ("|", "	", ",", ";")}
    sep = max(counts, key=counts.get)
    if counts[sep] == 0:
        raise RuntimeError(f"no delimiter found in the header of {path}: {head[:120]!r}")
    log(f"    delimiter sniffed: {sep!r} ({counts[sep]} occurrences in the header)")
    return sep


def _frame_from_file(path, ext, label):
    if ext == ".xlsx":
        df = pd.read_excel(path, dtype=str)
    else:
        sep = _sniff_sep(path)
        # dtype=str + keep_default_na=False: leading zeros (addr_zipcode '07008') and
        # alphanumeric licence numbers must survive verbatim.
        df = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False,
                         engine="python", on_bad_lines="warn")
    # every line ends with the delimiter, so pandas invents an empty trailing column
    drop = [c for c in df.columns
            if str(c).startswith("Unnamed:") and (df[c].astype(str).str.strip() == "").all()]
    if drop:
        df = df.drop(columns=drop)
        log(f"    dropped {len(drop)} phantom trailing column(s) from the trailing delimiter")
    if len(df.columns) < 3:
        raise RuntimeError(f"only {len(df.columns)} column(s) parsed from {path} "
                           f"({list(df.columns)[:4]}) — the delimiter is wrong; refusing "
                           f"to treat this as a valid frame.")
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


# Wider keyword set for recon so the practitioner-vs-facility/supply-chain picture is visible.
RECON_KEYWORDS = ["pharmac", "wholesal", "manufactur", "distribut", "device", "3pl",
                  "outsourc", "logistic", "repackag", "cds", "controlled"]


def recon(page):
    prof, ltype, status = _load_form(page)
    dump = {}
    for nm, s in [("Profession", prof), ("License Type", ltype), ("License Status", status)]:
        if s is None:
            log(f"{nm}: NOT FOUND — inspect nj_recon.png and adjust LOCATORS hints")
            continue
        opts = [o["text"] for o in _options(s) if o["text"]]
        dump[nm] = opts
        log(f"{nm}: name={_name_of(s).strip()!r} — {len(opts)} options -> {opts[:12]}{'...' if len(opts) > 12 else ''}")
        if s is ltype:
            if EXACT_TYPES:
                want = {t.strip().lower() for t in EXACT_TYPES}
                scope_hits = [o for o in opts if o.strip().lower() in want]
                lbl = "default exact scope (personnel)"
            else:
                scope_hits = [o for o in opts if any(k in o.lower() for k in TYPE_KEYWORDS)]
                lbl = "--types keyword scope"
            wide_hits = [o for o in opts if any(k in o.lower() for k in RECON_KEYWORDS)]
            log(f"  License Type — {lbl} ({len(scope_hits)}): {scope_hits}")
            log(f"  License Type — WIDE scan (pharmacy/facility/supply-chain/cds) ({len(wide_hits)}):")
            for h in wide_hits:
                log(f"      · {h}")
    json.dump(dump, open("nj_options.json", "w"), indent=2)
    log("full option lists -> nj_options.json  (attach it here to pick the exact in-scope types)")
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
    if EXACT_TYPES:
        want = {t.strip().lower() for t in EXACT_TYPES}
        scoped = [o for o in opts if o["text"].strip().lower() in want]
    else:
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


def _find_download_list(page):
    """The 'Download List' button on the SearchResults page (the real file trigger)."""
    for el in page.query_selector_all("a, button, input[type=submit], input[type=button]"):
        try:
            t = ((el.inner_text() or "") + " " + (el.get_attribute("value") or "")).strip().lower()
            if el.is_visible() and "download list" in t:
                return el
        except Exception:
            pass
    return None


def _find_any_download(page):
    """Looser fallback: any 'download' control (not a 'Home' nav) or a file-looking href."""
    for el in page.query_selector_all("a, button, input[type=submit], input[type=button]"):
        try:
            t = ((el.inner_text() or "") + " " + (el.get_attribute("value") or "")).strip().lower()
            if el.is_visible() and "download" in t and "home" not in t:
                return el
        except Exception:
            pass
    for a in page.query_selector_all("a[href]"):
        try:
            h = (a.get_attribute("href") or "").lower().split("?")[0]
            if a.is_visible() and any(h.endswith(e) for e in (".csv", ".xls", ".xlsx", ".zip", ".txt")):
                return a
        except Exception:
            pass
    return None


def _find_continue(page):
    """The 'Continue' control on the $0.00 Confirmation page."""
    for el in page.query_selector_all("a, button, input[type=submit], input[type=button]"):
        try:
            t = ((el.inner_text() or "") + " " + (el.get_attribute("value") or "")).strip().lower()
            if el.is_visible() and any(w in t for w in ("continue", "proceed", "i agree", "accept", "confirm")) \
                    and "return" not in t:
                return el
        except Exception:
            pass
    return None


def _find_pref_download(page):
    """The REAL file trigger: the 'Download' button on PrefDetails.aspx.

    Confirmed live 2026-08-31 by network trace: name='sch_button', id='sch_button',
    value='Download'. Excludes 'Close Window', which sits beside it. PrefDetails is the
    'Preferred Details' page whose screenshots explain Excel's text-import wizard --
    i.e. it exists precisely because the payload is delimited text.
    """
    for sel in ("input[type=submit]", "input[type=button]", "button", "a"):
        for el in page.query_selector_all(sel):
            try:
                if not el.is_visible():
                    continue
                t = ((el.inner_text() or "") + " " + (el.get_attribute("value") or "")).strip().lower()
                if "download" in t and "close" not in t and "home" not in t:
                    return el
            except Exception:
                pass
    return None


def _charge_amount(page):
    """Parse the 'your account will be charged $X' line. Returns (amount_or_None, text)."""
    try:
        txt = page.inner_text("body") or ""
    except Exception:
        txt = ""
    m = re.search(r"charged[^$]*\$\s*([\d,]+(?:\.\d+)?)", txt, re.I)
    return (float(m.group(1).replace(",", "")) if m else None), txt


def _download_current(page, dest_dir, idx, label):
    """Multi-step (confirmed via --diagnose + Confirmation screenshot, 2026-08):
    Search -> SearchResults.aspx grid -> 'Download List' -> Confirmation.aspx ($0.00) ->
    'Continue' -> PrefDetails.aspx -> 'Download' -> Data.txt (pipe-delimited).
    FOUR steps, not three: only the LAST click is a download. 'Continue' merely
    navigates, which is why expect_download() around it timed out at 300s."""
    # Step 1 — Search -> results grid (navigation, NOT a download).
    _click_submit(page)
    try:
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(1800)
    if "searchresults" not in page.url.lower():
        log(f"    (note: expected SearchResults, at {page.url})")
    # Step 2 — 'Download List' -> Confirmation page (navigation, NOT a download).
    btn = _find_download_list(page) or _find_any_download(page)
    if btn is None:
        raise RuntimeError(f"'Download List' not found on results page (url={page.url})")
    btn.click()
    try:
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(1500)
    # Step 3 — Confirmation page: verify the charge is $0.00, then 'Continue' delivers the file.
    if "confirmation" in page.url.lower() or _find_continue(page) is not None:
        amt, _txt = _charge_amount(page)
        if amt is not None and amt > 0:
            raise RuntimeError(f"PURCHASING GATE: confirmation shows a ${amt:.2f} charge — stopping. "
                               f"A paid download needs human approval (this used to be $0.00).")
        cont = _find_continue(page)
        if cont is None:
            raise RuntimeError(f"confirmation page but no 'Continue' control (url={page.url})")
        log(f"    confirmation OK (charge ${amt if amt is not None else 0:.2f}); clicking Continue")
        # Step 3b — THE FIX. 'Continue' is NOT the download; it only NAVIGATES to
        # PrefDetails.aspx. Wrapping this click in expect_download is why the run hung for
        # 300s and then reported "No downloads succeeded" (network trace, 2026-08-31:
        # 0 download events, 0 popups, 0 Content-Disposition responses after Continue).
        cont.click()
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(2500)
        log(f"    after Continue: {page.url}")
        # Step 4 — PrefDetails.aspx carries its own 'Download' (name=sch_button). THAT is
        # the file trigger; the server takes ~30s to build the ~6MB Data.txt.
        pref = _find_pref_download(page)
        if pref is not None:
            log("    PrefDetails reached; clicking its 'Download' (the real file trigger)")
            return _read_download(page, lambda: pref.click(), dest_dir, idx)
        # Some variants may hand the file straight off Continue — try that before failing.
        log(f"    no PrefDetails Download control at {page.url}; "
            f"falling back to any download control on this page")
        alt = _find_any_download(page)
        if alt is not None:
            return _read_download(page, lambda: alt.click(), dest_dir, idx)
        try:
            page.screenshot(path=f"nj_pref_fail_{idx}.png", full_page=True)
        except Exception:
            pass
        raise RuntimeError(f"Continue led to {page.url} but no download control was found "
                           f"(see nj_pref_fail_{idx}.png)")
    # Fallback: some variants download straight from 'Download List'.
    nxt = _find_any_download(page)
    if nxt is not None:
        return _read_download(page, lambda: nxt.click(), dest_dir, idx)
    try:
        page.screenshot(path=f"nj_dl_fail_{idx}.png", full_page=True)
    except Exception:
        pass
    raise RuntimeError(f"no download after 'Download List' (url={page.url}; see nj_dl_fail_{idx}.png)")


def _goto_fresh(page):
    """Reload the form to a clean state. Returns nothing — always RE-FIND selects after this,
    never reuse a handle across a postback (that is the 'Cannot find context' crash)."""
    page.goto(NJ_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)


def _set_status_all(page):
    """(Re-)find the Status select and set it to All. Safe to call right before selecting type."""
    status = _find_select(page, LOCATORS["status_name_hints"], _has_all)
    if status is not None:
        _set_all(status); page.wait_for_timeout(1200)


def full(page, out):
    # Read the License-Type options FIRST, before any select_option — an ASP.NET postback
    # (Status/Profession may auto-post-back) invalidates any handle grabbed earlier.
    _goto_fresh(page)
    ltype = _find_select(page, LOCATORS["license_type_name_hints"], _has_all)
    if ltype is None:
        raise RuntimeError("License Type select not found — run --recon and adjust LOCATORS hints.")
    dest = tempfile.mkdtemp(prefix="nj_")
    frames = []

    if ALL_QUERY:
        _set_status_all(page)
        lt = _find_select(page, LOCATORS["license_type_name_hints"], _has_all)   # re-find after postback
        _set_all(lt); page.wait_for_timeout(1000)
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
        uniq = _in_scope_types(ltype)          # read the list NOW, before touching any control
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
                # fresh page each iteration → no handle survives across a postback
                _goto_fresh(page)
                _set_status_all(page)                                   # may post back
                lt = _find_select(page, LOCATORS["license_type_name_hints"], _has_all)  # re-find AFTER
                if lt is None or _select_by_label_contains(lt, name) is None:
                    log(f"  [{i}/{len(uniq)}] {name}: option not found after reload — skipping"); continue
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
    # ---- DEDUPE GUARD ------------------------------------------------------------
    # Two separate traps here, both found live on this source (2026-08-31):
    #  (1) 5,169 of 39,016 Pharmacist rows have a BLANK license_no (Deleted / Pending /
    #      Withdrawn / Denied registry entries). drop_duplicates(subset=['license_no'])
    #      collapses every one of them into a SINGLE row -- losing 5,168 real records.
    #      So blank-key rows are split out and de-duped whole-row instead.
    #  (2) 152 licence numbers legitimately repeat because NJ carries STATUS HISTORY
    #      (Expired + Reinstatement Pending + Deleted on one number). The number alone is
    #      therefore not the grain; the ladder adds tie-break columns until the loss is
    #      plausible, and if nothing gets under 10% it KEEPS ALL ROWS and warns.
    chosen = None
    if key:
        blank_mask = allrows[key].fillna("").astype(str).str.strip() == ""
        keyed, blanks = allrows[~blank_mask], allrows[blank_mask]
        if len(blanks):
            log(f"DEDUPE: {len(blanks):,} row(s) have a blank {key!r} "
                f"(status: {dict(blanks['license_status_name'].value_counts()) if 'license_status_name' in blanks.columns else 'n/a'})"
                f" — these CANNOT be keyed; de-duping them whole-row instead of collapsing "
                f"them into one.")
        # Walk the ladder and pick the key that PRESERVES THE MOST rows, not merely the
        # first one under the threshold. Taking the first sub-10% candidate picked bare
        # ['license_no'] at 0.46% loss -- which looks clean but silently discarded real
        # STATUS-HISTORY rows (Reinstatement Pending collapsed from 66 rows to 4). Adding
        # license_status_name recovers them at 0.003% loss. Rule: extend while an added
        # tie-break column materially recovers rows; stop once it recovers nothing, so we
        # do not pile on columns until de-dupe becomes a no-op.
        ladder = []
        for i in range(len(DEDUPE_TIEBREAK) + 1):
            cand = list(dict.fromkeys(
                [key] + [c for c in DEDUPE_TIEBREAK[:i] if c in keyed.columns]))
            if cand not in ladder:
                ladder.append(cand)
        EPS = 0.0005          # 0.05 percentage points of the keyed population
        results = []
        for cand in ladder:
            d = keyed.drop_duplicates(subset=cand)
            loss = 1 - len(d) / len(keyed) if len(keyed) else 0
            results.append((cand, d, loss))
            log(f"  candidate key {cand} -> {len(d):,} of {len(keyed):,} (loss {loss:.3%})")
        best_loss = min(r[2] for r in results)
        # shortest key whose loss is within EPS of the best -> the true grain
        chosen, kept, chosen_loss = next(
            (c, d, l) for c, d, l in results if l <= best_loss + EPS)
        if chosen_loss > 0.10:
            log(f"  DEDUPE GUARD TRIPPED: the best candidate {chosen} still loses "
                f"{chosen_loss:.1%} (>10%) — KEEPING ALL {len(keyed):,} keyed rows. "
                f"Review the grain before trusting.")
            kept, chosen = keyed, ["(none — all rows kept)"]
        elif len(chosen) > 1:
            log(f"  chose {chosen} over bare [{key!r}]: the extra column(s) recover "
                f"{len(kept) - len(results[0][1]):,} row(s) that are legitimate history, "
                f"not duplicates.")
        b2 = blanks.drop_duplicates()
        if len(blanks):
            log(f"  blank-key rows: {len(blanks):,} -> {len(b2):,} after whole-row de-dupe")
        allrows = pd.concat([kept, b2], ignore_index=True)
        log(f"DEDUPE: keyed on {chosen}; {before:,} -> {len(allrows):,}")
    else:
        allrows = allrows.drop_duplicates()
        log(f"NO known licence-number column found (columns={list(allrows.columns)[:8]}) — "
            f"de-duped on WHOLE ROWS: {before:,} -> {len(allrows):,}. CONFIRM headers.")
    if allrows.empty:
        raise RuntimeError("0 rows after de-dupe — refusing to write an empty roster.")
    # per-type counts, and status spread, for the run summary
    if "__license_type" in allrows.columns:
        log("")
        log("per-type row counts:")
        for k_, v_ in allrows["__license_type"].value_counts().items():
            log(f"  {str(k_)[:44]:<46} {v_:>7,}")
    if "license_status_name" in allrows.columns:
        log("")
        log("licence-status spread:")
        for k_, v_ in allrows["license_status_name"].replace("", "(blank)").value_counts().items():
            log(f"  {str(k_)[:44]:<46} {v_:>7,}")
    log("")
    allrows.to_csv(out, index=False)
    log(f"wrote {out}: {len(allrows):,} rows "
        f"(stacked {before:,}, removed {before - len(allrows):,} duplicate(s), "
        f"key={chosen if key else 'whole row'})")
    log("VERIFY before trusting: (1) non-empty & plausible; (2) per-type counts vs an --all-query count; "
        "(3) leading zeros preserved (values are strings); (4) a known CDS registrant present; "
        "(5) both active and expired present.")


def diagnose(page):
    """Fill the form for the first in-scope type, click Search ONCE, and dump what the
    post-Search page presents — so we can see how the file is actually delivered
    (download event? results-page link? new tab? inline?). No 5-minute waits."""
    _goto_fresh(page)
    lt0 = _find_select(page, LOCATORS["license_type_name_hints"], _has_all)
    if lt0 is None:
        log("License Type select not found"); return
    uniq = _in_scope_types(lt0)
    if not uniq:
        log("no in-scope types matched"); return
    name = uniq[0]["text"]
    log(f"diagnosing with license type = {name!r}")
    _set_status_all(page)
    lt = _find_select(page, LOCATORS["license_type_name_hints"], _has_all)
    _select_by_label_contains(lt, name)
    page.wait_for_timeout(1000)
    got = {}
    page.on("download", lambda d: got.setdefault("file", d.suggested_filename))
    page.context.on("page", lambda p: got.setdefault("popup", p.url))
    log("clicking Search ...")
    if not _click_submit(page):
        log("!! could not find/click a Search control")
    page.wait_for_timeout(15000)   # let it render / a download start
    log(f"after Search: url = {page.url}")
    log(f"open tabs: {len(page.context.pages)}")
    if got.get("file"): log(f"DOWNLOAD EVENT fired -> {got['file']}")
    if got.get("popup"): log(f"POPUP/new tab -> {got['popup']}")
    if not got: log("no download event and no popup within 15s")
    links = []
    for a in page.query_selector_all("a[href]"):
        try:
            if a.is_visible():
                links.append(((a.inner_text() or "").strip()[:45], a.get_attribute("href")))
        except Exception:
            pass
    log(f"visible links ({len(links)}):")
    for t, h in links[:40]:
        log(f"   {t!r} -> {h}")
    files = [h for _, h in links if h and any(h.lower().split('?')[0].endswith(e)
             for e in (".csv", ".xls", ".xlsx", ".zip", ".txt"))]
    log(f"file-looking hrefs: {files}")
    btns = []
    for b in page.query_selector_all("button, input[type=submit], input[type=button]"):
        try:
            if b.is_visible():
                btns.append(((b.inner_text() or b.get_attribute("value") or "").strip()))
        except Exception:
            pass
    log("visible buttons: " + ", ".join([x for x in dict.fromkeys(btns) if x][:25]))
    body = ""
    try:
        body = (page.inner_text("body") or "")[:600]
    except Exception:
        pass
    log("page text (first 600 chars):\n" + body)
    page.screenshot(path="nj_after_search.png", full_page=True)
    log("screenshot -> nj_after_search.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--all-query", action="store_true", help="single All query + post-filter")
    ap.add_argument("--types", default="", help="comma keywords overriding the scope allowlist")
    ap.add_argument("--out", default="nj_licenses.csv")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--diagnose", action="store_true", help="click Search once and dump the post-Search page")
    args = ap.parse_args()
    global LIMIT, TYPE_KEYWORDS, EXACT_TYPES, ALL_QUERY
    LIMIT = args.limit
    ALL_QUERY = args.all_query
    if args.types.strip():
        TYPE_KEYWORDS = [k.strip().lower() for k in args.types.split(",") if k.strip()]
        EXACT_TYPES = []   # --types switches to keyword matching, overriding the exact personnel default
    with sync_playwright() as p:
        launch = {}
        cp = os.environ.get("CHROMIUM_PATH")
        if cp: launch["executable_path"] = cp
        b = p.chromium.launch(headless=not args.headed, **launch)
        page = b.new_page(accept_downloads=True)
        page.set_default_timeout(45000)
        try:
            mode = recon if args.recon else diagnose if args.diagnose else (lambda pg: full(pg, args.out))
            mode(page)
        finally:
            b.close()


if __name__ == "__main__":
    main()
