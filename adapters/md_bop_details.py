#!/usr/bin/env python3
"""MD Board of Pharmacy - PHASE B: fetch and parse the public Details pages.

Phase A harvested GUIDs from the gated search grid (Mark solved the captcha once).
Details.aspx is reported to be a PUBLIC, ungated GET - no session required - so this
phase uses plain HTTP rather than driving a browser.

That claim is VERIFIED ON THE FIRST RECORD rather than assumed: if the first fetch
comes back as a login/captcha/error page instead of a detail page, we stop
immediately. Finding out at record 1 costs one request; finding out at record 3,000
costs the whole run and produces a file full of error pages parsed as data.

Parses: Name, Address, City, State, Zip, Type, Subtype, Status, Original Issued,
Date Renewed, Expires - verbatim, with blanks preserved.
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import urllib.request

DETAIL = "https://mdbop.mylicense.com/verification/Details.aspx?result=%s"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

# Labels as rendered on the detail page -> our column names.
FIELD_MAP = [
    ("Name", "license_holder_name"),
    ("Address", "address_street"),
    ("City", "address_city"),
    ("State", "address_state"),
    ("Zip", "address_zip"),
    ("Type", "detail_type"),
    ("Subtype", "detail_subtype"),
    ("Status", "detail_status"),
    ("Original Issued", "original_issue_date"),
    ("Date Renewed", "date_renewed"),
    ("Expires", "expiration_date"),
]
OUT_COLS = (["license_number", "license_type", "status", "grid_name"]
            + [c for _, c in FIELD_MAP]
            + ["jurisdiction", "md_guid", "__source", "__retrieved"])


def log(m):
    print("[mdB] " + str(m), flush=True)


def fetch(guid, timeout=45):
    req = urllib.request.Request(DETAIL % guid, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def looks_gated(body):
    return bool(re.search(r"(please solve the captcha|g-recaptcha|Login\.aspx|"
                          r"Server Error|Runtime Error|session has expired)", body, re.I))


def strip_tags(x):
    x = re.sub(r"<[^>]+>", " ", x or "")
    return re.sub(r"\s+", " ", html.unescape(x)).strip()


def parse(body):
    """Pull label/value pairs out of the detail table, verbatim."""
    out = {}
    # MyLicense detail pages render as <td>Label:</td><td>Value</td> pairs; also
    # tolerate <span id="_label">/<span id="_value"> variants across skins.
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", body, re.S | re.I)
    txt = [strip_tags(c) for c in cells]
    for i, t in enumerate(txt):
        lab = t.rstrip(":").strip()
        for label, col in FIELD_MAP:
            if lab.lower() == label.lower() and col not in out:
                val = txt[i + 1] if i + 1 < len(txt) else ""
                # never let the next LABEL be captured as a value
                if val.rstrip(":").strip().lower() in {l.lower() for l, _ in FIELD_MAP}:
                    val = ""
                out[col] = val
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guids", default="md_guids.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.12)
    a = ap.parse_args()

    rows = json.load(open(a.guids, encoding="utf-8"))
    if not rows:
        raise RuntimeError("REFUSING: %s is empty" % a.guids)
    log("%s GUID(s) to fetch" % format(len(rows), ","))

    # --- verify the "public/ungated" claim on record 1 before spending the run
    probe = fetch(rows[0]["guid"])
    if looks_gated(probe):
        log("!! Details.aspx is NOT ungated - first fetch returned a gate/error page")
        log("   first 300 chars: %s" % strip_tags(probe)[:300])
        raise RuntimeError("STOPPING: detail pages require a session; do not brute-force")
    first = parse(probe)
    if not first:
        log("!! parsed NOTHING from the first detail page - selector mismatch")
        log("   first 400 chars: %s" % strip_tags(probe)[:400])
        raise RuntimeError("STOPPING: parser does not match the page shape")
    log("ungated GET confirmed; first record parsed %d field(s): %s"
        % (len(first), json.dumps(first)[:220]))

    results, failures = {}, []

    def work(r):
        for attempt in (0, 1, 2):
            try:
                body = fetch(r["guid"])
                if looks_gated(body):
                    raise RuntimeError("gated response")
                d = parse(body)
                if not d:
                    raise RuntimeError("empty parse")
                time.sleep(a.delay)
                return r["guid"], d
            except Exception as e:
                if attempt == 2:
                    return r["guid"], {"__error": "%s: %s" % (type(e).__name__, str(e)[:80])}
                time.sleep(1.5 * (attempt + 1))

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for n, (guid, d) in enumerate(ex.map(work, rows), 1):
            if "__error" in d:
                failures.append((guid, d["__error"]))
            else:
                results[guid] = d
            if n % 250 == 0:
                log("  %s/%s fetched (%d failed)" % (format(n, ","), format(len(rows), ","),
                                                     len(failures)))

    log("fetched %s, failed %d" % (format(len(results), ","), len(failures)))
    if failures:
        for g, e in failures[:8]:
            log("    FAIL %s %s" % (g, e))
        raise RuntimeError("REFUSING TO WRITE: %d detail fetch(es) failed; a partial "
                           "file would silently under-report" % len(failures))

    merged = []
    for r in rows:
        d = results.get(r["guid"], {})
        row = {"license_number": r["license_no"], "license_type": r["license_type"],
               "status": r["status"], "grid_name": r["name"],
               "jurisdiction": "MD", "md_guid": r["guid"],
               "__source": ("MD Board of Pharmacy facility verification - "
                            "mdbop.mylicense.com/Verification (facility=Y); detail pages "
                            "Details.aspx?result=<guid>"),
               "__retrieved": time.strftime("%Y-%m-%d")}
        for _, col in FIELD_MAP:
            row[col] = d.get(col, "")
        merged.append(row)

    if not merged:
        raise RuntimeError("REFUSING TO WRITE: zero rows")

    out = a.out or ("md-board-of-pharmacy - Company-Only - %s.csv" % time.strftime("%Y%m%d"))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(merged)
    log("wrote %s: %s rows x %d cols" % (out, format(len(merged), ","), len(OUT_COLS)))

    log("")
    log("per-type:")
    for k, v in Counter(r["license_type"] for r in merged).most_common():
        log("    %-34s %6d" % (k or "(blank)", v))
    log("per-status (grid):")
    for k, v in Counter(r["status"] for r in merged).most_common():
        log("    %-34s %6d" % (k or "(blank)", v))
    blank = sum(1 for r in merged if not r["license_number"].strip())
    log("blank license numbers: %d (kept verbatim)" % blank)
    log("detail/grid status agreement: %d/%d"
        % (sum(1 for r in merged if r["status"] == r["detail_status"]), len(merged)))


if __name__ == "__main__":
    main()
