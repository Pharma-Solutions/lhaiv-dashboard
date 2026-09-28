"""DoD verification for the three Tier-1 outputs. A clean run is not verification."""
import io
import re
import warnings

warnings.filterwarnings("ignore")
import pandas as pd
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0 Safari/537.36 LHAIV-tier1-adapter/1.0")
H = {"User-Agent": UA}
OK, BAD = "PASS", "**FAIL**"


def chk(label, cond, detail=""):
    print(f"  [{OK if cond else BAD}] {label}" + (f"  {detail}" if detail else ""))
    return bool(cond)


print("=" * 84)
print("NY — are the 21 removed rows genuine duplicates, or lost information?")
u = ("https://www.health.ny.gov/professionals/narcotic/licensing_and_certification/"
     "docs/licensed_entities.xlsx")
b = requests.get(u, headers=H, timeout=90).content
raw = pd.read_excel(io.BytesIO(b), dtype=str, header=1)
k = ["PREFIX/CLASS", "LICENSE #"]
dups = raw[raw.duplicated(subset=k, keep=False)].sort_values(k)
print(f"  rows involved in (class,number) collisions: {len(dups)}  groups: {dups.groupby(k).ngroups}")
diff = {}
for _, g in dups.groupby(k):
    for c in raw.columns:
        if g[c].nunique(dropna=False) > 1:
            diff[c] = diff.get(c, 0) + 1
print(f"  columns differing inside a colliding group: {diff if diff else '(none — exact duplicates)'}")
ident = len(dups) - dups.drop_duplicates().shape[0]
print(f"  fully identical rows among them: {ident} of {len(dups)}")
if diff:
    print("  sample groups:")
    shown = 0
    for key, g in dups.groupby(k):
        if g.drop_duplicates().shape[0] > 1:
            print(g[[c for c in ['PREFIX/CLASS', 'LICENSE #', 'LICENSEE NAME',
                                 'LICENSEE NAME.1', 'CITY', 'EXPIRATION DATE']
                     if c in g.columns]].to_string(index=False, max_colwidth=26))
            shown += 1
            if shown >= 3:
                break

print()
print("=" * 84)
print("DoD checks")

# ---- Colorado
print("\nCO  co_licenses.csv")
co = pd.read_csv("co_licenses.csv", dtype=str, keep_default_na=False)
api = requests.get("https://data.colorado.gov/resource/7s5z-vewr.json",
                   params={"$select": "count(*)", "$where":
                           "licensetype in('PDO','SPDO','TPDO','OSP','WHO','WHI','MFR',"
                           "'TPLP','NOF','PHA','PHAT','PHATP','PHACS')"},
                   headers=H, timeout=90).json()
api_n = int(api[0]["count"])
chk("row count matches the live API count", len(co) == api_n, f"csv={len(co):,} api={api_n:,}")
chk("plausible range (40k-70k for scope=pharmacy)", 40000 < len(co) < 70000, f"{len(co):,}")
chk("stamped State/__source/__extract_date",
    all(c in co.columns for c in ["State", "__source", "__extract_date"]),
    f"State={co['State'].iloc[0]!r}")
chk("all 13 expected prefixes present", co["licensetype"].nunique() == 13,
    f"{sorted(co['licensetype'].unique())}")
chk("known registrant present: Kuehne + Nagel (TPLP 3PL)",
    co["entityname"].str.contains("Kuehne", case=False, na=False).any())
chk("known registrant present: a Walgreen PDO",
    co[(co.licensetype == "PDO")]["entityname"].str.contains("Walgreen", case=False, na=False).any())
chk("both Active and Expired statuses present",
    {"Active", "Expired"} <= set(co["licensestatusdescription"].unique()),
    f"{sorted(set(co['licensestatusdescription'].unique()))[:6]}")
chk("discipline history retained (casenumber populated on some rows)",
    (co["casenumber"].astype(str).str.strip() != "").sum() > 1000,
    f"{(co['casenumber'].astype(str).str.strip()!='').sum():,} rows carry a case number")
chk("no unintended row loss (rows == API count, nothing de-duped away)", len(co) == api_n)

# ---- New York
print("\nNY  ny_bne_licenses.csv")
ny = pd.read_csv("ny_bne_licenses.csv", dtype=str, keep_default_na=False)
chk("plausible range (3.0k-4.5k)", 3000 < len(ny) < 4500, f"{len(ny):,}")
lz = [x for x in ny["LICENSE #"] if str(x).startswith("0")]
chk("LEADING ZEROS INTACT in 'LICENSE #'", len(lz) > 1900,
    f"{len(lz)} values start with 0, e.g. {lz[:3]}")
chk("LEADING ZEROS INTACT in 'PREFIX/CLASS'",
    sum(str(x).startswith("0") for x in ny["PREFIX/CLASS"]) > 3000)
chk("__valid_as_of captured from the banner",
    "__valid_as_of" in ny.columns and bool(str(ny["__valid_as_of"].iloc[0]).strip()),
    f"{ny['__valid_as_of'].iloc[0]!r}")
chk("split licensee-name columns joined",
    "LICENSEE NAME FULL" in ny.columns)
if "LICENSEE NAME FULL" in ny.columns:
    ex = ny[ny["LICENSEE NAME FULL"].str.contains("AMNEAL", na=False)]["LICENSEE NAME FULL"]
    chk("  continuation actually recovered (AMNEAL ... NEW YORK LLC)",
        any("NEW YORK" in v for v in ex), f"{list(ex[:1])}")
chk("known registrant present: POPULATION COUNCIL INC",
    ny["LICENSEE NAME"].str.contains("POPULATION COUNCIL", case=False, na=False).any())
chk("licence CLASSES not collapsed (multiple PREFIX/CLASS values)",
    ny["PREFIX/CLASS"].nunique() > 10, f"{ny['PREFIX/CLASS'].nunique()} classes")
chk("stamped State/__source", ny["State"].iloc[0] == "NY")

# ---- Virgin Islands
print("\nVI  vi_licenses.csv")
vi = pd.read_csv("vi_licenses.csv", dtype=str, keep_default_na=False)
chk("plausible range (>=100 absolute floor)", len(vi) >= 100, f"{len(vi)}")
lzv = [x for x in vi["License #"] if str(x).startswith("0")]
chk("LEADING ZEROS INTACT in 'License #'", len(lzv) >= 4, f"{lzv}")
chk("known registrant present: ABDALLAH",
    vi["Last Name"].str.contains("ABDALLAH", case=False, na=False).any())
for num, names in [("398", {"COYNE", "SCHIRM"}), ("437", {"DEFOUW", "MAIER"})]:
    got = set(vi[vi["License #"] == num]["Last Name"])
    chk(f"collision {num}: BOTH distinct licensees kept (not de-duped away)",
        names <= got, f"kept={sorted(got)}")
chk("whitespace stripped from keys",
    all(str(x) == str(x).strip() for x in vi["License #"]))
chk("pharmacists-only scope recorded", set(vi["License Type"]) == {"RPh"},
    f"{sorted(set(vi['License Type']))}")
chk("stamped State/__source/__valid_as_of",
    vi["State"].iloc[0] == "VI" and bool(str(vi["__valid_as_of"].iloc[0]).strip()),
    f"valid_as_of={vi['__valid_as_of'].iloc[0]!r}")


# ---- West Virginia (two files)
print("\nWV  wv_individual.csv / wv_facility.csv")
wvi = pd.read_csv("wv_individual.csv", dtype=str, keep_default_na=False)
wvf = pd.read_csv("wv_facility.csv", dtype=str, keep_default_na=False)

chk("individuals plausible (8k-16k)", 8000 < len(wvi) < 16000, f"{len(wvi):,}")
chk("facilities plausible (2.5k-6k)", 2500 < len(wvf) < 6000, f"{len(wvf):,}")

# counts must still match the live endpoints (no silent truncation)
for nm, url, got in (("individuals", "https://www.wvbop.com/public/roster.csv", len(wvi)),
                     ("facilities", "https://www.wvbop.com/public/rosterF.csv", len(wvf))):
    live = pd.read_csv(io.StringIO(requests.get(url, headers=H, timeout=90).text),
                       dtype=str, keep_default_na=False)
    chk(f"{nm}: row count matches the live CSV", len(live) == got,
        f"live={len(live):,} file={got:,}")

# keys unique -> nothing was lost to de-dupe
chk("individuals key unique (no rows lost)",
    wvi["License Number#"].nunique() == len(wvi),
    f"{wvi['License Number#'].nunique():,}/{len(wvi):,}")
chk("facilities key unique (no rows lost)",
    wvf["License Number"].nunique() == len(wvf),
    f"{wvf['License Number'].nunique():,}/{len(wvf):,}")

# leading zeros: zips AND the zeros embedded in alphanumeric licence numbers
zi = [x for x in wvi["Mailing Address Zip Code"] if str(x).startswith("0")]
chk("individuals LEADING ZEROS intact in zip", len(zi) > 150, f"{len(zi)} e.g. {zi[:3]}")
zf = [x for x in wvf["Physical Zip Code"] if str(x).startswith("0")]
chk("facilities LEADING ZEROS intact in zip", len(zf) > 350, f"{len(zf)} e.g. {zf[:3]}")
emb = [x for x in wvi["License Number#"] if re.match(r"^[A-Za-z]{2}0", str(x))]
chk("individuals embedded zeros intact in licence no (RP0...)", len(emb) > 1000,
    f"{len(emb):,} e.g. {emb[:3]}")

# coverage: which licence types each file actually carries
it = set(wvi["License Type"])
chk("individuals cover pharmacists/techs/interns",
    {"Registered Pharmacist", "Pharmacy Technician", "Intern"} <= it,
    f"{sorted(it)}")
ft = set(wvf["Type"])
chk("facilities cover the DSCSA trading-partner types",
    {"Wholesale Distributor", "Third-Party Logistics Provider", "Manufacturer"} <= ft,
    f"{len(ft)} types")
chk("facilities include retail pharmacies",
    any("Community Pharmacy" in t for t in ft))
chk("no overlap between the two files (individuals vs facilities keys)",
    not (set(wvi["License Number#"]) & set(wvf["License Number"])))

# stamping + the active-only scope record
for nm, d in (("individuals", wvi), ("facilities", wvf)):
    chk(f"{nm}: stamped State/__source/__extract_date",
        d["State"].iloc[0] == "WV" and bool(d["__source"].iloc[0])
        and bool(d["__extract_date"].iloc[0]),
        f"{d['__source'].iloc[0][:46]}")
    chk(f"{nm}: active-only scope recorded in __scope",
        d["__scope"].iloc[0] == "active-only" and set(d["Status"]) == {"Active"})

# DSCSA flags survived on the facility file
chk("facility DSCSA flags present (Controlled Substances / 503 A / 503 B)",
    all(c in wvf.columns for c in ["Controlled Substances", "503 A", "503 B"]))
chk("  some facilities flagged for controlled substances",
    (wvf["Controlled Substances"] == "Yes").sum() > 100,
    f"{(wvf['Controlled Substances']=='Yes').sum():,} flagged Yes")
chk("known WV facility type present: Wholesale Distributor rows",
    (wvf["Type"] == "Wholesale Distributor").sum() > 10,
    f"{(wvf['Type']=='Wholesale Distributor').sum()} rows")

print()
print("=" * 84)
print("Raw-CSV leading-zero proof (reading the file as TEXT via csv, not via pandas —")
print("this is what any downstream consumer actually sees on disk):")
import csv as _csv

for f, col in [("ny_bne_licenses.csv", "LICENSE #"), ("vi_licenses.csv", "License #"),
               ("wv_individual.csv", "Mailing Address Zip Code"),
               ("wv_facility.csv", "Physical Zip Code")]:
    with open(f, encoding="utf-8", newline="") as fh:
        rd = _csv.reader(fh)
        head = next(rd)
        idx = head.index(col)
        vals = [row[idx] for row in rd if row]
    z = [v for v in vals if v.startswith("0")]
    print(f"  {f}: {len(z)} of {len(vals)} rows have a leading zero -> e.g. {z[:5]}")
    assert z, f"{f}: leading zeros LOST on disk — dtype=str broke somewhere"
print("  (if these read 177 / 78 instead of 0177 / 078, int coercion has corrupted the file)")
