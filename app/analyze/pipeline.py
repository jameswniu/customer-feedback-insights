"""Analysis pipeline: a sync generator of Events (start, load, classify, cluster, score, verify, persist, answer, done)."""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Iterator

from app import db
from app.analyze import cluster as clustering
from app.analyze import nlp, scoring
from app.models import Classification, Event, Feedback

log = logging.getLogger("app.pipeline")
BATCH = 20
LAST_RUN: dict = {}


def _norm(s) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("’", "'")).strip().casefold()


def quote_ok(quote: str, text: str) -> bool:
    q = _norm(quote)
    return bool(q) and q in _norm(text)


def deadline_ok(deadline, quote: str) -> bool:
    return not deadline or _norm(deadline) in _norm(quote)


def _stored(fb: Feedback) -> Classification:
    """Rebuild a Classification for a row analyzed in an earlier run (sentiment, tags, urgency, deadline are stored)."""
    tags = list(fb.tags) or ["other"]
    return Classification(id=fb.id, sentiment=fb.sentiment or "neutral", tags=tags, urgency=fb.urgency or 0.0,
                          deadline=fb.deadline, theme_label=nlp.topic_label(fb.text, tags),
                          evidence_quote=nlp.pick_quote(fb.text, nlp.anchor_span(fb.text, fb.deadline)))


def _themes(items: list[Classification], by_id: dict[str, Feedback]):
    return clustering.build_themes(clustering.group(items), by_id)


def run_analysis(mode: str = "new") -> Iterator[Event]:
    t0 = step_t = time.perf_counter()
    run_id = "run-" + uuid.uuid4().hex[:8]
    scope = mode if mode in ("new", "all") else "new"
    engine = "llm" if nlp.llm_enabled() else "offline"
    nlp.reset_stats()

    def ev(step: str, message: str, **data) -> Event:
        nonlocal step_t
        now = time.perf_counter()
        ms, step_t = int((now - step_t) * 1000), now
        log.info(message, extra={"step": step, "duration_ms": ms})
        return Event.of(step, message, **data)

    def done(analyzed: int, n_themes: int, dropped: int) -> Event:
        dur = round(time.perf_counter() - t0, 3)
        used = nlp.engine_used()
        LAST_RUN.update(run_id=run_id, mode=used, scope=scope, analyzed=analyzed, themes=n_themes, dropped_unverified=dropped)
        return ev("done", f"Done in {dur}s: {analyzed} analyzed, {n_themes} themes, {dropped} dropped unverified",
                  run_id=run_id, analyzed=analyzed, themes=n_themes, dropped_unverified=dropped, duration_s=dur, mode=used,
                  fallback_batches=nlp.STATS["fallback_batches"])

    try:
        yield ev("start", f"Run {run_id} started: scope={scope}, engine={engine}", run_id=run_id, scope=scope, mode=engine)
        db.init_db()
        everything = db.list_feedback(limit=100000)
        by_id = {f.id: f for f in everything}
        target = [f for f in everything if scope == "all" or not f.analyzed_at]
        yield ev("load", f"Loaded {len(everything)} feedback rows, {len(target)} to analyze", total=len(everything), to_analyze=len(target))

        if not target and (db.list_themes() or not everything):
            yield ev("answer", "Nothing new to analyze; existing themes are current", urgent=0)
            yield done(0, len(db.list_themes()), 0)
            return

        classified: list[Classification] = []
        for i in range(0, len(target), BATCH):
            classified += nlp.classify_batch(target[i:i + BATCH])
            yield ev("classify", f"Classified {min(i + BATCH, len(target))}/{len(target)} items via {nlp.engine_used()}",
                     done=min(i + BATCH, len(target)), total=len(target))
        have = {c.id for c in classified}
        classified += [nlp.classify_one(f) for f in target if f.id not in have]
        fresh = {f.id for f in target}
        pool = classified + [_stored(f) for f in everything if f.id not in fresh]

        labels = sorted({c.theme_label for c in pool})
        yield ev("cluster", f"Grouped {len(pool)} items into {len(labels)} candidate themes", themes=len(labels), labels=labels[:6])

        themes = _themes(pool, by_id)
        n_urgent = sum(1 for c in pool if c.urgency >= scoring.URGENT_T)
        yield ev("score", f"Scored: {n_urgent} urgent items, "
                 f"{sum(t.urgency >= scoring.URGENT_T for t in themes)} urgent themes, "
                 f"{sum(t.criticality >= scoring.CRITICAL_T for t in themes)} critical themes",
                 urgent_items=n_urgent, urgent_themes=sum(t.urgency >= scoring.URGENT_T for t in themes),
                 critical_themes=sum(t.criticality >= scoring.CRITICAL_T for t in themes))

        bad = {c.id for c in pool if c.id not in by_id or not quote_ok(c.evidence_quote, by_id[c.id].text)
               or not deadline_ok(c.deadline, c.evidence_quote)}
        if bad:
            for c in pool:
                if c.id in bad:
                    c.evidence_quote, c.deadline, c.urgency = "", None, 0.0
            themes = _themes([c for c in pool if c.id not in bad], by_id)
        dropped = len(bad) + nlp.STATS["dropped_unverified"]  # plus model quotes and deadlines that failed the check inside nlp
        yield ev("verify", f"Verified {len(pool) - len(bad)}/{len(pool)} evidence quotes as verbatim; dropped {dropped} unverifiable",
                 verified=len(pool) - len(bad), dropped_unverified=dropped)

        # Themes first. If the second write fails the rows stay unanalyzed and the next run redoes them,
        # instead of being marked current under stale themes.
        db.save_themes(themes)
        db.save_classifications(classified)
        yield ev("persist", f"Saved {len(classified)} classifications and {len(themes)} themes", classifications=len(classified), themes=len(themes))

        urgent = sorted((c for c in pool if c.id not in bad and c.urgency >= scoring.URGENT_T), key=lambda c: -c.urgency)
        top = [{"feedback_id": c.id, "account": by_id[c.id].account, "deadline": c.deadline, "quote": c.evidence_quote[:140]} for c in urgent[:5]]
        lead = f"; top theme: {themes[0].name} ({themes[0].confidence} confidence)" if themes else ""
        yield ev("answer", f"{len(urgent)} urgent items across {len({by_id[c.id].account for c in urgent})} accounts{lead}",
                 urgent=len(urgent), top=top, themes=[t.name for t in themes[:5]])
        yield done(len(classified), len(themes), dropped)
    except Exception as exc:  # surfaced as an error event; main.py turns it into a 500 on the JSON path
        log.exception("analysis failed")
        yield ev("error", f"Analysis failed: {exc}")
