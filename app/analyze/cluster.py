"""Group classified feedback into themes (contract in CLAUDE.md).

Two call styles are accepted so pipeline.py can use either one.
group(pool) buckets Classification objects by theme label.
build_themes(groups, by_id) takes those buckets plus a Feedback lookup.
build_themes(feedback, labels) takes Feedback rows plus an id to label map.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Union

from app.analyze import scoring
from app.models import Classification, Feedback, Theme, now_iso


def _slug(label: str) -> str:
    return "theme-" + (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "other")


def group(pool: list[Classification]) -> dict[str, list[Classification]]:
    groups: dict[str, list[Classification]] = defaultdict(list)
    for c in pool:
        groups[c.theme_label or "Other feedback"].append(c)
    return dict(groups)


def _rows(first: Union[dict, list], second: Any) -> dict[str, list[tuple[str, str, float, str]]]:
    """Normalise both call styles to label -> [(id, account, urgency, sentiment)]."""
    out: dict[str, list[tuple[str, str, float, str]]] = defaultdict(list)
    if isinstance(first, dict):
        for label, items in first.items():
            for c in items:
                fb = second.get(c.id)
                if fb is not None:
                    out[label or "Other feedback"].append((c.id, fb.account, c.urgency, c.sentiment or "neutral"))
    else:
        for f in first:
            out[second.get(f.id) or "Other feedback"].append((f.id, f.account, f.urgency, f.sentiment or "neutral"))
    return out


def build_themes(first: Union[dict, list], second: Any) -> list[Theme]:
    """One theme per label. Urgency and criticality come from scoring.py, confidence from the stronger of the two."""
    now, themes, used = now_iso(), [], set()
    for label, items in _rows(first, second).items():
        accounts = sorted({a for _, a, _, _ in items})
        urgency = scoring.theme_urgency(u for _, _, u, _ in items)
        criticality = scoring.theme_criticality(len(accounts), len(items))
        negative = sum(1 for *_, s in items if s == "negative")
        tid, n = _slug(label), 2
        while tid in used:
            tid, n = f"{_slug(label)}-{n}", n + 1
        used.add(tid)
        themes.append(Theme(
            id=tid, name=label,
            description=f"{len(items)} items from {len(accounts)} accounts, {negative} negative",
            urgency=round(urgency, 2), criticality=round(criticality, 2),
            related_feedback_ids=[i for i, _, _, _ in sorted(items, key=lambda r: (-r[2], r[0]))],
            accounts=accounts, confidence=scoring.confidence_label(max(urgency, criticality)), updated_at=now))
    themes.sort(key=lambda t: (-t.urgency, -t.criticality, t.name))
    return themes
