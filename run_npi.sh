#!/bin/bash
# NPI (NPPES) enrichment for email-intake states.
#
#   ./run_npi.sh 20261006 WY SD MD GA      # named states
#   ./run_npi.sh 20261006 --all            # every registered source
#
# Replaces the one-off run_npi_<date>.sh scripts. Two things it does that those did
# not, both learned the hard way:
#
#   * It resolves each state's canonical CSV FROM THE REGISTRY rather than assuming
#     data/<ST>/_incoming/. SC lands in data/SC/sc-board-of-pharmacy/, and a
#     hardcoded path silently enriches nothing.
#   * It runs states SEQUENTIALLY. NPPES is a free public API; several parallel
#     streams would be impolite and invite rate-limiting. ~16k rows takes ~45 min,
#     so run it in the background and read the log.
#
# Enrichment is idempotent: it rewrites <name>_enriched.csv from the canonical CSV,
# so a re-run after a failure is safe.
set -u
REPO="$(cd "$(dirname "$0")" && pwd)"
PY="$REPO/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$REPO/.venv/bin/python"

DATE="${1:-}"
shift || true
if [ -z "$DATE" ] || [ $# -eq 0 ]; then
    echo "usage: $0 <YYYYMMDD> <STATE...|--all>" >&2
    exit 2
fi

if [ "$1" = "--all" ]; then
    STATES=$("$PY" -c "import sys; sys.path.insert(0,r'$REPO/adapters'); import email_sources as E; print(' '.join(sorted(E.ALL)))")
else
    STATES="$*"
fi

LOG="/c/Verified/data/_npi_${DATE}.log"
: > "$LOG"
echo "NPI run $DATE :: $STATES :: start $(date +%H:%M:%S)" | tee -a "$LOG"

for ST in $STATES; do
    # Ask the registry where this state's canonical CSV actually lives.
    F=$("$PY" - "$ST" "$DATE" "$REPO" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[3], "adapters"))
import email_sources as E
st, date = sys.argv[1], sys.argv[2]
sp = E.ALL.get(st)
if sp is None:
    sys.exit("  !! %s is not a registered source" % st)
print(os.path.join(sp.out_dir(r"C:\Verified\data"), sp.out_name(date)))
PYEOF
) || { echo "  !! could not resolve a path for $ST - skipped" | tee -a "$LOG"; continue; }

    if [ ! -f "$F" ]; then
        echo "  !! $ST: no canonical CSV at $F - skipped (run email_intake.py first)" | tee -a "$LOG"
        continue
    fi
    echo "================ $ST  $(date +%H:%M:%S)" >> "$LOG"
    "$PY" "$REPO/verified_enrich.py" "$F" --jurisdiction "$ST" --npi >> "$LOG" 2>&1
    echo "   exit=$? $(date +%H:%M:%S)" >> "$LOG"
done

echo "ALL DONE $(date +%H:%M:%S)" | tee -a "$LOG"
