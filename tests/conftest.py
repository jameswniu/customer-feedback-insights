"""Shared fixtures. Env is set BEFORE app is imported, so nothing here ever touches data/feedback.db."""
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "sample_feedback.json"
FIXTURES = ROOT / "tests" / "fixtures"
PY = sys.executable

PRODUCT_TAGS = {"bug", "performance", "usability", "feature_request", "integration", "security"}
CS_TAGS = {"billing", "onboarding", "support_quality", "churn_risk", "renewal", "security"}

_BOOT = Path(tempfile.mkdtemp(prefix="fi-tests-"))
os.environ.update(
    DB_PATH=str(_BOOT / "feedback.db"),
    LEDGER_PATH=str(_BOOT / "ledger.jsonl"),
    LOG_FILE=str(_BOOT / "app.log"),
    LLM_MODE="offline",
    OPEN_LOG_WINDOW="0",
)
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(ROOT))


def norm(s: str) -> str:
    """Whitespace and case normalisation, the same rule as the evidence check in CLAUDE.md."""
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def parse_sse(text: str) -> list:
    """Events from an SSE body: every `data:` line is one JSON Event."""
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Handle:
    pass


@contextlib.contextmanager
def serve(workdir: Path, **extra_env):
    """A real uvicorn subprocess on a free port with its own DB, ledger and log."""
    workdir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    env = dict(os.environ)
    env.update(
        DB_PATH=str(workdir / "feedback.db"),
        LEDGER_PATH=str(workdir / "ledger.jsonl"),
        LOG_FILE=str(workdir / "app.log"),
        LLM_MODE="offline",
        OPEN_LOG_WINDOW="0",
        PORT=str(port),
        API_URL=f"http://127.0.0.1:{port}",
        PYTHONPATH=str(ROOT),
    )
    env.pop("ANTHROPIC_API_KEY", None)
    env.update(extra_env)
    out_path = workdir / "uvicorn.out"
    out = open(out_path, "w")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not start: " + out_path.read_text()[-2000:])
        h = Handle()
        h.base, h.env, h.dir, h.proc = base, env, workdir, proc
        h.ledger, h.log, h.out = workdir / "ledger.jsonl", workdir / "app.log", out_path
        yield h
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with serve(tmp_path_factory.mktemp("srv")) as h:
        yield h


@pytest.fixture(scope="session")
def seed_rows():
    return json.loads(SEED.read_text())


@pytest.fixture(scope="session")
def expected():
    return json.loads((FIXTURES / "expected.json").read_text())


@pytest.fixture(scope="session")
def client():
    """In-process TestClient on the throwaway DB, ledger and log set above. Offline, no API key."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def ledger_path(client):
    return Path(os.environ["LEDGER_PATH"])


@pytest.fixture(scope="session")
def analyzed(client, seed_rows):
    """Seed ingested and analysed once for the whole session. Returns the /analyze JSON."""
    r = client.post("/feedback", json=seed_rows)
    assert r.status_code == 201, r.text
    r = client.post("/analyze", params={"mode": "all", "stream": "false"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def all_feedback(client, analyzed):
    r = client.get("/feedback", params={"limit": 1000})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def insights(client, analyzed):
    r = client.get("/insights")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def by_id(all_feedback):
    return {f["id"]: f for f in all_feedback}
