"""JSON lines to LOG_FILE plus readable lines on stderr (contract in CLAUDE.md)."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_EXTRAS = ("request_id", "step", "duration_ms", "run_id", "model", "latency_ms", "tokens_in", "tokens_out")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRAS:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:<5} {record.name} {record.getMessage()}"
        extras = " ".join(f"{k}={getattr(record, k)}" for k in _EXTRAS if getattr(record, k, None) is not None)
        text = f"{base}  {extras}" if extras else base
        return text + "\n" + self.formatException(record.exc_info) if record.exc_info else text


def setup_logging() -> None:
    """Idempotent. Honors LOG_LEVEL (default INFO) and LOG_FILE (default logs/app.log)."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    path = Path(os.environ.get("LOG_FILE", "logs/app.log"))
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_feedback_insights", False):
            root.removeHandler(handler)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(HumanFormatter())
    for handler in (file_handler, stream_handler):
        handler._feedback_insights = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    logging.getLogger("app").info("logging ready level=%s file=%s", level, path, extra={"step": "startup"})


@contextmanager
def log_step(step: str, logger: logging.Logger | None = None, **fields):
    """Time a pipeline step and log its duration_ms when the block exits."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        (logger or logging.getLogger("app.step")).info(
            "%s done", step, extra={"step": step, "duration_ms": round((time.perf_counter() - t0) * 1000), **fields})
