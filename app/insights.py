"""Build Insights from stored feedback and themes (contract in CLAUDE.md).

Recall first, so anything plausibly urgent or critical is listed with a confidence label.
Every claim carries a quote that is a verbatim substring of the feedback text, checked here.
A claim whose quote fails the check is dropped and counted in dropped_unverified.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Optional

from app import db
from app.config import get_settings
from app.models import (TEAMS, AtRiskAccount, Evidence, Feedback, InsightTheme, Insights,
                        UrgentIssue, now_iso)

try:  # eng2 owns the constants, defaults here match CLAUDE.md
    from app.analyze.scoring import AT_RISK_T, CRITICAL_T, URGENT_T
except Exception:
    URGENT_T, CRITICAL_T, AT_RISK_T = 0.4, 0.35, 0.35

log = logging.getLogger("app.insights")

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_SIGNAL = re.compile(
    r"(deadline|renew|expir|cancel|churn|escalat|block|outage|down\b|urgent|asap|immediately|launch|go-live|"
    r"contract|by (mon|tue|wed|thu|fri|sat|sun)|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}|\d{1,2}/\d{1,2})",
    re.I,
)
_RANK = {"urgent+critical": 3, "urgent": 2, "critical": 1, "watch": 0}
_CACHE: dict = {}
_LOCK = threading.Lock()
_TTL_S = 300


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _quote(fb: Feedback) -> str:
    """Pick the sentence that carries the signal, preferring the stated deadline."""
    parts = [p.strip() for p in _SENTENCES.split(fb.text) if p.strip()]
    if not parts:
        return ""
    if fb.deadline:
        hit = next((p for p in parts if _norm(fb.deadline) in _norm(p)), None)
        if hit:
            return hit[:240]
    hit = next((p for p in parts if _SIGNAL.search(p)), parts[0])
    return hit[:240]


def _verified(quote: str, fb: Feedback) -> bool:
    return bool(quote) and _norm(quote) in _norm(fb.text)


def _evidence(fbs: list[Feedback], limit: int, dropped: list[int]) -> list[Evidence]:
    out: list[Evidence] = []
    for fb in sorted(fbs, key=lambda f: (-f.urgency, f.sentiment != "negative", f.id))[:limit * 2]:
        q = _quote(fb)
        if not _verified(q, fb):
            dropped[0] += 1
            continue
        out.append(Evidence(feedback_id=fb.id, account=fb.account, source=fb.source, quote=q))
        if len(out) == limit:
            break
    return out


def _priority(urgency: float, criticality: float) -> str:
    u, c = urgency >= URGENT_T, criticality >= CRITICAL_T
    return "urgent+critical" if u and c else "urgent" if u else "critical" if c else "watch"


def _tags(fb: Feedback) -> set[str]:
    return set(fb.tags) or {"other"}


def _in_team(fb: Feedback, team: Optional[str]) -> bool:
    return team is None or bool(_tags(fb) & TEAMS[team])


def _engine() -> str:
    """The engine that really produced the last analysis, so a rejected key never shows as llm."""
    try:
        from app.analyze.pipeline import LAST_RUN
        return LAST_RUN.get("mode") or get_settings().mode
    except Exception:
        return get_settings().mode


def _signature() -> tuple:
    themes = db.list_themes()
    analyzed = [f.analyzed_at for f in db.list_feedback(limit=10000) if f.analyzed_at]
    return (len(themes), max((t.updated_at or "" for t in themes), default=""), db.count_feedback(),
            len(analyzed), max(analyzed, default=""))


def build_insights(team: Optional[str] = None) -> Insights:
    sig = _signature()
    key = (team, sig)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL_S:
            log.info("insights cache hit team=%s", team, extra={"step": "insights"})
            return hit[1]
    t0 = time.perf_counter()
    dropped = [0]
    feedback = [f for f in db.list_feedback(limit=10000) if f.analyzed_at]
    by_id = {f.id: f for f in feedback}
    scoped = [f for f in feedback if _in_team(f, team)]
    scoped_ids = {f.id for f in scoped}
    theme_of = {}

    themes: list[InsightTheme] = []
    for th in db.list_themes():
        members = [by_id[i] for i in th.related_feedback_ids if i in by_id and i in scoped_ids]
        if not members:
            continue
        for m in members:
            theme_of.setdefault(m.id, th.id)
        accounts = sorted({m.account for m in members})
        urgency = max(m.urgency for m in members)
        criticality = min(1.0, 0.6 * min(len(accounts) / 5, 1) + 0.4 * min(len(members) / 10, 1))
        themes.append(InsightTheme(
            id=th.id, name=th.name, description=th.description, urgency=round(urgency, 2),
            criticality=round(criticality, 2), related_feedback_ids=[m.id for m in members],
            accounts=accounts, confidence=th.confidence, updated_at=th.updated_at,
            priority=_priority(urgency, criticality), evidence=_evidence(members, 3, dropped),
        ))
    themes.sort(key=lambda t: (-_RANK[t.priority], -t.urgency, -t.criticality, t.name))

    issues: list[UrgentIssue] = []
    for fb in sorted(scoped, key=lambda f: (-f.urgency, f.timestamp or "")):
        if fb.urgency < URGENT_T:
            continue
        q = _quote(fb)
        if not _verified(q, fb):
            dropped[0] += 1
            continue
        conf = "high" if fb.deadline else "medium" if fb.urgency >= 0.7 else "low"
        issues.append(UrgentIssue(feedback_id=fb.id, account=fb.account, source=fb.source, quote=q,
                                  deadline=fb.deadline, theme_id=fb.theme_id or theme_of.get(fb.id), confidence=conf))

    at_risk: list[AtRiskAccount] = []
    for account in sorted({f.account for f in scoped}):
        items = [f for f in scoped if f.account == account]
        negative = sum(1 for f in items if f.sentiment == "negative")
        max_urg = max(f.urgency for f in items)
        n_themes = len({f.theme_id or theme_of.get(f.id) for f in items} - {None})
        risk = min(1.0, 0.4 * negative / len(items) + 0.4 * max_urg + 0.2 * min(n_themes / 3, 1))
        if risk < AT_RISK_T:
            continue
        reasons = [f"{negative} of {len(items)} items are negative"]
        dated = [f for f in items if f.deadline]
        if dated:
            reasons.append(f"deadline mentioned: {dated[0].deadline}")
        elif max_urg >= URGENT_T:
            reasons.append("blocking or cancellation language")
        if n_themes > 1:
            reasons.append(f"spans {n_themes} themes")
        at_risk.append(AtRiskAccount(
            account=account, risk=round(risk, 2), confidence="high" if risk >= 0.7 else "medium" if risk >= 0.5 else "low",
            reasons=reasons, evidence=_evidence(items, 2, dropped)))
    at_risk.sort(key=lambda a: (-a.risk, a.account))

    result = Insights(
        run_id=hashlib.sha1(repr(sig).encode()).hexdigest()[:8], generated_at=now_iso(), team=team,
        mode=_engine(), themes=themes, urgent_issues=issues, at_risk_accounts=at_risk,
        dropped_unverified=dropped[0])
    with _LOCK:
        _CACHE.clear()
        _CACHE[key] = (time.time(), result)
    log.info("insights built team=%s themes=%d urgent=%d at_risk=%d dropped=%d", team, len(themes), len(issues),
             len(at_risk), dropped[0], extra={"step": "insights", "duration_ms": round((time.perf_counter() - t0) * 1000)})
    return result
