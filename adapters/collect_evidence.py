#!/usr/bin/env python3
"""Deep read-only evidence collector for LHAIV source pressure-testing.

probe_sources.py yields a *verdict*; this yields the *evidence needed to verify it*.
For each URL it records, without ever submitting a form, logging in, solving a
CAPTCHA or paying anything:

  status / final_url / title  - did we even land on the intended page
  captcha_dom / captcha_script - reCAPTCHA/hCaptcha/Turnstile iframes + scripts
  file_links                  - real .csv/.xlsx/.zip/.json hrefs (the Tier-1 tell)
  list_now                    - does a result TABLE already render with NO input?
                                (the Tier-3 vs Tier-4 discriminator)
  table_rows / pagination / total_hint - is the population browsable, and how big
  selects                     - dropdowns + option counts: an enumerable axis,
                                which is exactly what the VT adapter exploits
  export_ui                   - export/CSV/Excel/print controls present in the DOM
  required_inputs             - a required name/license# box means single-record
  purchase_ui / records_ui    - list-sales / records-request language IN CONTEXT,
                                scoped to main content so footer boilerplate stops
                                masquerading as a real channel

Output: evidence.json (full, resumable) + evidence.csv (flat, for eyeballing).
"""
import json, os, re, time, argparse
import pandas as pd
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) LHAIV-source-recon/1.0 "
      "(read-only licensing-data feasibility check)")
POLITE = 2.0

FILE_RX = re.compile(r"\.(csv|xlsx?|zip|tsv|json|mdb|accdb)(\?|$)", re.I)
TOTAL_RX = re.compile(
    r"(?:of|totals?|found|showing|results?)\D{0,15}([\d,]{2,12})\s*"
    r"(?:records?|results?|licens\w*|rows?|entries|item)?", re.I)
PAGE_RX = re.compile(
    r"(next\s*(?:page|>)?|page\s+\d+\s+of\s+\d+|pagination|showing\s+\d+\s*[-]\s*\d+)", re.I)
EXPORT_RX = re.compile(
    r"\b(export|download|csv|excel|xlsx|spreadsheet|print list|print listing|extract)\b", re.I)
PURCH_RX = re.compile(
    r"\b(list sales|list request|purchase a list|roster request|roster download|"
    r"data request|licensee list|mailing list)\b", re.I)
RECORD_RX = re.compile(
    r"\b(public records request|open records request|records request|foia|uipa|nextrequest)\b", re.I)


def _sel_info(page):
    out = []
    for s in page.query_selector_all("select"):
        try:
            if not s.is_visible():
                continue
            opts = [(o.inner_text() or "").strip() for o in s.query_selector_all("option")]
            name = (s.get_attribute("name") or s.get_attribute("id")
                    or s.get_attribute("aria-label") or "")
            out.append({"name": name[:60], "n_options": len(opts),
                        "first": opts[0][:40] if opts else "",
                        "sample": [o[:34] for o in opts[1:6]]})
        except Exception:
            pass
    return out


def _required(page):
    out = []
    for i in page.query_selector_all("input[type=text], input[type=search], input:not([type])"):
        try:
            if not i.is_visible():
                continue
            lab = (i.get_attribute("placeholder") or i.get_attribute("name")
                   or i.get_attribute("id") or i.get_attribute("aria-label") or "")
            req = (i.get_attribute("required") is not None
                   or (i.get_attribute("aria-required") or "") == "true")
            out.append({"field": lab[:50], "required": bool(req)})
        except Exception:
            pass
    return out


def _tables(page):
    """Biggest data-ish table already on the page with NO interaction from us."""
    best = {"rows": 0, "cols": 0, "headers": []}
    for t in page.query_selector_all(
            "table, [role=table], .k-grid, .dataTable, .rgMasterTable, .ui-grid"):
        try:
            rows = t.query_selector_all("tr, [role=row]")
            if len(rows) <= best["rows"]:
                continue
            cells = rows[0].query_selector_all("th, td, [role=columnheader]") if rows else []
            hdr = [(c.inner_text() or "").strip()[:26] for c in cells]
            best = {"rows": len(rows), "cols": len(hdr), "headers": hdr[:12]}
        except Exception:
            pass
    return best


def _main_text(page):
    """Text scoped to main content, so footer 'Public Records' boilerplate stops
    masquerading as a real records channel (the probe's biggest false positive)."""
    for sel in ["main", "[role=main]", "#main", "#content", ".content", "article"]:
        try:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text() or ""
                if len(t) > 200:
                    return t
        except Exception:
            pass
    try:
        body = page.inner_text("body") or ""
    except Exception:
        return ""
    return body[: int(len(body) * 0.85)]  # crude footer strip


def probe(page, url):
    ev = {"url": url, "status": None, "final_url": "", "title": "", "error": ""}
    try:
        try:
            resp = page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        ev["status"] = resp.status if resp else None
        ev["final_url"] = page.url
        ev["title"] = (page.title() or "")[:120]
        html = page.content()
        main = _main_text(page)

        cap = page.query_selector_all(
            "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], "
            ".g-recaptcha, .h-captcha, .cf-turnstile, #captcha")
        ev["captcha_dom"] = len(cap)
        ev["captcha_script"] = bool(re.search(
            r"(recaptcha/api\.js|hcaptcha\.com/1|challenges\.cloudflare\.com/turnstile|grecaptcha)",
            html, re.I))

        files = []
        for a in page.query_selector_all("a[href]"):
            try:
                h = a.get_attribute("href") or ""
                if FILE_RX.search(h):
                    files.append({"href": h[:160], "text": (a.inner_text() or "").strip()[:60]})
            except Exception:
                pass
        ev["file_links"] = files[:12]
        ev["n_file_links"] = len(files)

        tb = _tables(page)
        ev["table_rows"], ev["table_cols"], ev["table_headers"] = tb["rows"], tb["cols"], tb["headers"]
        ev["list_now"] = tb["rows"] >= 5 and tb["cols"] >= 2

        ev["pagination"] = bool(PAGE_RX.search(main))
        tot = TOTAL_RX.search(main)
        ev["total_hint"] = tot.group(0).strip()[:60] if tot else ""
        ev["selects"] = _sel_info(page)
        ev["required_inputs"] = _required(page)[:12]

        btn = []
        for b in page.query_selector_all(
                "button, a, input[type=submit], input[type=button], [role=button]"):
            try:
                t = (b.inner_text() or b.get_attribute("value") or "").strip()
                if t:
                    btn.append(t[:44])
            except Exception:
                pass
        ev["export_ui"] = sorted({t for t in btn if EXPORT_RX.search(t)})[:10]
        ev["purchase_ui"] = sorted(set(PURCH_RX.findall(main.lower())))[:8]
        ev["records_ui"] = sorted(set(RECORD_RX.findall(main.lower())))[:8]
        ev["main_len"] = len(main)
    except Exception as e:
        ev["error"] = f"{type(e).__name__}: {e}"[:220]
    return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="LHAIV_Source_PressureTest_Worklist.xlsx")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="evidence.json")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    df = pd.read_excel(args.inp, sheet_name="Source Pressure-Test", header=5, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    def col(sub):
        for c in df.columns:
            if sub.lower() in c.lower():
                return c

    cS, cA, cU, cB = col("State"), col("Agency"), col("URL"), col("Bucket")
    df = df[df[cS].notna()]
    if args.only:
        L = tuple(x.strip().upper() for x in args.only.split(","))
        df = df[df[cB].astype(str).str.strip().str.startswith(L)]

    done = {}
    if args.resume and os.path.exists(args.out):
        done = {d["key"]: d for d in json.load(open(args.out, encoding="utf-8"))}
        print(f"resuming: {len(done)} already collected", flush=True)

    rows = [r for _, r in df.iterrows()]
    results = list(done.values())
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for n, r in enumerate(rows, 1):
            st, ag = str(r[cS]).strip(), str(r[cA]).strip()
            url = str(r.get(cU, "")).strip()
            key = f"{st}|{ag}"
            if key in done:
                continue
            if not url.lower().startswith("http"):
                results.append({"key": key, "State": st, "Agency": ag,
                                "Bucket": str(r[cB]), "url": url,
                                "error": "NO-URL (manual review)"})
                continue
            print(f"[{n}/{len(rows)}] {st} | {ag}", flush=True)
            pg = b.new_page(user_agent=UA)
            ev = probe(pg, url)
            try:
                pg.close()
            except Exception:
                pass
            ev.update({"key": key, "State": st, "Agency": ag, "Bucket": str(r[cB])})
            results.append(ev)
            print(f"    status={ev.get('status')} files={ev.get('n_file_links')} "
                  f"rows={ev.get('table_rows')} list_now={ev.get('list_now')} "
                  f"captcha={ev.get('captcha_dom')}/{ev.get('captcha_script')} "
                  f"sel={len(ev.get('selects') or [])} err={ev.get('error', '')[:60]}", flush=True)
            json.dump(results, open(args.out, "w", encoding="utf-8"), indent=1)
            time.sleep(POLITE)
        b.close()

    json.dump(results, open(args.out, "w", encoding="utf-8"), indent=1)
    flat = [{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
             for k, v in e.items()} for e in results]
    pd.DataFrame(flat).to_csv(args.out.replace(".json", ".csv"),
                              index=False, encoding="utf-8-sig")
    print(f"\nwrote {args.out} ({len(results)} rows) + csv")


if __name__ == "__main__":
    main()
