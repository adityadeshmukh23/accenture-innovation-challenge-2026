#!/usr/bin/env bash
# One-command AEGIS demo: serve, replay the seeded scenarios, leave the
# dashboard running. Invoked by `make demo`.
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"
PY=".venv/bin/python"
UVICORN=".venv/bin/uvicorn"
BASE="http://${HOST}:${PORT}"

bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; reset=$'\033[0m'

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port ${PORT} is already in use — stop that process or run: make demo PORT=8010"
  exit 1
fi

echo
echo "${bold}AEGIS${reset} — starting gateway on ${BASE}"
"$UVICORN" aegis.main:app --host "$HOST" --port "$PORT" --log-level warning &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then echo "gateway failed to start"; exit 1; fi
  sleep 0.25
done

"$PY" -m scenarios.runner --base-url "$BASE" --settle 5

"$PY" scripts/sync_docs.py --compare

echo
echo "  ${bold}verifying the audit ledger independently of the app…${reset}"
"$PY" -m aegis.tools.verify_ledger

cat <<EOF

  ${bold}${green}Dashboard: ${BASE}${reset}
  ${dim}Live decisions  — click any row for its reasoning trace and budget readout
  Metrics         — FP/FN, precision/recall per lane, latency, cost, calibration
  Human queue     — confirm or override a flag, then retrain and see the weights move
  Policy          — derived thresholds; move lambda and watch the cut points move${reset}

  ${dim}Ctrl-C to stop.${reset}

EOF

wait "$SERVER_PID"
