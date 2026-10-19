#!/usr/bin/env bash
# Offline smoke test: temp DB and port, seed ingest, analyze, insights checks, CLI ledger yes and no.
# Exits 0 only when every check passes. Never needs an API key.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

TMP="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
export PORT
export DB_PATH="$TMP/feedback.db" LEDGER_PATH="$TMP/ledger.jsonl" LOG_FILE="$TMP/app.log"
export LLM_MODE=offline OPEN_LOG_WINDOW=0 API_URL="http://127.0.0.1:$PORT"
unset ANTHROPIC_API_KEY

echo "smoke: port $PORT, temp dir $TMP"
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$TMP/uvicorn.out" 2>&1 &
SERVER_PID=$!

ready=""
for _ in $(seq 1 40); do
  if curl -fsS "$API_URL/health" >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 0.5
done
if [[ -z "$ready" ]]; then
  echo "FAIL  server did not become healthy"
  tail -n 25 "$TMP/uvicorn.out" || true
  exit 1
fi
echo "PASS  server healthy on $API_URL"

"$PY" - "$ROOT" "$TMP" <<'PYEOF'
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT, TMP = sys.argv[1], sys.argv[2]
API = os.environ["API_URL"]
LEDGER = os.environ["LEDGER_PATH"]
LOG = os.environ["LOG_FILE"]
PY = sys.executable
CLI_EXISTS = os.path.exists(os.path.join(ROOT, "app", "cli.py"))
results = []
S = {}


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = f"  ({detail})" if detail != "" else ""
    print(("PASS  " if ok else "FAIL  ") + name + tail, flush=True)
    return bool(ok)


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw, status = r.read().decode(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def norm(s):
    return " ".join(str(s).split()).casefold()


def deadline_supported(d, q):
    """A deadline is supported when it is in the quote verbatim, or an ISO date whose month and day (or weekday) the quote spells."""
    nd, nq = norm(d), norm(q)
    if nd in nq:
        return True
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", nd)
    if not m:
        return False
    import calendar
    import datetime
    y, mo, day = int(m[1]), int(m[2]), int(m[3])
    months = {calendar.month_name[mo].casefold(), calendar.month_abbr[mo].casefold()}
    spelled = any(re.search(rf"\b{mn}\.? {day}\b|\b{day} {mn}\b", nq) for mn in months)
    return spelled or f"{mo}/{day}" in nq or calendar.day_name[datetime.date(y, mo, day).weekday()].casefold() in nq


def ledger_lines():
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER) as f:
        return [json.loads(x) for x in f if x.strip()]


def cli(args, stdin=""):
    return subprocess.run([PY, "-m", "app.cli", *args], cwd=ROOT, input=stdin,
                          capture_output=True, text=True, timeout=120)


def tags_of(f):
    t = (f or {}).get("tags") or []
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except ValueError:
            t = [t]
    return t


def guarded(fn):
    try:
        fn()
    except Exception as exc:  # one broken phase must not hide the others
        check(f"{fn.__name__} ran without crashing", False, repr(exc)[:200])


def phase_health():
    st, h = call("GET", "/health")
    check("health reports offline mode", st == 200 and isinstance(h, dict) and h.get("mode") == "offline", f"{st} {h}")


def phase_ingest():
    seed = os.path.join(ROOT, "data", "sample_feedback.json")
    cli_ok = False
    if CLI_EXISTS:
        r = cli(["ingest", seed])
        cli_ok = check("CLI ingest of data/sample_feedback.json exits 0", r.returncode == 0,
                       (r.stdout + r.stderr).strip()[-160:])
    else:
        check("CLI ingest of data/sample_feedback.json exits 0", False, "app/cli.py missing, falling back to POST /feedback")
    if not cli_ok:
        with open(seed) as f:
            st, body = call("POST", "/feedback", json.load(f))
        check("fallback POST /feedback returns 201", st == 201, f"{st}")
    st, h = call("GET", "/health")
    check("feedback_count is 40 after ingest", isinstance(h, dict) and h.get("feedback_count") == 40, f"{h}")


def phase_analyze():
    st, a = call("POST", "/analyze?mode=all&stream=false")
    keys = {"run_id", "analyzed", "themes", "dropped_unverified", "duration_s", "mode"}
    isd = isinstance(a, dict)
    check("POST /analyze (json) returns the contract keys", st == 200 and isd and keys <= set(a),
          f"{st} missing={sorted(keys - set(a)) if isd else a}")
    check("analyze mode=all covers all 40 rows", isd and a.get("analyzed") == 40, f"analyzed={a.get('analyzed') if isd else a}")
    check("analyze ran offline", isd and a.get("mode") == "offline", f"mode={a.get('mode') if isd else a}")


def phase_stream():
    events, ctype = [], ""
    req = urllib.request.Request(API + "/analyze?mode=all&stream=true", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        ctype = r.headers.get("content-type", "")
        for line in r:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    steps = [e.get("step") for e in events]
    check("SSE content-type is text/event-stream", "text/event-stream" in ctype, ctype)
    check("SSE first event is start and last is done", steps[:1] == ["start"] and steps[-1:] == ["done"], f"steps={steps}")
    check("SSE has no error event and every event has step, message, ts",
          events and "error" not in steps and all({"step", "message", "ts"} <= set(e) for e in events))
    core = {"start", "classify", "cluster", "verify", "done"}
    check("SSE covers the core pipeline steps", core <= set(steps), f"missing={sorted(core - set(steps))}")


def phase_insights():
    st, ins = call("GET", "/insights")
    st2, fb = call("GET", "/feedback?limit=1000")
    by_id = {f["id"]: f for f in fb} if isinstance(fb, list) else {}
    S["by_id"] = by_id
    top = {"run_id", "generated_at", "team", "mode", "themes", "urgent_issues", "at_risk_accounts", "dropped_unverified"}
    ok = check("GET /insights has the Insights keys", st == 200 and isinstance(ins, dict) and top <= set(ins),
               f"{st} missing={sorted(top - set(ins)) if isinstance(ins, dict) else ins}")
    if not ok:
        return
    prios, conf = {"urgent+critical", "urgent", "critical", "watch"}, {"low", "medium", "high"}
    themes, urgent, risk = ins["themes"], ins["urgent_issues"], ins["at_risk_accounts"]
    check("themes carry priority, confidence and evidence",
          themes and all(t.get("priority") in prios and t.get("confidence") in conf and t.get("evidence") for t in themes),
          f"{len(themes)} themes")
    ukeys = {"feedback_id", "account", "source", "quote", "deadline", "theme_id", "confidence"}
    check("urgent_issues have the contract keys", urgent and all(ukeys <= set(u) and u["confidence"] in conf for u in urgent),
          f"{len(urgent)} urgent issues")
    rkeys = {"account", "risk", "confidence", "reasons", "evidence"}
    check("at_risk_accounts have the contract keys",
          risk and all(rkeys <= set(r) and r["confidence"] in conf and r["evidence"] for r in risk), f"{len(risk)} accounts")
    check("dropped_unverified is an int", isinstance(ins["dropped_unverified"], int), f"{ins['dropped_unverified']}")

    claims = []
    for t in themes:
        claims += [(f"theme {t.get('id')}", e) for e in t.get("evidence", [])]
    claims += [("urgent_issue", u) for u in urgent]
    for r in risk:
        claims += [(f"at_risk {r.get('account')}", e) for e in r.get("evidence", [])]
    bad = []
    for where, e in claims:
        f = by_id.get(e.get("feedback_id"))
        q = e.get("quote") or ""
        if f is None:
            bad.append((where, e.get("feedback_id"), "unknown feedback id"))
        elif not q.strip() or norm(q) not in norm(f["text"]):
            bad.append((where, e.get("feedback_id"), q[:60]))
        elif e.get("account") != f.get("account") or e.get("source") != f.get("source"):
            bad.append((where, e.get("feedback_id"), "account or source mismatch"))
    check(f"every evidence quote is a substring of its feedback text ({len(claims)} quotes)", claims and not bad,
          f"{len(bad)} bad, first={bad[:2]}")
    bad_dl = [(u["feedback_id"], u["deadline"]) for u in urgent if u.get("deadline") and not deadline_supported(u["deadline"], u.get("quote", ""))]
    check("no urgent deadline is invented (each appears in its quote)", urgent and not bad_dl, f"bad={bad_dl[:3]}")

    exp = json.load(open(os.path.join(ROOT, "tests", "fixtures", "expected.json")))
    nodl = [i for i in exp.get("no_deadline_ids", []) if (by_id.get(i) or {}).get("deadline")]
    check("rows with no date wording carry no deadline", any(f.get("analyzed_at") for f in by_id.values()) and not nodl,
          f"invented on {nodl[:5]}")
    want, got = set(exp["expected_urgent"]), {u["feedback_id"] for u in urgent}
    hit = want & got
    precision = len(hit) / len(got) if got else 0.0
    check(f"recall on expected-urgent >= 0.9 (precision {precision:.2f} printed, not asserted)", len(hit) / len(want) >= 0.9,
          f"recall={len(hit) / len(want):.2f} missed={sorted(want - got)}")

    st, _ = call("GET", "/insights?teams=bogus")
    check("?teams=bogus returns 400", st == 400, f"{st}")
    allowed_by_team = {
        "product": {"bug", "performance", "usability", "feature_request", "integration", "security", "other"},
        "customer_success": {"billing", "onboarding", "support_quality", "churn_risk", "renewal", "security", "other"},
    }
    for team, allowed in allowed_by_team.items():
        st, t = call("GET", f"/insights?teams={team}")
        if not check(f"?teams={team} returns 200", st == 200 and isinstance(t, dict), f"{st}"):
            continue
        leaks = [u["feedback_id"] for u in t.get("urgent_issues", []) if not set(tags_of(by_id.get(u["feedback_id"]))) & allowed]
        for th in t.get("themes", []):
            ids = th.get("related_feedback_ids") or [e["feedback_id"] for e in th.get("evidence", [])]
            if isinstance(ids, list) and not any(set(tags_of(by_id.get(i))) & allowed for i in ids):
                leaks.append(th.get("id"))
        check(f"?teams={team} leaks no item outside its tag set", not leaks, f"leaks={leaks[:5]}")


def phase_ledger():
    LKEYS = {"ts", "run_id", "target", "verdict", "reason", "mode", "insights_sha256"}
    reason = "urgent list missed the Acme renewal"
    if CLI_EXISTS:
        n0 = len(ledger_lines())
        r = cli(["insights"], "yes\n")
        L = ledger_lines()
        check("CLI insights + 'yes' appends one yes ledger line",
              r.returncode == 0 and len(L) == n0 + 1 and L[-1].get("verdict") == "yes",
              f"rc={r.returncode} lines={len(L)} err={r.stderr.strip()[-150:]}")
        n1 = len(L)
        r = cli(["insights"], f"no {reason}\n")
        L = ledger_lines()
        check("CLI insights + 'no <reason>' appends one no line carrying the reason",
              r.returncode == 0 and len(L) == n1 + 1 and L[-1].get("verdict") == "no" and reason in (L[-1].get("reason") or ""),
              f"rc={r.returncode} lines={len(L)} last={L[-1] if L else None} err={r.stderr.strip()[-150:]}")
    else:
        check("CLI insights writes ledger yes and no lines", False, "app/cli.py missing")
    L = ledger_lines()
    check("ledger lines carry the contract keys and a 64-hex insights_sha256",
          L and all(LKEYS <= set(x) and re.fullmatch(r"[0-9a-f]{64}", str(x.get("insights_sha256", ""))) for x in L), f"{len(L)} lines")
    n = len(L)
    st1, _ = call("POST", "/ledger", {"target": "insights", "verdict": "no", "reason": ""})
    st2, _ = call("POST", "/ledger", {"target": "insights", "verdict": "no"})
    check("POST /ledger verdict=no without a reason returns 422", st1 == 422 and st2 == 422, f"{st1},{st2}")
    check("rejected ledger posts wrote nothing", len(ledger_lines()) == n)
    st3, _ = call("POST", "/ledger", {"target": "insights", "verdict": "yes", "reason": ""})
    check("POST /ledger verdict=yes without a reason returns 201", st3 == 201, f"{st3}")
    st4, lg = call("GET", "/ledger?limit=10")
    check("GET /ledger lists entries", st4 == 200 and isinstance(lg, list) and len(lg) >= 2, f"{st4} {len(lg) if isinstance(lg, list) else lg}")


def phase_logs():
    lines = [x for x in open(LOG) if x.strip()]
    parsed = [json.loads(x) for x in lines[:20]]
    check("log file gained JSON lines with ts, level, msg",
          lines and all({"ts", "level", "msg"} <= set(p) for p in parsed), f"{len(lines)} lines")


for phase in (phase_health, phase_ingest, phase_analyze, phase_stream, phase_insights, phase_ledger, phase_logs):
    guarded(phase)

total, bad = len(results), results.count(False)
print(f"\nSMOKE {'PASS' if not bad else 'FAIL'}: {total - bad}/{total} checks passed")
sys.exit(1 if bad else 0)
PYEOF
