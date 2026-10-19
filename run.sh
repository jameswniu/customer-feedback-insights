#!/usr/bin/env bash
# Feedback Insights launcher: venv, deps, API on :8000, log window, CLI help.
# Usage: ./run.sh          start the API and open a log window
#        ./run.sh stop     stop the API this script started
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PID_FILE=".run.pid"
LOG_FILE="${LOG_FILE:-logs/app.log}"
VENV_PY=".venv/bin/python"

alive() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

if [[ "${1:-start}" == "stop" ]]; then
  if alive; then
    kill "$(cat "$PID_FILE")" && echo "Stopped the API (pid $(cat "$PID_FILE"))."
  else
    echo "No running API found."
  fi
  rm -f "$PID_FILE"
  exit 0
fi

if alive; then
  echo "The API is already running (pid $(cat "$PID_FILE")) on http://127.0.0.1:$PORT."
  echo "Stop it first with: ./run.sh stop"
  exit 0
fi
if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Something already answers on port $PORT. Pick another port: PORT=8001 ./run.sh" >&2
  exit 1
fi

if [[ ! -x "$VENV_PY" ]]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi
echo "Checking dependencies ..."
"$VENV_PY" -m pip install -q --disable-pip-version-check -r requirements.txt

mkdir -p data logs debug "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

echo "Starting the API on http://127.0.0.1:$PORT ..."
nohup "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >> logs/uvicorn.out 2>&1 &
echo $! > "$PID_FILE"

ready=""
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ready=1; break; fi
  alive || break
  sleep 0.5
done
if [[ -z "$ready" ]]; then
  echo "The API did not start. Last lines of logs/uvicorn.out:" >&2
  tail -n 20 logs/uvicorn.out >&2 || true
  alive && kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 1
fi
echo "API is up: $(curl -fsS "http://127.0.0.1:$PORT/health")"

# Second Terminal window tailing the app log (macOS only, never blocks on a permission prompt).
open_log_window() {
  osascript -e "tell application \"Terminal\"
    activate
    do script \"cd '$PWD' && tail -n 50 -f '$LOG_FILE'\"
  end tell" >/dev/null 2>&1 &
  local pid=$!
  for _ in $(seq 1 16); do
    kill -0 "$pid" 2>/dev/null || { wait "$pid"; return $?; }
    sleep 0.5
  done
  kill "$pid" 2>/dev/null || true
  return 1
}
if [[ "${OPEN_LOG_WINDOW:-1}" != "0" ]] && command -v osascript >/dev/null 2>&1; then
  open_log_window && echo "Opened a Terminal window tailing $LOG_FILE" \
    || echo "Could not open a log window. Run in another terminal: tail -f $LOG_FILE"
else
  echo "Watch the logs in a second terminal: tail -f $LOG_FILE"
fi

echo
echo "---- CLI ----"
"$VENV_PY" -m app.cli --help || echo "(app.cli is not available yet)"
echo
if [[ "$PORT" != "8000" ]]; then echo "Point the CLI at this port first: export API_URL=http://127.0.0.1:$PORT"; fi
cat <<EOF
Quick start (another terminal, same folder):
  source .venv/bin/activate
  python -m app.cli ingest data/sample_feedback.json
  python -m app.cli analyze
  python -m app.cli insights --team product
Stop the API: ./run.sh stop
EOF
