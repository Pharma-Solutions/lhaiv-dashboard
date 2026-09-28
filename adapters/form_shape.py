#!/usr/bin/env python3
"""Read-only form-shape inspector: the Tier-3 vs Tier-4 discriminator.

Tier 3 (scrapeable) needs ALL of:
  - no CAPTCHA on the search
  - an enumerable axis: a dropdown with many real options, or an A-Z browse,
    or an "All" option - something that lets you walk the whole population
  - no REQUIRED free-text identifier (a mandatory name/license# box means you
    must already know the licensee, i.e. single-record only -> Tier 4)

We never submit anything; we only read the form's shape.
"""
import re
import sys

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) LHAIV-source-recon/1.0 "
      "(read-only licensing-data feasibility check)")
CAP_RX = re.compile(r"(recaptcha/api\.js|hcaptcha\.com/1|challenges\.cloudflare\.com/turnstile|grecaptcha)", re.I)
ALL_RX = re.compile(r"^\s*(all|any|-?-?\s*all\s*-?-?|\(all\)|all types?|select all)\s*$", re.I)
AZ_RX = re.compile(r"(browse|a-z|alphabetical|by letter|starts with)", re.I)


def look(page, url):
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            return {"url": url, "error": f"{type(e).__name__}: {e}"[:120]}
    page.wait_for_timeout(3000)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    txt = soup.get_text(" ", strip=True)

    out = {"url": url, "status_title": (page.title() or "")[:70],
           "captcha": bool(CAP_RX.search(html)), "error": ""}

    # visible captcha widget vs merely-loaded script
    try:
        out["captcha_widget"] = bool(page.query_selector(
            "iframe[src*='recaptcha'], .g-recaptcha, iframe[src*='hcaptcha'], .cf-turnstile"))
    except Exception:
        out["captcha_widget"] = False

    sels = []
    for s in soup.find_all("select"):
        opts = [o.get_text(strip=True) for o in s.find_all("option")]
        if len(opts) < 2:
            continue
        sels.append({"name": (s.get("name") or s.get("id") or "")[:46],
                     "n": len(opts),
                     "has_all": any(ALL_RX.match(o) for o in opts),
                     "sample": [o[:30] for o in opts[:5]]})
    sels.sort(key=lambda x: -x["n"])
    out["selects"] = sels[:6]
    out["max_select"] = sels[0]["n"] if sels else 0
    out["any_all_option"] = any(s["has_all"] for s in sels)

    reqs = []
    for i in soup.find_all("input"):
        if (i.get("type") or "text").lower() not in ("text", "search", ""):
            continue
        nm = (i.get("name") or i.get("id") or i.get("placeholder") or "")[:40]
        if not nm:
            continue
        reqs.append({"f": nm, "req": i.has_attr("required") or i.get("aria-required") == "true"})
    out["text_inputs"] = reqs[:10]
    out["n_required_text"] = sum(1 for r in reqs if r["req"])

    out["az_browse"] = bool(AZ_RX.search(txt))
    out["export_words"] = sorted({w for w in re.findall(
        r"\b(export|download|csv|excel|print listing|print list|full list|entire list)\b", txt, re.I)})[:6]
    out["n_tables"] = len(soup.find_all("table"))
    return out


def main():
    urls = sys.argv[1:]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for u in urls:
            pg = b.new_page(user_agent=UA)
            r = look(pg, u)
            try:
                pg.close()
            except Exception:
                pass
            print("=" * 92)
            print(r["url"])
            if r.get("error"):
                print("  ERROR:", r["error"])
                continue
            print(f"  title={r['status_title']!r}")
            print(f"  CAPTCHA script={r['captcha']}  visible-widget={r['captcha_widget']}")
            print(f"  enumerable: max_select={r['max_select']}  any_'All'_option={r['any_all_option']}  "
                  f"az_browse={r['az_browse']}")
            print(f"  required free-text fields={r['n_required_text']}  tables={r['n_tables']}")
            if r["export_words"]:
                print(f"  export words: {r['export_words']}")
            for s in r["selects"]:
                print(f"    select {s['name']!r}: {s['n']} opts all={s['has_all']} {s['sample']}")
            if r["text_inputs"]:
                print(f"    text inputs: {[(x['f'], x['req']) for x in r['text_inputs'][:6]]}")
        b.close()


if __name__ == "__main__":
    main()
