#!/usr/bin/env python3
"""Write the CONFIRMED pressure-test verdicts into the worklist's yellow columns.

Every verdict below carries an explicit provenance in Confirmed-by:
  live-probe   = I fetched the page/file/API myself in this session and read the signals
  file-opened  = I downloaded the actual file and inspected its rows/columns
  adapter      = a working adapter already pulls this source (strongest evidence)
  tracker      = the team already holds the data / already identified the channel
  prior-note   = a human's earlier hands-on note (weighted, but not re-confirmed by me)
  UNVERIFIED   = could not confirm; flagged for manual review

Tier 1 bulk download / open-data / API      -> automate
Tier 2 purchase or records/roster request   -> human purchasing gate
Tier 3 enumerable no-CAPTCHA portal         -> scrape (one adapter per platform family)
Tier 4 CAPTCHA or single-record only        -> Hermai candidate ONLY if no Tier-2 fallback
"""
import warnings

warnings.filterwarnings("ignore")
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill

WL = "LHAIV_Source_PressureTest_Worklist.xlsx"
TODAY = "2026-08-27"
ME = "Claude Code recon"

# (State, Agency-prefix) -> dict(dl, tier, mech, platform, by, notes)
V = {}


def add(state, agency, dl, tier, mech, platform, by, notes):
    V[(state, agency)] = dict(dl=dl, tier=tier, mech=mech, platform=platform,
                              by=by, notes=notes)


# ---------------------------------------------------------------- E + F (probed)
add("Alabama", "Alabama Board of Pharmacy",
    "Maybe (scrape)", "3",
    "Enumerable portal, but query-expensive: license type x state grid",
    "custom (albop.com)", f"{ME}: live-probe (no CAPTCHA) + prior-note",
    "No CAPTCHA found. Prior hands-on note: search is gated by state, ~23 license types x 52 "
    "states = ~1,196 query combinations, no bulk export. Scrapeable but the highest query cost "
    "in the set. WORKLIST URL IS THE HOMEPAGE - locate the actual search endpoint before building.")

add("California", "Food and Drug Branch",
    "N without Hermai (single-record)", "4",
    "Single 'verify a business' lookup only; no export",
    "Azure web app", f"{ME}: live-probe + prior-note",
    "No CAPTCHA, but no enumerable axis and no export found. Prior note confirms a second CDPH "
    "portal behaves the same. FALLBACK NOT YET TESTED: CA Public Records Act request to CDPH FDB - "
    "try that before treating as Hermai.")

add("New Mexico", "New Mexico Board of Pharmacy",
    "Purchase", "2",
    "Paid bulk license-list request (~$1,500 all types + statuses)",
    "Salesforce (my.site.com)", f"{ME}: live-probe (HTTP 200) + prior-note",
    "Confirmed reachable: nmrldlpi.my.site.com/bcd/s/license-list-request (Salesforce form, "
    "reCAPTCHA protects the form itself). Route to the purchasing gate - do NOT scrape.")

add("South Dakota", "Board of Pharmacy",
    "Purchase/Records (portal is CAPTCHA-gated)", "4 -> 2 fallback",
    "Portal CAPTCHA-gated; manual monthly roster form is the real channel",
    "igovSolution", f"{ME}: live-probe (reCAPTCHA sitekey confirmed) + prior-note",
    "CONFIRMED real Google reCAPTCHA v2 on the igovSolution lookup (btnAudioCaptcha + "
    "recaptcha/api.js). Prior note: bulk download not available; the runbook records SD as igov "
    "yet served by a manual monthly form. NOT a clean Hermai candidate - the records/roster form "
    "is the fallback. Confirms platform != method.")

add("West Virginia", "West Virginia Board of Pharmacy",
    "N without Hermai (CAPTCHA)", "4",
    "reCAPTCHA on verify tool; $10 certified copy is single-record only",
    "custom ASP (wvbop.com)", f"{ME}: live-probe (3 reCAPTCHA widgets confirmed)",
    "CONFIRMED reCAPTCHA. The $10 fee buys ONE certified license copy, not a bulk file - so it is "
    "not a Tier-2 channel. FALLBACK NOT YET TESTED: WV FOIA request to the Board. "
    "TRUE HERMAI CANDIDATE if FOIA yields nothing.")

add("Wisconsin", "Pharmacy Examining Board",
    "NEEDS MANUAL REVIEW", "?",
    "Recorded URL is dead (DNS NXDOMAIN)",
    "unknown (DSPS migrated to Salesforce 'LicensE')",
    f"{ME}: live-probe - dspslicenselist.wi.gov does not resolve",
    "COULD NOT CLASSIFY. dspslicenselist.wi.gov fails DNS resolution entirely (not a timeout). "
    "dsps.wi.gov is up and license.wi.gov/s/ is a Salesforce Experience site, but I found no public "
    "credential-list page from the pharmacist page. Prior note said 'list view available by "
    "credential type, browsable and scrapable' - that URL is gone. A human needs to find the "
    "current DSPS list endpoint.")

add("American Samoa", "Health Services Regulatory Board",
    "Records request (phone/fax)", "2",
    "No online presence at all; phone/fax request only",
    "none", f"{ME}: no URL exists + prior-note",
    "No lookup tool anywhere; not listed in national directories. Phone 684-633-1222 / fax "
    "684-633-1869. Prior note: the territory has ONE pharmacy, inside its only hospital - so the "
    "population is trivially small. NOT a Hermai candidate (nothing to scrape).")

add("California", "Controlled Chemical Substance Program",
    "Records request", "2",
    "No portal; records request to CA DOJ Bureau of Investigation",
    "none", f"{ME}: no URL exists + prior-note",
    "Run by CA DOJ Bureau of Investigation, not DCA. No permittee search, no roster, no bulk "
    "mechanism. Contact CCSP@doj.ca.gov / (916) 210-4313. NOT a Hermai candidate - there is no "
    "portal to scrape. Route to records request.")

add("Guam", "Guam Board of Examiners for Pharmacy",
    "Y (scrape)", "3",
    "Paginated directory: type=All, status=All, 100 rows/page, ~13 pages",
    "custom (guamhplo directory)", f"{ME}: live-probe - form shape confirmed",
    "CONFIRMED Tier 3 and easy. No CAPTCHA. 'type' dropdown HAS an 'All' option (plus LOCAL "
    "PHARMACY / LOCAL WHOLESALE / NON RESIDENT PHARMACY / NON RESIDENT WHOLESALE), 'license_status' "
    "has 'All', 'show_records' goes to 100/page, A-Z sort, and every text input is optional. "
    "~1,214 licensees = ~13 page fetches. NOTE: worklist URL corrected to the directory "
    "(web.guamhplo.org/directory/gbep); the guamhplo.org/gbep landing page has no data.")

add("Hawaii", "Narcotics Enforcement Division",
    "Records request (fee, case-by-case)", "2",
    "UIPA records request; portal is individual verification only",
    "custom (law.hawaii.gov)", f"{ME}: live-probe + prior-note",
    "No CAPTCHA but no enumerable axis - individual registration verification only. The fee-based "
    "option is a generic UIPA request form charged hourly, case-by-case, with no guaranteed bulk "
    "deliverable. Prior note flags a record-request PDF worth pursuing. Tier 2, low confidence in "
    "the deliverable - confirm scope with the agency before paying.")

add("Iowa", "Iowa Board of Pharmacy",
    "Records request (bulk list reported available)", "2",
    "Open-records request via NextRequest; portal itself is CAPTCHA-gated",
    "igovSolution", f"{ME}: live-probe (both the reCAPTCHA and the NextRequest portal)",
    "CONFIRMED reCAPTCHA on the igovSolution lookup, AND confirmed the Tier-2 fallback is live: "
    "iowaopenrecords.nextrequest.com (HTTP 200), ~10 business day response. Prior note: bulk list "
    "available but file contents unknown. NOT a Hermai candidate - request the file and inspect "
    "its fields; if sufficient, Iowa drops off the Hermai list entirely.")

add("Kansas", "Kansas Board of Pharmacy",
    "Purchase", "2",
    "Open-records roster purchase (~$45)",
    "Tyler/eLicense", f"{ME}: live-probe (HTTP 200 on the open-records page)",
    "The eLicense portal is a search form only (txtLPRNum / txtName / txtCity + a 53-option State "
    "dropdown) - the 46 'table rows' my collector saw are legacy ASP.NET LAYOUT tables, not data. "
    "Real channel confirmed reachable: pharmacy.ks.gov/forms-faqs/open-records, ~$45. Cheap - route "
    "to the purchasing gate.")

add("Louisiana", "Board of Drug and Device Distributors",
    "Y (scrape)", "3",
    "Enumerable lookup: state='--All--', all text inputs optional, no CAPTCHA",
    "custom PHP (lms.drugboard.la.gov)", f"{ME}: live-probe - form shape confirmed in the iframe",
    "CONFIRMED Tier 3; the prior note ('bulk list available, scrapable, recommend reconsider') is "
    "vindicated. IMPORTANT: the real form is inside an IFRAME at "
    "lms.drugboard.la.gov/public/lookup.php - the outer wholesaler-license-lookup page exposes no "
    "form, which is why earlier passes found nothing. 'state' dropdown offers '--All--' (69 opts) "
    "and license_number / company_name / company_dba / tpl / address / city are all optional.")

add("Louisiana", "Bureau of Sanitarian Services",
    "Out of scope (recommend removing row)", "OUT-OF-SCOPE",
    "Agency issues no license in scope",
    "n/a", f"{ME}: live-probe + prior-note",
    "No license lookup or bulk tool - only a permit-renewal payment portal and a general records "
    "system. Prior note concludes this bureau issues no license of its own; its requirements point "
    "to the Board of Pharmacy and the Board of Wholesale Drug Distributors, both already in scope. "
    "RECOMMEND dropping this row (human decision) rather than tiering it.")

add("Maryland", "Office of Controlled Substances Administration",
    "N without Hermai (single-record)", "4",
    "CDS search by number or registrant name only; no export",
    "custom ASP.NET", f"{ME}: live-probe (landing wrapper) + prior-note",
    "No CAPTCHA detected, but no enumerable axis either. The page I reached exposes only a language "
    "selector and a site-search box, so the real CDS form was not assessed directly - the "
    "single-record finding rests on the prior hands-on note. Prior note also judged scraping "
    "'more cumbersome than it is worth'. FALLBACK NOT YET TESTED: MD Public Information Act "
    "request. Probable Hermai candidate pending that PIA check.")

add("Mississippi", "Mississippi Board of Pharmacy",
    "N without Hermai (CAPTCHA)", "4",
    "reCAPTCHA-gated verification; single-record only",
    "custom ASP.NET (gateway.mbp.ms.gov)",
    f"{ME}: live-probe (reCAPTCHA sitekey confirmed)",
    "CONFIRMED reCAPTCHA v2 with a live sitekey. Worth noting the panel ships as "
    "<div id='divCAPTCHA' style='display:none'> - it is revealed CONDITIONALLY, so a casual visit "
    "may not show it while automated volume would. FALLBACK NOT YET TESTED: MS Public Records Act. "
    "TRUE HERMAI CANDIDATE if the PRA yields nothing.")

add("Missouri", "Bureau of Narcotics and Dangerous Drugs",
    "Y (scrape)", "3",
    "Registrant search, no CAPTCHA, per-page selector offers 'All'",
    "MyLicense-family ASP.NET", f"{ME}: live-probe - form shape confirmed",
    "CONFIRMED Tier 3 and unusually favourable: no CAPTCHA, both name fields optional, and the "
    "cboPerPage dropdown includes an 'All' option - a single request may return the entire "
    "registrant population rather than needing pagination.")

add("New Jersey", "Drug and Medical Device Registration",
    "Maybe (scrape)", "3",
    "Registration search with 4 dropdown axes, no CAPTCHA",
    "MyLicense-family ASP.NET", f"{ME}: live-probe (no CAPTCHA, 4 enumerable selects)",
    "NJ DEPT OF HEALTH - a DIFFERENT agency from the Consumer Affairs portal below; do not conflate "
    "them. No CAPTCHA and four dropdowns with >=4 options give an enumerable axis. The prior note "
    "on this row pointed at newjersey.mylicense.com, which belongs to the Drug Control Unit row, "
    "not here. Tier 3 probable - confirm the result grid renders without a name.")

add("New Jersey", "New Jersey Drug Control Unit",
    "Y (bulk download portal)", "1 (Tier-3 floor)",
    "MyLicense Verification_Bulk: 'All' profession + license type, no CAPTCHA",
    "MyLicense (bulk module)", f"{ME}: live-probe - form shape + option lists confirmed",
    "BEST NEW FIND. newjersey.mylicense.com/Verification_Bulk/ advertises 'select the type of "
    "license you wish to search for and download' with BUSINESS DOWNLOAD SEARCH "
    "(Search.aspx?facility=Y) and PERSON DOWNLOAD SEARCH (facility=N). NO CAPTCHA, no login, no "
    "fee. profession_name = 54 options INCLUDING 'All'; license_type_name = 88 (business) / 289 "
    "(person) options including 'All'; name / license_no / city are all OPTIONAL. Relevant types "
    "present: 'CDS Out of State Pharmacy', 'CDS ADS Branch', 'CDS Automated Dispensing Sys'. "
    "I did NOT submit the form (hard rule), so whether the output is a FILE (Tier 1) or an HTML "
    "grid (Tier 3) is the one open question - one human click settles it. Either way NOT Hermai.")

add("New York", "Board of Pharmacy",
    "N without Hermai (single-record)", "4",
    "NYSED verification search: single record only, no export",
    "custom (eservices.nysed.gov)", f"{ME}: live-probe + prior-note",
    "No CAPTCHA, but no enumerable dropdown axis and no export control found. NOTE the contrast "
    "with NY Bureau of Narcotic Enforcement (same state), which publishes a full 3,785-row xlsx - "
    "so NY is only partly blocked. FALLBACK NOT YET TESTED: NY FOIL request to NYSED Office of the "
    "Professions. Probable Hermai candidate pending FOIL.")

add("North Carolina", "Food & Drug Drug Program",
    "N without Hermai (CAPTCHA)", "4",
    "reCAPTCHA present; JS-heavy single-record lookup",
    "custom (apps.ncagr.gov AgRSysPortal)",
    f"{ME}: live-probe (reCAPTCHA script confirmed) + prior-note",
    "reCAPTCHA infrastructure confirmed on the page. Prior note: JS-heavy, single-record lookup, no "
    "bulk export. FALLBACK NOT YET TESTED: NC Public Records Act request to NCDA&CS. Probable "
    "Hermai candidate pending that request.")

add("North Carolina", "North Carolina Drug Control Unit",
    "Records request", "2",
    "No portal of any kind; records request only",
    "none", f"{ME}: no URL exists + prior-note",
    "No lookup tool or portal at all - registration and renewal forms only. Contact "
    "1-800-662-7030 / NCCSAREG@dhhs.nc.gov. NOT a Hermai candidate: there is nothing to scrape. "
    "Route to a records request.")

add("Oklahoma", "Bureau of Narcotics and Dangerous Drugs Control",
    "Y (scrape)", "3",
    "Thentia register: iterate 20 registration types x 3 statuses, no CAPTCHA",
    "Thentia", f"{ME}: live-probe - form shape confirmed",
    "CONFIRMED Tier 3. No CAPTCHA. registrationType = 20 options (Analytical Laboratory, "
    "Business: Hospital and Pharmacy, Clinical Detoxification, ...), profession = 11, status = 3 "
    "(Active/Inactive), and 'keywords' is optional. No 'All' option, so the adapter iterates the "
    "20 registration types - exactly the vt_adapter loop-the-dropdown shape.")

add("Oregon", "Pharmaceutical Representative Licensing",
    "NEEDS MANUAL REVIEW", "?",
    "Probe's Tier-1 file was a blank template; real lookup not yet assessed",
    "NAIC external lookup / DFR", f"{ME}: live-probe + file-opened (Tier 1 REFUTED)",
    "The probe called this Tier 1 on a .xlsx link. I DOWNLOADED IT: "
    "DCBS-Disclosure-Log.xlsx is a BLANK REPORTING TEMPLATE - the 'Disclosure Log' sheet has 0 "
    "rows and 27 columns of HCP-interaction fields that reps fill in themselves. It is NOT a "
    "licensee roster. Tier 1 conclusively refuted. The actual licensee path appears to run through "
    "external-lookup-web.prod.naic.org (NPN-based) plus a DFR check-license page, neither of which "
    "I assessed. Needs a human pass.")

add("Pennsylvania", "Drugs, Devices and Cosmetics Program",
    "Purchase (Tier 3 also possible)", "2",
    "Paid roster via DOS list-requests; DDC lookup itself is DBA-name search",
    "custom ASP (apps.health.pa.gov)", f"{ME}: live-probe + prior-note",
    "The DDC lookup takes one optional 'PublicDBA' text input, no CAPTCHA, and renders tables - so "
    "a Tier-3 scrape may be viable. CAVEAT I want flagged: the paid-roster link in the prior note "
    "(pa.gov/agencies/dos/.../list-requests) belongs to the Dept of STATE, whereas DDC sits under "
    "the Dept of HEALTH - those may not be the same population. Confirm the DOS list actually "
    "covers DDC registrants before purchasing.")

add("Pennsylvania", "Pennsylvania State Board of Pharmacy",
    "Purchase (login required)", "2",
    "PALS 'List Sales': create account, log in, purchase",
    "PA PALS", f"{ME}: live-probe + prior-note",
    "PALS List Sales requires account creation and login before purchase - both hard-gated actions "
    "I will not perform. Human purchasing gate. Path per prior note: pals.pa.gov -> Online Sales "
    "-> 'List Sales' under Other Services.")

add("Puerto Rico", "Assistant Secretary for the Regulation of Public Health",
    "NEEDS MANUAL REVIEW", "?",
    "SPA rendered no inspectable form; registry described as internal",
    "custom SPA (orcps.salud.pr.gov)", f"{ME}: live-probe - could not assess",
    "COULD NOT CLASSIFY. The portal is a single-page app; no selects, tables or text inputs were "
    "exposed to a read-only render, so I cannot honestly call it Tier 3 or Tier 4. Prior note: "
    "individual verification / renewal / certificate validation only, and the site states the "
    "registry of all issued licenses is maintained INTERNALLY - which points to a records request. "
    "Needs a human with the page open.")

add("South Carolina", "Bureau of Drug Control",
    "N without Hermai (CAPTCHA + 25-result cap)", "4",
    "reCAPTCHA; results capped at 25; 'Print Listing' only, no export",
    "custom (apps.dhec.sc.gov)", f"{ME}: live-probe (reCAPTCHA widget confirmed)",
    "CONFIRMED reCAPTCHA widget. Results cap at 25 per search with a 'refine your search' prompt, "
    "and the only output control is 'Print Listing' - no CSV/Excel. The cap plus the CAPTCHA is "
    "what makes this genuinely Tier 4 rather than a slow Tier 3. FALLBACK NOT YET TESTED: SC FOIA. "
    "TRUE HERMAI CANDIDATE if FOIA yields nothing.")

add("Texas", "Drug Manufacturers & Distributors",
    "N without Hermai (single-record)", "4",
    "Single-record search; open-records is case-by-case with no set format",
    "custom (dshs.texas.gov)", f"{ME}: live-probe (CMS wrapper only) + prior-note",
    "The URL I probed is a DSHS CMS wrapper (only ENG/ES language selects) - the real search "
    "was not reached, so the single-record finding rests on the prior hands-on note (search by "
    "owner name, license number, city or county; no export). FALLBACK: TX Public Information Act, "
    "which the prior note describes as case-by-case with no set fee or format. Tier 4 with a weak "
    "Tier-2 fallback - confirm the real search endpoint before deciding.")

add("Utah", "Controlled Substance Precursor",
    "Purchase", "2",
    "Paid bulk data request (~$539)",
    "Cloudflare-fronted (dopl.utah.gov)",
    f"{ME}: live-probe - 403 Cloudflare challenge; purchase portal down at probe time",
    "CORRECTS THE PROBE, which said 'Tier 4 CAPTCHA'. dopl.utah.gov/csp/ returns HTTP 403 with a "
    "Cloudflare interstitial ('Just a moment...', _cf_chl_opt, server: cloudflare) - that is a "
    "whole-site EDGE challenge, not a per-search CAPTCHA on a lookup form. Different meaning, "
    "different remedy. Prior note records a bulk download for ~$539 via "
    "secure.utah.gov/datarequest/professionals/requestExemption.html - which redirected to "
    "secure.utah.gov/maintenance/tempdown.html when I checked, so the channel is DOCUMENTED but "
    "NOT live-confirmed. RECHECK the purchase portal. Not a Hermai candidate.")

add("Vermont", "Vermont Board of Pharmacy",
    "Y (bulk roster download)", "1",
    "Pega roster download: loop 33 profession types, stack + dedupe",
    "Pega", f"{ME}: adapter - vt_adapter.py already pulls this source",
    "PROVEN TIER 1 - the strongest evidence in the whole set, and the probe got it WRONG (it said "
    "Tier 2). vt_adapter.py drives "
    "secure.professionals.vermont.gov/prweb/... , discovers 33 profession types at runtime and "
    "produced vt_licenses.csv = 22,602 rows x 23 columns. The probe failed because the worklist URL "
    "was sos.vermont.gov/pharmacy/ - the agency HOMEPAGE, not the data portal. "
    "ACTION: update the source URL to the prweb portal.")

# ------------------------------------------------------- live-verified A/D rows
add("Colorado", "Board of Pharmacy",
    "Y (open-data API)", "1",
    "Socrata API - no key needed, filter licensetype",
    "Socrata", f"{ME}: live-probe of the API + tracker (data in hand)",
    "RE-CONFIRMED LIVE. data.colorado.gov dataset 7s5z-vewr: count(*) = 1,604,837 rows, 24 columns, "
    "JSON API answers without an app token. Fields include licensetype / licensenumber / "
    "licensestatusdescription / expiration, so filter licensetype to the pharmacy codes. Best "
    "source class in the set - a real API, not a file.")

add("New York", "Department of Health, Bureau of Narcotic Enf",
    "Y (direct file)", "1",
    "Direct .xlsx: licensed_entities.xlsx",
    "static file", f"{ME}: file-opened - 3,785 rows verified",
    "RE-CONFIRMED LIVE by downloading it: licensed_entities.xlsx = 3,785 rows x 8 cols, header "
    "reads 'ALL LICENSES VALID AS OF 8/7/2026', columns PREFIX/CLASS, LICENSE #, EXPIRATION DATE, "
    "LICENSEE NAME, CITY, STATE, OWNER/OPERATOR. Genuine full-population file at a stable URL - "
    "trivial adapter.")

add("Virgin Islands", "Virgin Islands Board of Pharmacy",
    "Y (direct file)", "1",
    "Direct .xlsx roster posted on the board page",
    "static file", f"{ME}: file-opened - 150 rows verified",
    "RE-CONFIRMED LIVE by downloading it: PHARMACY-LIST-10312025-fnl.xlsx = 150 rows x 7 cols "
    "(Last/First/Middle, Credential, License Type, License #, Expiration). CAVEAT: this file is "
    "PHARMACISTS ONLY (RPh) - it does not appear to include facilities/wholesalers, so confirm "
    "whether the facility population is covered elsewhere. URL is date-stamped (.../2025/11/...), "
    "so an adapter must re-discover the link rather than hard-code it.")

# ------------------------------------------------------------------- defaults
BUCKET_DEFAULT = {
    "A": dict(dl="Y (bulk file)", tier="1",
              mech="Free bulk file download - already obtained",
              by=f"tracker (data in hand); URL re-checked live by {ME}",
              notes="Bucket A: the team already holds this file, which is stronger evidence than "
                    "any live heuristic. URL confirmed reachable in this pass (no dead links "
                    "across all 45 A-D rows). NOTE: the probe scored most of these Tier 2 - that "
                    "is the classifier matching 'public records' boilerplate in site footers, "
                    "not a real downgrade."),
    "B": dict(dl="Records/roster request", tier="2",
              mech="Records or roster request channel already identified",
              by=f"tracker (channel identified); URL re-checked live by {ME}",
              notes="Bucket B: request channel already identified by the team - route to the "
                    "records/purchasing gate. Not a Hermai candidate."),
    "C": dict(dl="Purchased", tier="2",
              mech="Paid list already purchased and received",
              by=f"tracker (data in hand); URL re-checked live by {ME}",
              notes="Bucket C: paid list already in hand. Recurring cost is the only open item."),
}


def main():
    df = pd.read_excel(WL, sheet_name="Source Pressure-Test", header=5, dtype=str).fillna("")
    wb = load_workbook(WL)
    ws = wb["Source Pressure-Test"]
    hdr = {ws.cell(row=6, column=c).value: c for c in range(1, ws.max_column + 1)}
    yellow = PatternFill("solid", fgColor="FFF2CC")

    def lookup(state, agency):
        for (s, a), v in V.items():
            if state.strip() == s and agency.strip().startswith(a):
                return v
        return None

    stats, unmatched = {}, []
    for i, r in df.iterrows():
        excel_row = 7 + i
        st, ag, bucket = r["State"].strip(), r["Agency"].strip(), r["Bucket"].strip()
        v = lookup(st, ag)
        if v is None:
            b = bucket[0].upper()
            d = BUCKET_DEFAULT.get(b)
            if d is None:
                # bucket D: File Download but a fee attached -> purchase, else free roster
                prior = r["Prior finding (from tracker)"]
                if "published roster" in bucket.lower():
                    d = dict(dl="Y (published roster)", tier="1",
                             mech="Free published roster (PDF) - already obtained",
                             by=f"tracker (data in hand); URL re-checked live by {ME}",
                             notes="Bucket D: free published roster already obtained. PDF parsing "
                                   "is the only work; no fee, no gate.")
                elif "fee=" in prior:
                    d = dict(dl="Purchase", tier="2",
                             mech="Bulk file exists but is priced - purchase required",
                             by=f"tracker (channel + fee identified); URL re-checked live by {ME}",
                             notes="Bucket D reclassified to TIER 2, not Tier 1: the bulk file "
                                   "channel is identified but carries a fee, so it goes through "
                                   "the purchasing gate rather than an adapter. All six bucket-D "
                                   "'File Download' rows carry fees.")
                else:
                    d = dict(dl="Y (bulk file)", tier="1",
                             mech="Free bulk file channel identified, not yet pulled",
                             by=f"tracker (channel identified); URL re-checked live by {ME}",
                             notes="Bucket D: free bulk channel identified but not yet pulled - "
                                   "build the adapter.")
                    unmatched.append(f"{st} | {ag}")
            v = dict(platform="", **d)

        vals = {"Downloadable?": v["dl"], "Tier": v["tier"], "Access mechanism": v["mech"],
                "Platform": v.get("platform", ""), "Confirmed-by": v["by"],
                "Date": TODAY, "Notes": v["notes"]}
        for k, val in vals.items():
            if k in hdr:
                c = ws.cell(row=excel_row, column=hdr[k], value=val)
                c.fill = yellow
                if k == "Tier":
                    c.font = Font(bold=True)
        stats[v["tier"]] = stats.get(v["tier"], 0) + 1

    wb.save(WL)
    print(f"wrote verdicts into {WL}")
    print("\nFinal tier tally:")
    for k in sorted(stats):
        print(f"  Tier {k:<14} {stats[k]}")
    print(f"\nexplicit verdicts authored: {len(V)}   rows defaulted by bucket: {76 - len(V)}")
    if unmatched:
        print("free-bucket-D rows using generic default:", unmatched)


if __name__ == "__main__":
    main()
