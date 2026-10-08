#!/usr/bin/env python3
"""Registry of board deliveries received BY EMAIL - the authoritative source.

WHY THIS EXISTS
  Portal crawling was the original acquisition path and it has now been measured
  as unreliable: the MD board's own list contains 1,371 licences our completed,
  partitioned crawl never returned, with a clean temporal boundary (nothing issued
  before 2010 is missing, roughly half of every year after is). Email deliveries
  come from the regulator, carry a named sender and a date, and have matched the
  portal on every field we have been able to cross-check. Email is therefore the
  system of record; the portal is at best corroboration.

WHAT A SourceSpec IS
  One board delivery = one spec. It names the email it came from, the workbooks
  attached to it, how each workbook's sheets map to licence types, the header
  aliases that sheet uses, and the row counts measured at intake. The engine in
  email_intake.py is generic; everything state-specific lives here, so adding a
  jurisdiction is a data change, not a new script.

ON expected_rows
  These are a TRIPWIRE, not a filter. Next month's file will legitimately differ.
  A deviation is reported loudly and a collapse below half is fatal, but a count
  that merely changed is never silently accepted or silently corrected.
"""

# license_type comes from a column in the sheet rather than from the sheet name.
TYPE_FROM_COLUMN = "__type_from_column__"

# The canonical output schema. Every source emits all of these; a field the source
# does not carry is emitted empty rather than omitted, so one reader handles every
# jurisdiction and a missing field is visibly missing instead of absent.
CANONICAL = [
    "facility_name", "dba", "license_number", "license_type", "license_status",
    "business_activity", "issue_date", "expiration_date",
    "address_line1", "address_line2", "address_city", "address_county",
    "address_state", "address_zip", "phone", "fax",
    "credential_type_prefix", "attention_line",
    "jurisdiction", "__source_sheet", "__source_file", "__source_email", "__retrieved",
]
# dba / phone / fax added 2026-10-06. Only SD publishes them today (DBA 40.9%, phone
# 99.9%, fax 71.7% of its 2,700 rows) and they were being DROPPED - the one real
# advantage the superseded per-type files had. Empty for the boards that do not supply
# them, exactly as address_county is empty outside MD: one schema, a missing field
# visibly missing rather than absent.


class Workbook(object):
    def __init__(self, filename, sheets, expected_rows=None, type_column=None):
        self.filename = filename
        self.sheets = sheets                      # {sheet name: license_type} or TYPE_FROM_COLUMN
        self.expected_rows = expected_rows or {}  # {sheet name: int}
        self.type_column = type_column


class SourceSpec(object):
    def __init__(self, state, agency, scope, workbooks=None, aliases=None, email=None,
                 known_status=None, documented_near_dups=None,
                 documented_state_literals=None, known_types=None,
                 allow_blank_license=False, allow_blank_name=False,
                 output_subdir=None, acquisition=None, notes=""):
        self.state = state
        self.agency = agency
        self.scope = scope                        # "Company-Only" | "Complete"
        # Exactly one of email / acquisition. `email` = the board sent it and we have a
        # named sender, subject and receipt date. `acquisition` = the file reached us some
        # other way (a manual drop) and those fields DO NOT EXIST - they are left absent
        # rather than guessed, so a reader can tell "not recorded" from "recorded as X".
        assert (email is None) != (acquisition is None),             "%s: give exactly one of email= or acquisition=" % state
        self.email = email
        self.acquisition = acquisition
        # Observed license_type values. Drift is FLAGGED and the row is kept verbatim -
        # a new type is news, not an error, and must never be silently dropped or
        # mapped onto the nearest known value.
        self.known_types = set(known_types or ())
        # Some boards publish rows with no credential number or no business name. They
        # are real records and are KEPT and COUNTED; dropping them would quietly shrink
        # the roster. Opt-in per source so a blank stays an error everywhere else.
        self.allow_blank_license = allow_blank_license
        self.allow_blank_name = allow_blank_name
        # Where the canonical CSV lands, relative to data/<ST>/. None = _incoming.
        self.output_subdir = output_subdir
        assert workbooks and aliases, "%s: workbooks and aliases are required" % state
        self.workbooks = workbooks
        self.aliases = aliases                    # {canonical: [accepted header, ...]}
        self.known_status = known_status          # set, or None when the source has no status column
        self.documented_near_dups = documented_near_dups or set()
        # Literal address_state values this board publishes that are neither a US
        # state nor a code the enricher recognises as foreign. Kept verbatim at
        # intake; listed here so the verifier stays a TRIPWIRE - a documented value
        # passes, a NEW unrecognised one still fails loudly. A permanently-red
        # check gets ignored, which is how a real one would slip through.
        self.documented_state_literals = {s.upper() for s in (documented_state_literals or ())}
        self.notes = notes

    @property
    def key(self):
        return self.state

    def out_name(self, datestamp):
        return "%s - %s - %s - %s.csv" % (self.state, self.agency, self.scope, datestamp)

    def source_string(self):
        if self.email:
            e = self.email
            return ("%s - received by email %s from %s <%s>, subject %r"
                    % (e["authority"], e["received"], e["sender_name"], e["sender"], e["subject"]))
        a = self.acquisition
        return ("%s - %s, acquired %s, delivered to %s (sender and received date NOT "
                "RECORDED - pending chain-of-custody confirmation)"
                % (a["authority"], a["acquired_via"], a["acquired_date"], a["delivery_email"]))

    @property
    def retrieved(self):
        return (self.email or self.acquisition).get(
            "received", (self.acquisition or {}).get("acquired_date", ""))

    def out_dir(self, data_root):
        import os
        return os.path.join(data_root, self.state, self.output_subdir or "_incoming")


# --------------------------------------------------------------------------- WY
WY = SourceSpec(
    state="WY", agency="wy-board-of-pharmacy", scope="Company-Only",
    email={
        "authority": "WY State Board of Pharmacy",
        "sender": "bop@wyo.gov", "sender_name": "Heather Hansen",
        "subject": "Wyoming Board of Pharmacy Sept 2026 List Request",
        "received": "2026-09-29",
    },
    workbooks=[
        Workbook("WY_Pharmacies (Res, Ins, Tel, NR) Sept 2026.xlsx",
                 sheets={"Resident": "Pharmacy - Resident",
                         "Nonresident": "Pharmacy - Nonresident",
                         "Institutional": "Pharmacy - Institutional",
                         "Telepharmacy": "Pharmacy - Telepharmacy"},
                 expected_rows={"Nonresident": 1158, "Resident": 131,
                                "Institutional": 38, "Telepharmacy": 4}),
        Workbook("WY_WD & 3PL Sept 2026.xlsx",
                 sheets={"Human Use": "Wholesale Distributor - Human Use",
                         "Vet Use": "Wholesale Distributor - Veterinary Use",
                         "Med O2": "Wholesale Distributor - Medical Oxygen",
                         "3PL": "Third-Party Logistics Provider"},
                 expected_rows={"Human Use": 614, "Vet Use": 76,
                                "Med O2": 103, "3PL": 183}),
    ],
    aliases={
        "license_number": ["license number"], "facility_name": ["business name"],
        "license_status": ["status"], "issue_date": ["issue date"],
        "expiration_date": ["expiration date"], "address_line1": ["street 1"],
        "address_line2": ["street 2"], "address_city": ["city"],
        "address_state": ["state"], "address_zip": ["zip"],
        "business_activity": ["business activity"],
    },
    known_status={"Active", "Pending Inspection", "Pending Renewal", "Cancelled"},
    # Six 3PL licences the board lists twice on consecutive rows: same number, name,
    # status and dates, second copy with a blank address (3PL0029 differs only as
    # "Row Dr" vs "Row Drive"). Not byte-identical, so whole-row de-dupe leaves them,
    # and collapsing them would mean choosing which source row to discard.
    documented_near_dups={"3PL0010", "3PL0029", "3PL0068", "3PL0135", "3PL0185", "3PL0227"},
    notes="business_activity keeps the board's spelling, including 'Wholesale Distributer'.",
)

# --------------------------------------------------------------------------- GA
GA = SourceSpec(
    state="GA", agency="ga-board-of-pharmacy", scope="Company-Only",
    email={
        "authority": "GA State Board of Pharmacy",
        "sender": "sandra.mason@dch.ga.gov", "sender_name": "Sandra Mason",
        "subject": "Georgia Board of Pharmacy",
        "received": "2026-09-29",
    },
    workbooks=[
        Workbook("GA_PHARMACY FACILITIES - September 2026.xlsx",
                 sheets=TYPE_FROM_COLUMN, expected_rows={"Sheet1": 7373}),
    ],
    aliases={
        "license_number": ["license number"], "facility_name": ["facility name"],
        "license_type": ["profession name (license type)", "license type"],
        "license_status": ["license status"], "issue_date": ["issue date"],
        "expiration_date": ["expiration date"], "address_line1": ["address"],
        "address_city": ["city"], "address_state": ["state"],
        "address_zip": ["zip code"],
    },
    known_status={"Active", "Lapsed-Late Renewal Period",
                  "Active - Renewal Pending", "Probation"},
    notes=("Facilities only. The same email carried PHARMACIST and PHARMACY TECHNICIAN "
           "workbooks (19,224 + 27,691 individuals), out of scope and deliberately not "
           "registered here. Zip Code is numeric in the source, so 832 rows arrived with "
           "their leading zero already stripped."),
)

# --------------------------------------------------------------------------- MD
# Replaces the portal crawl for these two types. The crawl returned 9,899 rows but
# omitted 1,371 licences the board itself lists, so its output is not a census.
MD = SourceSpec(
    state="MD", agency="md-board-of-pharmacy", scope="Company-Only",
    email={
        "authority": "MD Board of Pharmacy",
        "sender": "dhmh.mdbop@maryland.gov", "sender_name": "Jacqueline J. Green",
        "subject": "LighthouseAI Roster Request",
        "received": "2026-09-29",
    },
    workbooks=[
        Workbook("MD_all_pharmacies.xlsx", sheets={"Sheet1": "Pharmacy"},
                 expected_rows={"Sheet1": 2215}),
        Workbook("MD_all_distributors.xlsx", sheets={"Sheet1": "Distributor"},
                 expected_rows={"Sheet1": 1667}),
    ],
    aliases={
        "license_number": ["license no"], "facility_name": ["name"],
        "issue_date": ["issue date"], "expiration_date": ["expiration date"],
        "address_line1": ["address1"], "address_line2": ["address2"],
        "address_city": ["city"], "address_county": ["county"],
        "address_state": ["state"], "address_zip": ["zipcode"],
    },
    # The board's lists carry NO status column. They appear to be active-only - the
    # portal held ~6,700 licences absent from them - but the board has not said so,
    # so license_status is left EMPTY rather than assumed "Active".
    known_status=None,
    notes=("Headers start on row 3. No status column: left blank, never inferred. "
           "Pharmacists and technicians (13,116 + 11,715) are individuals, out of scope."),
)

# --------------------------------------------------------------------------- SD
# Three sheets in one workbook: FT/PT resident pharmacies (100-/200-series),
# Nonresident pharmacies (400-series), and Wholesale/Other Distributors/503B
# (600-series). Banner reads "Active and in Good Standing" but there is NO
# per-row status column, so license_status is left blank, never inferred (the MD
# pattern). Source ZIPs carry a leading backtick text-marker and the FT/PT sheet
# ZIP header reads "1"; both are kept verbatim at intake and cleaned downstream.
# Email provenance confirmed from the delivery message (verified@ shared mailbox).
SD = SourceSpec(
    state="SD", agency="sd-board-of-pharmacy", scope="Company-Only",
    email={
        "authority": "SD Board of Pharmacy",
        "sender": "Beth.Windschitl@state.sd.us", "sender_name": "Windschitl, Beth",
        "subject": ("Pharma Solutions USA dba LighthouseAI / SD BOP Data List Request "
                    "Fulfillment (SD FT/PT Pharmacies, Nonresident & Wholesale ) 09/30/2026"),
        "received": "2026-10-05",
    },
    workbooks=[
        Workbook("SD_Data List 9.30.26.xlsx",
                 sheets={"FT PT SD Pharmacies": "Pharmacy - Resident",
                         "Nonresident": "Pharmacy - Nonresident",
                         "Wholesale": "Wholesale / Distributor / 503B"},
                 expected_rows={"FT PT SD Pharmacies": 292,
                                "Nonresident": 1056, "Wholesale": 1352}),
    ],
    aliases={
        "license_number": ["license #"], "facility_name": ["business"],
        "business_activity": ["type of practice"],
        "issue_date": ["issued"], "expiration_date": ["expiration"],
        "address_line1": ["address 1"], "address_line2": ["address 2"],
        "address_city": ["city"], "address_state": ["state"],
        "address_zip": ["zip", "1"],
        "dba": ["dba"], "phone": ["phone"], "fax": ["fax"],
    },
    known_status=None,
    # Source spells it "Outside USA" (mixed case); the adapter upper-cases every
    # address_state. Verified present in the Wholesale sheet, column "State", on
    # exactly these three Canadian wholesalers - the same three that appear as
    # ON/QC in WY and as blank in GA.
    documented_state_literals=("OUTSIDE USA",),
    notes=("Headers on row 6. No status column: left blank, never inferred. "
           "ZIPs carry a leading backtick text-marker, kept verbatim at intake. "
           "FT/PT ZIP header is '1'. DBA/Phone/Fax entered the canonical schema 2026-10-06; 5 DBA cells hold the literal 'N/A' and are kept VERBATIM (the superseded per-type files blanked them, which is interpretation, not transcription). "
           "3 Wholesale rows carry address_state 'OUTSIDE USA' (Canadian wholesalers: Jubilant "
           "DraxImage, AX Pharmaceutical, BWXT Medical) - faithfully kept. As of 2026-10-06 "
           "verified_enrich recognises the literal via FOREIGN_STATE_LITERALS, so these 3 "
           "rows enrich to Nonresident / basis 'foreign address' and SD has 0 Unknown "
           "- verified after the change, not assumed. "
           "Delivered to the verified@lighthouseai.com shared mailbox (State License Data "
           "Requests folder); Internet-Message-Id "
           "<SA9PR09MB528084D6EA991E2F73443E5CB4962@SA9PR09MB5280.namprd09.prod.outlook.com>. "
           "Sender Beth Windschitl, Senior Secretary. Board states active-license-holders "
           "only, snapshot at time of download, reliable but not guaranteed; payment check "
           "#3493 dated 09/29/26. The 9.30.26 in the filename is the board's snapshot date; "
           "the delivery email was received 2026-10-05."),
)

# --------------------------------------------------------------------------- OR
# Oregon Board of Pharmacy facility license rosters, purchased via the board's
# List Order Form ($80/category x 4). FOUR category workbooks, one sheet each; the
# sheet is always "Active <category>" so license_status is uniformly "Active".
# License Type is a per-row column kept VERBATIM (TYPE_FROM_COLUMN) - Retail/
# Institutional/Home Dialysis/Charitable/Remote Dispensing drug outlets, Manufacturer,
# Wholesaler (Prescription/Class III/Nonprescription), Drug Distribution Agent. Lists
# include out-of-state (non-resident) registrants, so address_state spans ~50 states.
# Zips arrive as TEXT already zero-padded; dates are real datetimes.
OR = SourceSpec(
    state="OR", agency="or-board-of-pharmacy", scope="Company-Only",
    email={
        "authority": "OR Board of Pharmacy",
        "sender": "PHARMACY.PUBLICRECORDS@bop.oregon.gov",
        "sender_name": "PHARMACY PUBLIC RECORDS * BOP",
        "subject": "2026.10.06 Lists Request - LighthouseAI",
        "received": "2026-10-06",
    },
    workbooks=[
        Workbook("2026.10.05 List Request LighthouseAI (Pharmacies).xlsx",
                 sheets=TYPE_FROM_COLUMN,
                 expected_rows={"Active Pharmacies with License ": 1697}),
        Workbook("2026.10.05 List Request LighthouseAI (Manufacturers).xlsx",
                 sheets=TYPE_FROM_COLUMN,
                 expected_rows={"Active Manufacturers with Licen": 1515}),
        Workbook("2026.10.05 List Request LighthouseAI (Wholesalers).xlsx",
                 sheets=TYPE_FROM_COLUMN,
                 expected_rows={"Active Wholesalers with License": 891}),
        Workbook("2026.10.05 List Request LighthouseAI (Drug Distribution Agents).xlsx",
                 sheets=TYPE_FROM_COLUMN,
                 expected_rows={"Active Drug Distribution Agent ": 505}),
    ],
    aliases={
        "license_number": ["license number"], "license_type": ["license type"],
        "license_status": ["license status"], "facility_name": ["sort name"],
        "issue_date": ["issue date"], "expiration_date": ["expiration date"],
        "address_line1": ["address line 1"], "address_line2": ["address line 2"],
        "address_city": ["city"], "address_state": ["state"], "address_zip": ["zip"],
    },
    known_status={"Active"},
    notes=("Four category workbooks (one sheet each), purchased via the OBOP List Order "
           "Form at $80/category (wholesalers, manufacturers, drug distribution agents, "
           "retail drug outlets). license_type kept verbatim from the column. Lists "
           "include out-of-state (non-resident) registrants, so address_state spans ~50 "
           "states and the enricher treats non-OR rows as nonresident. Zips arrive as text, "
           "already zero-padded (no reconstruction expected); a few issue_date / zip cells "
           "are blank in-source and kept empty. Delivered to the verified@lighthouseai.com "
           "shared mailbox (State License Data Requests). Files board-named 2026.10.05; the "
           "delivery email ('2026.10.06 Lists Request - LighthouseAI') was received 2026-10-06."),
)


# --------------------------------------------------------------------------- SC
# A MANUAL FILE DROP, not an email delivery - acquisition= rather than email=, so the
# sender and received date are absent rather than invented. Mark to supply them for
# chain of custody. Structurally this follows OR: single board, TYPE_FROM_COLUMN.
SC = SourceSpec(
    state="SC", agency="sc-board-of-pharmacy", scope="Company-Only",
    output_subdir="sc-board-of-pharmacy",
    acquisition={
        "authority": "SC Board of Pharmacy",
        "acquired_via": "manual file drop by Mark",
        "acquired_date": "2026-10-06",
        "delivery_email": "verified@lighthouseai.com",
        "sender": None,        # NOT RECORDED - do not guess
        "received": None,      # NOT RECORDED - do not guess
    },
    workbooks=[
        Workbook("SC_Pharmacies All 10.6.2026.xlsx",
                 sheets=TYPE_FROM_COLUMN,
                 expected_rows={"ContactsByBoard": 19123}),
    ],
    aliases={
        "license_number": ["credential number"],
        "license_type": ["license description"],
        "license_status": ["status"],
        "facility_name": ["business name"],
        "issue_date": ["issuance date"], "expiration_date": ["expiration date"],
        "address_line1": ["address1"], "address_line2": ["address2"],
        "address_city": ["city"], "address_state": ["state code"],
        "address_zip": ["zipcode"], "phone": ["business phone"],
        "credential_type_prefix": ["credential type prefix"],
        # "company" is NOT an alternate business name. Of the 449 rows where it is
        # populated, 436 differ from Business Name and carry an attention / care-of
        # line: "ATTN: MEDICAL", "PHARMACY DEPT", "c/o SHIPPER'S WAREHOUSE". Using it
        # as a facility_name fallback would inject those into name matching - and it
        # would rescue nothing: all 10 blank-Business-Name rows have company empty.
        "attention_line": ["company"],
    },
    known_status={
        "ACTIVE", "ACTIVE IN RENEWAL", "APPROVED", "CANCELED", "CEASE AND DESIST",
        "CLOSED", "DENIED", "INACTIVE", "LAPSED", "PENDING", "PERMANENTLY REVOKED",
        "RELINQUISHED", "REVOKED", "SUSPENDED", "VOLUNTARY SURRENDERED", "WITHDRAWN",
    },
    known_types={
        "Non-Resident Wholesale/Distributor", "Pharmacy", "Non-Dispensing Drug Outlet",
        "Non-Resident Pharmacy", "Non-Resident Medical Gas/DME",
        "EMS Non-dispensing Drug Outlet", "Medical Gas/Legend Device",
        "Non-Resident Third Party Logistics Provider",
        "Non Resident Manufacturer/Repackager", "Non-Resident Virtual Manufacturer",
        "Wholesale/Distributor", "Non-Resident Outsourcing Facility",
        "Non-resident Non-Dispensing Pharmacy", "Manufacturer/Repackager",
        "Non-Resident Virtual Wholesale", "Narcotic Treatment Program",
        "Health System Non-Dispensing Permit", "Outsourcing Facility",
        "Third Party Logistics Provider",
        "Non-Resident Central Fill Pharmacy Permit Application",
        "In-State Central Fill Pharmacy", "Narcotic Treatment Program Satellite",
    },
    # 4 rows have no Credential Number and 10 no Business Name. Real records; dropping
    # them would quietly shrink the roster, so they are kept and counted.
    allow_blank_license=True, allow_blank_name=True,
    # "NA" is not a place - SC uses it for two foreign rows (Cencora Global Procurement
    # in Cork, Ireland; Jubilant DraxImage in Kirkland, Quebec). Too ambiguous to make a
    # global foreign rule, so it is documented HERE and residency is decided from the
    # address. "PQ" needed no entry: it is pre-1991 Quebec and was added to the
    # enricher's FOREIGN_CODES, where it belongs.
    documented_state_literals=("NA",),
    notes=("Single sheet 'ContactsByBoard'; Board column constant 'PHARMACY'. "
           "license_type is per-row from 'License Description', NOT collapsed or "
           "canonicalised - that is downstream. Status is preserved RAW and intake does "
           "NOT filter on it: all 19,123 rows land, disciplinary tail included. "
           "'Credential Type Prefix' (e.g. PY) is preserved separately and never folded "
           "into license_number."),
)


ALL = {s.state: s for s in (WY, GA, MD, SD, OR, SC)}
