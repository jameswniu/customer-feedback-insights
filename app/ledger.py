"""Append-only JSONL ledger of "Good? yes/no + reason" ratings. Never rewritten."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional

from app.config import get_settings
from app.models import TEAMS, LedgerEntry, LedgerIn, now_iso

_lock = threading.Lock()


def insights_hash(insights: dict) -> str:
    """sha256 of the insights as shown; generated_at changes every call so it is left out."""
    body = {k: v for k, v in insights.items() if k != "generated_at"}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _team_of(target: str) -> Optional[str]:
    tail = target.partition(":")[2].strip().lower()
    return tail if tail in TEAMS else None


def _current_insights(target: str) -> Optional[dict]:
    try:
        from app.insights import build_insights
        ins = build_insights(_team_of(target))
        return json.loads(ins.model_dump_json()) if hasattr(ins, "model_dump_json") else dict(ins)
    except Exception:
        return None


def append(entry: LedgerIn) -> LedgerEntry:
    run_id, sha = entry.run_id, entry.insights_sha256
    if not (run_id and sha):
        cur = _current_insights(entry.target)
        if cur:
            run_id = run_id or cur.get("run_id") or None
            sha = sha or insights_hash(cur)
    row = LedgerEntry(
        ts=now_iso(), run_id=run_id, target=entry.target, verdict=entry.verdict,
        reason=entry.reason, mode=get_settings().mode, insights_sha256=sha,
    )
    path = get_settings().ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row.model_dump(), ensure_ascii=False)
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return row


def read(limit: int = 20) -> list[LedgerEntry]:
    path = get_settings().ledger_path
    if not path.exists():
        return []
    out: list[LedgerEntry] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(LedgerEntry.model_validate(json.loads(line)))
            except ValueError:
                continue
    return out[-limit:]
