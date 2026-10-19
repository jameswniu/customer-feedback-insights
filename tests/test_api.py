"""API round trip over a real uvicorn process: ingest, analyze (JSON and SSE), insights, validation."""
import httpx
import pytest

from conftest import parse_sse

STEPS = ["start", "load", "classify", "cluster", "score", "verify", "persist", "answer", "done"]


@pytest.fixture(scope="module")
def api(server, seed_rows):
    with httpx.Client(base_url=server.base, timeout=120) as c:
        r = c.post("/feedback", json=seed_rows)
        assert r.status_code == 201, r.text
        yield c


def test_health_offline_with_seed_count(api, seed_rows):
    j = api.get("/health").json()
    assert j == {"status": "ok", "mode": "offline", "feedback_count": len(seed_rows)}


def test_ingest_keeps_ids_and_is_idempotent(api, seed_rows):
    r = api.post("/feedback", json=seed_rows)
    assert r.status_code == 201
    assert r.json()["ids"] == [x["id"] for x in seed_rows]
    assert api.get("/health").json()["feedback_count"] == len(seed_rows)


def test_get_feedback_filters(api, seed_rows):
    assert len(api.get("/feedback", params={"limit": 1000}).json()) == len(seed_rows)
    acme = api.get("/feedback", params={"account": "acme corp"}).json()
    assert acme and {f["account"] for f in acme} == {"Acme Corp"}
    assert len(api.get("/feedback", params={"limit": 5}).json()) == 5


def test_post_feedback_single_and_validation(api):
    r = api.post("/feedback", json={"source": "email", "account": "Test Co", "text": "A single row."})
    assert r.status_code == 201 and r.json()["ingested"] == 1 and len(r.json()["ids"]) == 1
    for bad in ({"account": "X", "text": "no source"}, {"source": "email", "text": "no account"},
                {"source": "email", "account": "X"}, {"source": "email", "account": "X", "text": ""},
                {"source": "email", "account": "X", "text": "   "}):
        assert api.post("/feedback", json=bad).status_code == 422, bad


def test_analyze_json_shape(api, seed_rows):
    r = api.post("/analyze", params={"mode": "all", "stream": "false"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) >= {"run_id", "analyzed", "themes", "dropped_unverified", "duration_s", "mode"}
    assert j["mode"] == "offline" and j["analyzed"] >= len(seed_rows) and j["themes"] > 0
    assert isinstance(j["dropped_unverified"], int) and j["duration_s"] >= 0
    print(f"analyze duration_s={j['duration_s']} (target p50 < 20 s)")


def test_analyze_stream_emits_every_step_in_order(api):
    r = api.post("/analyze", params={"mode": "all", "stream": "true"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "no-cache" in r.headers.get("cache-control", "")
    ev = parse_sse(r.text)
    assert ev, "no events"
    for e in ev:
        assert set(e) >= {"step", "message", "ts", "data"}, e
    steps = [e["step"] for e in ev]
    assert "error" not in steps, [e for e in ev if e["step"] == "error"]
    assert steps[0] == "start" and steps[-1] == "done"
    first = [steps.index(s) for s in STEPS if s in steps]
    assert first == sorted(first), steps
    missing = [s for s in STEPS if s not in steps]
    assert not missing, f"steps never emitted: {missing}"


def test_analyze_new_after_all_has_nothing_left(api):
    r = api.post("/analyze", params={"mode": "new", "stream": "false"})
    assert r.status_code == 200, r.text
    assert r.json()["analyzed"] == 1 or r.json()["analyzed"] == 0, r.json()  # only the Test Co row may be new


def test_analyze_bad_mode_is_400(api):
    assert api.post("/analyze", params={"mode": "bogus", "stream": "false"}).status_code == 400


def test_insights_endpoint_over_http(api):
    j = api.get("/insights").json()
    assert j["themes"] and j["urgent_issues"] and j["team"] in (None, "")
    assert api.get("/insights", params={"teams": "bogus"}).status_code == 400
