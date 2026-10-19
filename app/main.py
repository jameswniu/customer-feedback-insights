"""FastAPI app: feedback in, analysis out (SSE), insights, ledger."""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app import db, ledger
from app.config import get_settings
from app.models import TEAMS, Event, Feedback, FeedbackIn, LedgerEntry, LedgerIn

log = logging.getLogger("app.main")


def _setup_logging() -> None:
    """Use the logging_setup module when it exists, else plain stderr logging."""
    try:
        from app import logging_setup
        for name in ("setup_logging", "configure_logging", "init_logging", "setup"):
            fn = getattr(logging_setup, name, None)
            if callable(fn):
                fn()
                return
    except Exception as exc:  # logging must never stop the API
        print(f"logging_setup failed ({exc}); using basicConfig")
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _setup_logging()
    db.init_db()
    s = get_settings()
    log.info("startup mode=%s db=%s ledger=%s", s.mode, s.db_path, s.ledger_path)
    yield


app = FastAPI(title="Feedback Insights", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _access_log(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - t0) * 1000)
    response.headers["X-Request-ID"] = rid
    log.info("%s %s -> %s", request.method, request.url.path, response.status_code,
             extra={"request_id": rid, "duration_ms": ms})
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": get_settings().mode, "feedback_count": db.count_feedback()}


@app.post("/feedback", status_code=201)
def post_feedback(payload: Union[FeedbackIn, list[FeedbackIn]] = Body(...)) -> dict:
    rows = payload if isinstance(payload, list) else [payload]
    ids = db.insert_feedback(rows)
    log.info("ingested %d feedback rows", len(ids), extra={"step": "ingest"})
    return {"ingested": len(ids), "ids": ids}


@app.get("/feedback", response_model=list[Feedback])
def get_feedback(account: Optional[str] = None, limit: int = Query(1000, ge=1, le=10000)):
    return db.list_feedback(account=account, limit=limit)


# ---- analyze -------------------------------------------------------------------------------

def _frame(ev: Any) -> str:
    if hasattr(ev, "model_dump_json"):
        step, payload = getattr(ev, "step", "message"), ev.model_dump_json()
    else:
        step = ev.get("step", "message") if isinstance(ev, dict) else "message"
        payload = json.dumps(ev, default=str)
    return f"event: {step}\ndata: {payload}\n\n"


def _ev_dict(ev: Any) -> dict:
    return ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)


def _sse(mode: str):
    try:
        from app.analyze.pipeline import run_analysis
    except Exception as exc:
        yield _frame(Event.of("error", f"analysis engine unavailable: {exc}"))
        return
    try:
        for ev in run_analysis(mode):
            yield _frame(ev)
    except Exception as exc:
        log.exception("analyze failed")
        yield _frame(Event.of("error", f"{type(exc).__name__}: {exc}"))


def _count(v: Any, fallback: int) -> int:
    if isinstance(v, bool):
        return fallback
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, (list, tuple, set, dict)):
        return len(v)
    return fallback


def _summarize(events: list[dict], elapsed: float) -> dict:
    """Fold the event stream into the JSON result. The `done` event's data wins."""
    merged: dict[str, Any] = {}
    for e in events:
        merged.update(e.get("data") or {})
    done = next((e.get("data") or {} for e in reversed(events) if e.get("step") == "done"), {})
    pick = lambda k: done.get(k, merged.get(k))  # noqa: E731
    return {
        "run_id": pick("run_id") or uuid.uuid4().hex[:8],
        "analyzed": _count(pick("analyzed"), _count(merged.get("classified", merged.get("count")), 0)),
        "themes": _count(pick("themes"), db.count_themes()),
        "dropped_unverified": _count(pick("dropped_unverified"), 0),
        "duration_s": round(elapsed, 2),
        "mode": pick("mode") or get_settings().mode,
    }


@app.post("/analyze")
def analyze(mode: str = Query("new"), stream: bool = Query(True)):
    if mode not in ("new", "all"):
        raise HTTPException(400, "mode must be 'new' or 'all'")
    if stream:
        return StreamingResponse(
            _sse(mode), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        from app.analyze.pipeline import run_analysis
    except Exception as exc:
        raise HTTPException(501, f"analysis engine unavailable: {exc}")
    t0 = time.perf_counter()
    try:
        events = [_ev_dict(e) for e in run_analysis(mode)]
    except Exception as exc:
        log.exception("analyze failed")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    err = next((e for e in events if e.get("step") == "error"), None)
    if err:
        raise HTTPException(500, err.get("message") or "analysis error")
    return _summarize(events, time.perf_counter() - t0)


# ---- insights + ledger ---------------------------------------------------------------------

@app.get("/insights")
def insights(teams: Optional[str] = Query(None), team: Optional[str] = Query(None)):
    t = (teams or team or "").strip().lower() or None
    if t == "all":
        t = None
    if t and t not in TEAMS:
        raise HTTPException(400, f"unknown team '{t}'; use product or customer_success")
    try:
        from app.insights import build_insights
    except Exception as exc:
        raise HTTPException(501, f"insights engine unavailable: {exc}")
    try:
        return build_insights(t)
    except Exception as exc:
        log.exception("build_insights failed")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


@app.post("/ledger", status_code=201, response_model=LedgerEntry)
def post_ledger(entry: LedgerIn):
    row = ledger.append(entry)
    log.info("ledger %s %s", row.verdict, row.target, extra={"step": "ledger"})
    return row


@app.get("/ledger", response_model=list[LedgerEntry])
def get_ledger(limit: int = Query(20, ge=1, le=1000)):
    return ledger.read(limit)
