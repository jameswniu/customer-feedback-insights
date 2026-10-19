"""Offline recall-first classifier. Quotes are sliced from the original text, so they are verbatim by construction."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from app.analyze import scoring
from app.models import SENTIMENTS, Classification
from app.models import TAGS as TAXONOMY

log = logging.getLogger("app.nlp")

I = re.I
NEG = re.compile(r"\b(?:broken|bugs?|crash\w*|errors?|fail\w*|slow\w*|lag\w*|time[- ]?outs?|unusable|terrible|awful|horrible|worst|frustrat\w*|annoy\w*|disappoint\w*|unacceptable|ridiculous|useless|confus\w*|cannot|can't|unable|doesn't|not working|won't|issues?|problems?|outage|down|blocked|blocker|stuck|lost|missing|complain\w*|angry|upset|unhappy|cancel\w*|churn\w*|leaving|competitor\w*|refund\w*|overcharg\w*|breach|leak\w*|escalat\w*|hate|poor|degrad\w*|downtime|hang\w*|freez\w*|regress\w*|wrong|incorrect|ignored)\b", I)
POS = re.compile(r"\b(?:love\w*|great|awesome|excellent|amazing|fantastic|helpful|thank\w*|happy|delighted|impressed|easy|smooth|intuitive|perfect|good|nice|appreciate\w*|wonderful|best|pleased)\b", I)
BLOCK = re.compile(r"\b(?:blocked|blocker|blocking|outage|(?:is|are|was|still|went|completely|totally) down|can't (?:log ?in|access|use|work|process|ship|export|sign)|cannot (?:log ?in|access|use|work|process|ship|export|sign)|unable to|stuck|stopped working|not working|prod(?:uction)? (?:is )?down|p0|p1|sev ?[01]|urgent\w*|asap|immediately|emergency|show-?stopper|data loss|lost data|cancel\w*|terminat\w*|escalat\w*|churn\w*|switch\w* to|leaving|competitor\w*|alternatives?|lawsuit|legal|ceo|cto)\b", I)
FRUS = re.compile(r"\b(?:frustrat\w*|annoy\w*|disappoint\w*|unacceptable|ridiculous|terrible|awful|worst|fed up|sick of|tired of|again and again|still waiting|no response|repeatedly|multiple times|every time|constantly|unhappy|upset|angry|furious|hate|useless)\b", I)

TAGS = {t: re.compile(p, I) for t, p in {
    "bug": r"\b(?:bugs?|broken|crash\w*|errors?|fail\w*|glitch\w*|defect\w*|regress\w*|exception|doesn't work|not working|stopped working|won't (?:load|work|save|open)|wrong|incorrect|outage|(?:still|is|are|was|went) down|stuck|blocked|blocker|corrupt\w*|data loss|lost data|duplicate\w*)\b",
    "performance": r"\b(?:slow\w*|lag\w*|latency|time[- ]?outs?|timing out|takes (?:forever|minutes|ages|too long)|loading|performance|sluggish|spinner|freez\w*|hang\w*)\b",
    "usability": r"\b(?:confus\w*|unintuitive|clunky|cluttered|hard to|difficult|can't find|too many clicks|ux|ui|navigation|not obvious|usability|awkward|tedious)\b",
    "feature_request": r"\b(?:feature requests?|would (?:be great|love|like|be nice)|wish|please add|can you add|ability to|option to|add support|support for|roadmap|we need|missing feature|nice to have)\b",
    "integration": r"\b(?:integrat\w*|api|webhooks?|salesforce|hubspot|slack|jira|zapier|sso|saml|okta|connector|sync\w*|import\w*|export\w*|csv|sftp|oauth|third-party)\b",
    "security": r"\b(?:security|vulnerab\w*|breach\w*|leak\w*|permissions?|unauthori[sz]ed|gdpr|soc ?2|compliance|pii|encrypt\w*|pen ?test|audit\w*|2fa|mfa|access control|hipaa)\b",
    "billing": r"\b(?:billing|invoice\w*|charge[ds]?|overcharg\w*|refund\w*|payment\w*|pricing|price|credit card|per seat|expensive|discount\w*)\b",
    "onboarding": r"\b(?:onboard\w*|getting started|set ?up|training|implementation|kick-?off|first week|new users?|documentation|docs|tutorial|learning curve|go[- ]?live)\b",
    "support_quality": r"\b(?:support|tickets?|no (?:response|reply)|unresponsive|still waiting|response time|csm|account manager|never heard|ignored|nobody|no one)\b",
    "churn_risk": r"\b(?:cancel\w*|churn\w*|leav(?:e|ing)|switch\w*|competitor\w*|alternatives?|not renew\w*|won't renew|terminat\w*|stop using|move to|replace\w*|walk away|look(?:ing)? elsewhere)\b",
    "renewal": r"\b(?:renew\w*|contract (?:end|expir)\w*|expir\w*|term ends|up for renewal|annual contract|subscription ends)\b",
}.items()}

TOPICS = [(n, re.compile(p, I)) for n, p in [
    ("Login & authentication", r"\b(?:log ?in|sign ?in|sso|saml|okta|password|2fa|mfa|authenticat\w*|oauth)\b"),
    ("Reports & exports", r"\b(?:export\w*|reports?|reporting|csv|pdf|dashboards?|analytics|charts?)\b"),
    ("Billing & invoices", r"\b(?:billing|invoice\w*|charged|overcharg\w*|refund\w*|payment\w*|pricing)\b"),
    ("API & integrations", r"\b(?:api|webhooks?|integrat\w*|salesforce|hubspot|slack|jira|zapier|connector|sftp)\b"),
    ("Performance & speed", r"\b(?:slow\w*|lag\w*|latency|time[- ]?outs?|performance|loading|freez\w*|sluggish)\b"),
    ("Onboarding & setup", r"\b(?:onboard\w*|set ?up|getting started|training|implementation|kick-?off|tutorial|documentation|docs)\b"),
    ("Mobile app", r"\b(?:mobile|ios|android|iphone)\b"),
    ("Notifications & email", r"\b(?:notifications?|alerts?|emails?)\b"),
    ("Search", r"\bsearch\w*\b"),
    ("Data import & sync", r"\b(?:import\w*|sync\w*|upload\w*|migrat\w*)\b"),
    ("Permissions & security", r"\b(?:permissions?|roles?|access control|security|vulnerab\w*|breach|gdpr|soc ?2|compliance|audit|encrypt\w*)\b"),
    ("Support responsiveness", r"\b(?:support|tickets?|response time|csm|account manager)\b"),
    ("Renewal & contract risk", r"\b(?:renew\w*|contract|expir\w*|cancel\w*|churn\w*|competitor\w*|switch\w*)\b"),
]]
TAG_LABEL = {"bug": "Bugs & errors", "performance": "Performance & speed", "usability": "Usability", "feature_request": "Feature requests",
             "integration": "API & integrations", "security": "Permissions & security", "billing": "Billing & invoices", "onboarding": "Onboarding & setup",
             "support_quality": "Support responsiveness", "churn_risk": "Renewal & contract risk", "renewal": "Renewal & contract risk", "other": "Other feedback"}

MON = "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
MONF = "january|february|march|april|june|july|august|september|october|november|december"
DOW = "monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri"
CUE = re.compile(r"\b(?:by|before|until|till|due|deadline|renew\w*|expir\w*|launch\w*|go[- ]?live|going live|goes live|ends?|ending|contract|cut-?off|must|need\w*|have to|ready|demo|audit|board|cutover|migration|release|rollout|conference|event|kick-?off|trial|pilot|term|terminat\w*|cancel\w*|decision|decide|evaluat\w*|sign\w*)\b", I)
# (pattern, needs a deadline cue word in the same sentence)
DL = [(re.compile(p, I), cue) for p, cue in [
    (r"\b(?:by|before|until|till|due)\s+(?:the\s+)?(?:end of (?:the )?(?:day|week|month|quarter|year)|eod|eow|eom|eoq|tomorrow|tonight|today|next (?:week|month|quarter)|this (?:week|month|quarter)|(?:this |next |end of )?(?:" + DOW + r")|(?:early |mid |late |end of )?(?:" + MONF + r"))\b", False),
    (r"\b(?:this|next)\s+(?:" + DOW + r")\b", True),
    (r"\b(?:" + MON + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b", True),
    (r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", True),
    (r"\b(?:end of (?:the )?(?:day|week|month|quarter|year)|eod|eow|eom|eoq|tomorrow|tonight|next (?:week|month|quarter))\b", True),
    (r"\b(?:in|within)\s+(?:the next\s+)?(?:\d+|a|an|one|two|three|four|five|six|seven|ten|few)\s+(?:business\s+)?(?:days?|weeks?|months?)\b", True),
    (r"\b(?:\d+|one|two|three|four|five|six|seven|ten)\s+(?:days?|weeks?|months?)\s+(?:left|remaining|away|until|from now)\b", True),
    (r"\b(?:(?:end of|by|in)\s+)?Q[1-4](?:\s+\d{4})?\b", True),
    (r"\b(?:in|early|mid|late)\s+(?:" + MONF + r")\b", True),
]]
CALENDAR = (2, 3)  # DL entries that are concrete calendar dates (month day, ISO or numeric)
NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "few": 3}


def _g(item, key):
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _sentences(text: str):
    spans = []
    for m in re.finditer(r"[^.!?\n]+(?:[.!?]+|$)", text):
        seg = text[m.start():m.end()]
        s, e = m.start() + len(seg) - len(seg.lstrip()), m.end() - (len(seg) - len(seg.rstrip()))
        if e > s:
            spans.append((s, e))
    return spans or [(0, len(text))]


def _base(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d.date()
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).date()


def _resolve(phrase: str, base: date):
    """Days from the feedback date to the phrase, or None when it cannot be pinned down."""
    p = phrase.lower()
    try:
        m = re.search(r"\b(" + MON + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", p)
        if m:
            mo = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)[:3]) + 1
            d = date(int(m.group(3) or base.year), mo, int(m.group(2)))
            if not m.group(3) and (d - base).days < -30:
                d = date(d.year + 1, mo, d.day)
            return (d - base).days
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", p)
        if m:
            return (date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - base).days
        m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", p)
        if m:
            yr = int(m.group(3) or base.year)
            d = date(yr + 2000 if yr < 100 else yr, int(m.group(1)), int(m.group(2)))
            if not m.group(3) and (d - base).days < -30:
                d = date(d.year + 1, d.month, d.day)
            return (d - base).days
        m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|ten|few)\s+(?:business\s+)?(day|week|month)", p)
        if m:
            return int(NUM.get(m.group(1)) or m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        if re.search(r"\b(?:eod|today|tonight)\b|end of (?:the )?day", p):
            return 0
        if "tomorrow" in p:
            return 1
        m = re.search(r"\b(" + DOW + r")\b", p)
        if m:
            wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(m.group(1)[:3])
            return (wd - base.weekday()) % 7 or (7 if "next" in p else 0)
        if re.search(r"end of (?:the )?week|\beow\b|this week", p):
            return (4 - base.weekday()) % 7
        if re.search(r"end of (?:the )?month|\beom\b|this month", p):
            nxt = date(base.year + (base.month == 12), base.month % 12 + 1, 1)
            return (nxt - timedelta(days=1) - base).days
        if "next week" in p:
            return 7
        if "next month" in p:
            return 30
        m = re.search(r"q([1-4])", p)
        if m:
            q = int(m.group(1))
            end = date(base.year + (q == 4), q * 3 % 12 + 1, 1) - timedelta(days=1)
            return (end - base).days
        m = re.search(r"(" + MONF + r")", p)
        if m:
            mo = [x for x in MONF.split("|")].index(m.group(1)) + 1
            d = date(base.year, mo, 1)
            if (d - base).days < -30:
                d = date(base.year + 1, mo, 1)
            return (d - base).days
    except (ValueError, IndexError):
        return None
    return None


def find_deadline(text: str, ts=None):
    """(phrase, (start, end), days) for the most urgent real time phrase, else None. Never invents one."""
    low = text.replace("’", "'")
    base, sents, found = _base(ts), _sentences(low), []
    for idx, (rx, needs_cue) in enumerate(DL):
        for m in rx.finditer(low):
            sent = next((low[s:e] for s, e in sents if s <= m.start() < e), low)
            days = _resolve(m.group(0), base)
            # recall-first: a concrete calendar date within ~4 months counts even with no cue word ("closes the books on October 21")
            if needs_cue and not CUE.search(sent) and not (idx in CALENDAR and days is not None and 0 <= days <= 120):
                continue
            found.append((m.group(0), (m.start(), m.end()), days))
    if not found:
        return None
    dated = [f for f in found if f[2] is not None]
    upcoming = [f for f in dated if f[2] >= 0]
    if upcoming:
        return min(upcoming, key=lambda f: f[2])
    return max(dated, key=lambda f: f[2]) if dated else found[0]


def pick_quote(text: str, span=None, max_len: int = 220) -> str:
    """Verbatim slice of `text`: the sentence around `span`, else the first sentence."""
    spans = _sentences(text)
    s, e = next(((a, b) for a, b in spans if span and a <= span[0] < b), spans[0])
    if span and span[1] > e:
        s, e = max(0, span[0] - 80), min(len(text), span[1] + 80)
    if e - s > max_len:
        s = max(s, (span[0] if span else s) - 60)
        e = s + max_len
    return text[s:e].strip() or text[:max_len].strip()


def anchor_span(text: str, deadline=None):
    """Where the evidence quote should sit: the deadline phrase, else the strongest signal word."""
    low = text.replace("’", "'")
    if deadline:
        i = low.lower().find(str(deadline).lower())
        if i >= 0:
            return (i, i + len(deadline))
    for rx in (BLOCK, FRUS, NEG):
        m = rx.search(low)
        if m:
            return m.span()
    return None


def topic_label(text: str, tags) -> str:
    low = text.replace("’", "'")
    for name, rx in TOPICS:
        if rx.search(low):
            return name
    return TAG_LABEL.get(tags[0] if tags else "other", "Other feedback")


def classify_one(item) -> Classification:
    text = _g(item, "text") or ""
    low = text.replace("’", "'")
    dl = find_deadline(text, _g(item, "timestamp"))
    neg, pos = len(NEG.findall(low)), len(POS.findall(low))
    sentiment = "negative" if neg and neg >= pos else "positive" if pos else "neutral"
    tags = [t for t, rx in TAGS.items() if rx.search(low)] or ["other"]
    urgency = scoring.feedback_urgency(dl[2] if dl else None, bool(dl), bool(BLOCK.search(low)), bool(FRUS.search(low)))
    deadline = text[dl[1][0]:dl[1][1]] if dl else None
    quote = pick_quote(text, dl[1] if dl else anchor_span(text))
    return Classification(id=str(_g(item, "id")), sentiment=sentiment, tags=tags, urgency=urgency, deadline=deadline,
                          theme_label=topic_label(text, tags), evidence_quote=quote)


LLM_BATCH = 20
LLM_TIMEOUT = 20.0
API_URL = "https://api.anthropic.com/v1/messages"
LABELS = sorted({n for n, _ in TOPICS} | set(TAG_LABEL.values()))
SYSTEM = (
    "You classify customer feedback for internal Customer Success and Product teams. Reply with ONLY a JSON array, one object per input "
    "item, no prose and no code fences. Each object has these fields. id (copied from the input). sentiment (negative, neutral or positive). "
    f"tags (a list from exactly these: {', '.join(TAXONOMY)}). "
    "urgency (a number from 0 to 1: 1 for a deadline within 14 days of the item's timestamp, 0.8 for a later deadline, 0.7 for blocked, "
    "outage, cancel or escalate, 0.4 for frustration with no date, otherwise 0). "
    "deadline (the time phrase copied EXACTLY from the text, such as 'by Friday' or 'October 21', or null; never infer or invent one). "
    f"theme_label (exactly one of: {'; '.join(LABELS)}). "
    "evidence_quote (a passage copied EXACTLY, character for character, from that item's text, that best supports the urgency or theme; "
    "when there is a deadline the quote must contain it). Recall beats precision. A human reviews every result, so when unsure about "
    "urgency rate it higher."
)


def _settings():
    from app.config import get_settings
    return get_settings()


def llm_enabled() -> bool:
    """True only when a key is present and LLM_MODE is not offline."""
    try:
        v = _settings().llm_enabled
        return bool(v() if callable(v) else v)
    except Exception:
        return False


def effective_mode() -> str:
    """The engine that actually classifies. Report this, never the env flag, as the run mode (B3)."""
    return "llm" if llm_enabled() else "offline"


STATS = {"llm_batches": 0, "fallback_batches": 0}  # per process, reset by the caller at the start of a run


def reset_stats() -> None:
    STATS.update(llm_batches=0, fallback_batches=0)


def engine_used() -> str:
    """What actually classified since reset_stats(): llm only when at least one batch really came back from the model."""
    return "llm" if STATS["llm_batches"] else "offline"


_KEY_RX = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def _redact(text: str, key: str = "") -> str:
    """Strip the API key (the configured value and anything shaped like one) from text before it is logged."""
    if key:
        text = text.replace(key, "[redacted]")
    return _KEY_RX.sub("[redacted]", text)


def _nrm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("’", "'")).strip().casefold()


def _strip_fences(t: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.I).strip()


def _call_llm(batch, s) -> list:
    import httpx  # imported here so the offline path needs nothing extra
    payload = [{"id": str(_g(i, "id")), "timestamp": _g(i, "timestamp"), "account": _g(i, "account"), "source": _g(i, "source"),
                "text": _g(i, "text")} for i in batch]
    # thinking is off because it eats max_tokens and truncates the JSON on a full batch of 20 (seen live 10/19)
    content = json.dumps(payload, ensure_ascii=False)
    body = {"model": s.llm_model, "max_tokens": 8192, "system": SYSTEM, "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": content}]}
    if log.isEnabledFor(logging.DEBUG):
        log.debug("llm request model=%s batch=%d prompt=%s", s.llm_model, len(batch), _redact(f"{SYSTEM}\n---\n{content}", s.anthropic_api_key))
    t0 = time.perf_counter()
    r = httpx.post(API_URL, json=body, timeout=LLM_TIMEOUT, headers={
        "x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    r.raise_for_status()
    data = r.json()
    ms, usage = int((time.perf_counter() - t0) * 1000), data.get("usage") or {}
    log.info("llm classify model=%s latency_ms=%d input_tokens=%s output_tokens=%s items=%d", s.llm_model, ms,
             usage.get("input_tokens"), usage.get("output_tokens"), len(batch), extra={"step": "classify", "duration_ms": ms})
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if log.isEnabledFor(logging.DEBUG):  # redact first, then cut, so a key can never survive as a half-cut fragment
        log.debug("llm response model=%s text=%s", s.llm_model, _redact(text, s.anthropic_api_key)[:2000])
    obj = json.loads(_strip_fences(text))
    rows = obj if isinstance(obj, list) else (obj.get("items") or obj.get("results") or [])
    return [r for r in rows if isinstance(r, dict)]


def _coerce(raw: dict, item) -> Classification:
    """Validate one model row against the contract. Anything off falls back to the offline answer for that item."""
    text, base = _g(item, "text") or "", classify_one(item)
    quote = raw.get("evidence_quote")
    if not isinstance(quote, str) or not quote.strip() or _nrm(quote) not in _nrm(text):
        log.warning("llm quote for %s is not verbatim, using the offline answer for that item", base.id)
        return base
    deadline = raw.get("deadline")
    deadline = deadline.strip() if isinstance(deadline, str) and deadline.strip() else None
    if deadline and _nrm(deadline) not in _nrm(text):
        deadline = None  # never invent a deadline
    if deadline and _nrm(deadline) not in _nrm(quote):
        quote = base.evidence_quote if base.deadline and _nrm(deadline) in _nrm(base.evidence_quote) else quote
    try:
        u = float(raw.get("urgency") or 0)
    except (TypeError, ValueError):
        u = 0.0
    if deadline:
        days = _resolve(deadline, _base(_g(item, "timestamp")))
        urgency = 1.0 if (days is not None and days <= 14) else 0.8
    else:
        urgency = min((0.0, 0.4, 0.7), key=lambda v: abs(v - min(max(u, 0.0), 0.7)))
    sentiment = raw.get("sentiment") if raw.get("sentiment") in SENTIMENTS else base.sentiment
    tags = [t for t in (raw.get("tags") or []) if t in TAXONOMY] or base.tags
    label = next((x for x in LABELS if x.lower() == str(raw.get("theme_label", "")).strip().lower()), base.theme_label)
    out = Classification(id=base.id, sentiment=sentiment, tags=tags, urgency=urgency, deadline=deadline, theme_label=label, evidence_quote=quote)
    if base.urgency > out.urgency:  # recall first: never lose what the offline rules caught
        out.urgency, out.deadline, out.evidence_quote = base.urgency, base.deadline, base.evidence_quote
    return out


def _llm_batch(batch, s) -> list[Classification]:
    for attempt in (1, 2):
        try:
            rows = {str(r.get("id")): r for r in _call_llm(batch, s)}
            if not rows:
                raise ValueError("no usable rows in the model reply")
            out = [_coerce(rows[str(_g(i, "id"))], i) if str(_g(i, "id")) in rows else classify_one(i) for i in batch]
            STATS["llm_batches"] += 1
            return out
        except Exception as exc:
            log.warning("llm batch attempt %d failed: %s %s", attempt, type(exc).__name__, _redact(str(exc), s.anthropic_api_key)[:160])
    log.warning("llm batch fell back to the offline rules after one retry (%d items)", len(batch))
    STATS["fallback_batches"] += 1
    return [classify_one(i) for i in batch]


def classify_batch(items) -> list[Classification]:
    items = list(items)
    if not llm_enabled():
        return [classify_one(i) for i in items]
    s, out = _settings(), []
    for i in range(0, len(items), LLM_BATCH):
        out += _llm_batch(items[i:i + LLM_BATCH], s)
    return out
