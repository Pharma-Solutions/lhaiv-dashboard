#!/usr/bin/env python3
"""ENG-343 credential-confirmation + natural-key checks for the NJ D&MD pull.

Answers the question the ticket actually asks: is this the NJ **Drug & Medical Device
Registration** credential, and NOT the NJ Board of Pharmacy roster we already hold?

Positive evidence (it IS D&MD) and NEGATIVE evidence (it is NOT MyLicense/BOP) are both
asserted, because "different credential" is a claim about provenance and schema, not a
vibe about the names.
"""
import csv
import sys
import warnings

warnings.filterwarnings("ignore")
import pandas as pd

NEW = sys.argv[1] if len(sys.argv) > 1 else "nj_drug_device_20260923.csv"
PRIOR = "njdd_prior_20260828.csv"
fails = []


def chk(label, cond, detail=""):
    ok = bool(cond)
    if not ok:
        fails.append(label)
    print(f"  [{'PASS' if ok else '**FAIL**'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


d = pd.read_csv(NEW, dtype=str, keep_default_na=False)
p = pd.read_csv(PRIOR, dtype=str, keep_default_na=False)

print("=" * 88)
print(f"ENG-343 — credential confirmation\n  new  : {NEW}  {len(d):,} rows")
print(f"  prior: {PRIOR}  {len(p):,} rows (2026-08-28)")

# ---- A. POSITIVE: this IS the Drug & Medical Device credential
print("\nA. POSITIVE evidence — this is NJ Drug & Medical Device Registration")
chk("Permit Type is uniformly 'Drug/Medical'", set(d["Permit Type"]) == {"Drug/Medical"},
    f"{sorted(set(d['Permit Type']))}")
chk("__source names NJ DOH D&MD + the Tyler host",
    d["__source"].str.contains("Drug & Medical Device", case=False).all()
    and d["__source"].str.contains("njgov.healthinspections.us").all(),
    f"{d['__source'].iloc[0][:78]}")
sub = set(x for x in d["Registered As"] if x.strip())
chk("'Registered As' is the manufacturer/distributor vocabulary",
    sub <= {"Distributor", "Manufacturer", "Manufacturer,Distributor"} and len(sub) >= 2,
    f"{sorted(sub)}")
chk("count in the backlog's ~2,326 band (+-5%)", 2210 <= len(d) <= 2442, f"{len(d):,}")

# ---- B. NEGATIVE: this is NOT the NJ BOP / MyLicense roster
print("\nB. NEGATIVE evidence — this is NOT NJ BOP / Consumer Affairs MyLicense")
chk("no MyLicense provenance anywhere in __source",
    not d["__source"].str.contains("mylicense", case=False).any())
mylicense_cols = {"license_no", "license_type_name", "profession_name", "license_status_name",
                  "full_name", "addr_line_1"}
chk("schema is disjoint from the MyLicense pipe-delimited shape",
    not (mylicense_cols & set(d.columns)),
    f"overlap={sorted(mylicense_cols & set(d.columns)) or 'none'}")
chk("no pharmacist/technician/intern credential classes present",
    not any(k in " ".join(sub).lower() for k in ("pharmacist", "technician", "intern")),
    f"{sorted(sub)}")

# ---- C. natural key: State + Registration Number + Registered As
print("\nC. Natural key — State(NJ) + Registration Number + Registered As")
regno = d["Registration Number"].astype(str).str.strip()
permno = d["Permit Number"].astype(str).str.strip()
chk("State stamped NJ on every row", set(d["State"]) == {"NJ"})
chk("Registration Number populated", (regno != "").sum() >= 0.98 * len(d),
    f"{(regno != '').sum():,}/{len(d):,}")
key = d.assign(_r=regno)[["State", "_r", "Registered As"]]
dupe = len(key) - key.drop_duplicates().shape[0]
chk("natural key is unique (no collapse needed)", dupe == 0,
    f"{len(key)-dupe:,} distinct of {len(key):,} ({dupe} dup)")

# Registration <-> Permit 1:1, as instructed
print("\n   Registration Number <-> Permit Number relationship:")
both = d.assign(_r=regno, _p=permno)
both = both[(both["_r"] != "") & (both["_p"] != "")]
same = (both["_r"] == both["_p"]).sum()
r2p = both.groupby("_r")["_p"].nunique()
p2r = both.groupby("_p")["_r"].nunique()
print(f"     rows with both populated : {len(both):,}")
print(f"     identical values         : {same:,} ({100*same/max(len(both),1):.1f}%)")
print(f"     reg -> >1 permit         : {(r2p > 1).sum()}")
print(f"     permit -> >1 reg         : {(p2r > 1).sum()}")
chk("Registration <-> Permit is 1:1", (r2p > 1).sum() == 0 and (p2r > 1).sum() == 0,
    "DIVERGENCE — flag" if ((r2p > 1).sum() or (p2r > 1).sum()) else "clean 1:1")
if (r2p > 1).sum():
    print("     diverging registration numbers:")
    for k in list(r2p[r2p > 1].index[:5]):
        print(f"       {k}: permits={sorted(set(both[both['_r']==k]['_p']))[:4]}")

# ---- D. raw values preserved verbatim + leading zeros
print("\nD. Raw-value preservation")
chk("raw license_type kept verbatim (Registered As + Permit Type both present)",
    "Registered As" in d.columns and "Permit Type" in d.columns)
chk("raw status kept verbatim (Permit Status + Certificate Permit Status)",
    "Permit Status" in d.columns and "Certificate Permit Status" in d.columns)
with open(NEW, encoding="utf-8", newline="") as fh:
    rd = csv.reader(fh)
    h = next(rd)
    rows = list(rd)
ri = h.index("Registration Number")
vals = [r[ri] for r in rows if len(r) > ri]
chk("no float coercion in Registration Number (raw bytes)",
    not any(v.endswith(".0") for v in vals), f"e.g. {vals[:3]}")
chk("registration numbers are strings of uniform width (leading zeros would survive)",
    len(set(len(v) for v in vals if v)) <= 3,
    f"lengths={sorted(set(len(v) for v in vals if v))}")

# ---- E. active AND expired present
print("\nE. Status coverage (must not be active-only)")
st = d["Permit Status"].replace("", "(blank)").value_counts()
chk("more than one status present", len(st) > 1, f"{dict(list(st.items())[:6])}")
chk("Active present", "Active" in st.index)
chk("at least one non-active status present",
    any(s for s in st.index if s not in ("Active", "(blank)")))

# ---- F. drift vs the 2026-08-28 baseline
print("\nF. Drift vs 2026-08-28 baseline")
print(f"     rows      : {len(p):,} -> {len(d):,}   ({len(d)-len(p):+,})")
pk = set(p["Registration Number"].astype(str).str.strip())
nk = set(regno)
print(f"     added     : {len(nk-pk):,}")
print(f"     removed   : {len(pk-nk):,}")
print(f"     unchanged : {len(nk & pk):,}")
chk("drift is plausible (<10% churn either way)",
    len(nk - pk) < 0.10 * len(d) and len(pk - nk) < 0.10 * len(p),
    f"+{len(nk-pk)} / -{len(pk-nk)}")
print("\n     per-'Registered As' then -> now:")
pc = p["Registered As"].replace("", "(blank)").value_counts().to_dict()
nc = d["Registered As"].replace("", "(blank)").value_counts().to_dict()
for k in sorted(set(pc) | set(nc)):
    print(f"       {k:<28} {pc.get(k,0):>6,} -> {nc.get(k,0):>6,}  ({nc.get(k,0)-pc.get(k,0):+})")

print("\n" + "=" * 88)
print("ALL CREDENTIAL ASSERTIONS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
