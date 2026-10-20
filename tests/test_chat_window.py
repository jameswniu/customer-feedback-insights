"""The chat window script that run.sh opens, and the stop command beside it.

scripts/chat.sh loads the sample feedback into an empty local API and runs the analysis when nothing is analyzed yet,
then starts the chat. Each test runs the real script against a real server with piped input, the same script the
Terminal window runs.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from conftest import PY, ROOT, SEED, serve

CHAT_SH = ROOT / "scripts" / "chat.sh"
GREETING = "Feedback Insights chat."
LOAD_NOTICE = "the sample feedback is loaded first"
ANALYZE_NOTICE = "the analysis runs first"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="chat.sh needs curl")


def run_chat(srv, *lines, **env_extra):
    stdin = "\n".join(lines) + "\n"
    env = dict(srv.env, PYTHON=PY, **env_extra)
    return subprocess.run(["bash", str(CHAT_SH)], input=stdin, capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=120)


def count(srv):
    return httpx.get(srv.base + "/health", timeout=10).json()["feedback_count"]


def themes(srv):
    return httpx.get(srv.base + "/insights", timeout=30).json()["themes"]


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    with serve(tmp_path_factory.mktemp("chat-window")) as h:
        yield h


def test_first_run_loads_the_sample_analyzes_it_then_opens_the_chat(srv):
    assert count(srv) == 0
    r = run_chat(srv, "exit")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert LOAD_NOTICE in out and ANALYZE_NOTICE in out
    assert "Ingested 40 rows" in out
    assert "Done in" in out                                        # the analysis ran to its last step
    assert out.index(LOAD_NOTICE) < out.index(ANALYZE_NOTICE) < out.index(GREETING)   # load, analyze, then chat
    assert count(srv) == 40
    assert themes(srv), "the first-run analysis left no themes"


def test_second_run_goes_straight_to_the_chat(srv):
    assert count(srv) == 40
    r = run_chat(srv, "exit")
    assert r.returncode == 0, r.stderr
    assert LOAD_NOTICE not in r.stdout and ANALYZE_NOTICE not in r.stdout
    assert GREETING in r.stdout
    assert count(srv) == 40                                        # nothing was loaded twice


def test_a_question_is_answered_from_the_loaded_data(srv):
    r = run_chat(srv, "Which accounts need attention today?", "", "exit")
    assert r.returncode == 0, r.stderr
    assert "Loaded the latest analysis, 40 items" in r.stdout
    assert "[fb-" in r.stdout                                      # the answer cites feedback ids


def test_loaded_but_unanalyzed_feedback_is_analyzed_before_the_chat(tmp_path):
    """A first run that stopped after the load must be picked up on the next launch."""
    with serve(tmp_path) as h:
        assert httpx.post(h.base + "/feedback", json=json.loads(SEED.read_text()), timeout=30).status_code == 201
        assert themes(h) == []
        r = run_chat(h, "exit")
        assert r.returncode == 0, r.stderr
        assert LOAD_NOTICE not in r.stdout                         # the feedback was already there
        assert ANALYZE_NOTICE in r.stdout and "Done in" in r.stdout
        assert themes(h), "the analysis did not run"
        assert count(h) == 40


def test_load_sample_zero_leaves_an_empty_api_empty(tmp_path):
    with serve(tmp_path) as h:
        r = run_chat(h, "exit", LOAD_SAMPLE="0")
        assert r.returncode == 0, r.stderr
        assert LOAD_NOTICE not in r.stdout and ANALYZE_NOTICE not in r.stdout
        assert GREETING in r.stdout
        assert count(h) == 0


def test_without_an_api_it_says_how_to_start_one(srv):
    env = dict(srv.env, PYTHON=PY, API_URL="http://127.0.0.1:1")
    r = subprocess.run(["bash", str(CHAT_SH)], input="exit\n", capture_output=True, text=True,
                       cwd=ROOT, env=env, timeout=60)
    assert r.returncode == 1
    assert "Cannot reach the API" in r.stderr
    assert "./run.sh" in r.stderr
    assert GREETING not in r.stdout


@pytest.mark.parametrize("script", ["run.sh", "scripts/chat.sh"])
def test_the_launch_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- an API that goes wrong half way ------------------------------------------------------------

class StubApi:
    """A stand-in API that answers /health ok only the first `health_ok` times, and /insights ok only if asked to.
    It records every request, so a test can see that nothing was written."""

    def __init__(self, health_ok, insights_ok):
        self.seen = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.seen.append(("GET", self.path))
                if self.path == "/health":
                    ok = sum(1 for _, path in stub.seen if path == "/health") <= health_ok
                    return self._send(200, {"status": "ok", "mode": "offline", "feedback_count": 5}) if ok \
                        else self._send(500, {"detail": "boom"})
                if self.path.startswith("/insights"):
                    return self._send(200, {"themes": []}) if insights_ok else self._send(500, {"detail": "boom"})
                self._send(404, {})

            def do_POST(self):
                stub.seen.append(("POST", self.path))
                self._send(500, {"detail": "a write nobody should have made"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def writes(self):
        return [r for r in self.seen if r[0] == "POST"]


def _chat_against(stub):
    env = dict(os.environ, PYTHON=PY, API_URL=stub.url)
    return subprocess.run(["bash", str(CHAT_SH)], input="exit\n", capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=60)


def test_a_failed_health_probe_is_not_read_as_an_empty_api():
    """The first reachability check passes, then the count cannot be read. That must not look like zero feedback."""
    stub = StubApi(health_ok=1, insights_ok=True)
    try:
        r = _chat_against(stub)
    finally:
        stub.close()
    assert stub.writes() == [], f"wrote to an API it could not read, {stub.writes()}"
    assert "Could not read what the API holds" in r.stderr
    assert LOAD_NOTICE not in r.stdout and ANALYZE_NOTICE not in r.stdout


def test_a_failed_insights_probe_does_not_start_an_analysis():
    stub = StubApi(health_ok=99, insights_ok=False)
    try:
        r = _chat_against(stub)
    finally:
        stub.close()
    assert stub.writes() == [], f"wrote to an API it could not read, {stub.writes()}"
    assert "Could not read what the API holds" in r.stderr
    assert ANALYZE_NOTICE not in r.stdout
    assert GREETING in r.stdout                                    # the chat still opens and reports for itself


# ---- ./run.sh stop ------------------------------------------------------------------------------

def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop(tmp_path, setup, api=True):
    """Run a copy of run.sh stop beside a pid file that names an orphaned process, which is how the API runs.
    With api=False the process does not look like the API, as after a reboot when a stale pid is reused."""
    shutil.copy(ROOT / "run.sh", tmp_path / "run.sh")
    ready = tmp_path / "ready"
    program = f"import pathlib, signal, time; {setup}; pathlib.Path({str(ready)!r}).write_text('1'); time.sleep(60)"
    marker = "uvicorn app.main:app" if api else "something-else"
    started = subprocess.run(["bash", "-c", f'"{PY}" -c "$0" {marker} >/dev/null 2>&1 & echo $!', program],
                             capture_output=True, text=True, timeout=10)
    pid = int(started.stdout.strip())
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        assert ready.exists(), "the stand-in API never started"
        (tmp_path / ".run.pid").write_text(str(pid))
        return subprocess.run(["bash", "run.sh", "stop"], cwd=tmp_path, capture_output=True, text=True, timeout=30), pid
    except BaseException:
        _kill(pid)
        raise


def test_stop_waits_for_the_api_to_exit_and_clears_the_pid_file(tmp_path):
    r, pid = _stop(tmp_path, "pass")
    assert r.returncode == 0, r.stderr
    assert "Stopped the API" in r.stdout
    assert not _alive(pid)                                         # it had exited by the time stop returned
    assert not (tmp_path / ".run.pid").exists()


def test_stop_keeps_the_pid_file_while_the_api_is_still_busy(tmp_path):
    r, pid = _stop(tmp_path, "signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    try:
        assert r.returncode == 1
        assert "still finishing a request" in r.stderr
        assert (tmp_path / ".run.pid").read_text() == str(pid)     # still there, so stop can be run again
        assert _alive(pid)
    finally:
        _kill(pid)
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    again = subprocess.run(["bash", "run.sh", "stop"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert again.returncode == 0 and "No running API found" in again.stdout
    assert not (tmp_path / ".run.pid").exists()


def test_stop_leaves_an_unrelated_process_alone(tmp_path):
    """After a reboot the pid file can name some other process. Stop must not signal it."""
    r, pid = _stop(tmp_path, "pass", api=False)
    try:
        assert r.returncode == 0, r.stderr
        assert "No running API found" in r.stdout
        assert _alive(pid), "stop signalled a process that is not the API"
        assert not (tmp_path / ".run.pid").exists()                # the stale file is cleared
    finally:
        _kill(pid)
