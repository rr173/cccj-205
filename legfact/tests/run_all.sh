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
python3 -I tests/generate_stand_fixtures.py
python3 -I tests/generate_backtest_fixtures.py

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

echo "== 9. stand reconciliation: handover warehouse + sidecar =="
python3 -I - "$WORK" <<'PY'
import os, sys
sys.path.insert(0, "src")
from legfact import Warehouse
work = sys.argv[1]
wh = Warehouse(os.path.join(work, "wh_stand"))
wh.ingest_batch(
    os.path.join("fixtures", "stand", "handover", "wh", "manifest_1.json"),
    os.path.join("fixtures", "stand", "handover", "wh", "batch_1"))
wh.close()
PY
SIDECAR="$WORK/stand_recon.sqlite"
python3 -I src/legfact/cli.py stand run \
  --warehouse "$WORK/wh_stand" --airport XYZ \
  --window-start "2026-03-10T09:00:00+08:00" \
  --window-end   "2026-03-10T11:00:00+08:00" \
  --receipt-batch 1 --cutoff "2026-03-10T12:00:00+08:00" \
  --roster fixtures/stand/handover/inputs/visits.ndjson \
  --ledger fixtures/stand/handover/inputs/ledger.ndjson \
  --map    fixtures/stand/handover/inputs/map.ndjson \
  --sensor fixtures/stand/handover/inputs/sensor.ndjson \
  --out "$SIDECAR" > "$WORK/stand_run.json"

echo "== 10. stand reconciliation: hand table + naive interval cross-check =="
python3 -I tests/check_stand_recon.py --sidecar "$SIDECAR"

echo "== 11. stand reconciliation: behavioural suite =="
python3 -I tests/test_stand_recon.py

echo "== 12. arrival backtest: hand-computable warehouse + run =="
python3 -I - "$WORK" <<'PY'
import os, sys
sys.path.insert(0, "src")
from legfact import Warehouse
work = sys.argv[1]
wh = Warehouse(os.path.join(work, "wh_backtest"))
for b in range(1, 6):
    wh.ingest_batch(
        os.path.join("fixtures", "backtest", "wh", f"manifest_{b}.json"),
        os.path.join("fixtures", "backtest", "wh", f"batch_{b}"))
wh.close()
PY
BTOUT="$WORK/backtest_out"
python3 -I src/legfact/cli.py backtest run \
  --warehouse "$WORK/wh_backtest" \
  --spec fixtures/backtest/inputs/spec.json \
  --roster fixtures/backtest/inputs/roster.ndjson \
  --visibility fixtures/backtest/inputs/visibility.ndjson \
  --out "$BTOUT" > "$WORK/backtest_run.json"

echo "== 13. arrival backtest: independent per-checkpoint oracle =="
python3 -I tests/check_backtest.py \
  --out-dir "$BTOUT" --warehouse "$WORK/wh_backtest" \
  --spec fixtures/backtest/inputs/spec.json \
  --roster fixtures/backtest/inputs/roster.ndjson \
  --visibility fixtures/backtest/inputs/visibility.ndjson

echo "== 14. arrival backtest: behavioural suite =="
python3 -I tests/test_backtest.py

if [ "${LANDING_SCALE:-0}" = "1" ]; then
  echo "== 15. landing-fee mart: 30k-root / 120k-message scale rehearsal =="
  python3 -I tests/scale_landing.py
else
  echo "== 15. landing scale rehearsal skipped (set LANDING_SCALE=1 to run) =="
fi

if [ "${STAND_SCALE:-0}" = "1" ]; then
  echo "== 16. stand reconciliation: 20k-visit / 200k-ledger / 1m-observation =="
  python3 -I tests/scale_stand_recon.py
else
  echo "== 16. stand scale rehearsal skipped (set STAND_SCALE=1 to run) =="
fi

if [ "${BACKTEST_SCALE:-0}" = "1" ]; then
  echo "== 17. arrival backtest: 60k-root / 72-batch / 400k-message rehearsal =="
  python3 -I tests/scale_backtest.py
else
  echo "== 17. backtest scale rehearsal skipped (set BACKTEST_SCALE=1 to run) =="
fi

echo "ALL GATES PASSED"
