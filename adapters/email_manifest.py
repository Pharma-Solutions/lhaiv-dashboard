#!/usr/bin/env python3
"""Chain of custody for email-sourced rosters.

Email is the authoritative source, which is only meaningful if any landed row can
be traced back to a named sender, a date, and a file whose bytes are pinned. This
builds that index from the provenance sidecars email_intake.py writes, and - the
part that matters - RE-HASHES every referenced file so a source workbook that was
edited, re-saved or replaced after intake is caught rather than assumed intact.

A board re-sending a corrected list under the same filename is the realistic case.
Without a stored hash that silently becomes "the data changed and nobody noticed".

Usage:
  python email_manifest.py            # build + verify, write data\\_email_manifest.json
  python email_manifest.py --check    # verify only, non-zero exit on drift
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA = r"C:\Verified\data"
INDEX = os.path.join(DATA, "_email_manifest.json")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect():
    out = []
    for p in sorted(glob.glob(os.path.join(DATA, "*", "*", "*.provenance.json"))):
        try:
            out.append((p, json.load(open(p, encoding="utf-8"))))
        except Exception as e:
            print("  !! unreadable provenance %s: %s" % (p, e))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify only; do not rewrite the index")
    a = ap.parse_args()

    entries, drift = [], 0
    print("=" * 76)
    print("EMAIL CHAIN OF CUSTODY")
    print("=" * 76)
    for path, prov in collect():
        d = os.path.dirname(path)
        e = prov.get("email")
        acq = prov.get("acquisition")
        print()
        print("%s  %s   %s rows" % (prov["state"], prov["agency"], format(prov["rows"], ",")))
        if e:
            print("   from   %s <%s>" % (e["sender_name"], e["sender"]))
            print("   subject %r   received %s" % (e["subject"], e["received"]))
        elif acq:
            print("   via    %s   acquired %s" % (acq["acquired_via"], acq["acquired_date"]))
            print("   mailbox %s   sender/received NOT RECORDED - pending confirmation"
                  % acq["delivery_email"])
        else:
            print("   !! no email or acquisition block in this provenance file")

        files = []
        for f in prov["source_files"]:
            # Source workbooks always live in data/<ST>/_incoming, even when the
            # canonical output lands in an agency folder beside it (SC).
            fp = os.path.join(DATA, prov["state"], "_incoming", f["file"])
            if not os.path.exists(fp):
                print("   !! MISSING source workbook: %s" % f["file"])
                drift += 1
                state = "missing"
                now = None
            else:
                now = sha256(fp)
                state = "ok" if now == f["sha256"] else "CHANGED"
                if state != "ok":
                    drift += 1
            print("   %-9s %-52s %s" % (state, f["file"][:52], (now or "")[:16]))
            files.append(dict(f, verified=state, current_sha256=now))

        op = os.path.join(d, prov["output"])
        if os.path.exists(op):
            now = sha256(op)
            ostate = "ok" if now == prov.get("output_sha256") else "CHANGED"
        else:
            ostate, now = "missing", None
        if ostate != "ok":
            drift += 1
        print("   %-9s %-52s %s  (output)" % (ostate, prov["output"][:52], (now or "")[:16]))

        entries.append({
            "state": prov["state"], "agency": prov["agency"], "scope": prov["scope"],
            "email": e, "acquisition": acq, "rows": prov["rows"],
            "distinct_license_numbers": prov.get("distinct_license_numbers"),
            "by_type": prov.get("by_type", {}), "stats": prov.get("stats", {}),
            "source_files": files,
            "output": prov["output"], "output_verified": ostate,
            "output_sha256": prov.get("output_sha256"), "notes": prov.get("notes", ""),
            "built": prov.get("built"),
        })

    print()
    print("=" * 76)
    print("%d delivery/deliveries   %s rows total   integrity drift: %d"
          % (len(entries), format(sum(x["rows"] for x in entries), ","), drift))
    print("=" * 76)

    if not a.check:
        json.dump({"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "source_of_record": "email from the issuing board",
                   "deliveries": entries}, open(INDEX, "w", encoding="utf-8"), indent=1)
        print("index -> %s" % INDEX)
    sys.exit(1 if drift else 0)


if __name__ == "__main__":
    main()
