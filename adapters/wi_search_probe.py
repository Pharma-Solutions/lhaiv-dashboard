#!/usr/bin/env python3
"""WI DSPS - capture ONE searchLicense response per credential, then stop.

RULES OF ENGAGEMENT (we tripped Cloudflare on 2026-09-28 by over-probing)
  * one headed session, ~5 calls total, multi-second pauses between them
  * ANY 403 -> stop immediately for the day. No retry, no UA rotation, no stealth
    patching, no proxy. Those would evade a protection the operator put there.
  * replay the known contract; do NOT re-drive the UI (that was most of the traffic)

CONTRACT (captured 2026-09-28)
  DSPS_LicensesLookupController.searchLicense
    searchType : "Profession"
    dataObj    : {"selectedCategory":"Health","selectedProfession":"<credentialId>"}
    recapToken : ""     - wired but inert; kept empty, never populated

The response is matched by ACTION ID, not by taking the largest body.
"""
import json
import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "https://license.wi.gov/s/license-lookup"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
CREDENTIALS = [
    ("Wholesale Distributor of Prescription Drugs", "0eh3d00000002xfAAA"),
    ("Pharmacy (In-State)",                          "0eh3d00000002xcAAA"),
    ("Pharmacy (Out-of-State)",                      "0eh3d00000002xdAAA"),
    ("Third-Party Logistics Provider",               "0eh3d00000002xeAAA"),
    ("Drug or Device Manufacturer",                  "0eh3d00000002xZAAQ"),
]
PAUSE = 12          # seconds between calls - deliberate, not exploratory
OUT = "wi_search_payloads.json"


def log(m):
    print("[wi] " + str(m), flush=True)


def main():
    env = {}

    def on_req(r):
        if "/aura" in r.url and r.method == "POST" and "ApexAction" in (r.post_data or ""):
            if not env:
                env["url"] = r.url
                env["body"] = r.post_data
                env["headers"] = dict(r.headers)

    payloads = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = b.new_context(user_agent=UA, viewport=None, locale="en-US")
        pg = ctx.new_page()
        pg.set_default_timeout(120000)
        pg.on("request", on_req)

        try:
            pg.goto(URL, wait_until="networkidle", timeout=90000)
        except Exception:
            pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(9000)

        body_txt = pg.inner_text("body")
        if re.search(r"(attention required|verify you are human|checking your browser)",
                     body_txt, re.I):
            log("!! Cloudflare challenge on page load - still throttled. STOPPING.")
            b.close()
            sys.exit(3)
        if not env:
            log("!! no Aura envelope captured on load - cannot replay. STOPPING.")
            b.close()
            sys.exit(4)
        log("page loaded, envelope captured")

        parts = urllib.parse.parse_qs(env["body"], keep_blank_values=True)
        base_msg = json.loads(parts["message"][0])
        hdr = {k: v for k, v in env["headers"].items()
               if k.lower().startswith(("x-sfdc", "x-b3"))
               or k.lower() in ("referer", "accept-language")}
        hdr["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        hdr["x-sfdc-lds-endpoints"] = ("ApexActionController.execute:"
                                       "DSPS_LicensesLookupController.searchLicense")

        for n, (label, cid) in enumerate(CREDENTIALS):
            if n:
                log("  pausing %ds" % PAUSE)
                time.sleep(PAUSE)
            msg = json.loads(json.dumps(base_msg))
            act = msg["actions"][0]
            action_id = "%d;a" % (900 + n)          # distinctive, for response matching
            act["id"] = action_id
            act["params"] = {"namespace": "", "classname": "DSPS_LicensesLookupController",
                             "method": "searchLicense",
                             "params": {"searchType": "Profession",
                                        "dataObj": json.dumps(
                                            {"selectedCategory": "Health",
                                             "selectedProfession": cid}),
                                        "recapToken": ""},
                             "cacheable": False, "isContinuation": False}
            parts["message"] = [json.dumps(msg, separators=(",", ":"))]
            body = urllib.parse.urlencode({k: v[0] for k, v in parts.items()})

            out = pg.evaluate("""async ([url, body, hdr]) => {
                const r = await fetch(url, {method:'POST', credentials:'include',
                                            headers:hdr, body});
                return {status:r.status, text:await r.text()};
            }""", [env["url"], body, hdr])
            st, txt = out["status"], out["text"]
            log("%-44s HTTP %s  %s bytes" % (label, st, format(len(txt), ",")))

            if st == 403 or not txt.strip().startswith("{"):
                log("")
                log("!! 403 / non-JSON - Cloudflare is still throttling.")
                log("   STOPPING for the day per the rules of engagement.")
                log("   No retry, no UA rotation, no stealth, no proxy.")
                open("wi_block_evidence.html", "w", encoding="utf-8").write(txt[:4000])
                b.close()
                sys.exit(5)

            j = json.loads(txt)
            act_out = next((a for a in j.get("actions", []) if a.get("id") == action_id), None)
            if act_out is None:
                log("   !! no action matching id %r in the response" % action_id)
                continue
            if act_out.get("state") != "SUCCESS":
                log("   state=%s error=%s" % (act_out.get("state"),
                                              json.dumps(act_out.get("error"))[:300]))
                continue
            rv = act_out.get("returnValue")
            rv = rv.get("returnValue", rv) if isinstance(rv, dict) else rv
            payloads[label] = {"credential_id": cid, "raw_len": len(txt), "returnValue": rv}
            if isinstance(rv, list):
                log("   -> %s record(s)" % format(len(rv), ","))
            elif isinstance(rv, dict):
                log("   -> dict, keys=%s" % list(rv.keys())[:12])
            else:
                log("   -> %s" % type(rv).__name__)
        b.close()

    json.dump(payloads, open(OUT, "w", encoding="utf-8"), indent=1)
    log("")
    log("wrote %s" % OUT)
    log("=" * 74)
    for label, d in payloads.items():
        rv = d["returnValue"]
        if isinstance(rv, list):
            n = len(rv)
            log("%-44s %6s record(s)   cap-tell: %s"
                % (label, format(n, ","), "SUSPICIOUS (round)" if n and n % 50 == 0 else "no"))
        else:
            log("%-44s %s" % (label, type(rv).__name__))
    first = next((d["returnValue"] for d in payloads.values()
                  if isinstance(d["returnValue"], list) and d["returnValue"]), None)
    if first:
        log("")
        log("FIELD SCHEMA (%d fields on row 0):" % len(first[0]))
        for k, v in first[0].items():
            log("   %-38s %r" % (k, str(v)[:56]))


if __name__ == "__main__":
    main()
