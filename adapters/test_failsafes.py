"""Prove the fail-safe scaffolding actually fires. Asserting a guard exists is not
the same as watching it stop something."""
import json
import os
import warnings

warnings.filterwarnings("ignore")
import pandas as pd

import tier1_fetch as T

PASS, FAIL = "PASS", "**FAIL**"


def expect_raise(label, fn, needle):
    try:
        fn()
    except RuntimeError as e:
        ok = needle.lower() in str(e).lower()
        print(f"  [{PASS if ok else FAIL}] {label}")
        print(f"        raised: {str(e)[:150]}")
        return ok
    except Exception as e:
        print(f"  [{FAIL}] {label} — wrong exception type {type(e).__name__}: {e}")
        return False
    print(f"  [{FAIL}] {label} — DID NOT RAISE")
    return False


print("1. refuse-empty")
expect_raise("0 rows -> raise",
             lambda: T.failsafe(pd.DataFrame({"a": []}), "t", "zz", "x.csv"),
             "refusing to write an empty file")

print("\n2. absolute floor (the VI case — a percentage band is meaningless at n=150)")
expect_raise("below abs_floor -> raise",
             lambda: T.failsafe(pd.DataFrame({"a": range(40)}), "t", "zz", "x.csv",
                                abs_floor=100),
             "below the absolute floor")

print("\n3. de-dupe loss guard (the Colorado bug this batch actually hit)")
# Models Colorado exactly: licensenumber restarts from 0 inside each prefix, so the
# NUMBER alone has 10 distinct values while (type, number) has all 100. The two columns
# must vary INDEPENDENTLY or the fixture does not reproduce the bug.
bad = pd.DataFrame({
    "licensetype": [f"T{i // 10}" for i in range(100)],   # 10 prefixes
    "licensenumber": [str(i % 10) for i in range(100)],   # numbers restart per prefix
})
assert bad["licensenumber"].nunique() == 10
assert bad.drop_duplicates(subset=["licensetype", "licensenumber"]).shape[0] == 100
expect_raise("wrong key destroying 90% of rows -> raise",
             lambda: T.dedupe(bad, "t"),
             "key is wrong")

print("\n4. the SAME data with the correct key passes")
try:
    out = T.dedupe(bad, "t", key_cols=["licensetype", "licensenumber"])
    ok = len(out) == 100
    print(f"  [{PASS if ok else FAIL}] correct composite key keeps all 100 rows -> {len(out)}")
except Exception as e:
    print(f"  [{FAIL}] correct key raised unexpectedly: {e}")

print("\n5. missing expected key column -> raise (header drift)")
expect_raise("absent key column -> raise",
             lambda: T.dedupe(bad, "t", key_cols=["licensetype", "NOPE"]),
             "absent")

print("\n6. no-silent-truncation: HTTP non-200 fails loudly")
expect_raise("404 -> raise",
             lambda: T.fetch("https://data.colorado.gov/resource/7s5z-vewr.json",
                             {"$select": "count(*)", "$where": "bogus_col = 1"}, src="t"),
             "http")

print("\n7. guardrail: a URL that redirects to a login/payment/CAPTCHA stops the run")
expect_raise("login-looking final URL -> raise",
             lambda: T.fetch("https://httpbin.org/redirect-to?url=https://example.com/login",
                             src="t"),
             "stopping per guardrail")

print("\n8. count-regression warning (not a raise — a loud warning)")
cf = "zz_last_count.json"
json.dump({"rows": 1000, "at": "2026-01-01"}, open(cf, "w"))
print("     with a previous baseline of 1000, now passing 500 (50% drop):")
T.failsafe(pd.DataFrame({"a": range(500)}), "t", "zz", "x.csv")
os.remove(cf)

print("\n9. paging validation is a hard check, not a log line")
print("     (exercised live in the CO run: 'paging validated: fetched 56,775 == API count')")
print("     forcing a mismatch by shrinking the expected count:")
_real = T.co_count
try:
    T.co_count = lambda where: 999999
    expect_raise("fetched != API count -> raise",
                 lambda: T.fetch_co(".", "dscsa"),
                 "refusing to write a partial file")
finally:
    T.co_count = _real
