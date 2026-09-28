#!/usr/bin/env python3
"""ENG-346 - New Mexico Board of Pharmacy adapter.

CHANNEL (established by nm_recon.py / nm_recon2.py, 2026-09-24)
  Salesforce Experience Cloud site nmrldlpi.my.site.com/bcd, Apex controller
  `RLDLicenseSearchController`:
      getPicklistValues()                              -> boards (lstOfRA) + counties
      getRATName(boardName)                            -> license types for that board
      onSearch(professionValue, licenseTypeValue, ...) -> FULL result set, one shot

  onSearch returns every matching record in a single Apex response as raw sObject
  JSON. The portal's "Displaying 1 of 21 Page" control is CLIENT-SIDE paging over
  an already-complete payload, so there is no page walk and no dropped-page risk.

  This is the FREE public search. It is a different channel from the paid
  Licensee List Request (~$1,500) at /bcd/s/license-list-request. Nothing here is
  purchased, no account is created, no CAPTCHA is touched.

STRATEGY
  Driving the Aura UI 36 times is slow and fragile. Instead: load the page once,
  perform ONE real search so the browser mints a valid aura.context + token, then
  replay that exact POST per license type with licenseTypeValue swapped. The
  replay goes through page.request so it inherits the session cookies.

GOVERNOR LIMIT WATCH
  Apex SOQL caps at 50,000 rows. A type at/near 50,000 would be silently truncated,
  so any type returning >= SOQL_WARN is flagged loudly rather than trusted.
"""
import argparse
import csv
import json
import re
import time
import urllib.parse
from collections import Counter

from playwright.sync_api import sync_playwright

URL = "https://nmrldlpi.my.site.com/bcd/s/rld-public-search?language=en_US"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
BOARD = "Board of Pharmacy"
SEED_TYPE = "Wholesale Drug Distributor"   # the one search we drive through the UI
SOQL_WARN = 49_000                         # governor-limit proximity alarm

# --- scope: supply-chain license types (Mark, 2026-09-24: "just the 15 for now") -----
# Drawn on DSCSA trading-partner lines: manufacturer / wholesale distributor /
# repackager / 3PL / outsourcing facility / non-resident dispenser. Deliberately
# EXCLUDES individual credentials (Registered Pharmacist, Pharmacy Technician,
# Pharmacist Intern, Pharmacist Clinician, Practitioner Controlled Substance) and
# care-setting registrations (clinics, nursing homes, EMS, home care, vet, research).
SUPPLY_CHAIN_TYPES = [
    "Manufacturer",
    "Virtual Manufacturer",
    "Wholesale Drug Distributor",
    "Virtual Wholesale Distributor",
    "In State Wholesaler",
    "Drug Warehouse",
    "Repackager",
    "Medical Gas Repackager",
    "Medical Gas Seller",
    "Third Party Logistics Provider",
    "Resident Outsourcing Facility",
    "Non Resident Outsourcing Facility",
    "Non Resident Pharmacy",
    "Non Resident Sterile Pharmacy",
    "Contact Lens Distributor",
]
# Borderline, currently OUT - in-state dispensers. These ARE DSCSA dispensers but
# not upstream supply chain; flip them in if the scope widens.
DISPENSER_TYPES = [
    "In State Retail Pharmacy",
    "In State Hospital Pharmacy",
    "In State Sterile Pharmacy",
    "In State Telepharmacy",
]

# Scalar fields worth landing. Nested sObjects are reduced to the one useful leaf.
SCALARS = [
    ("Name", "license_number"),
    ("Regulatory_Authorization_Type_Name__c", "license_type"),      # raw, verbatim
    ("Regulatory_Authorization_Type__c", "license_type_category"),  # raw, verbatim
    ("Status", "license_status"),                                   # raw, verbatim
    ("License_Holder_Name__c", "license_holder_name"),
    ("License_Holder_Street__c", "address_street"),
    ("License_Holder_City__c", "address_city"),
    ("License_Holder_State__c", "address_state"),
    ("License_Holder_Postal_Code__c", "address_zip"),
    ("Licensee_County__c", "county"),
    ("periodStartDate__c", "issue_date"),
    ("periodEnd__c", "expiration_date"),
    ("PeriodStart", "period_start_utc"),
    ("PeriodEnd", "period_end_utc"),
    ("TemporaryLicense__c", "temporary_license"),
    ("Regulatory_Authority__c", "board"),
    ("Id", "sf_record_id"),
    ("AccountId", "sf_account_id"),
    ("ContactId", "sf_contact_id"),
]


QUARANTINE_JUNK = True
# Placeholder records seeded into the portal by NM staff. Both patterns are ANCHORED
# to the whole field, never a substring, so a real licensee such as "Test Labs Inc"
# or "Protest Pharmacy" survives. Observed in the wild: holder names "test",
# "test test", "Test Test", "Enter Organization Name", "Enter Organization Name
# testing"; license numbers literally "test".
JUNK_LICENSE_NUMBERS = {"test", "testing", "test1", "123", "xxx"}
JUNK_NAME_RE = re.compile(
    r"^(?:(?:test|testing)(?:[\s.]+(?:test|testing))*"      # test / test test / Test Test
    r"|enter organization name(?:[\s.]+\w+)?"               # the literal form-placeholder
    r"|abc|xxx|n/?a|none)$", re.I)


def is_junk(row):
    return (row["license_number"].strip().lower() in JUNK_LICENSE_NUMBERS
            or bool(JUNK_NAME_RE.match(row["license_holder_name"].strip())))


def log(msg):
    print("[nm] " + str(msg), flush=True)


def swap_type(post_body, new_type):
    """Rewrite licenseTypeValue inside the url-encoded aura message.

    The message is url-encoded JSON inside message=...; decode only that one
    parameter, retarget it, re-encode. Hitting the raw string with a regex would be
    fragile against type names containing commas and spaces.
    """
    parts = urllib.parse.parse_qs(post_body, keep_blank_values=True)
    msg = json.loads(parts["message"][0])
    hit = False
    for a in msg["actions"]:
        p = a.get("params", {}).get("params")
        if isinstance(p, dict) and "licenseTypeValue" in p:
            p["licenseTypeValue"] = new_type
            p["professionValue"] = BOARD
            hit = True
    if not hit:
        raise RuntimeError("seed envelope has no licenseTypeValue to swap")
    parts["message"] = [json.dumps(msg, separators=(",", ":"))]
    return urllib.parse.urlencode({k: v[0] for k, v in parts.items()})


def extract_rows(payload):
    """Pull the record list out of an Aura ApexAction response, or raise."""
    j = json.loads(payload)
    acts = j.get("actions") or []
    if not acts:
        raise RuntimeError("no actions in aura response: " + payload[:300])
    a = acts[0]
    if a.get("state") != "SUCCESS":
        raise RuntimeError("aura action state=" + str(a.get("state")) + ": "
                           + json.dumps(a.get("error"))[:400])
    rv = a.get("returnValue")
    rv = rv.get("returnValue") if isinstance(rv, dict) else rv
    if rv is None:
        return []
    if not isinstance(rv, list):
        raise RuntimeError("unexpected returnValue shape: " + type(rv).__name__)
    return rv


def flatten(rec, lic_type):
    out = {}
    for src, dst in SCALARS:
        v = rec.get(src)
        out[dst] = "" if v is None else str(v)
    # entity-type signal - the only useful leaf in the nested Account
    rt = (rec.get("Account") or {}).get("RecordType") or {}
    out["sf_account_record_type"] = str(rt.get("DeveloperName") or "")
    rat = rec.get("RegulatoryAuthorizationType") or {}
    out["hidden_in_public_search"] = str(rat.get("Hide_in_Public_Search__c", ""))
    out["state"] = "NM"
    out["__license_type_queried"] = lic_type
    out["__source"] = ("NM RLD Board of Pharmacy public license search - "
                       "nmrldlpi.my.site.com/bcd/s/rld-public-search "
                       "(RLDLicenseSearchController.onSearch)")
    out["__retrieved"] = time.strftime("%Y-%m-%d")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", action="store_true",
                    help="probe EVERY board license type for its record count; write no data file")
    ap.add_argument("--types", default="supply-chain",
                    choices=["supply-chain", "supply-chain+dispensers", "all"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between type queries")
    args = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=not args.headed)
        ctx = b.new_context(user_agent=UA, viewport={"width": 1500, "height": 1100})
        pg = ctx.new_page()
        pg.set_default_timeout(90000)

        seed = {}

        def on_request(r):
            if r.method == "POST" and "aura" in r.url and "ApexAction" in r.url:
                pd = r.post_data or ""
                if "onSearch" in urllib.parse.unquote_plus(pd):
                    seed["url"] = r.url
                    seed["body"] = pd

        ctx.on("request", on_request)

        log("loading " + URL)
        try:
            pg.goto(URL, wait_until="networkidle", timeout=90000)
        except Exception:
            pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(9000)

        # --- one real UI search, purely to mint a valid aura envelope
        log("seeding envelope via UI search: " + BOARD + " / " + SEED_TYPE)
        pg.locator("button:has-text('Select a Profession')").first.click()
        pg.wait_for_timeout(1200)
        pg.locator("[role=option]:has-text('" + BOARD + "')").first.click()
        pg.wait_for_timeout(2500)
        pg.locator("button:has-text('Select a License Type')").first.click()
        pg.wait_for_timeout(1200)
        pg.locator("[role=option]:has-text('" + SEED_TYPE + "')").first.click()
        pg.wait_for_timeout(1200)
        pg.locator("button:has-text('Search'), lightning-button:has-text('Search')").last.click()
        pg.wait_for_timeout(10000)

        if not seed.get("body"):
            pg.screenshot(path="nm_seed_fail.png", full_page=True)
            raise RuntimeError("never captured an onSearch POST - UI shape changed; "
                               "see nm_seed_fail.png")
        log("envelope captured")

        # --- authoritative type list, straight from the portal
        types_all = json.load(open("nm_pharmacy_license_types.json", encoding="utf-8"))
        if args.counts or args.types == "all":
            targets = types_all
        elif args.types == "supply-chain+dispensers":
            targets = SUPPLY_CHAIN_TYPES + DISPENSER_TYPES
        else:
            targets = SUPPLY_CHAIN_TYPES

        # enum-drift guard: our allowlist must still exist upstream
        unknown = [t for t in targets if t not in types_all]
        if unknown:
            raise RuntimeError("ENUM DRIFT - allowlisted types absent from the portal: "
                               + str(unknown))
        log(str(len(targets)) + " license type(s) to query ("
            + ("count probe" if args.counts else args.types) + ")")

        frames, counts, failures = [], {}, []
        for i, t in enumerate(targets, 1):
            body = swap_type(seed["body"], t)
            try:
                resp = pg.request.post(
                    seed["url"], data=body,
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    timeout=180000)
                rows = extract_rows(resp.text())
            except Exception as e:
                failures.append((t, type(e).__name__ + ": " + str(e)[:200]))
                log("  %2d/%d %-46s !! %s: %s" % (i, len(targets), t,
                                                  type(e).__name__, str(e)[:110]))
                continue
            counts[t] = len(rows)
            flag = "  <-- NEAR SOQL LIMIT, DO NOT TRUST" if len(rows) >= SOQL_WARN else ""
            log("  %2d/%d %-46s %7s%s" % (i, len(targets), t, format(len(rows), ","), flag))
            if not args.counts:
                frames.extend(flatten(r, t) for r in rows)
            time.sleep(args.delay)

        b.close()

    # ---------------- reporting + guards ----------------
    total = sum(counts.values())
    log("")
    log("total across " + str(len(counts)) + " type(s): " + format(total, ","))
    if failures:
        log(str(len(failures)) + " type(s) FAILED:")
        for t, e in failures:
            log("    " + t + ": " + e)

    with open("nm_type_counts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["license_type", "record_count"])
        for t in sorted(counts, key=lambda k: -counts[k]):
            w.writerow([t, counts[t]])
    log("wrote nm_type_counts.csv")

    if args.counts:
        log("count probe only - no data file written")
        return

    if not frames:
        raise RuntimeError("REFUSING TO WRITE: zero rows collected")
    if failures:
        raise RuntimeError("REFUSING TO WRITE: " + str(len(failures)) + " type(s) failed; "
                           "a partial file would silently under-report")

    # --- quarantine portal junk BEFORE any de-dupe -------------------------------
    # The portal carries seeded test records (license number literally "test",
    # holder name "test" / "Enter Organization Name", city "city", state "state").
    # They are never silently dropped: they go to their own file so the exclusion
    # is auditable. Flip QUARANTINE_JUNK to False to land them with the rest.
    clean, junk = [], []
    for r in frames:
        if is_junk(r):
            junk.append(r)
        else:
            clean.append(r)
    if junk:
        log("quarantined %s portal test/placeholder record(s) -> nm_quarantine.csv"
            % format(len(junk), ","))
    if not QUARANTINE_JUNK:
        clean, junk = frames, []
    if len(junk) > 0.02 * max(len(frames), 1):
        raise RuntimeError("junk filter matched %.1f%% of rows - too broad, refusing"
                           % (100.0 * len(junk) / len(frames)))

    # --- de-dupe: WHOLE ROW only ------------------------------------------------
    # Colorado lesson: collapsing on a natural key silently destroys real records
    # when the source reuses a number across distinct entities or keeps disciplinary
    # history. Whole-row de-dupe is lossless by construction. Natural-key collisions
    # are REPORTED as a diagnostic, never collapsed.
    seen, deduped, dups = set(), [], 0
    for r in clean:
        sig = tuple(sorted(r.items()))
        if sig in seen:
            dups += 1
            continue
        seen.add(sig)
        deduped.append(r)
    loss = dups / max(len(clean), 1)
    log("de-dupe (whole row, lossless): %s -> %s (removed %s, %.2f%%)"
        % (format(len(clean), ","), format(len(deduped), ","), format(dups, ","), loss * 100))
    if loss > 0.10:
        raise RuntimeError("whole-row de-dupe dropped %.1f%% - the source is repeating "
                           "itself; investigate before trusting" % (loss * 100))

    keyc = Counter((r["state"], r["license_number"], r["license_type"]) for r in deduped)
    multi = {k: v for k, v in keyc.items() if v > 1}
    if multi:
        log("")
        log("NATURAL-KEY COLLISIONS RETAINED (not collapsed): %s key(s), %s excess row(s)"
            % (format(len(multi), ","), format(sum(v - 1 for v in multi.values()), ",")))
        for k, v in sorted(multi.items(), key=lambda kv: -kv[1])[:10]:
            log("    %s / %s  x%d" % (k[1], k[2], v))
        log("    -> these are distinct records sharing a number (history or reuse);")
        log("       resolve downstream, do not collapse here")

    out = args.out or ("nm_pharmacy_" + time.strftime("%Y%m%d") + ".csv")
    cols = ([d for _, d in SCALARS]
            + ["sf_account_record_type", "hidden_in_public_search", "state",
               "__license_type_queried", "__source", "__retrieved"])
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(deduped)
    log("wrote " + out + ": " + format(len(deduped), ",") + " rows x " + str(len(cols)) + " cols")

    if junk:
        with open("nm_quarantine.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(junk)
        log("wrote nm_quarantine.csv: " + format(len(junk), ",") + " excluded junk row(s)")

    st = Counter(r["license_status"] for r in deduped)
    log("")
    log("status spread:")
    for k, v in st.most_common():
        log("    %-28s %7s" % (k or "(blank)", format(v, ",")))
    log("")
    log("VERIFY before trusting (a clean run is NOT verification):")
    log("  1. per-type counts here vs an independent portal count")
    log("  2. active AND non-active statuses present")
    log("  3. leading zeros intact in the RAW BYTES (license numbers are WD000..., etc.)")
    log("  4. a known NM wholesaler present (CARDINAL / MCKESSON / CENCORA)")
    log("  5. no type at/near 50,000 (SOQL governor truncation)")


if __name__ == "__main__":
    main()
