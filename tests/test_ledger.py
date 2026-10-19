"""Ledger: a yes and a no with a reason land as JSONL lines, bad entries are 422, nothing is rewritten."""
import json
import re

import pytest


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


def test_yes_and_no_with_reason_are_appended(client, ledger_path, insights):
    before = lines(ledger_path)
    r1 = client.post("/ledger", json={"target": "insights", "verdict": "yes", "reason": ""})
    r2 = client.post("/ledger", json={"target": "insights:product", "verdict": "no", "reason": "Acme theme missed Initech"})
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    after = lines(ledger_path)
    assert len(after) == len(before) + 2
    y, n = json.loads(after[-2]), json.loads(after[-1])
    assert y["verdict"] == "yes" and n["verdict"] == "no" and n["reason"] == "Acme theme missed Initech"
    for e in (y, n):
        assert set(e) >= {"ts", "run_id", "target", "verdict", "reason", "mode", "insights_sha256"}, e
        assert e["mode"] == "offline"
        assert e["run_id"] == insights["run_id"]
        assert re.fullmatch(r"[0-9a-f]{64}", e["insights_sha256"] or ""), e["insights_sha256"]


@pytest.mark.parametrize("body", [
    {"target": "insights", "verdict": "no"},
    {"target": "insights", "verdict": "no", "reason": ""},
    {"target": "insights", "verdict": "no", "reason": "   "},
    {"target": "insights", "verdict": "maybe", "reason": "x"},
    {"target": "insights"},
    {},
])
def test_bad_entries_are_422_and_write_nothing(client, ledger_path, body):
    before = lines(ledger_path)
    r = client.post("/ledger", json=body)
    assert r.status_code == 422, (body, r.status_code, r.text)
    assert lines(ledger_path) == before


def test_ledger_is_append_only(client, ledger_path, insights):
    client.post("/ledger", json={"target": "insights", "verdict": "yes"})
    snap = ledger_path.read_bytes()
    client.post("/ledger", json={"target": "insights", "verdict": "no", "reason": "second rating"})
    assert ledger_path.read_bytes().startswith(snap)
    assert len(ledger_path.read_bytes()) > len(snap)


def test_get_ledger_returns_latest_in_order(client, ledger_path, insights):
    client.post("/ledger", json={"target": "insights", "verdict": "yes", "reason": "marker-a"})
    client.post("/ledger", json={"target": "insights", "verdict": "no", "reason": "marker-b"})
    got = client.get("/ledger", params={"limit": 2}).json()
    assert [e["reason"] for e in got] == ["marker-a", "marker-b"]
    assert len(client.get("/ledger", params={"limit": 1}).json()) == 1
