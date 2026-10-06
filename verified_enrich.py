#!/usr/bin/env python3
"""
verified_enrich.py  —  LHAI Verified data-enrichment for Master Data files.

Adds two columns to a state license roster:
  1. resident_nonresident  (+ resnon_basis)  — 100% offline, no dependencies beyond pandas
  2. NPI                    (+ npi_basis)     — via NPPES; pharmacies only, blank where no confident match

Pilot-proven on the Kentucky pharmacy + wholesaler lists (2026-09-21).

USAGE
  # resident/nonresident only (offline), for one file:
  python verified_enrich.py --jurisdiction KY --no-npi  "KY - Entire Pharmacy List_090926.csv"

  # get the real NPI pharmacy match rate FIRST, on a 300-row sample (run where NPPES is reachable):
  python verified_enrich.py --jurisdiction KY --npi --sample 300  "KY - Entire Pharmacy List_090926.csv"

  # full enrichment (resident/nonresident + NPI join) for one or many files:
  python verified_enrich.py --jurisdiction KY --npi  file1.csv file2.csv ...

NOTES
  - Column names are auto-detected across the heterogeneous Master Data schemas
    (NAME/location_name/BusinessName ; LICENSE TYPE/registration_type/LicenseType ; STATE/phys_state/FacilityState ...).
  - NPI is only attempted for rows that look like PHARMACIES (retail/community dispensers), because
    wholesalers/manufacturers/3PLs are not health-care providers and have no NPI. It is NEVER fabricated:
    a value is written only on a confident NPPES match (retail-pharmacy taxonomy 3336C0003X + address/ZIP agreement),
    otherwise NPI is left blank with the reason in npi_basis.
  - --jurisdiction is the 2-letter code of the ISSUING board (e.g. KY), used to decide resident vs
    nonresident when the license type carries no explicit marker (facility in-state = resident).
"""
import argparse, sys, time, re, json
import pandas as pd

# Windows consoles default to cp1252, which cannot encode characters like the
# warning glyph below and would crash the run *before* the enriched file is
# written. Force UTF-8 on the streams (no effect on the CSV, which is written
# UTF-8 by to_csv regardless).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- column auto-detection ------------------------------------------------
NAME_COLS  = ["name","location_name","businessname","business name","legal_name","organization_name","sort name","facility name","full name","combined_name"]
TYPE_COLS  = ["license type","registration_type","licensetype","license_type","licensetypedescription","description","profession","proftype","class","les_description","lst_description","factype"]
# Physical/practice-location fields lead (they decide residency); mailing is the fallback.
# NOTE: an "Incorporation State" column is the entity's domicile (e.g. DE), NOT its physical
# location, so it must never be read as the residency state — pick_state_col drops it below.
STATE_COLS = ["physical state","physical_state","phys state","physicalstate","state","phys_state","facilitystate","mail state","mailstate","mailing state","addr state","ba_state","st"]
CITY_COLS  = ["physical city","physicalcity","phys city","city","phys_city","facilitycity","mail city","mailing city","addr city","ba_city"]
ZIP_COLS   = ["physical zip","physicalzip","phys zip","zip","zipcode","zip code","postal","phys_zip","facilityzip","mail zip","mailing zip","ba_zip","mailzipcode"]
ADDR_COLS  = ["physical street","physical address","address 1","address1","address","street","phys_address1","facilityaddress","mail street","mailing address 1","mailing_address","addr 1","ba_address","adress"]

def pick_state_col(df, cols):
    """Choose the column that actually holds a PHYSICAL state.

    pick() exact-matches a column literally named `state` before anything else. In the
    canonical adapter schema that column is the constant issuing/jurisdiction stamp, while
    the licensee's real state sits in a separate column (e.g. `address_state`). Picking the
    stamp is what forced the manual state->jurisdiction rename on NJ and NM, and would have
    mis-stamped ~2,200 NM rows Resident.

    So: gather every state-like candidate, and prefer one whose values VARY. A constant
    column is only chosen when no varying candidate exists - which is the correct answer for
    genuine single-jurisdiction rosters (KY Manufacturer, AZ, DC)."""
    def distinct(c):
        vals = {normalize_state(v) for v in df[c].tolist()}
        return {v for v in vals if v}

    # An "Incorporation State" / "State of Incorporation" column is the entity's legal
    # domicile, not where it physically operates, so it must never be treated as the
    # residency state (AR's GLSuite roster has one, and it varies — DE/TX/etc. — so it
    # would otherwise be accepted as a real, varying state column and mis-key residency).
    cols = [c for c in cols if "incorpor" not in c.lower()]

    primary = pick(cols, STATE_COLS)
    if primary is None:
        return None
    # A column whose NAME explicitly denotes the physical/practice location is the true
    # residency state even when it is constant across the file. Do NOT override it with a
    # varying mailing column: an in-state facility type (AR hospitals, specialty pharmacies)
    # has Physical State == the jurisdiction for every row (all Resident), while its Mail
    # State varies because of out-of-state corporate HQ addresses — overriding to Mail State
    # wrongly stamped 12 in-state AR facilities Nonresident. The constant-override below is
    # only for a constant *issuing/jurisdiction stamp*, i.e. a generically-named column.
    primary_is_physical = any(t in primary.lower() for t in ("physical", "phys", "facility"))
    if len(distinct(primary)) > 1 or primary_is_physical:
        # A real, varying state column (or an explicitly physical one). LEAVE IT ALONE. An
        # earlier attempt here ranked candidates by "most distinct values" and that is not a
        # proxy for "physical": it swapped LA's `State (P)` (physical) for `State (M)`
        # (mailing) and OR's `DOMICILESTATE` for `BUS_STATE`, moving ~970 rows wrongly. The
        # defect being overridden is specifically a CONSTANT, generically-named stamp.
        return primary

    low = {c.lower().strip(): c for c in cols}
    cands = []
    for cand in STATE_COLS:
        if cand in low and low[cand] not in cands:
            cands.append(low[cand])
    for cand in STATE_COLS:
        if len(cand) <= 2:
            continue
        for lc, orig in low.items():
            if cand in lc and orig not in cands:
                cands.append(orig)
    for c in cands:
        if c != primary and len(distinct(c)) > 1:
            return c
    return primary

def pick(cols, candidates):
    low = {c.lower().strip(): c for c in cols}
    for cand in candidates:                       # exact match first
        if cand in low: return low[cand]
    for cand in candidates:                       # loose contains-match, but NOT for 1-2 char
        if len(cand) <= 2: continue               # e.g. "st" must never match "licenseStatus"/"lastName"
        for lc, orig in low.items():
            if cand in lc: return orig
    return None

US_STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS",
 "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH",
 "OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","PR","VI","GU","MP","AS"}
STATE_NAMES = {"ALABAMA":"AL","ALASKA":"AK","ARIZONA":"AZ","ARKANSAS":"AR","CALIFORNIA":"CA","COLORADO":"CO",
 "CONNECTICUT":"CT","DELAWARE":"DE","DISTRICT OF COLUMBIA":"DC","FLORIDA":"FL","GEORGIA":"GA","HAWAII":"HI",
 "IDAHO":"ID","ILLINOIS":"IL","INDIANA":"IN","IOWA":"IA","KANSAS":"KS","KENTUCKY":"KY","LOUISIANA":"LA",
 "MAINE":"ME","MARYLAND":"MD","MASSACHUSETTS":"MA","MICHIGAN":"MI","MINNESOTA":"MN","MISSISSIPPI":"MS",
 "MISSOURI":"MO","MONTANA":"MT","NEBRASKA":"NE","NEVADA":"NV","NEW HAMPSHIRE":"NH","NEW JERSEY":"NJ",
 "NEW MEXICO":"NM","NEW YORK":"NY","NORTH CAROLINA":"NC","NORTH DAKOTA":"ND","OHIO":"OH","OKLAHOMA":"OK",
 "OREGON":"OR","PENNSYLVANIA":"PA","RHODE ISLAND":"RI","SOUTH CAROLINA":"SC","SOUTH DAKOTA":"SD",
 "TENNESSEE":"TN","TEXAS":"TX","UTAH":"UT","VERMONT":"VT","VIRGINIA":"VA","WASHINGTON":"WA",
 "WEST VIRGINIA":"WV","WISCONSIN":"WI","WYOMING":"WY","PUERTO RICO":"PR"}

def normalize_state(val):
    """Return a valid US 2-letter code, or '' if the value isn't a recognizable state/territory
    (so status/name/junk columns and blanks collapse to '')."""
    v = re.sub(r'\s+', ' ', str(val).strip().upper())
    if v in US_STATES: return v
    return STATE_NAMES.get(v, "")

DIRECTIONAL = {"NE", "NW", "SE", "SW"}   # street quadrants that collide with state codes
_STREET_SUFFIX = re.compile(
    r"\b(ST|STREET|AVE|AVENUE|BLVD|BOULEVARD|RD|ROAD|DR|DRIVE|LN|LANE|WAY|PKWY|PARKWAY|"
    r"CT|COURT|PL|PLACE|TER|TERRACE|CIR|CIRCLE|HWY|HIGHWAY|PIKE|TRL|TRAIL|SQ|SQUARE)\b\.?\s*$",
    re.I)

def _looks_like_street_line(tok):
    """True when a comma-part reads as a street line rather than a city.
    Used only to disqualify a directional quadrant from being read as a state."""
    t = str(tok).strip()
    if not t: return False
    return bool(re.match(r'^\d', t) or _STREET_SUFFIX.search(t))

def addr_state_strong(addr):
    """State from strong, unambiguous address patterns only (validated against US_STATES):
    a 2-letter code followed by a ZIP, or a comma-delimited state token. Street-only lines
    (no comma, no ZIP) return '' so suffixes like 'CT'/'DR' can't be misread as a state.

    DIRECTIONAL QUADRANT GUARD: a comma-delimited NE/NW/SE/SW immediately after a street
    line is a quadrant, not a state. '4431 Anaheim Ave., NE, Ste. A' and
    '4414 BENNING ROAD, NE' are Albuquerque and Washington DC, not Nebraska. The test is
    the PRECEDING token, not a following unit token - the DC cases have nothing after the
    quadrant at all. A genuine 'Omaha, NE' still parses, because 'Omaha' is not a street
    line, and '123 Main St, Omaha, NE 68101' is caught by the ST-<zip> pattern above."""
    a = str(addr).strip()
    if not a: return ""
    for tok in reversed(re.findall(r'\b([A-Za-z]{2})[,\s]+\d{5,9}(?:-\d{4})?\b', a)):  # ST <zip>
        if tok.upper() in US_STATES: return tok.upper()
    parts = [p.strip() for p in a.split(',') if p.strip()]                             # ..., ST[, ...]
    for i in range(len(parts) - 1, -1, -1):
        tok = parts[i].upper()
        if tok not in US_STATES: continue
        if tok in DIRECTIONAL and i > 0 and _looks_like_street_line(parts[i - 1]):
            continue
        return tok
    return ""

def addr_state_weak(addr):
    """Loose fallback: a trailing 2-letter state ('City ST'). Only used when there is no state
    column and no strong pattern (e.g. Puerto Rico's 'Catano PR')."""
    m = re.search(r'\b([A-Za-z]{2})\s*$', str(addr).strip())
    return m.group(1).upper() if (m and m.group(1).upper() in US_STATES) else ""

def state_from_addr(addr):   # used by the NPI query
    return addr_state_strong(addr) or addr_state_weak(addr)

# ---- foreign-address detection -------------------------------------------
# A US_STATES membership test is applied FIRST everywhere below, so codes that are
# genuinely US states (DE Delaware, IN Indiana, LA, MS, OK, OR, PA, VA...) can never be
# mistaken for foreign ones even though they collide with country abbreviations.
FOREIGN_CODES = {
    "AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT",   # Canada
    "UK","GB","IE","IT","FR","ES","BE","CH","SE","NO","DK","FI","AT","PT","GR",
    "PL","CZ","AU","NZ","JP","CN","KR","MX","BR","ZA","IL","SG","HK","TW",
}
FOREIGN_TOKENS = re.compile(
    r"\b(CANADA|ONTARIO|QUEBEC|BRITISH COLUMBIA|ALBERTA|MANITOBA|SASKATCHEWAN|"
    r"NOVA SCOTIA|NEW BRUNSWICK|NEWFOUNDLAND|UNITED KINGDOM|ENGLAND|SCOTLAND|WALES|"
    r"IRELAND|ITALY|ITALIA|GERMANY|DEUTSCHLAND|FRANCE|SPAIN|NETHERLANDS|BELGIUM|"
    r"SWITZERLAND|SWEDEN|DENMARK|NORWAY|AUSTRALIA|JAPAN|SINGAPORE)\b", re.I)
CA_POSTAL = re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", re.I)
UK_POSTAL = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s+\d[A-Z]{2}\b", re.I)

# Some boards publish a LITERAL in the state field instead of a code. SD's Wholesale
# sheet uses "Outside USA" for its three Canadian registrants (Jubilant DraxImage, AX
# Pharmaceutical, BWXT Medical). Checked against the STATE FIELD ONLY - never against
# address text - so a street or business name can never trip it. Add a value here only
# when it has actually been OBSERVED in a board's data; guessing variants would widen
# the match on no evidence. The US_STATES/STATE_NAMES guard is applied first, exactly
# as for FOREIGN_CODES, so a genuine US state can never be read as foreign.
FOREIGN_STATE_LITERALS = {
    "OUTSIDE USA",          # observed: SD Board of Pharmacy, Wholesale sheet, 2026-09-30
}

def looks_foreign(addr, raw_state=""):
    """True when the row is plainly outside the US.

    Without this, a foreign row whose address will not parse falls through to a constant
    issuing-state column and is stamped Resident - the NJ case (8 rows: ON x4, QC, BC, IT,
    UK). Puerto Rico and other US territories are in US_STATES and are never flagged."""
    a = str(addr or "")
    rs = re.sub(r"\s+", " ", str(raw_state or "").strip().upper())
    if rs and rs not in US_STATES and rs not in STATE_NAMES and rs in FOREIGN_CODES:
        return True
    if rs and rs not in US_STATES and rs not in STATE_NAMES and rs in FOREIGN_STATE_LITERALS:
        return True
    if FOREIGN_TOKENS.search(a) or FOREIGN_TOKENS.search(rs):
        return True
    return bool(CA_POSTAL.search(a) or UK_POSTAL.search(a))

def load_table(path):
    """Robust reader for the heterogeneous Master Data files: handles .xlsx/.xls and CSVs
    with odd encodings or ragged rows without crashing."""
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path, dtype=str).fillna("")
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

# ---- resident / nonresident ----------------------------------------------
def derive_resnon(df, type_col, state_col, addr_col, jurisdiction):
    """Resident/Nonresident per row.

    Order: an explicit marker in the licence type wins outright. Otherwise precedence
    depends on WHAT THE STATE COLUMN ACTUALLY IS, decided from the data rather than assumed:

      * VARYING state column  -> it is a real physical-state field, so it leads, and the
        address only fills gaps. This also stops a mid-street parse from overriding a good
        column value.
      * CONSTANT state column -> it is an issuing/jurisdiction stamp (every row identical),
        so the address leads and the column is the fallback. Without this, a canonical
        adapter file whose `state` column is the constant jurisdiction stamps every row
        Resident - the reason NJ and NM needed a manual state->jurisdiction rename first.

    Between the address parse and the constant-column fallback sits a foreign-address check,
    so an unparseable overseas row becomes Nonresident instead of inheriting the stamp.

    The constant-column path deliberately still falls back to the column: single-jurisdiction
    street-only rosters (KY Manufacturer, AZ, DC) have a constant column that IS the true
    state, and must not regress to Unknown."""
    juris = (jurisdiction or "").upper().strip()
    n = len(df)
    types = df[type_col].astype(str).str.upper().tolist() if type_col else [""] * n
    raw_state = [str(v) for v in df[state_col].tolist()] if state_col else [""] * n
    col_state = [normalize_state(v) for v in raw_state] if state_col else [""] * n
    addrs = df[addr_col].tolist() if addr_col else [""] * n

    # the varies-or-not test the old docstring claimed but never performed
    distinct = {v for v in col_state if v}
    col_varies = len(distinct) > 1

    out, basis = [], []
    for i in range(n):
        lt = types[i]
        if "NON-RESIDENT" in lt or "NON RESIDENT" in lt or "NONRESIDENT" in lt:
            out.append("Nonresident"); basis.append("license type"); continue
        if "RESIDENT" in lt:
            out.append("Resident"); basis.append("license type"); continue

        ps, why = "", ""
        if col_varies and col_state[i]:
            ps, why = col_state[i], "facility state (column)"
        if not ps:
            ps = addr_state_strong(addrs[i])
            why = "facility state (address)" if ps else why
        if not ps and looks_foreign(addrs[i], raw_state[i]):
            out.append("Nonresident"); basis.append("foreign address"); continue
        if not ps and col_state[i]:
            ps, why = col_state[i], "facility state (column)"
        if not ps:
            ps = addr_state_weak(addrs[i])
            why = "facility state (address, loose)" if ps else why

        if ps and juris:
            out.append("Resident" if ps == juris else "Nonresident"); basis.append(why)
        else:
            out.append("Unknown"); basis.append("no physical state found")
    return out, basis

# ---- NPI via NPPES ---------------------------------------------------------
PHARMACY_HINT = re.compile(r"PHARMAC|DRUG (STORE|OUTLET)|APOTHECARY", re.I)
RETAIL_TAXONOMY = "3336C0003X"  # Community/Retail Pharmacy
NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"

def looks_pharmacy(type_val, name_val):
    return bool(PHARMACY_HINT.search(str(type_val)) or PHARMACY_HINT.search(str(name_val)))

def norm_name(n):
    n = re.sub(r"#\s*\S+", "", str(n))                 # drop store numbers ("#1234")
    n = re.sub(r"\b(L\.?L\.?C|INC|CORP|CO|LLP|LP|LTD)\b\.?", "", n, flags=re.I)
    n = re.sub(r"[^A-Za-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()

PHARM_TAXO_PREFIX = "3336"  # all pharmacy taxonomies (retail, institutional, mail-order, specialty, LTC, ...)

def city_norm(c):
    c = str(c).upper().strip().replace(".", "")
    c = re.sub(r"^ST\b", "SAINT", c); c = re.sub(r"^MT\b", "MOUNT", c)
    return re.sub(r"\s+", " ", c)

def _is_pharmacy(res):
    for t in res.get("taxonomies", []):
        if str(t.get("code","")).startswith(PHARM_TAXO_PREFIX) or "pharmacy" in str(t.get("desc","")).lower():
            return True
    return False

def _match(results, city, zipc):
    """Confident match: a pharmacy-taxonomy org whose LOCATION address agrees on ZIP5 (preferred)
    or normalized city. Returns (npi, basis) or ('', '')."""
    z5 = str(zipc)[:5]; cn = city_norm(city)
    for keyed_on in ("zip", "city"):
        for res in results:
            if not _is_pharmacy(res): continue
            for a in res.get("addresses", []):
                if a.get("address_purpose") != "LOCATION": continue
                if keyed_on == "zip" and z5 and str(a.get("postal_code",""))[:5] == z5:
                    return str(res.get("number","")), "zip5 + pharmacy taxonomy"
                if keyed_on == "city" and cn and city_norm(a.get("city","")) == cn:
                    return str(res.get("number","")), "city + pharmacy taxonomy"
    return "", ""

def nppes_lookup(session, name, city, state, zipc):
    """Return (npi, basis) with a confident match, else ('', reason). Never guesses.
    Two passes: exact-ish normalized name, then a broadened name (helps chains where the store
    number/suffix differs), both keyed on ZIP5 then normalized city, limit 200."""
    def query(orgname):
        params = {"version":"2.1","enumeration_type":"NPI-2","organization_name":orgname,"limit":"200"}
        if state: params["state"] = state
        try:
            return session.get(NPPES_URL, params=params, timeout=25).json().get("results") or []
        except Exception:
            return None
    nm = norm_name(name)
    if not nm: return "", "no usable name"
    res = query(nm + "*")
    if res is None: return "", "lookup error"
    npi, why = _match(res, city, zipc)
    if npi: return npi, why
    toks = nm.split()
    broad = " ".join(toks[:2]) if len(toks) >= 2 else (toks[0] if toks else "")
    if broad and broad != nm:
        res2 = query(broad + "*")
        if res2:
            npi, why = _match(res2, city, zipc)
            if npi: return npi, why + " (broadened name)"
    return "", "no confident NPPES match"

# ---- main ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--jurisdiction", default="", help="2-letter issuing-board state code, e.g. KY")
    ap.add_argument("--npi", dest="npi", action="store_true", help="attempt NPI enrichment (pharmacies only)")
    ap.add_argument("--no-npi", dest="npi", action="store_false")
    ap.add_argument("--sample", type=int, default=0, help="only process a random N-row sample and report match rate")
    ap.add_argument("--throttle", type=float, default=0.15, help="seconds between NPPES calls")
    ap.set_defaults(npi=False)
    a = ap.parse_args()

    session = None
    if a.npi:
        try:
            import requests
            session = requests.Session()
            session.headers.update({"User-Agent":"LHAI-Verified-enrichment/1.0"})
        except ImportError:
            print("ERROR: --npi needs the 'requests' package (pip install requests).", file=sys.stderr); sys.exit(2)

    for path in a.files:
        try:
            df = load_table(path)
        except FileNotFoundError:
            print(f"ERROR: file not found: {path}\n  -> check the exact name/quoting; on Windows wrap it in double quotes.", file=sys.stderr); continue
        except Exception as e:
            print(f"ERROR reading {path}: {type(e).__name__}: {e}\n  -> if this is a delimiter/format issue, open it once in Excel and re-save as CSV UTF-8.", file=sys.stderr); continue
        cols = list(df.columns)
        type_col  = pick(cols, TYPE_COLS)
        state_col = pick_state_col(df, cols)
        name_col  = pick(cols, NAME_COLS)
        city_col  = pick(cols, CITY_COLS)
        zip_col   = pick(cols, ZIP_COLS)
        addr_col  = pick(cols, ADDR_COLS)
        print(f"\n[{path}]  rows={len(df)}  name={name_col} type={type_col} state={state_col} addr={addr_col} city={city_col} zip={zip_col}")

        rn, basis = derive_resnon(df, type_col, state_col, addr_col, a.jurisdiction)
        df["resident_nonresident"] = rn
        df["resnon_basis"] = basis
        vc = pd.Series(rn).value_counts().to_dict()
        print("  resident/nonresident:", vc)
        tot = len(rn); res = vc.get("Resident",0); non = vc.get("Nonresident",0); unk = vc.get("Unknown",0)
        if tot and (res == 0 or non == 0):
            print("  [!] one-sided result — the state column was likely mis-detected; verify before using.")
        elif tot and unk/tot > 0.4:
            print(f"  [!] {unk/tot:.0%} Unknown — address/state parsing weak for this file; verify before using.")

        if a.npi:
            work = df.sample(min(a.sample, len(df)), random_state=1) if a.sample else df
            npi_vals, npi_basis = {}, {}
            attempted = matched = 0
            for idx, row in work.iterrows():
                if not looks_pharmacy(row.get(type_col,""), row.get(name_col,"")):
                    npi_vals[idx]=""; npi_basis[idx]="not a pharmacy (no NPI expected)"; continue
                attempted += 1
                qstate = normalize_state(row.get(state_col,"")) if state_col else ""
                if not qstate and addr_col: qstate = state_from_addr(row.get(addr_col,""))
                npi, why = nppes_lookup(session, row.get(name_col,""), row.get(city_col,""),
                                        qstate, row.get(zip_col,""))
                npi_vals[idx]=npi; npi_basis[idx]=why
                if npi: matched += 1
                time.sleep(a.throttle)
            df["NPI"] = df.index.map(lambda i: npi_vals.get(i,""))
            df["npi_basis"] = df.index.map(lambda i: npi_basis.get(i,"") if a.sample else npi_basis.get(i,""))
            rate = (matched/attempted*100) if attempted else 0
            print(f"  NPI: pharmacy rows attempted={attempted}  confident matches={matched}  match rate={rate:.1f}%"
                  + ("  (SAMPLE)" if a.sample else ""))
        else:
            df["NPI"] = ""
            df["npi_basis"] = "not attempted (--no-npi)"

        if not a.sample:
            out = path.rsplit(".",1)[0] + "_enriched.csv"
            df.to_csv(out, index=False)
            print(f"  wrote {out}")

if __name__ == "__main__":
    main()
