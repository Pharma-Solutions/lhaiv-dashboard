#!/usr/bin/env python3
"""HI NED rebuild: controls on the candidate, and a row-by-row diff against the
2026-10-14 build that is in Master Data.

Every control that can read zero is run against the OLD file as well, which is
known to carry the defect. If a detector finds nothing there either, the detector
is broken and its zero on the candidate means nothing.

Usage:  python hi_ned_diff.py OLD_CSV NEW_CSV CONVERTED_XLSX CONVERSION_REPORT OUT_DIFF_CSV OUT_JSON
"""
import collections
import csv
import json
import re
import sys

import openpyxl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HEADER_STRINGS = ["NED Number", "Expiration Date", "Full Name", "Last Name", "First Name",
                  "Middle Name", "Organization Name", "Business Address"]
FIELDS = ["facility_name", "license_type", "license_status", "expiration_date",
          "address_line1", "address_city", "address_state", "address_zip"]


def load(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def header_hits(rows):
    return [(r["license_number"], k, h) for r in rows for k, v in r.items()
            if not k.startswith("__") for h in HEADER_STRINGS if h.lower() in (v or "").lower()]


def repeated_word(name):
    w = re.findall(r"[A-Za-z0-9.&'-]+", name.upper())
    return len(w) != len(set(w))


def iso(d):
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", d or "")
    return "%s-%s-%s" % (m.group(3), m.group(2), m.group(1)) if m else d


def main():
    old_p, new_p, xlsx_p, rep_p, diff_p, json_p = sys.argv[1:7]
    old, new = load(old_p), load(new_p)
    rep = json.load(open(rep_p, encoding="utf-8"))
    C = {}

    # 1. NED tokens in the raw text (pdftotext, independent of pdfplumber) vs rows out
    C["ned_tokens_vs_rows"] = [{k: f[k] for k in ("file", "raw_ned_tokens_pdftotext",
                                                  "rows_out", "distinct_ned")}
                               for f in rep["files"]]
    by_type = collections.Counter(r["license_type"] for r in new)
    C["rows_after_dedupe"] = {"rows": len(new), "distinct": len({r["license_number"] for r in new}),
                              "by_type": dict(by_type)}

    # 2. header strings in data, with the old file as the positive control
    C["header_bleed"] = {"candidate": header_hits(new), "old_file_positive_control": header_hits(old)}

    # 3. every state is 2 letters; control = the count that matched
    bad_st = [(r["license_number"], r["address_state"]) for r in new
              if not re.fullmatch(r"[A-Z]{2}", r["address_state"])]
    C["state_two_letters"] = {"matched": len(new) - len(bad_st), "of": len(new), "bad": bad_st}

    # 4. ZIP shapes and pads
    shapes = collections.Counter(re.sub(r"\d", "9", r["address_zip"]) for r in new)
    C["zip_shapes"] = dict(shapes)
    # the flag list comes from the converted workbook: the ZIP as the PDF printed it
    wb = openpyxl.load_workbook(xlsx_p, read_only=True)
    four, noorg = [], []
    for ws in wb.worksheets:
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r[0] and not r[7]:
                noorg.append((r[0], ws.title, r[3], r[4], r[5]))
            if r[0] and re.fullmatch(r"\d{4}", str(r[11] or "")):
                four.append((r[0], ws.title, r[10], r[11]))
    wb.close()
    newz = {r["license_number"]: r["address_zip"] for r in new}
    C["zip_4digit_padded"] = {"count": len(four), "states": dict(collections.Counter(f[2] for f in four)),
                              "all_padded_in_output": all(newz[f[0]] == f[3].zfill(5) for f in four),
                              "rows": [f[0] for f in four]}
    # 5. E17431
    C["E17431"] = {"rows_in_candidate": sum(1 for r in new if r["license_number"] == "E17431"),
                   "rows_in_old": sum(1 for r in old if r["license_number"] == "E17431")}
    # 6. individuals: no Organization Name in source -> Full Name is "Last, First"
    C["no_organization_name_in_source"] = noorg

    # brief heuristics, both files
    def street_nodigit(rows):
        return sum(1 for r in rows if r["address_line1"] and not r["address_line1"][0].isdigit())
    C["address_line1_not_digit_first"] = {"candidate": street_nodigit(new), "old": street_nodigit(old)}
    C["name_repeats_a_word"] = {"candidate": sum(repeated_word(r["facility_name"]) for r in new),
                                "old": sum(repeated_word(r["facility_name"]) for r in old)}
    C["address_city_blank"] = {"candidate": sum(1 for r in new if not r["address_city"]),
                               "old": sum(1 for r in old if not r["address_city"])}

    # ---- diff, keyed on license_number (+ occurrence)
    def key(rows):
        d, c = {}, collections.Counter()
        for r in rows:
            c[r["license_number"]] += 1
            d[(r["license_number"], c[r["license_number"]])] = r
        return d
    ok, nk = key(old), key(new)
    out, kinds = [], collections.Counter()
    recs_changed = set()
    for k in sorted(set(ok) | set(nk)):
        o, n = ok.get(k), nk.get(k)
        if o is None or n is None:
            kinds["only_old" if n is None else "only_new"] += 1
            out.append({"license_number": k[0], "occurrence": k[1], "field": "*ROW*",
                        "kind": "only_old" if n is None else "only_new",
                        "old": (o or {}).get("facility_name", ""), "new": (n or {}).get("facility_name", "")})
            recs_changed.add(k[0])
            continue
        for f in FIELDS:
            ov, nv = o.get(f, ""), n.get(f, "")
            if f == "expiration_date":
                if iso(ov) == nv:
                    kinds["expiration_date: format only (DD-MM-YYYY -> ISO)"] += 1
                    continue
            if ov == nv:
                continue
            if f == "address_line1" and ov == ("%s %s" % (nv, n["address_city"])).strip():
                kind = "street+city split (old held both)"
            elif f == "address_city" and not ov:
                kind = "city filled (old blank)"
            else:
                kind = "value changed"
            kinds["%s: %s" % (f, kind)] += 1
            if kind == "value changed":
                recs_changed.add(k[0])
            out.append({"license_number": k[0], "occurrence": k[1], "field": f, "kind": kind,
                        "old": ov, "new": nv})
    with open(diff_p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["license_number", "occurrence", "field", "kind", "old", "new"])
        w.writeheader()
        w.writerows(out)
    C["diff_summary"] = dict(kinds)
    C["diff_records_with_a_real_value_change"] = len(recs_changed)
    json.dump(C, open(json_p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    brief = {k: v for k, v in C.items() if k not in ("header_bleed", "zip_4digit_padded")}
    brief["header_bleed"] = {k: len(v) for k, v in C["header_bleed"].items()}
    brief["zip_4digit_padded"] = {k: v for k, v in C["zip_4digit_padded"].items() if k != "rows"}
    print(json.dumps(brief, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
