"""The CLI against a real server: live events, insights with quotes, and a yes and a no in the ledger."""
import json
import re
import subprocess

import pytest

from conftest import PY, ROOT, SEED

STEPS = ["start", "load", "classify", "cluster", "score", "verify", "persist", "answer", "done"]


def cli(server, *args, stdin=None, timeout=90):
    return subprocess.run([PY, "-m", "app.cli", *args], input=stdin, capture_output=True, text=True,
                          cwd=ROOT, env=server.env, timeout=timeout)


def ledger(server):
    return [json.loads(x) for x in server.ledger.read_text().splitlines()] if server.ledger.exists() else []


@pytest.fixture(scope="module")
def ready(server):
    r = cli(server, "ingest", str(SEED))
    assert r.returncode == 0 and "Ingested 40" in r.stdout, r.stdout + r.stderr
    return server


def test_analyze_prints_every_step_live(ready):
    r = cli(ready, "analyze", "--mode", "all")
    assert r.returncode == 0, r.stdout + r.stderr
    seen = re.findall(r"^\S+\s+(\w+)\s", r.stdout, flags=re.M)
    first = [seen.index(s) for s in STEPS if s in seen]
    assert first == sorted(first) and set(STEPS) <= set(seen), seen


def test_insights_prints_sections_with_quotes_and_no_rating_without_input(ready):
    before = len(ledger(ready))
    r = cli(ready, "insights", "--no-rate")
    assert r.returncode == 0, r.stderr
    for head in ("URGENT ISSUES", "THEMES", "AT-RISK ACCOUNTS"):
        assert head in r.stdout, head
    assert '"' in r.stdout
    assert len(ledger(ready)) == before


def test_yes_and_no_with_reason_reach_the_ledger(ready):
    before = len(ledger(ready))
    r = cli(ready, "insights", stdin="yes\n")
    assert r.returncode == 0 and "Good? (yes/no) + reason:" in r.stdout, r.stdout + r.stderr
    r = cli(ready, "insights", "--team", "product", stdin="no the export theme is split in two\n")
    assert r.returncode == 0, r.stdout + r.stderr
    rows = ledger(ready)
    assert len(rows) == before + 2
    y, n = rows[-2], rows[-1]
    assert y["verdict"] == "yes" and n["verdict"] == "no"
    assert n["reason"] == "the export theme is split in two" and n["target"] == "insights:product"
    assert re.fullmatch(r"[0-9a-f]{64}", n["insights_sha256"] or "")


def test_no_without_a_reason_writes_nothing(ready):
    before = len(ledger(ready))
    r = cli(ready, "insights", stdin="no\n")
    assert r.returncode == 0 and len(ledger(ready)) == before, r.stdout + r.stderr


def test_ledger_command_lists_entries(ready):
    r = cli(ready, "ledger", "--limit", "5")
    assert r.returncode == 0 and "yes" in r.stdout and "no" in r.stdout, r.stdout


def test_cli_without_a_server_fails_cleanly(server):
    env = dict(server.env, API_URL="http://127.0.0.1:9")
    r = subprocess.run([PY, "-m", "app.cli", "health"], capture_output=True, text=True, cwd=ROOT, env=env, timeout=30)
    assert r.returncode != 0 and "Traceback" not in r.stderr, r.stderr
