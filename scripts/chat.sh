#!/usr/bin/env bash
# The chat window. run.sh opens it on macOS, and anywhere else you run it in a second terminal.
# It makes sure there is something to ask about. An empty API at a loopback address (127.0.0.1 or localhost) gets the
# sample feedback, and when no row is analyzed yet the analysis runs, so a first run that stopped half way is picked up
# again next time. A URL on another host never gets the sample. A tunnel that forwards a loopback port to another machine
# looks local to this script, so LOAD_SAMPLE=0 is how to say no there.
# Usage: ./scripts/chat.sh          PORT=8001 ./scripts/chat.sh    for an API on another port
set -u
cd "$(dirname "$0")/.."

[[ -t 1 ]] && printf '\033]0;Feedback Insights chat\007'   # names the window in terminals that show titles

PORT="${PORT:-8000}"
export API_URL="${API_URL:-http://127.0.0.1:$PORT}"
PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  echo "Run ./run.sh first, it creates .venv." >&2
  exit 1
fi

# Prints one number read from the API. When the call or the parse fails it prints nothing and fails,
# so a probe that went wrong is never mistaken for a count of zero.
# Args are the path and a Python expression over d, the decoded JSON.
api_number() {
  curl -fsS "$API_URL$1" 2>/dev/null | "$PY" -c "import json, sys; d = json.load(sys.stdin); print($2)" 2>/dev/null
}

# Loads the sample into an empty loopback API and runs the analysis when no row is analyzed. Readiness counts analyzed
# rows, not themes, so an analysis that legitimately found no themes is not run again.
# If any probe fails it does nothing at all, and the chat that follows reports the real problem.
prepare() {
  local loaded analyzed this_machine=""
  loaded="$(api_number /health 'd["feedback_count"]')" && analyzed="$(api_number '/feedback?limit=1000' 'sum(1 for f in d if f.get("analyzed_at"))')" || {
    echo "Could not read what the API holds, so nothing was loaded or analyzed first." >&2
    return 0
  }
  case "$API_URL" in http://127.0.0.1:*|http://localhost:*) this_machine=1 ;; esac
  case "${LOAD_SAMPLE:-1}" in 0|false|no) this_machine="" ;; esac
  if [[ "$loaded" == "0" && -n "$this_machine" && -f data/sample_feedback.json ]]; then
    echo "Nothing is loaded yet, so the sample feedback is loaded first."
    "$PY" -m app.cli ingest data/sample_feedback.json || return 0
    loaded="$(api_number /health 'd["feedback_count"]')" || return 0
  fi
  if [[ "$loaded" != "0" && "$analyzed" == "0" ]]; then
    echo "Nothing is analyzed yet, so the analysis runs first."
    "$PY" -m app.cli analyze
    echo
  fi
}

if ! curl -fsS "$API_URL/health" >/dev/null 2>&1; then
  echo "Cannot reach the API at $API_URL. Start it with ./run.sh" >&2
  exit 1
fi
prepare
"$PY" -m app.cli chat
