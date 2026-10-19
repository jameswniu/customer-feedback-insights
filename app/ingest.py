"""Load feedback from JSON, JSONL or CSV into FeedbackIn, tolerant of header aliases."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from app.models import FeedbackIn

ALIASES = {
    "text": ("text", "body", "comment", "comments", "message", "feedback", "content", "description", "review", "verbatim"),
    "account": ("account", "customer", "company", "client", "org", "organization", "account_name", "customer_name", "company_name"),
    "source": ("source", "channel", "origin", "medium", "platform"),
    "timestamp": ("timestamp", "date", "created_at", "created", "time", "datetime", "submitted_at", "received_at", "created_on"),
    "id": ("id", "feedback_id", "ticket_id", "uuid", "ref"),
}
LIST_KEYS = ("feedback", "items", "data", "rows", "records", "results", "entries")
DATE_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S")


def _key(k: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(k).strip().lower())


def norm_timestamp(v: Any) -> Optional[str]:
    """Naive UTC ISO 8601 string, or the raw string when it cannot be parsed."""
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(secs, timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    s = str(v).strip()
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in DATE_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return s
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


LINE_KEY = "__line__"


def _csv_rows(text: str, delim: str, offset: int = 0) -> list[dict]:
    """CSV records as dicts, each tagged with the physical line it starts on (the header is line 1).

    `offset` counts blank lines that were stripped off the top of the file before parsing.
    """
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = next(reader, None)
    if header is None:
        return []
    out: list[dict] = []
    prev = reader.line_num
    for rec in reader:
        start, prev = prev + 1, reader.line_num
        if not rec:
            continue
        row = dict(zip(header, rec))
        row[LINE_KEY] = start + offset
        out.append(row)
    return out


def parse_content(content: str, suffix: str = "") -> list[dict]:
    """Raw rows from JSON (list or {feedback: [...]}), JSONL or CSV/TSV text."""
    s = content.strip()
    if suffix in (".csv", ".tsv") or (suffix not in (".json", ".jsonl", ".ndjson") and s[:1] not in ("[", "{", "")):
        delim = "\t" if suffix == ".tsv" else ","
        if suffix not in (".csv", ".tsv"):
            try:
                delim = csv.Sniffer().sniff(s[:2048], delimiters=",;\t|").delimiter
            except csv.Error:
                pass
        return _csv_rows(s, delim, content[: len(content) - len(content.lstrip())].count("\n"))
    try:
        data = json.loads(s)
    except ValueError:
        data = [json.loads(line) for line in s.splitlines() if line.strip()]
    if isinstance(data, dict):
        for k in LIST_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
        lists = [v for v in data.values() if isinstance(v, list)]
        return lists[0] if lists else [data]
    return data if isinstance(data, list) else []


def read_rows(path: Union[str, Path]) -> list[dict]:
    p = Path(path).expanduser()
    return parse_content(p.read_text(encoding="utf-8-sig"), p.suffix.lower())


def parse_rows(rows: list[dict], skipped: Optional[list[tuple[str, int, str]]] = None) -> list[FeedbackIn]:
    """Map header aliases onto FeedbackIn. Rows without text are skipped.

    Pass a list as `skipped` to learn which. It gets (kind, number, reason) per skipped row, with kind
    "line" for CSV (the physical line, header is 1) and "row" for JSON (position in the list, from 1).
    """
    out: list[FeedbackIn] = []
    for i, raw in enumerate(rows, 1):
        where = ("line", raw[LINE_KEY]) if isinstance(raw, dict) and LINE_KEY in raw else ("row", i)
        if not isinstance(raw, dict):
            if skipped is not None:
                skipped.append((*where, "not an object"))
            continue
        row = {_key(k): v for k, v in raw.items() if k != LINE_KEY}

        def pick(field: str) -> Any:
            for alias in ALIASES[field]:
                v = row.get(alias)
                if v is not None and str(v).strip() != "":
                    return v
            return None

        text = pick("text")
        if text is None:
            if skipped is not None:
                skipped.append((*where, "no text"))
            continue
        out.append(FeedbackIn(
            id=pick("id"),
            source=str(pick("source") or "unknown"),
            account=str(pick("account") or "unknown"),
            text=str(text),
            timestamp=norm_timestamp(pick("timestamp")),
        ))
    return out


def load_feedback(path: Union[str, Path]) -> list[FeedbackIn]:
    return parse_rows(read_rows(path))
