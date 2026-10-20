"""Chat: answer a plain-sentence question from the analyzed insights, quoted and verbatim-checked (design rules 1 and 2).

No new analysis happens here. A keyword rule picks the intent, the answer is read from app.insights.build_insights and
app.db, and every quote is checked against its source text before it is shown. A claim whose quote fails is dropped
and counted in dropped_unverified. The raw question and the quotes are never logged.

A question that is about the feedback but that no rule can place gets a question back instead of a guess. The reply
lists three options, each one a question this module understands, so the client can send the one the user picks.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Iterator, Optional

from app import db
from app.analyze import nlp
from app.insights import build_insights
from app.models import Event

log = logging.getLogger("app.ask")

NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
TEAM_LABEL = {None: "all teams", "customer_success": "the customer success view", "product": "the product view"}
INTENT_LABEL = {
    "accounts": "at-risk accounts", "urgent": "urgent items", "escalate": "themes to escalate", "overview": "a team overview",
    "account_why": "why an account is at risk", "theme_evidence": "the evidence for a theme",
    "unknown_account": "an account I cannot find", "unknown_theme": "a theme I cannot find",
    "clarify": "I am not sure which of three things you mean",
}
HELP = ("I answer from the analyzed feedback only, with quotes. Try, Which accounts need attention today? What's urgent this week? "
        "What should we escalate? Why is <account> at risk? What's the evidence for the <theme> theme? "
        "Add customer success or product to scope any of them.")

TEAM_WORDS = {
    "customer_success": re.compile(r"\b(customer success|cs team|cs view|csms?|success team)\b"),
    "product": re.compile(r"\b(product view|product team|product managers?|pm view|pms|product)\b"),
}
ATTN = re.compile(r"\b(attention|at risk|risk|risky|churn\w*|unhappy|worried|trouble|health)\b")
ACCT_WORD = re.compile(r"\b(accounts?|customers?|clients?|companies|company|who)\b")
ESC = re.compile(r"\b(escalat\w*|themes?|top issues?|biggest|main problems?|priorit\w*|patterns?|trends?|what matters|focus)\b")
URGENT = re.compile(r"\b(urgent\w*|due|deadlines?|blocked|blocking|blockers?|outages?|down|asap|this week|today|tomorrow|soon|expir\w*|"
                    r"renew\w*|launch\w*|critical|fires?|on fire|time sensitive|overdue|lapsed|can t|cannot)\b")
BLOCKING = re.compile(r"\b(blocked|blocking|blockers?|outages?|down|can t|cannot)\b")
SUBJECT = re.compile(r"\b(accounts?|customers?|clients?|companies|company|who|feedback|tickets?|issues?|problems?|complain\w*|users?)\b")
NEED = re.compile(r"\b(need\w*|care|attention|look|focus|worr\w*|trouble|priorit\w*|watch|follow up|check on|reach out)\b")
TIMEWORD = re.compile(r"\b(days?|weeks?|months?|tomorrow|tonight|soon|deadlines?|overdue|next|coming)\b")
WINDOW = re.compile(r"\b(?:next|coming|following)\s+(?:\w+\s+)?(?:days?|weeks?|months?)\b|\b(?:in|within)\s+(?:\w+\s+)?(?:days?|weeks?|months?)\b")
HELP_ASK = re.compile(r"\b(help|what can you do|how (?:do|does) (?:this|it) work|commands?|usage|instructions?)\b")
EVID = re.compile(r"\b(evidence|quotes?|examples?|proof|supporting|what are (?:people|customers|users) saying|show me)\b")
WHY_NAME = re.compile(r"\bwhy\s+(?:is|are|does|do|did)\s+(.+?)\s+(?:at risk|risky|unhappy|on the list|flagged|in trouble|a risk|churning|upset)\b")
THEME_NAME = re.compile(r"\b(?:for|of|on|about)\s+(?:the\s+)?(.+?)\s+theme\b")
GENERIC = {"the", "inc", "corp", "corporation", "llc", "ltd", "co", "company", "group", "industries", "enterprises", "holdings",
           "bank", "health", "systems", "labs", "technologies", "tech", "test", "and"}
NOT_A_NAME = {"anyone", "anybody", "anything", "something", "this", "that", "it", "they", "the account", "an account", "my account",
              "our account", "the customer", "a customer", "so many accounts", "everyone", "everything"}


def _fold(s) -> str:
    """Lowercase, accent-free, punctuation-free, so Café, CAFE and cafe! all match."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _ws(s) -> str:
    return " ".join(str(s or "").replace("’", "'").lower().split())


def _count(n: int, one: str, many: str) -> str:
    return f"{(NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)).capitalize()} {one if n == 1 else many}"


STUCK = re.compile(r"\b(blocked|blocking|blockers?|outages?|down|can ?not|can't|unable|stuck|stopped working|not working|fail\w*|broken|locked out)\b", re.I)


def _scope(team: Optional[str]) -> str:
    return "across all teams" if team is None else f"in {TEAM_LABEL[team]}"


def _original(question: str, folded_name: str) -> str:
    """The name as the user typed it (case and punctuation kept), found by its folded words."""
    words = folded_name.split()
    m = re.search(r"[\W_]*".join(map(re.escape, words)), question, re.I) if words else None
    return m.group(0).strip() if m else folded_name


def _match_account(q: str, accounts: list[str]) -> Optional[str]:
    full = [a for a in accounts if re.search(rf"\b{re.escape(_fold(a))}\b", q)]
    if full:
        return max(full, key=lambda a: len(_fold(a)))
    words = set(q.split())
    part = sorted(a for a in accounts if any(t not in GENERIC and len(t) >= 3 and t in words for t in _fold(a).split()))
    return part[0] if part else None


def _match_theme(question: str, q: str, themes: list[str]) -> Optional[str]:
    for t in themes:
        if re.search(rf"\b{re.escape(_fold(t))}\b", q):
            return t
    label = nlp.topic_label(question, [])  # SSO, login, export and so on map to the same theme labels the analysis uses
    return next((t for t in themes if t.lower() == label.lower()), None) if label != "Other feedback" else None


def understand(question: str, accounts: list[str], themes: list[str]) -> dict:
    q = _fold(question)
    team = next((t for t, rx in TEAM_WORDS.items() if rx.search(q)), None)
    out = {"intent": "help", "team": team, "account": None, "theme": None, "name": None, "focus": None}
    if not q:
        return out
    acct = _match_account(q, accounts)
    if acct:
        return {**out, "intent": "account_why", "account": acct}
    why = WHY_NAME.search(q)
    if why and why.group(1) not in NOT_A_NAME:
        return {**out, "intent": "unknown_account", "name": _original(question, why.group(1))}
    theme, evid = _match_theme(question, q, themes), EVID.search(q)
    if theme and (evid or re.search(r"\btheme\b", q)):
        return {**out, "intent": "theme_evidence", "theme": theme}
    named = THEME_NAME.search(q)
    if evid and named:
        return {**out, "intent": "unknown_theme", "name": _original(question, named.group(1))}
    if ATTN.search(q) or (ACCT_WORD.search(q) and re.search(r"\bescalat\w*", q)):
        return {**out, "intent": "accounts"}
    if ESC.search(q):
        return {**out, "intent": "escalate"}
    if URGENT.search(q):
        return {**out, "intent": "urgent", "focus": "blocked" if BLOCKING.search(q) else None}
    if team:
        return {**out, "intent": "overview"}
    if not HELP_ASK.search(q) and (SUBJECT.search(q) or NEED.search(q)):
        return {**out, "intent": "clarify"}  # about the feedback, but no rule can place it, so ask instead of guessing
    return out


# ---- the question back. Each option is a question understand() maps to a real intent, so a pick always gets an answer. ----

OPTIONS = {
    "accounts": ("Accounts at risk, most at risk first", "Which accounts need attention today?"),
    "urgent": ("Urgent items, each with its deadline", "What's urgent this week?"),
    "escalate": ("Themes to escalate, highest priority first", "What should we escalate?"),
}


def _clarify_options(q: str) -> list[dict]:
    """The three things the chat can answer, the likeliest first. A team word never gets here, it already picked the overview."""
    score = {"accounts": 0, "urgent": 0, "escalate": 0}
    if ACCT_WORD.search(q) or NEED.search(q):
        score["accounts"] += 1
    if TIMEWORD.search(q) or URGENT.search(q):
        score["urgent"] += 1
    if re.search(r"\b(should|first|fix|work on|top)\b", q):
        score["escalate"] += 1
    order = sorted(score, key=lambda k: (-score[k], list(OPTIONS).index(k)))
    return [{"label": OPTIONS[k][0], "question": OPTIONS[k][1]} for k in order]


def _clarify_text(options: list[dict], windowed: bool) -> str:
    lines = ["I am not sure which of these you mean."]
    if windowed:
        lines.append("I cannot filter by a date range yet, so the urgent items show each deadline for you to read.")
    lines += [f"{n}. {o['label']}" for n, o in enumerate(options, 1)]
    lines.append("Type 1, 2 or 3, or ask it another way.")
    return "\n".join(lines)


# ---- building the groups. A group is one claim line, its items are the quotes that back it. ----

def _item(kind: str, feedback_id: str, account: str, quote: str, source: str = "", **extra) -> dict:
    return {"kind": kind, "feedback_id": feedback_id, "account": account, "quote": quote, "source": source, **extra}


def _urgent_groups(ins, focus: Optional[str] = None) -> list[dict]:
    issues = list(ins.urgent_issues)
    if focus == "blocked":
        issues.sort(key=lambda u: 0 if STUCK.search(u.quote) else 1)  # stable, so blocked items lead and nothing is dropped
    groups = []
    for u in issues:
        when = f"deadline {u.deadline}" + (", already passed" if getattr(u, "lapsed", False) else "") if u.deadline else "no stated date"
        groups.append({"head": f"{u.account}, {when}, {u.confidence} confidence.", "items": [_item(
            "urgent", u.feedback_id, u.account, u.quote, u.source, deadline=u.deadline, confidence=u.confidence,
            lapsed=bool(getattr(u, "lapsed", False)), theme_id=u.theme_id)]})
    return groups


def _risk_groups(ins) -> list[dict]:
    return [{"head": f"{a.account}, risk {a.risk:.2f}, {a.confidence} confidence. Why: {'; '.join(a.reasons)}.", "items": [
        _item("at_risk", e.feedback_id, e.account, e.quote, e.source, risk=a.risk, confidence=a.confidence, reasons=a.reasons)
        for e in a.evidence]} for a in ins.at_risk_accounts]


def _theme_head(t) -> str:
    names = ", ".join(t.accounts[:4]) + (", and more" if len(t.accounts) > 4 else "")
    return f"{t.name}, {t.priority}, {len(t.accounts)} account{'s' if len(t.accounts) != 1 else ''} ({names}), {t.confidence} confidence."


def _theme_groups(ins) -> list[dict]:
    picked = [t for t in ins.themes if t.priority != "watch"] or list(ins.themes[:3])  # nothing urgent or critical, show the top few anyway
    return [{"head": _theme_head(t), "items": [
        _item("theme", e.feedback_id, e.account, e.quote, e.source, theme=t.name, priority=t.priority, accounts=t.accounts,
              confidence=t.confidence) for e in t.evidence]} for t in picked]


def _account_groups(ins, account: str) -> list[dict]:
    groups, seen = [], set()
    for a in ins.at_risk_accounts:
        if a.account == account:
            groups.append({"head": f"{account} is at risk, risk {a.risk:.2f}, {a.confidence} confidence. Why: {'; '.join(a.reasons)}.",
                           "items": [_item("at_risk", e.feedback_id, e.account, e.quote, e.source, risk=a.risk, confidence=a.confidence,
                                           reasons=a.reasons) for e in a.evidence]})
            seen |= {e.feedback_id for e in a.evidence}
    for g in _urgent_groups(ins):
        if g["items"][0]["account"] == account and g["items"][0]["feedback_id"] not in seen:
            groups.append({"head": "Also urgent, " + g["head"][len(account) + 2:], "items": g["items"]})
    if not groups:  # recall first, so show whatever quotes the themes hold for this account
        for t in ins.themes:
            for e in t.evidence:
                if e.account == account and e.feedback_id not in seen:
                    seen.add(e.feedback_id)
                    groups.append({"head": f"In the {t.name} theme.", "items": [_item("theme", e.feedback_id, e.account, e.quote, e.source,
                                                                                       theme=t.name, priority=t.priority)]})
    return groups


def _evidence_groups(ins, theme: str) -> tuple[list[dict], str]:
    t = next((x for x in ins.themes if x.name == theme), None)
    if t is None:
        return [], f"{theme} has no items {_scope(ins.team)}."
    return ([{"head": f"{e.account}, via {e.source or 'unknown source'}.", "items": [_item(
        "theme", e.feedback_id, e.account, e.quote, e.source, theme=t.name, priority=t.priority, accounts=t.accounts, confidence=t.confidence)]}
        for e in t.evidence], f"Evidence for the {t.name} theme. {_theme_head(t)}")


# ---- verifying and writing ----

def _verify(sections: list[dict]) -> tuple[list[dict], int]:
    """Keep only quotes that are verbatim substrings of their feedback text. A group left with no quote is dropped whole."""
    ids = sorted({it["feedback_id"] for s in sections for g in s["groups"] for it in g["items"]})
    texts = {f.id: _ws(f.text) for f in db.get_feedback(ids)} if ids else {}
    dropped = 0
    for s in sections:
        kept = []
        for g in s["groups"]:
            good = [it for it in g["items"] if it["quote"] and _ws(it["quote"]) in texts.get(it["feedback_id"], "\0")]
            dropped += len(g["items"]) - len(good)
            if good:
                kept.append({**g, "items": good})
        s["groups"] = kept
    return sections, dropped


def _render(sections: list[dict]) -> str:
    lines = []
    for s in sections:
        if s["groups"] and s.get("title"):
            lines.append(f"{s['title']} ({len(s['groups'])}):")
        for n, g in enumerate(s["groups"], 1):
            quotes = " ".join(f'"{it["quote"]}" [{it["feedback_id"]}]' for it in g["items"])
            lines.append(f"{n}. {g['head']} {quotes}")
    return "\n".join(lines)


def _answer(info: dict, ins, names: dict) -> tuple[str, list[dict], str]:
    """Return the header line, the sections, and the empty-result sentence."""
    intent, scope = info["intent"], _scope(info["team"])
    if intent == "accounts":
        return "{n} at risk " + scope + ", most at risk first.", [{"groups": _risk_groups(ins)}], f"No account is at risk {scope} right now."
    if intent == "urgent":
        order = ", the ones that read as blocked or stuck first, then the rest." if info["focus"] == "blocked" else ", most urgent first."
        return "{n} " + scope + order, [{"groups": _urgent_groups(ins, info["focus"])}], f"Nothing urgent {scope} right now."
    if intent == "escalate":
        return "{n} " + scope + ", highest priority first.", [{"groups": _theme_groups(ins)}], f"No themes {scope} yet."
    if intent == "overview":
        return f"Overview {scope}.", [{"title": "Urgent", "groups": _urgent_groups(ins)}, {"title": "At risk", "groups": _risk_groups(ins)},
                                      {"title": "Themes", "groups": _theme_groups(ins)}], f"Nothing to show {scope}."
    if intent == "account_why":
        return f"What the analysis says about {info['account']} {scope}.", [{"groups": _account_groups(ins, info["account"])}], \
            f"{info['account']} is in the data but is not at risk and has nothing urgent {scope}."
    groups, head = _evidence_groups(ins, info["theme"])
    return head, [{"groups": groups}], f"No quotes for the {info['theme']} theme {scope}."


def _plural_header(header: str, n: int, intent: str) -> str:
    if "{n}" not in header:
        return header
    unit = {"accounts": ("account", "accounts"), "urgent": ("urgent item", "urgent items"), "escalate": ("theme to escalate", "themes to escalate")}[intent]
    return header.replace("{n}", _count(n, *unit), 1)


def answer_question(question: str) -> Iterator[Event]:
    t0 = step_t = time.perf_counter()

    def ev(step: str, message: str, **data) -> Event:
        nonlocal step_t
        now = time.perf_counter()
        ms, step_t = int((now - step_t) * 1000), now
        log.info("ask step=%s", step, extra={"step": step, "duration_ms": ms})  # never the message, it holds the user's words and the quotes
        return Event.of(step, message, **data)

    try:
        db.init_db()
        everything = db.list_feedback(limit=100000)
        analyzed = [f for f in everything if f.analyzed_at]
        accounts = sorted({f.account for f in everything})
        info = understand(question, accounts, [t.name for t in db.list_themes()])
        intent, team = info["intent"], info["team"]
        about = info["account"] or info["theme"] or info["name"]
        yield ev("understand", f"Reading your question, {INTENT_LABEL.get(intent, 'something I cannot answer yet')}"
                 + (f", {about}" if about else "") + f", {TEAM_LABEL[team]}", intent=intent, team=team, account=info["account"], theme=info["theme"])
        if intent == "help":
            yield ev("answer", HELP, intent="help", team=team, account=None, items=[])
            yield ev("done", "Done", intent="help", items=0, dropped_unverified=0, duration_s=round(time.perf_counter() - t0, 3))
            return
        if not analyzed:
            msg = ("There is no feedback yet. Run ingest, then analyze, and ask again." if not everything
                   else "Feedback is loaded but not analyzed yet. Run analyze, then ask again.")
            yield ev("fetch", "No analysis found", items=0, themes=0)
            yield ev("answer", msg, intent="no_analysis", team=team, account=None, items=[])
            yield ev("done", "Done", intent="no_analysis", items=0, dropped_unverified=0, duration_s=round(time.perf_counter() - t0, 3))
            return
        if intent == "clarify":  # after the guard above, so options are never offered when every pick would be a dead end
            q = _fold(question)
            options = _clarify_options(q)
            yield ev("answer", _clarify_text(options, bool(WINDOW.search(q))), intent="clarify", team=None, account=None,
                     items=[], options=options)
            yield ev("done", "Done", intent="clarify", items=0, dropped_unverified=0, duration_s=round(time.perf_counter() - t0, 3))
            return
        ins = build_insights(team)
        yield ev("fetch", f"Loaded the latest analysis, {len(analyzed)} items, {len(db.list_themes())} themes", items=len(analyzed),
                 themes=len(ins.themes), run_id=ins.run_id)
        if intent in ("unknown_account", "unknown_theme"):
            known = accounts if intent == "unknown_account" else sorted({t.name for t in ins.themes})
            noun = "account" if intent == "unknown_account" else "theme"
            msg = f"I cannot find {noun} \"{info['name']}\" in the data. The {noun}s I know: {', '.join(known) or 'none yet'}."
            yield ev("answer", msg, intent=intent, team=team, account=None, items=[])
            yield ev("done", "Done", intent=intent, items=0, dropped_unverified=0, duration_s=round(time.perf_counter() - t0, 3))
            return
        header, sections, empty = _answer(info, ins, {})
        sections, dropped = _verify(sections)
        n_groups = sum(len(s["groups"]) for s in sections)
        n_quotes = sum(len(g["items"]) for s in sections for g in s["groups"])
        yield ev("verify", f"Checked every quote against its source text, {n_quotes} verified, {dropped} dropped", verified=n_quotes,
                 dropped_unverified=dropped)
        items = [it for s in sections for g in s["groups"] for it in g["items"]]
        first = sections[0]["groups"] if len(sections) == 1 else [g for s in sections for g in s["groups"]]
        text = (_plural_header(header, len(first), intent) + "\n" + _render(sections)) if n_groups else empty
        yield ev("answer", text, intent=intent, team=team, account=info["account"], theme=info["theme"], items=items,
                 dropped_unverified=dropped)
        yield ev("done", "Done", intent=intent, items=len(items), dropped_unverified=dropped, duration_s=round(time.perf_counter() - t0, 3),
                 run_id=ins.run_id)
    except Exception as exc:  # surfaced as an error event, like run_analysis
        log.exception("ask failed")
        yield ev("error", f"Could not answer: {exc}")
