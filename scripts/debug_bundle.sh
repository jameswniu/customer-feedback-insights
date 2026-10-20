#!/usr/bin/env bash
# Writes debug/bundle-<timestamp>.tar.gz: last 2000 log lines (scrubbed), redacted env, versions, row counts.
# Secrets never leave: key-looking tokens are scrubbed from every file in the bundle.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

export LOG_FILE="${LOG_FILE:-logs/app.log}"
export DB_PATH="${DB_PATH:-data/feedback.db}"
export LEDGER_PATH="${LEDGER_PATH:-data/ledger.jsonl}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="debug/bundle-$STAMP.tar.gz"
mkdir -p debug
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
B="$WORK/bundle-$STAMP"
mkdir -p "$B"

# Everything below runs in one python so scrubbing is case-insensitive and portable (BSD sed lacks \s and I).
"$PY" - "$B" <<'PYEOF'
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys

B = sys.argv[1]
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", re.I)
APP_NAMES = ["ANTHROPIC_API_KEY", "LLM_MODEL", "LLM_MODE", "DB_PATH", "LEDGER_PATH", "LOG_LEVEL", "LOG_FILE",
             "PORT", "OPEN_LOG_WINDOW", "OPEN_CHAT_WINDOW", "API_URL"]
TOKEN = re.compile(r"sk-[A-Za-z0-9_-]{6,}")
KV = re.compile(r"""(?i)(["']?(?:api[_-]?key|token|secret|password|authorization|bearer)["']?\s*[:=]\s*["']?)[^"',\s}]+""")
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def scrub(text):
    text = TOKEN.sub("<redacted>", text)
    text = BEARER.sub(r"\1<redacted>", text)
    return KV.sub(lambda m: m.group(1) + "<redacted>", text)


def write(name, text):
    with open(os.path.join(B, name), "w") as f:
        f.write(scrub(text))


# last 2000 log lines
log = os.environ["LOG_FILE"]
if os.path.exists(log):
    with open(log, errors="replace") as f:
        write("app.log.tail", "".join(f.readlines()[-2000:]))
else:
    write("app.log.tail", f"log file {log} does not exist yet\n")

# env: only the names the app uses plus any secret-looking names, values of secret names redacted
names = list(APP_NAMES)
others = sum(1 for n in os.environ if SECRET_NAME.search(n) and n not in APP_NAMES)
lines = [f"# {others} other secret-looking variables in the shell are omitted, names included"]
for n in names:
    if n not in os.environ:
        lines.append(f"{n}=<unset>")
    elif SECRET_NAME.search(n):
        lines.append(f"{n}=<redacted>")
    else:
        lines.append(f"{n}={os.environ[n]}")
write("env.txt", "\n".join(lines) + "\n")

# versions
def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as exc:
        return f"<{cmd[0]} failed: {exc}>"

freeze = run([sys.executable, "-m", "pip", "freeze"]).splitlines()[:60]
write("versions.txt", "\n".join([
    f"python {sys.version.split()[0]} ({sys.executable})",
    f"platform {platform.platform()}",
    f"uname {run(['uname', '-a'])}",
    "pip freeze (first 60):", *freeze, ""]))

# row counts
out = []
db = os.environ["DB_PATH"]
if os.path.exists(db):
    con = sqlite3.connect(db)
    for label, sql in (("feedback rows", "SELECT COUNT(*) FROM feedback"),
                       ("feedback analyzed", "SELECT COUNT(*) FROM feedback WHERE analyzed_at IS NOT NULL"),
                       ("theme rows", "SELECT COUNT(*) FROM theme")):
        try:
            out.append(f"{label}: {con.execute(sql).fetchone()[0]}")
        except sqlite3.Error as exc:
            out.append(f"{label}: <{exc}>")
    con.close()
else:
    out.append(f"db {db} does not exist yet")
led = os.environ["LEDGER_PATH"]
if os.path.exists(led):
    with open(led) as f:
        entries = [json.loads(x) for x in f if x.strip()]
    out.append(f"ledger lines: {len(entries)} (yes {sum(e.get('verdict') == 'yes' for e in entries)}, no {sum(e.get('verdict') == 'no' for e in entries)})")
else:
    out.append(f"ledger {led} does not exist yet")
write("counts.txt", "\n".join(out) + "\n")
PYEOF

tar -czf "$OUT" -C "$WORK" "bundle-$STAMP"
echo "Wrote $ROOT/$OUT"
tar -tzf "$OUT" | sed 's/^/  /'
