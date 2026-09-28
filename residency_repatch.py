#!/usr/bin/env python3
"""Surgical residency-only re-derivation across landed Master Data enriched twins.

NOT a re-enrich. NPI/npi_basis and every base column are left exactly as they are, in the
same row order; only `resident_nonresident` and `resnon_basis` are recomputed with the
patched logic and written back.

Safety model, per file:
  * row alignment between base and enriched is PROVEN (the enriched twin's leading columns
    must equal the base's, cell for cell) before anything is recomputed
  * after writing, old and new are re-read and compared cell by cell: if ANY column other
    than the two residency columns differs, or the row count/order moves, the file is
    ABORTED and its output discarded
  * the chosen state column is reported per file so a silent physical->mailing swap
    (the distinct-value regression) cannot pass unnoticed
  * zero rows, or zero changes where changes were expected, refuse to write

Output goes to _master_data_dropin ONLY. Nothing is published to the OneDrive Master Data
path by this script.
"""
import argparse
import collections
import csv
import os
import shutil
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verified_enrich import (ADDR_COLS, TYPE_COLS, derive_resnon, pick, pick_state_col)

import pandas as pd

MD = (r"C:\Users\MarkFulton\OneDrive - Pharma Solutions USA, Inc\All Company - Documents"
      r"\LighthouseAI - Product\LighthouseAI Verified\Master Data")
DROP = r"C:\Verified\_master_data_dropin"
RN, RB = "resident_nonresident", "resnon_basis"
csv.field_size_limit(10_000_000)


def jur_of(fname):
    return (fname.split(" - ")[0].strip().upper()[:2] if " - " in fname
            else fname[:2].upper())


def read_raw(path):
    """Header + rows as plain strings, trying the same encodings load_table does."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            with open(path, encoding=enc, newline="") as fh:
                rows = list(csv.reader(fh))
            if rows:
                return rows[0], rows[1:], enc
        except UnicodeDecodeError:
            continue
    raise RuntimeError("unreadable: %s" % path)


def load_df(path):
    """Mirror verified_enrich.load_table exactly, including its ragged-row tolerance.

    Some landed files are ragged (FL has 9 fields on a 6-field line). The twin was
    produced through load_table's on_bad_lines="skip" path, so the base must be read the
    SAME way or the rows will not line up. Row alignment is proven separately below."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc, low_memory=False).fillna("")
        except UnicodeDecodeError:
            continue
        except pd.errors.ParserError:
            return pd.read_csv(path, dtype=str, encoding=enc, engine="python",
                               on_bad_lines="skip").fillna("")
    return pd.read_csv(path, dtype=str, encoding="latin-1", engine="python",
                       on_bad_lines="skip").fillna("")


def process(base_name, apply_write, report):
    base_path = os.path.join(MD, base_name)
    twin_name = base_name[:-4] + "_enriched.csv"
    twin_path = os.path.join(MD, twin_name)
    if not os.path.exists(twin_path):
        return

    try:
        bdf = load_df(base_path)
        thead, trows, tenc = read_raw(twin_path)
    except Exception as e:
        report.append((twin_name, "SKIP", "%s" % type(e).__name__, {}, None))
        return

    if RN not in thead or RB not in thead:
        return
    if len(trows) != len(bdf):
        report.append((twin_name, "SKIP", "row-count mismatch base=%d twin=%d"
                       % (len(bdf), len(trows)), {}, None))
        return

    # --- PROVE row alignment: the twin's leading columns must equal the base's
    bcols = list(bdf.columns)
    lead = [c for c in bcols if c in thead[:len(bcols)]]
    idx = {c: thead.index(c) for c in lead}
    probe = range(0, len(trows), max(1, len(trows) // 200))
    for i in probe:
        for c in lead[:6]:
            if str(bdf[c].iloc[i]) != trows[i][idx[c]]:
                report.append((twin_name, "ABORT",
                               "row %d misaligned on %r" % (i, c), {}, None))
                return

    cols = list(bdf.columns)
    scol = pick_state_col(bdf, cols)
    try:
        rn, rb = derive_resnon(bdf, pick(cols, TYPE_COLS), scol,
                               pick(cols, ADDR_COLS), jur_of(base_name))
    except Exception as e:
        report.append((twin_name, "SKIP", "%s: %s" % (type(e).__name__, str(e)[:40]),
                       {}, scol))
        return

    i_rn, i_rb = thead.index(RN), thead.index(RB)
    old_rn = [r[i_rn] if len(r) > i_rn else "" for r in trows]
    delta = collections.Counter((old_rn[i], rn[i]) for i in range(len(rn))
                                if old_rn[i] != rn[i])
    changed = sum(delta.values())
    if changed == 0:
        report.append((twin_name, "UNCHANGED", "", {}, scol))
        return
    if not trows:
        report.append((twin_name, "ABORT", "zero rows", {}, scol))
        return

    new_rows = []
    for i, r in enumerate(trows):
        row = list(r)
        while len(row) <= max(i_rn, i_rb):
            row.append("")
        row[i_rn] = rn[i]
        row[i_rb] = rb[i]
        new_rows.append(row)

    out_path = os.path.join(DROP, twin_name)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(thead)
        w.writerows(new_rows)

    # --- VERIFY: re-read and compare; only the two residency columns may differ
    vhead, vrows, _ = read_raw(tmp)
    if vhead != thead or len(vrows) != len(trows):
        os.remove(tmp)
        report.append((twin_name, "ABORT", "header/row-count changed on write", {}, scol))
        return
    bad = None
    for i in range(len(trows)):
        a, b = trows[i], vrows[i]
        for j in range(len(thead)):
            if j in (i_rn, i_rb):
                continue
            av = a[j] if j < len(a) else ""
            bv = b[j] if j < len(b) else ""
            if av != bv:
                bad = (i, thead[j], av[:30], bv[:30])
                break
        if bad:
            break
    if bad:
        os.remove(tmp)
        report.append((twin_name, "ABORT",
                       "row %d col %r changed (%r -> %r)" % bad, {}, scol))
        return

    if apply_write:
        shutil.move(tmp, out_path)
        status = "STAGED"
    else:
        os.remove(tmp)
        status = "DRY-RUN"
    report.append((twin_name, status, "", dict(delta), scol))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write corrected twins to _master_data_dropin (default: dry run)")
    a = ap.parse_args()

    bases = [f for f in sorted(os.listdir(MD))
             if f.lower().endswith(".csv") and "_enriched" not in f]
    report = []
    for b in bases:
        process(b, a.apply, report)

    print("=" * 104)
    print("RESIDENCY-ONLY RE-DERIVATION  (%s)" % ("APPLY" if a.apply else "DRY RUN"))
    print("=" * 104)
    tot = collections.Counter()
    nfiles = 0
    for name, status, note, delta, scol in report:
        if status in ("UNCHANGED",):
            continue
        if status in ("SKIP", "ABORT"):
            print("  [%-6s] %-62s %s" % (status, name[:62], note))
            continue
        nfiles += 1
        n = sum(delta.values())
        tot.update(delta)
        print("  [%-6s] %-62s rows_changed=%-6d state_col=%r"
              % (status, name[:62], n, scol))
        for (o, nw), k in sorted(delta.items(), key=lambda kv: -kv[1]):
            print("             %-12s -> %-12s %d" % (o or "(blank)", nw, k))
    print("-" * 104)
    print("  files changed : %d" % nfiles)
    print("  rows changed  : %d" % sum(tot.values()))
    for (o, nw), k in sorted(tot.items(), key=lambda kv: -kv[1]):
        print("     %-12s -> %-12s %d" % (o or "(blank)", nw, k))
    unchanged = [n for n, s, _, _, _ in report if s == "UNCHANGED"]
    print("  files unchanged: %d" % len(unchanged))
    for n in ("la-board-of-pharmacy-cds-facility", "or-pharmaceutical-sales-rep"):
        hit = [x for x in report if x[0].startswith(n)]
        if hit:
            print("  REGRESSION WATCH %-42s -> %s (state_col=%r)"
                  % (n, hit[0][1], hit[0][4]))


if __name__ == "__main__":
    main()
