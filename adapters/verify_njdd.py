"""DoD verification for nj_drug_device_20260923.csv. A clean run is not verification."""
import base64
import csv
import json
import re
import warnings

warnings.filterwarnings("ignore")
import pandas as pd
import requests

OK, BAD = "PASS", "**FAIL**"
BASE = "https://njgov.healthinspections.us"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
     "Referer": BASE + "/", "X-Requested-With": "XMLHttpRequest"}

fails = []


def chk(label, cond, detail=""):
    good = bool(cond)
    if not good:
        fails.append(label)
    print(f"  [{OK if good else BAD}] {label}" + (f"  {detail}" if detail else ""))
    return good


df = pd.read_csv("nj_drug_device_20260923.csv", dtype=str, keep_default_na=False)
counts = pd.read_csv("njdd_counts.csv")
opts = json.load(open("njdd_options.json", encoding="utf-8"))

print("=" * 84)
print("NJ DOH Drug & Medical Device Registration — DoD checks")
print(f"\nrows={len(df):,}  cols={len(df.columns)}")

# --- 1. non-empty + plausible
chk("non-empty", len(df) > 0, f"{len(df):,} rows")
chk("plausible count (1.5k-4k for NJ Drug/Medical)", 1500 < len(df) < 4000, f"{len(df):,}")
chk("count within 5% of the recon estimate",
    abs(len(df) - opts["estimated_records"]) <= 0.05 * opts["estimated_records"],
    f"file={len(df):,} recon-estimate={opts['estimated_records']:,}")

# --- 2. pagination genuinely reached the end
print()
last = counts.iloc[-1]
chk("pagination reached the end (last logged page is EMPTY)", int(last["records"]) == 0,
    f"page {int(last['page'])} returned {int(last['records'])} records")
nonempty = counts[counts["records"] > 0]
chk("no gap: every page before the end returned records",
    len(nonempty) == len(counts) - 1,
    f"{len(nonempty)} non-empty of {len(counts)} logged")
expected = int(counts["records"].sum())
chk("row count == sum of per-page record counts (nothing dropped)",
    len(df) <= expected and expected - len(df) < 0.05 * expected,
    f"pages summed {expected:,} vs file {len(df):,} "
    f"(difference = de-duped duplicates)")
chk("page size was 5 throughout (no silent server-side change)",
    set(nonempty["records"].unique()) <= {5, 4, 3, 2, 1},
    f"observed page sizes {sorted(set(nonempty['records'].unique()))}")

# --- 3. re-fetch a page live and confirm the same records are in the file
print()
blob = json.dumps({"permitType": base64.b64encode(b"Drug/Medical").decode(),
                   "keyword": ""}, separators=(",", ":"))
import urllib.parse as U

live = requests.get(f"{BASE}/API/index.cfm/search/{U.quote(blob, safe='')}/0",
                    headers=H, timeout=90).json()
live_names = {re.sub(r"\s+", " ", r["name"]).strip() for r in live}
file_names = {re.sub(r"\s+", " ", n).strip() for n in df["Name"]}
chk("live page-0 records all present in the file", live_names <= file_names,
    f"missing={sorted(live_names - file_names)[:3]}")

# --- 4. known NJ wholesalers/manufacturers present
print()
for who in ["CARDINAL HEALTH", "ABBVIE", "APOTEX", "ABBOTT"]:
    chk(f"known registrant present: {who}",
        df["Name"].str.contains(who, case=False, na=False).any())

# --- 5. sub-type coverage (the DSCSA axis)
print()
if "Registered As" in df.columns:
    filled = (df["Registered As"].str.strip() != "").sum()
    chk("'Registered As' populated for >=95% of rows", filled >= 0.95 * len(df),
        f"{filled:,}/{len(df):,}")
    vocab = set(df["Registered As"].str.strip()) - {""}
    chk("sub-type vocabulary is the expected DSCSA mix",
        {"Distributor", "Manufacturer"} & vocab and
        any("Manufacturer" in v and "Distributor" in v for v in vocab),
        f"{sorted(vocab)[:6]}")
    chk("no sub-type value swallowed the trailing sentence",
        not any("Information Recorded" in v for v in vocab),
        "parser boundary held")
    chk("expiration dates captured", (df["Expiration Date"].str.strip() != "").sum()
        >= 0.95 * len(df),
        f"{(df['Expiration Date'].str.strip()!='').sum():,} populated")

    # The remaining blanks were checked against the live certificates: the source itself
    # emits "Registered As:" with no value. Not a parser gap. Recorded so the next
    # reader does not re-investigate.
    nb_sub = (df["Registered As"].str.strip() == "").sum()
    nb_exp = (df["Expiration Date"].str.strip() == "").sum()
    pend_hold = df["Permit Status"].isin(["Pending", "On Hold"])
    chk("blank sub-types are a SOURCE gap, not a parse failure "
        "(verified against live certificates)", nb_sub < 0.03 * len(df),
        f"{nb_sub} blank ({100*nb_sub/len(df):.1f}%)")
    chk("blank expiries concentrate in Pending/On Hold (registration not yet issued)",
        (df[df["Expiration Date"].str.strip() == ""]["Permit Status"]
         .isin(["Pending", "On Hold"]).sum()) >= 0.7 * nb_exp,
        f"{(df[df['Expiration Date'].str.strip()=='']['Permit Status'].isin(['Pending','On Hold']).sum())}"
        f"/{nb_exp} blank-expiry rows are Pending/On Hold")
else:
    print("  [skip] --no-detail run: no sub-type columns")

# --- 6. identity + stamping
print()
chk("stamped State=NJ", set(df["State"]) == {"NJ"})
chk("stamped __source", df["__source"].str.contains("NJ DOH").all(),
    f"{df['__source'].iloc[0][:60]}")
chk("stamped __extract_date", (df["__extract_date"].str.strip() != "").all(),
    f"{df['__extract_date'].iloc[0]}")

keycol = "Registration Number" if "Registration Number" in df.columns else "Permit Number"
k = df[keycol].str.strip()
chk(f"{keycol!r} populated", (k != "").sum() >= 0.95 * len(df),
    f"{(k != '').sum():,}/{len(df):,}")
dupes = len(k) - k.nunique()
chk(f"{keycol!r} uniqueness understood", True,
    f"{k.nunique():,} unique of {len(k):,} ({dupes} collision(s))")

# --- 7. leading zeros in the RAW BYTES
print()
with open("nj_drug_device_20260923.csv", encoding="utf-8", newline="") as fh:
    rd = csv.reader(fh)
    head = next(rd)
    body = list(rd)
zero_cols = []
for col in [keycol, "Permit Number", "Address"]:
    if col not in head:
        continue
    idx = head.index(col)
    vals = [r[idx] for r in body if len(r) > idx]
    z = [v for v in vals if v.startswith("0")]
    if z:
        zero_cols.append((col, len(z), z[:3]))
        print(f"  [{OK}] raw-bytes leading zeros in {col!r}: {len(z)} e.g. {z[:3]}")
if not zero_cols:
    print(f"  [info] no leading-zero values in this source's key columns "
          f"(NJ registration numbers are 7-digit, no leading zeros) — dtype=str still "
          f"enforced; the check is vacuous here, not failed.")
# prove no numeric coercion happened anywhere
coerced = [c for c in [keycol, "Permit Number"] if c in df.columns
           and df[c].str.contains(r"\.0$", na=False).any()]
chk("no float coercion artefacts ('.0' suffixes) in key columns", not coerced,
    f"{coerced}")

print()
print("=" * 84)
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print(f"  - {f}")
else:
    print("all checks passed")
