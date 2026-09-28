#!/usr/bin/env python3
"""Wisconsin DSPS (license.wi.gov) - credential roster adapter.

STATUS: WRITTEN BUT NOT YET RUN END-TO-END. license.wi.gov began returning Cloudflare
403s to this environment on 2026-09-28 after too many automated probes, so the
searchLicense RESPONSE SHAPE has never been observed. Everything about the request is
measured; the response parsing is deliberately TOLERANT and self-reporting - the first
successful run prints the real field names and flags anything it could not map, rather
than silently dropping data. Treat the first run as the final recon step.

CHANNEL (measured 2026-09-28)
  WI migrated off licensesearch.wi.gov (that host no longer has a DNS record) to
  Salesforce Experience Cloud. Guest-accessible, no login, no enforced captcha:
      DSPS_LicensesLookupController.searchLicense
        searchType : "Profession"
        dataObj    : {"selectedCategory":"Health","selectedProfession":"<credentialId>"}
        recapToken : ""      - wired but inert; kept empty, NEVER populated
  The Aura POST must be issued from inside the page (page.request is 403'd) and carries
  the page-scope x-sfdc-* headers captured from a genuine request on load.

WHAT THE GRID CARRIES (confirmed from the live guest UI)
  Credential/License Number | Profession | Credential/License Type | Name | DBA |
  City | State | Zip Code | Granted | License Status
  There is NO street address - city/state/zip only. State arrives as a FULL NAME
  ("Wisconsin"), which normalize_state() handles; a naive 2-letter parser would fail.
  Licence numbers look like "3096 - 45" where the suffix is the credential-type code -
  kept VERBATIM, dash and spacing included.

CLOUDFLARE
  Any 403 stops the run immediately. No retry, no UA rotation, no stealth patching, no
  proxy - those evade a protection the operator put there deliberately.
"""
import argparse
import collections
import csv
import json
import os
import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

URL = "https://license.wi.gov/s/license-lookup"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
SCOPE_FILE = "wi_scope.json"
# Observed in the live License Status column filter (Mark, 2026-09-28). A value outside
# this set on a run is enum drift and must be reported, not silently accepted. Note the
# absence of Pending/Denied - unlike MD, WI does not appear to publish un-issued records,
# so blank licence numbers should be rare or absent.
KNOWN_STATUS = {"Active", "Inactive", "Expired", "Revoked", "Voluntary Surrender"}

# Observed totals from the live grid footer ("1181 - 1190 of 1190"), 2026-09-28. The
# datatable pages CLIENT-SIDE over a complete payload - it knows the total immediately -
# so one Apex call per credential returns everything and there is no page walk. These are
# sanity anchors: a large deviation means either real-world churn or a truncated payload.
EXPECTED_TOTAL = {"Wholesale Distributor of Prescription Drugs": 1190}
CAP_TELLS = {500, 1000, 2000, 2500, 5000, 10000}
TYPES_FILE = "wi_license_types.json"
PAUSE = 12          # deliberate spacing between calls; this site throttles

# our column <- first response key whose name contains any of these fragments
FIELD_HINTS = [
    ("license_number", ["credentiallicensenumber", "licensenumber", "credentialnumber",
                        "licenseno", "credential"]),
    ("profession",     ["profession"]),
    ("license_type",   ["credentiallicensetype", "licensetype", "credentialtype", "type"]),
    ("license_holder_name", ["name"]),
    ("dba",            ["dba", "doingbusiness"]),
    ("address_city",   ["city"]),
    ("address_state",  ["state"]),
    ("address_zip",    ["zip", "postal"]),
    ("granted_date",   ["granted", "issue", "effective"]),
    ("license_status", ["status"]),
]
OUT_COLS = [c for c, _ in FIELD_HINTS] + [
    "jurisdiction", "__credential", "__entity", "__natural_key", "__source", "__retrieved"]


def log(m):
    print("[wi] " + str(m), flush=True)


def build_mapping(sample):
    """Map response keys -> our columns, reporting what matched and what did not."""
    keys = list(sample.keys())
    norm = {k: re.sub(r"[^a-z]", "", k.lower()) for k in keys}
    mapping, used = {}, set()
    for col, hints in FIELD_HINTS:
        best = None
        for h in hints:
            for k in keys:
                if k in used:
                    continue
                if h in norm[k]:
                    # prefer the shortest key name - avoids 'name' matching 'dbaName'
                    if best is None or len(norm[k]) < len(norm[best]):
                        best = k
            if best:
                break
        if best:
            mapping[col] = best
            used.add(best)
    log("  field mapping discovered:")
    for col, _ in FIELD_HINTS:
        log("    %-22s <- %r" % (col, mapping.get(col, "(UNMAPPED)")))
    unmapped = [k for k in keys if k not in used]
    if unmapped:
        log("  response keys NOT mapped (review - may hold data we want): %s" % unmapped)
    return mapping


def records_from(rv):
    """Find the record list in whatever shape returnValue arrives as."""
    if isinstance(rv, list):
        return rv
    if isinstance(rv, dict):
        for k in ("records", "licenses", "results", "data", "lstLicense", "licenseList"):
            v = rv.get(k)
            if isinstance(v, list):
                return v
        for v in rv.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--out", default=None)
    ap.add_argument("--headed", action="store_true", default=True)
    a = ap.parse_args()

    scope = json.load(open(SCOPE_FILE, encoding="utf-8"))
    baseline = {r["label"] for r in json.load(open(TYPES_FILE, encoding="utf-8"))}
    drift = [s["label"] for s in scope if s["label"] not in baseline]
    if drift:
        raise RuntimeError("ENUM DRIFT: scoped credential(s) absent from %s: %s"
                           % (TYPES_FILE, drift))
    log("enum-drift guard: %d scoped credential(s) all present in the baseline" % len(scope))

    env, per_type, records, mapping = {}, {}, {}, None

    def on_req(r):
        if "/aura" in r.url and r.method == "POST" and "ApexAction" in (r.post_data or ""):
            if not env:
                env.update(url=r.url, body=r.post_data, headers=dict(r.headers))

    with sync_playwright() as p:
        b = p.chromium.launch(headless=not a.headed, args=["--start-maximized"])
        ctx = b.new_context(user_agent=UA, viewport=None, locale="en-US")
        pg = ctx.new_page()
        pg.set_default_timeout(120000)
        pg.on("request", on_req)
        try:
            pg.goto(URL, wait_until="networkidle", timeout=90000)
        except Exception:
            pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(9000)

        if re.search(r"(attention required|verify you are human)", pg.inner_text("body"), re.I):
            log("!! Cloudflare challenge on load - STOPPING. No evasion.")
            b.close()
            sys.exit(3)
        if not env:
            log("!! no Aura envelope captured - STOPPING")
            b.close()
            sys.exit(4)

        parts = urllib.parse.parse_qs(env["body"], keep_blank_values=True)
        base = json.loads(parts["message"][0])
        hdr = {k: v for k, v in env["headers"].items()
               if k.lower().startswith(("x-sfdc", "x-b3"))
               or k.lower() in ("referer", "accept-language")}
        hdr["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        hdr["x-sfdc-lds-endpoints"] = ("ApexActionController.execute:"
                                       "DSPS_LicensesLookupController.searchLicense")

        for n, cred in enumerate(scope):
            if n:
                time.sleep(a.pause)
            msg = json.loads(json.dumps(base))
            act = msg["actions"][0]
            act["id"] = "%d;a" % (900 + n)
            act["params"] = {"namespace": "", "classname": "DSPS_LicensesLookupController",
                             "method": "searchLicense",
                             "params": {"searchType": "Profession",
                                        "dataObj": json.dumps(
                                            {"selectedCategory": cred["category"],
                                             "selectedProfession": cred["value"]}),
                                        "recapToken": ""},
                             "cacheable": False, "isContinuation": False}
            parts["message"] = [json.dumps(msg, separators=(",", ":"))]
            body = urllib.parse.urlencode({k: v[0] for k, v in parts.items()})
            out = pg.evaluate("""async ([url, body, hdr]) => {
                const r = await fetch(url, {method:'POST', credentials:'include',
                                            headers:hdr, body});
                return {status:r.status, text:await r.text()};
            }""", [env["url"], body, hdr])

            if out["status"] == 403 or not out["text"].strip().startswith("{"):
                log("!! HTTP %s on %r - Cloudflare is throttling. STOPPING; nothing written."
                    % (out["status"], cred["label"]))
                b.close()
                sys.exit(5)

            j = json.loads(out["text"])
            action = next((x for x in j.get("actions", []) if x.get("id") == act["id"]), None)
            if action is None or action.get("state") != "SUCCESS":
                log("  %-62s NO RESULT (%s)" % (cred["label"],
                                                (action or {}).get("state")))
                continue
            rv = action.get("returnValue")
            rv = rv.get("returnValue", rv) if isinstance(rv, dict) else rv
            rows = records_from(rv)
            per_type[cred["label"]] = len(rows)
            note = ""
            exp = EXPECTED_TOTAL.get(cred["label"])
            if exp is not None:
                delta = len(rows) - exp
                note = "  (expected ~%s, %+d)" % (format(exp, ","), delta)
                if abs(delta) > max(25, 0.05 * exp):
                    note += "  <-- DEVIATION: verify before trusting"
            if len(rows) in CAP_TELLS:
                note += "  <-- ROUND TOTAL: looks like a server cap, not a count"
            log("  %-62s %6s row(s)%s" % (cred["label"], format(len(rows), ","), note))
            if rows and mapping is None:
                mapping = build_mapping(rows[0])
            for r in rows:
                rec = {col: str(r.get(mapping.get(col), "") or "")
                       for col, _ in FIELD_HINTS}
                rec["jurisdiction"] = "WI"
                rec["__credential"] = cred["label"]
                rec["__entity"] = cred["entity"]
                lic = rec["license_number"].strip()
                rec["__natural_key"] = ("WI|%s|%s" % (lic, cred["label"]) if lic
                                        else "WI||%s|%s" % (rec["license_holder_name"]
                                                            .strip().upper(), cred["label"]))
                rec["__source"] = ("WI DSPS credential lookup - license.wi.gov/s/license-lookup "
                                   "(DSPS_LicensesLookupController.searchLicense)")
                rec["__retrieved"] = time.strftime("%Y-%m-%d")
                records.setdefault(rec["__natural_key"], rec)
        b.close()

    # ---------------- guards ----------------
    if not records:
        raise RuntimeError("REFUSING TO WRITE: zero rows collected")
    missing = [c["label"] for c in scope if c["label"] not in per_type]
    if missing:
        raise RuntimeError("REFUSING TO WRITE: %d credential(s) returned nothing: %s"
                           % (len(missing), missing))

    seen_status = {r["license_status"].strip() for r in records.values() if r["license_status"].strip()}
    unknown = seen_status - KNOWN_STATUS
    if unknown:
        log("")
        log("!! ENUM DRIFT in License Status - values not in the 2026-09-28 baseline:")
        for u in sorted(unknown):
            log("     %r" % u)
        log("   (reported, not fatal: a new status is a real-world change, not a bug -")
        log("    but confirm it before landing)")
    blank_lic = [r for r in records.values() if not r["license_number"].strip()]
    if blank_lic:
        log("")
        log("!! %d row(s) with a blank licence number (statuses=%s) - kept verbatim, keyed"
            % (len(blank_lic),
               dict(collections.Counter(r["license_status"] for r in blank_lic))))
        log("   on (name, credential). Unexpected for WI - verify before landing.")

    log("")
    for label, n in sorted(per_type.items(), key=lambda kv: -kv[1]):
        tell = "  <-- ROUND: check for a cap" if n and n % 50 == 0 else ""
        log("  %-62s %6s%s" % (label, format(n, ","), tell))
    total_raw = sum(per_type.values())
    log("  %-62s %6s raw / %s distinct"
        % ("TOTAL", format(total_raw, ","), format(len(records), ",")))

    out = a.out or ("wi-dsps-pharmacy-board - Complete - %s.csv" % time.strftime("%Y%m%d"))
    path = os.path.join(r"C:\Verified\_master_data_dropin", out)
    if os.path.exists(path):
        raise RuntimeError("REFUSING TO OVERWRITE an existing file: %s" % path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(records.values())
    log("")
    log("wrote %s: %s rows x %d cols" % (path, format(len(records), ","), len(OUT_COLS)))
    log("status spread: %s"
        % dict(collections.Counter(r["license_status"] for r in records.values()).most_common(12)))
    log("entity split : %s"
        % dict(collections.Counter(r["__entity"] for r in records.values())))
    wi = sum(1 for r in records.values()
             if r["address_state"].strip().upper() in ("WI", "WISCONSIN"))
    log("WI-addressed : %s of %s (%.1f%%)"
        % (format(wi, ","), format(len(records), ","), 100.0 * wi / len(records)))
    log("")
    log("NEXT: verify on an INDEPENDENT axis (membership, not counts) before landing.")


if __name__ == "__main__":
    main()
