#!/usr/bin/env bash
# Deterministic end-to-end verification.
# Regenerates synthetic fixtures in place and runs every test gate using
# isolated temporary warehouses (nothing under fixtures/ is mutated later).
set -euo pipefail
cd "$(dirname "$0")/.."

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== 1. regenerate synthetic fixtures =="
python3 -I tests/generate_fixtures.py
python3 -I tests/generate_landing_fixtures.py

echo "== 2. build main-suite warehouse =="
python3 -I - "$WORK" <<'PY'
import os, sys
sys.path.insert(0, "src")
from legfact import Warehouse
work = sys.argv[1]
wh = Warehouse(os.path.join(work, "wh_main"))
for b in (1, 2, 3):
    wh.ingest_batch(
        os.path.join("fixtures", "main", f"manifest_{b}.json"),
        os.path.join("fixtures", "main", f"batch_{b}"))
wh.close()
PY

echo "== 3. independent hand-written expectation checker =="
python3 -I tests/check_walkthrough.py "$WORK/wh_main"

echo "== 4. behavioural suite (edges, atomicity, copy/reopen, ordering, memory) =="
python3 -I tests/test_warehouse.py

echo "== 5. cancellation semantics suite =="
python3 -I tests/test_cancellation.py

echo "== 6. identity graph scale (deep chain / wide fork, bounded memory) =="
python3 -I tests/test_identity_scale.py

echo "== 7. landing-fee mart: independent walkthrough/edge oracle =="
python3 -I tests/check_landing_mart.py "$WORK/landing_check"

echo "== 8. landing-fee mart: rejection / atomicity / idempotency / ledger =="
python3 -I tests/test_landing_mart.py

if [ "${LANDING_SCALE:-0}" = "1" ]; then
  echo "== 9. landing-fee mart: 30k-root / 120k-message scale rehearsal =="
  python3 -I tests/scale_landing.py
else
  echo "== 9. scale rehearsal skipped (set LANDING_SCALE=1 to run) =="
fi

echo "ALL GATES PASSED"
