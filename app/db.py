"""SQLite repo. One connection per call: callers run in threadpool threads.

feedback.timestamp is a naive ISO 8601 string (UTC). Ids missing on ingest are a hash of
source|account|text, so re-ingesting the same file is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from app.config import get_settings
from app.models import Classification, Feedback, FeedbackIn, Theme, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback(
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  account TEXT NOT NULL,
  timestamp TEXT,
  text TEXT NOT NULL,
  sentiment TEXT,
  tags TEXT DEFAULT '[]',
  urgency REAL DEFAULT 0,
  deadline TEXT,
  theme_id TEXT,
  analyzed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_account ON feedback(account);
CREATE INDEX IF NOT EXISTS idx_feedback_analyzed ON feedback(analyzed_at);
CREATE TABLE IF NOT EXISTS theme(
  id TEXT PRIMARY KEY,
  name TEXT,
  description TEXT,
  urgency REAL,
  criticality REAL,
  related_feedback_ids TEXT,
  accounts TEXT,
  confidence TEXT,
  updated_at TEXT
);
"""

_ready: set[str] = set()
_lock = threading.Lock()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    if not path.exists():
        _ready.discard(key)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if key not in _ready:
            with _lock:
                con.executescript(SCHEMA)
                con.commit()
                _ready.add(key)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _naive_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _make_id(r: FeedbackIn) -> str:
    h = hashlib.sha1(f"{r.source}|{r.account}|{r.text}".encode("utf-8")).hexdigest()[:10]
    return f"fb-{h}"


def _loads(s: Optional[str]) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _fb(row: sqlite3.Row) -> Feedback:
    return Feedback(
        id=row["id"], source=row["source"], account=row["account"], timestamp=row["timestamp"],
        text=row["text"], sentiment=row["sentiment"], tags=_loads(row["tags"]),
        urgency=row["urgency"] or 0.0, deadline=row["deadline"], theme_id=row["theme_id"],
        analyzed_at=row["analyzed_at"],
    )


def _theme(row: sqlite3.Row) -> Theme:
    return Theme(
        id=row["id"], name=row["name"] or "", description=row["description"] or "",
        urgency=row["urgency"] or 0.0, criticality=row["criticality"] or 0.0,
        related_feedback_ids=_loads(row["related_feedback_ids"]), accounts=_loads(row["accounts"]),
        confidence=row["confidence"] or "medium", updated_at=row["updated_at"],
    )


def init_db() -> None:
    with _conn():
        pass


def insert_feedback(rows: list[FeedbackIn]) -> list[str]:
    ids: list[str] = []
    with _conn() as con:
        for r in rows:
            fid = r.id or _make_id(r)
            ts = r.timestamp or _naive_now()
            cur = con.execute("SELECT text FROM feedback WHERE id=?", (fid,)).fetchone()
            if cur is None:
                con.execute(
                    "INSERT INTO feedback(id, source, account, timestamp, text) VALUES (?,?,?,?,?)",
                    (fid, r.source, r.account, ts, r.text),
                )
            elif cur["text"] != r.text:
                con.execute(
                    "UPDATE feedback SET source=?, account=?, timestamp=?, text=?, sentiment=NULL, "
                    "tags='[]', urgency=0, deadline=NULL, theme_id=NULL, analyzed_at=NULL WHERE id=?",
                    (r.source, r.account, ts, r.text, fid),
                )
            ids.append(fid)
    return ids


def list_feedback(account: Optional[str] = None, limit: int = 1000, only_unanalyzed: bool = False) -> list[Feedback]:
    q = "SELECT * FROM feedback"
    where: list[str] = []
    args: list = []
    if account:
        where.append("account = ? COLLATE NOCASE")
        args.append(account)
    if only_unanalyzed:
        where.append("analyzed_at IS NULL")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY timestamp, id LIMIT ?"
    args.append(int(limit))
    with _conn() as con:
        return [_fb(r) for r in con.execute(q, args).fetchall()]


def get_feedback(ids: list[str]) -> list[Feedback]:
    found: dict[str, Feedback] = {}
    ids = list(ids)
    with _conn() as con:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(f"SELECT * FROM feedback WHERE id IN ({marks})", chunk).fetchall():
                found[r["id"]] = _fb(r)
    return [found[i] for i in ids if i in found]


def save_classifications(items: list[Classification]) -> None:
    now = now_iso()
    with _conn() as con:
        con.executemany(
            "UPDATE feedback SET sentiment=?, tags=?, urgency=?, deadline=?, analyzed_at=? WHERE id=?",
            [(c.sentiment, json.dumps(c.tags), c.urgency, c.deadline, now, c.id) for c in items],
        )


def save_themes(themes: list[Theme]) -> None:
    """Replaces all themes and re-points feedback.theme_id at them (first theme wins)."""
    now = now_iso()
    with _conn() as con:
        con.execute("DELETE FROM theme")
        con.execute("UPDATE feedback SET theme_id=NULL")
        for t in themes:
            con.execute(
                "INSERT OR REPLACE INTO theme(id, name, description, urgency, criticality, "
                "related_feedback_ids, accounts, confidence, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (t.id, t.name, t.description, t.urgency, t.criticality, json.dumps(t.related_feedback_ids),
                 json.dumps(t.accounts), t.confidence, t.updated_at or now),
            )
            con.executemany(
                "UPDATE feedback SET theme_id=? WHERE id=? AND theme_id IS NULL",
                [(t.id, fid) for fid in t.related_feedback_ids],
            )


def list_themes() -> list[Theme]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM theme ORDER BY urgency DESC, criticality DESC, name").fetchall()
        return [_theme(r) for r in rows]


def count_feedback() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]


def count_themes() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM theme").fetchone()[0]
