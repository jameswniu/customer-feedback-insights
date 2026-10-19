"""Shared pieces for the eval runner: server handles, a local stand-in for the model endpoint, assertion helpers."""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = ROOT / ".venv" / "bin" / "python"
PY = str(VENV_PY if VENV_PY.exists() else sys.executable)
EVALS = ROOT / "tests" / "evals"
FIX = EVALS / "fixtures"
SEED = ROOT / "data" / "sample_feedback.json"
EXPECTED = json.loads((ROOT / "tests" / "fixtures" / "expected.json").read_text())

PRODUCT = {"bug", "performance", "usability", "feature_request", "integration", "security", "other"}
CS = {"billing", "onboarding", "support_quality", "churn_risk", "renewal", "security", "other"}
TEAMSET = {"product": PRODUCT, "customer_success": CS}


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("’", "'")).strip().casefold()


def verbatim(quote, text) -> bool:
    q = norm(quote)
    return bool(q) and q in norm(text)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Acc:
    """Collects the checks of one case. Any failed check fails the case."""

    def __init__(self):
        self.fails: list[str] = []
        self.notes: list[str] = []

    def check(self, cond, ok_msg: str = "", fail_msg: str = "") -> bool:
        if cond:
            if ok_msg:
                self.notes.append(ok_msg)
        else:
            self.fails.append(fail_msg or ok_msg or "check failed")
        return bool(cond)

    def result(self) -> tuple[bool, str]:
        return (False, "; ".join(self.fails)) if self.fails else (True, "; ".join(self.notes))


class FakeAnthropic:
    """Local stand-in for api.anthropic.com. mode '401' rejects every call, mode 'model' answers with valid
    classifications and, for one target id, a fabricated quote and deadline."""

    def __init__(self, mode: str, target=None, fake_quote=None, fake_deadline=None):
        self.mode, self.target, self.fake_quote, self.fake_deadline = mode, target, fake_quote, fake_deadline
        self.hits: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                try:
                    items = json.loads(body["messages"][0]["content"])
                except Exception:
                    items = []
                outer.hits.append({"key": self.headers.get("x-api-key"), "items": len(items)})
                if outer.mode == "401":
                    return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
                rows = []
                for it in items:
                    row = {"id": it["id"], "sentiment": "negative", "tags": ["bug"], "urgency": 0.0, "deadline": None,
                           "theme_label": "Bugs & errors", "evidence_quote": (it.get("text") or "")[:120]}
                    if it["id"] == outer.target:
                        row.update(urgency=1.0, deadline=outer.fake_deadline, evidence_quote=outer.fake_quote)
                    rows.append(row)
                self._send(200, {"id": "msg_eval", "type": "message", "role": "assistant", "model": "fake",
                                 "content": [{"type": "text", "text": json.dumps(rows)}],
                                 "usage": {"input_tokens": 1, "output_tokens": 1}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/messages"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Srv:
    """One real API process on a temp database and port, offline unless the case sets otherwise."""

    fake = None

    def __init__(self, workdir: Path, extra_env=None, launcher=False, fake_url=None):
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.out_path = self.dir / "uvicorn.out"
        for _ in range(3):  # a port can be taken between free_port() and the bind, so retry on a new one
            if self._start(extra_env, launcher, fake_url):
                return
        raise RuntimeError("server did not start after 3 tries: " + self.out_path.read_text()[-600:])

    def _start(self, extra_env, launcher, fake_url) -> bool:
        self.port = free_port()
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = ""  # an explicit empty value stops config.py from loading a real key out of .env
        env.update(PORT=str(self.port), DB_PATH=str(self.dir / "feedback.db"), LEDGER_PATH=str(self.dir / "ledger.jsonl"),
                   LOG_FILE=str(self.dir / "app.log"), LLM_MODE="offline", OPEN_LOG_WINDOW="0",
                   API_URL=f"http://127.0.0.1:{self.port}", PYTHONPATH=str(ROOT))
        env.update(extra_env or {})
        if fake_url:
            env["EVALS_FAKE_API_URL"] = fake_url
        cmd = [PY, str(EVALS / "launch_server.py")] if launcher else \
              [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)]
        self._out = open(self.out_path, "w")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=self._out, stderr=subprocess.STDOUT)
        self.env = env
        self.base = f"http://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.base, timeout=180)
        for _ in range(100):
            if self.proc.poll() is not None:
                break
            try:
                if self.http.get("/health", timeout=1).status_code == 200 and self.proc.poll() is None:
                    return True
            except httpx.HTTPError:
                time.sleep(0.2)
        self.close()
        return False

    def get(self, path, **params):
        return self.http.get(path, params=params or None)

    def post(self, path, json=None, **params):
        return self.http.post(path, json=json, params=params or None)

    def cli(self, *args, stdin=None, timeout=180):
        return subprocess.run([PY, "-m", "app.cli", *args], input=stdin, capture_output=True, text=True,
                              cwd=ROOT, env=self.env, timeout=timeout)

    def ingest(self, path):
        return self.cli("ingest", str(path))

    def analyze(self, mode="all") -> dict:
        r = self.post("/analyze", mode=mode, stream="false")
        if r.status_code != 200:
            raise RuntimeError(f"POST /analyze answered {r.status_code}: {r.text[:200]}")
        return r.json()

    def stream_analyze(self, mode="all"):
        t0, first, events = time.perf_counter(), None, []
        with self.http.stream("POST", "/analyze", params={"mode": mode, "stream": "true"}) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    first = first if first is not None else time.perf_counter() - t0
                    events.append(json.loads(line[5:].strip()))
        return events, first

    def insights(self, team=None) -> dict:
        r = self.get("/insights", **({"teams": team} if team else {}))
        if r.status_code != 200:
            raise RuntimeError(f"GET /insights answered {r.status_code}: {r.text[:200]}")
        return r.json()

    def feedback(self) -> list[dict]:
        return self.get("/feedback", limit=10000).json()

    def ledger_path(self) -> Path:
        return Path(self.env["LEDGER_PATH"])

    def ledger_lines(self) -> list[dict]:
        p = self.ledger_path()
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []

    def log_text(self) -> str:
        p = Path(self.env["LOG_FILE"])
        return (p.read_text() if p.exists() else "") + self.out_path.read_text()

    def close(self):
        with contextlib.suppress(Exception):
            self.http.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        with contextlib.suppress(Exception):
            self._out.close()


class Ctx:
    """Owns the shared baseline server (seed ingested and analyzed once, read-only cases only) and builds fresh ones."""

    def __init__(self, tmp: Path):
        self.tmp, self._base, self._n = Path(tmp), None, 0

    @property
    def baseline(self) -> Srv:
        if self._base is None:
            s = Srv(self.tmp / "baseline")
            r = s.ingest(SEED)
            if r.returncode != 0:
                raise RuntimeError("seed ingest failed: " + (r.stdout + r.stderr)[:200])
            s.analyze("all")
            self._base = s
        return self._base

    @contextlib.contextmanager
    def fresh(self, seed=False, analyze=True, env=None, launcher=False, fake_url=None):
        self._n += 1
        s = Srv(self.tmp / f"fresh{self._n}", env, launcher, fake_url)
        try:
            if seed:
                r = s.ingest(SEED)
                if r.returncode != 0:
                    raise RuntimeError("seed ingest failed: " + (r.stdout + r.stderr)[:200])
                if analyze:
                    s.analyze("all")
            yield s
        finally:
            s.close()

    def close(self):
        if self._base:
            self._base.close()


class Case:
    def __init__(self, raw: dict, ctx: Ctx):
        self.raw, self.id, self.p, self.setup, self.ctx = raw, raw["id"], raw.get("params", {}), raw.get("setup", {}), ctx

    def rows(self, fixture: str | None = None) -> list[dict]:
        return json.loads((FIX / (fixture or self.p["fixture"])).read_text())


# ---- helpers over an Insights payload ------------------------------------------------------------

def fbmap(srv: Srv) -> dict:
    return {f["id"]: f for f in srv.feedback()}


def urgent_ids(ins: dict) -> set:
    return {u["feedback_id"] for u in ins["urgent_issues"]}


def shown_ids(ins: dict) -> set:
    return urgent_ids(ins) | {i for t in ins["themes"] for i in t["related_feedback_ids"]}


def best_theme(ins: dict, ids) -> dict | None:
    ids = set(ids)
    ranked = sorted(ins["themes"], key=lambda t: (-len(ids & set(t["related_feedback_ids"])), -t["urgency"]))
    return ranked[0] if ranked and ids & set(ranked[0]["related_feedback_ids"]) else None


def all_quotes(ins: dict):
    for t in ins["themes"]:
        for e in t["evidence"]:
            yield f"theme {t['name']}", e
    for u in ins["urgent_issues"]:
        yield f"urgent {u['feedback_id']}", u
    for a in ins["at_risk_accounts"]:
        for e in a["evidence"]:
            yield f"account {a['account']}", e


def bad_quotes(ins: dict, fbm: dict) -> list:
    out = []
    for where, e in all_quotes(ins):
        f = fbm.get(e["feedback_id"])
        if not f or not verbatim(e["quote"], f["text"]):
            out.append((where, e["feedback_id"], (e.get("quote") or "")[:50]))
    return out


def only_tag(fb: dict, tag: str) -> bool:
    return list(fb["tags"]) == [tag]


def theme_snapshot(ins: dict) -> dict:
    return {t["id"]: (tuple(sorted(t["related_feedback_ids"])), t["criticality"], t["urgency"], t["priority"]) for t in ins["themes"]}
