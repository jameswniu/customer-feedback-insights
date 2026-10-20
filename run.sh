#!/usr/bin/env bash
# Feedback Insights launcher: venv, deps, API on :8000, a chat window and a log window.
# Usage: ./run.sh          start the API and, on macOS, open the chat and log windows
#        ./run.sh stop     stop the API this script started
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PID_FILE=".run.pid"
LOG_FILE="${LOG_FILE:-logs/app.log}"
VENV_PY=".venv/bin/python"
CHAT_HINT="./scripts/chat.sh"
[[ "$PORT" == "8000" ]] || CHAT_HINT="PORT=$PORT ./scripts/chat.sh"

# The pid file counts only while that pid is still the API, because after a reboot a stale file can name an unrelated process.
alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid cmd
  pid="$(cat "$PID_FILE")"
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(ps -o command= -p "$pid" 2>/dev/null)" || return 1
  [[ "$cmd" == *"uvicorn app.main:app"* ]]
}
is_off() { [[ "$1" == "0" || "$1" == "false" || "$1" == "no" ]]; }

if [[ "${1:-start}" == "stop" ]]; then
  if alive; then
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    if kill -0 "$pid" 2>/dev/null; then
      echo "The API (pid $pid) is still finishing a request. Run ./run.sh stop again in a moment." >&2
      exit 1
    fi
    echo "Stopped the API (pid $pid)."
  else
    echo "No running API found."
  fi
  rm -f "$PID_FILE"
  exit 0
fi

if alive; then
  echo "The API is already running (pid $(cat "$PID_FILE")) on http://127.0.0.1:$PORT."
  echo "Chat with it in a terminal: $CHAT_HINT"
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
health="$(curl -fsS "http://127.0.0.1:$PORT/health" || echo '{}')"
mode="$("$VENV_PY" -c 'import json, sys; print(json.loads(sys.argv[1]).get("mode", "?"))' "$health" 2>/dev/null || echo "?")"
case "$mode" in
  llm) how="an API key is set, so Claude is tried first and the rules cover any failure" ;;
  offline) how="rule-based, no API key needed" ;;
  *) how="mode $mode" ;;
esac
echo "API is on at http://127.0.0.1:$PORT ($how)"

# A chat window and a log window, macOS Terminal only. One osascript call opens both and it never blocks on a
# permission prompt. The shell commands travel as arguments, so paths with spaces or quotes need no escaping here.
open_windows() {   # args are the log command and the chat command, and an empty one skips that window
  osascript - "$1" "$2" >/dev/null 2>&1 <<'APPLESCRIPT' &
on run argv
  set n to length of argv
  set wasRunning to application "Terminal" is running
  tell application "Terminal"
    activate
    if not wasRunning then delay 1
    set reuseDefault to not wasRunning
    repeat with k from 1 to n
      set cmd to item k of argv
      if cmd is not "" then
        if reuseDefault then
          try
            do script cmd in window 1
          on error
            do script cmd
          end try
          set reuseDefault to false
        else
          do script cmd
        end if
      end if
    end repeat
  end tell
end run
APPLESCRIPT
  local pid=$!
  for _ in $(seq 1 16); do
    kill -0 "$pid" 2>/dev/null || { wait "$pid"; return $?; }
    sleep 0.5
  done
  kill "$pid" 2>/dev/null || true
  return 1
}

OPEN_LOG_WINDOW="${OPEN_LOG_WINDOW:-1}"
OPEN_CHAT_WINDOW="${OPEN_CHAT_WINDOW:-$OPEN_LOG_WINDOW}"
here="$(printf '%q' "$PWD")"
log_cmd=""; chat_cmd=""
is_off "$OPEN_LOG_WINDOW" || log_cmd="cd $here && printf '\\033]0;Feedback Insights logs\\007' && tail -n 50 -f $(printf '%q' "$LOG_FILE")"
is_off "$OPEN_CHAT_WINDOW" || chat_cmd="cd $here && PORT=$(printf '%q' "$PORT") LOAD_SAMPLE=$(printf '%q' "${LOAD_SAMPLE:-1}") bash ./scripts/chat.sh"

opened=""
if [[ -n "$log_cmd$chat_cmd" ]] && command -v osascript >/dev/null 2>&1; then
  open_windows "$log_cmd" "$chat_cmd" && opened=1
fi
echo
if [[ -n "$opened" && -n "$chat_cmd" ]]; then echo "Opened a Terminal window for the chat."; else echo "Chat in a second terminal with $CHAT_HINT"; fi
if [[ -n "$opened" && -n "$log_cmd" ]]; then echo "Opened a Terminal window following $LOG_FILE."; else echo "Logs in a third terminal with tail -f $LOG_FILE"; fi

echo
echo "This terminal shows the API is on, and it stays free for the other commands."
if [[ "$PORT" != "8000" ]]; then echo "Point the CLI at this port first with export API_URL=http://127.0.0.1:$PORT"; fi
cat <<'EOF'
  source .venv/bin/activate
  python -m app.cli ingest <file.json or file.csv>
  python -m app.cli analyze
  python -m app.cli insights --team product
  python -m app.cli --help
Stop the API with ./run.sh stop
EOF
