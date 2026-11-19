#!/usr/bin/env bash
# Run Locust at several concurrency levels against an already running stack and write CSVs to data/loadtest/<label>/.
#   scripts/run_loadtest.sh <label> "<users list>" [duration]      e.g.  scripts/run_loadtest.sh w1 "1 5 10 20 40" 60s
set -euo pipefail
cd "$(dirname "$0")/.."
LABEL=${1:?label}; USERS=${2:-"1 5 10 20"}; DUR=${3:-60s}
HOST=${LOAD_HOST:-http://localhost:8000}
LOCUST=${LOCUST:-locust}
mkdir -p data/loadtest/$LABEL
for u in $USERS; do
  echo "=== $LABEL: $u users for $DUR"
  $LOCUST -f loadtest/locustfile.py --headless -u $u -r $u -t $DUR --host $HOST \
      --csv data/loadtest/$LABEL/u$u --only-summary > data/loadtest/$LABEL/u$u.log 2>&1 || true
  python3 scripts/summarise_load.py data/loadtest/$LABEL/u${u}_stats.csv $u
done
