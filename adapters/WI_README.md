# WI DSPS — recon artifacts and why the adapter has never run

**Status: BLOCKED.** `wi_adapter.py` is written and committed but **has never completed a
single end-to-end run**, so its JSON field mapping is unverified. Do not treat any WI
output as trustworthy until it has.

## What happened

Wisconsin migrated off `licensesearch.wi.gov` (DNS record gone) to `license.wi.gov`, a
Salesforce Experience Cloud site. Recon established the real contract:

```
DSPS_LicensesLookupController.searchLicense
  searchType : "Profession"
  dataObj    : {"selectedCategory":"Health","selectedProfession":"<credentialId>"}
  recapToken : ""      # wired but INERT - kept empty, never populated
```

`recapToken` is present in the request shape but the server does not enforce it. It is
left empty deliberately. **Never populate it** — that would be forging a CAPTCHA token.

On 2026-09-28 the environment was blocked by Cloudflare after roughly a dozen automated
sessions — my own over-probing, not a trap. The block persisted through 2026-09-30 and
broadened from the Aura endpoint to plain page GETs. `wi_block_evidence.html` and
`wi_search_response_blocked.html` are two distinct captures of the resulting
"Sorry, you have been blocked / You are unable to access license.wi.gov" page.

## Rules of engagement (these are why there is no WI data)

Any `403` stops work for the day. **No retry, no user-agent rotation, no stealth
patching, no proxy, no residential IP.** Those would evade a protection the operator
deliberately put in place. The block is a reason to stop, not an obstacle to route
around. The open options are: wait for it to decay, run from an unblocked network, or
fall back to a records request — never evasion.

## The artifacts

| file | what it is |
|---|---|
| `wi_adapter.py` | the adapter. Written, committed, **never run end-to-end** |
| `wi_search_probe.py` | minimal replay of the captured contract; ~5 calls, 12s apart, stops on any 403 |
| `wi_recon.py`, `wi_recon3.py`, `wi_recon4.py` | the recon progression that found the contract |
| `wi_recon4.json`, `wi_recon5.json`, `wi_recon6.json` | captured Aura request/response pairs (`{requests, responses}`) |
| `wi_license_types.json` | all 254 credential types — Health 84, Trades 72, Business 70, Unarmed Combat Sports 23, Manufactured Homes 5 |
| `wi_scope.json` | the 10 in-scope Health establishment credentials with their Salesforce ids |
| `wi_block_evidence.html` | Cloudflare block page |
| `wi_search_response_blocked.html` | a second block capture. **Was named `.json`** — it is HTML, and a `json.load()` against it fails. Renamed 2026-10-08 |

`.log` and `.png` run output from these probes is gitignored and deliberately not tracked.

## Before anyone runs this again

The adapter's `build_mapping()` is unit-tested against camelCase and `__c` response
shapes, but no real payload has ever reached it. The enum-drift guard passes (10/10
scoped credentials present in the baseline) because it runs before any network call —
that is not evidence the adapter works. Treat the first successful run as recon, check
the field mapping and per-credential counts against the portal by eye, and only then
believe the numbers.
