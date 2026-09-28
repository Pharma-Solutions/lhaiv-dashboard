"""DoD verification for nj_licenses.csv (NJ pharmacy personnel). A green run is not proof."""
import csv
import re
import warnings

warnings.filterwarnings("ignore")
import pandas as pd

CSV = r"C:\Verified\lhaiv-dashboard-pipeline\nj_licenses.csv"
OK, BAD = "PASS", "**FAIL**"
fails = []


def chk(label, cond, detail=""):
    good = bool(cond)
    if not good:
        fails.append(label)
    print(f"  [{OK if good else BAD}] {label}" + (f"  {detail}" if detail else ""))
    return good


df = pd.read_csv(CSV, dtype=str, keep_default_na=False)
print("=" * 84)
print(f"NJ pharmacy personnel — DoD checks\nrows={len(df):,}  cols={len(df.columns)}")

# --- structure: proves the pipe delimiter was honoured
print()
chk("parsed into many columns (not 1 -> delimiter was honoured)", len(df.columns) >= 18,
    f"{len(df.columns)} columns")
for c in ["license_no", "license_status_name", "license_type_name", "full_name",
          "addr_zipcode", "__license_type", "State", "__source", "__extract_date"]:
    chk(f"column present: {c}", c in df.columns)
chk("no phantom trailing column", not [c for c in df.columns if str(c).startswith("Unnamed:")],
    f"{[c for c in df.columns if str(c).startswith('Unnamed:')]}")

# --- per-type coverage: all 4 personnel types
print()
types = set(df["__license_type"])
expected = {"Pharmacist", "Pharmacist Graduate License", "Pharmacy Intern",
            "Pharmacy Technician"}
chk("all 4 personnel license types present", expected <= types, f"{sorted(types)}")
chk("no prescriber/facility CDS types leaked in", not (types - expected),
    f"extra={sorted(types - expected)}")
print("\n  per-type row counts:")
for k, v in df["__license_type"].value_counts().items():
    print(f"    {k:<34} {v:>8,}")

# --- plausible totals
print()
counts = df["__license_type"].value_counts().to_dict()
chk("Pharmacist plausible (25k-60k)", 25000 < counts.get("Pharmacist", 0) < 60000,
    f"{counts.get('Pharmacist', 0):,}")
chk("Pharmacy Technician plausible (10k-80k)",
    10000 < counts.get("Pharmacy Technician", 0) < 80000,
    f"{counts.get('Pharmacy Technician', 0):,}")
chk("every type non-empty", all(v > 0 for v in counts.values()))
chk("total plausible (50k-200k)", 50000 < len(df) < 200000, f"{len(df):,}")

# --- leading zeros in the RAW BYTES, not via pandas
print()
with open(CSV, encoding="utf-8", newline="") as fh:
    rd = csv.reader(fh)
    head = next(rd)
    rows = list(rd)
zi = head.index("addr_zipcode")
zips = [r[zi] for r in rows if len(r) > zi]
lz = [z for z in zips if z.startswith("0")]
chk("LEADING ZEROS intact in addr_zipcode (raw bytes)", len(lz) > 100,
    f"{len(lz):,} of {len(zips):,} e.g. {lz[:4]}")
chk("no float coercion ('.0') in addr_zipcode", not any(z.endswith(".0") for z in zips))
li = head.index("license_no")
lics = [r[li] for r in rows if len(r) > li]
alnum = [x for x in lics if re.match(r"^\d+[A-Za-z]", x)]
chk("license_no kept alphanumeric verbatim (e.g. 28RI...)", len(alnum) > 1000,
    f"{len(alnum):,} alphanumeric e.g. {alnum[:2]}")

# --- a known NJ pharmacist present
print()
chk("known NJ pharmacist present (from the live sample: CHOHAN)",
    df["last_name"].str.contains("CHOHAN", case=False, na=False).any())
chk("emails populated for many rows (payload not truncated)",
    (df["addr_email"].str.contains("@", na=False)).sum() > 1000,
    f"{(df['addr_email'].str.contains('@', na=False)).sum():,} with an @")
chk("addresses populated", (df["addr_city"].str.strip() != "").sum() > 0.8 * len(df))

# --- status spread: active AND expired both present
print()
st = set(df["license_status_name"])
chk("both Active and Expired present", {"Active", "Expired"} <= st)
chk("status history retained (Reinstatement Pending survived de-dupe)",
    (df["license_status_name"] == "Reinstatement Pending").sum() > 0,
    f"{(df['license_status_name']=='Reinstatement Pending').sum()} rows")
print("\n  status spread:")
for k, v in df["license_status_name"].replace("", "(blank)").value_counts().items():
    print(f"    {str(k):<26} {v:>8,}")

# --- de-dupe integrity: blank-key rows were NOT collapsed
print()
blank = (df["license_no"].str.strip() == "").sum()
chk("blank-license_no rows PRESERVED (not collapsed to one)", blank > 1000,
    f"{blank:,} rows with a blank license_no")
nb = df[df["license_no"].str.strip() != ""]
pair = nb.drop_duplicates(subset=["license_no", "license_status_name"])
chk("(license_no, license_status_name) is unique among keyed rows",
    len(pair) == len(nb), f"{len(pair):,}/{len(nb):,}")

# --- stamping
print()
chk("State stamped NJ", set(df["State"]) == {"NJ"})
chk("__source stamped", df["__source"].str.contains("MyLicense").all(),
    f"{df['__source'].iloc[0]}")
chk("__extract_date stamped", (df["__extract_date"].str.strip() != "").all(),
    f"{df['__extract_date'].iloc[0]}")

print()
print("=" * 84)
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print(f"  - {f}")
else:
    print("all checks passed")
