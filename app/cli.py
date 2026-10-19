"""Terminal client: ingest, analyze (live events), insights (then rate), ledger.

Talks to the API at API_URL. After insights it asks "Good? (yes/no) + reason" and
POSTs the answer to /ledger. Pipe a line on stdin or pass --rate to skip the prompt.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import datetime
from typing import Optional

import httpx

from app import ingest as ingest_mod
from app.config import get_settings
from app.ledger import insights_hash
from app.models import TEAMS

TTY = sys.stdout.isatty()


class CliError(Exception):
    pass


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


def bold(s: str) -> str: return _c("1", s)
def dim(s: str) -> str: return _c("2", s)
def red(s: str) -> str: return _c("31", s)
def green(s: str) -> str: return _c("32", s)


PRIORITY_COLOR = {"urgent+critical": "1;31", "urgent": "33", "critical": "35", "watch": "2"}


def client(read: Optional[float] = 120) -> httpx.Client:
    return httpx.Client(base_url=get_settings().api_url, timeout=httpx.Timeout(10.0, read=read))


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except Exception:
        return r.text[:200]
    if isinstance(d, list) and d:
        d = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
    return str(d)


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400:
        raise CliError(f"{what} failed ({r.status_code}): {_detail(r)}")


def wrap(s: str, indent: str = "     ", width: int = 100) -> str:
    return textwrap.fill(s, width=width, initial_indent=indent, subsequent_indent=indent)


# ---- ingest --------------------------------------------------------------------------------

def cmd_ingest(args: argparse.Namespace) -> int:
    try:
        raw = ingest_mod.read_rows(args.path)
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read {args.path}: {exc}")
    rows = ingest_mod.parse_rows(raw)
    if not rows:
        raise CliError(f"no usable rows in {args.path} (each row needs text)")
    with client() as cl:
        r = cl.post("/feedback", json=[x.model_dump(exclude_none=True) for x in rows])
    _check(r, "ingest")
    skipped = len(raw) - len(rows)
    note = f" ({skipped} skipped, no text)" if skipped else ""
    print(f"Ingested {r.json()['ingested']} rows from {args.path}{note}")
    return 0


# ---- analyze -------------------------------------------------------------------------------

def _local_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now().strftime("%H:%M:%S")


def print_event(ev: dict) -> None:
    step = str(ev.get("step", "?"))
    color = {"error": "31", "answer": "1;32", "done": "32"}.get(step, "36")
    print(f"{dim(_local_time(ev.get('ts', '')))}  {_c(color, step.ljust(8))} {ev.get('message', '')}", flush=True)


def cmd_analyze(args: argparse.Namespace) -> int:
    failed = finished = False
    with client(read=None) as cl:
        with cl.stream("POST", "/analyze", params={"mode": args.mode, "stream": "true"}) as r:
            if r.status_code >= 400:
                r.read()
                raise CliError(f"analyze failed ({r.status_code}): {_detail(r)}")
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                print_event(ev)
                failed = failed or ev.get("step") == "error"
                finished = finished or ev.get("step") == "done"
    if failed or not finished:
        print(red("Analysis did not finish cleanly."), file=sys.stderr)
        return 1
    return 0


# ---- insights ------------------------------------------------------------------------------

def render_insights(ins: dict) -> str:
    L: list[str] = []
    L.append(bold(f"INSIGHTS  {ins.get('team') or 'all teams'}  mode={ins.get('mode', '?')}  run={ins.get('run_id') or '-'}"))

    urgent = ins.get("urgent_issues") or []
    L += ["", bold(f"URGENT ISSUES ({len(urgent)})")]
    if not urgent:
        L.append("  none found")
    for i, u in enumerate(urgent, 1):
        head = f"  {i}. {u.get('account', '?')} ({u.get('source', '?')})  confidence {u.get('confidence', '?')}"
        if u.get("deadline"):
            head += f"  deadline {u['deadline']}"
        L.append(head)
        L.append(wrap(f"\"{u.get('quote', '')}\"  [{u.get('feedback_id', '')}]"))

    themes = ins.get("themes") or []
    L += ["", bold(f"THEMES ({len(themes)})")]
    if not themes:
        L.append("  none found")
    for i, t in enumerate(themes, 1):
        pr = t.get("priority", "watch")
        L.append(f"  {i}. {t.get('name', '?')}  {_c(PRIORITY_COLOR.get(pr, '0'), pr)}  confidence {t.get('confidence', '?')}")
        L.append(dim(
            f"     urgency {float(t.get('urgency') or 0):.2f} | criticality {float(t.get('criticality') or 0):.2f}"
            f" | {len(t.get('accounts') or [])} accounts | {len(t.get('related_feedback_ids') or [])} items"))
        ev = t.get("evidence") or []
        if ev:
            e = ev[0]
            L.append(wrap(f"\"{e.get('quote', '')}\"  - {e.get('account', '')} [{e.get('feedback_id', '')}]"))
            if len(ev) > 1:
                L.append(dim(f"     +{len(ev) - 1} more quotes"))

    risk = ins.get("at_risk_accounts") or []
    L += ["", bold(f"AT-RISK ACCOUNTS ({len(risk)})")]
    if not risk:
        L.append("  none found")
    for i, a in enumerate(risk, 1):
        L.append(f"  {i}. {a.get('account', '?')}  risk {float(a.get('risk') or 0):.2f}  confidence {a.get('confidence', '?')}")
        if a.get("reasons"):
            L.append(wrap("why: " + "; ".join(a["reasons"][:3])))
        ev = a.get("evidence") or []
        if ev:
            e = ev[0]
            L.append(wrap(f"\"{e.get('quote', '')}\"  [{e.get('feedback_id', '')}]"))

    L += ["", dim(f"{ins.get('dropped_unverified', 0)} claims dropped because their quote could not be verified.")]
    return "\n".join(L)


def parse_verdict(line: str) -> tuple[Optional[str], str]:
    s = line.strip()
    if not s:
        return None, ""
    first, _, rest = s.partition(" ")
    word = first.strip(",.:;-+").lower()
    verdict = {"y": "yes", "yes": "yes", "n": "no", "no": "no"}.get(word)
    if verdict is None:
        return None, s
    return verdict, rest.strip(" -:,+").strip()


def _ask(prompt: str) -> Optional[str]:
    try:
        return input(prompt)
    except EOFError:
        return None


def get_rating(first: Optional[str]) -> Optional[tuple[str, str]]:
    line = first
    for _ in range(3):
        if line is None:
            return None
        verdict, reason = parse_verdict(line)
        if verdict is None:
            line = _ask("Please answer yes or no (+ reason): ")
            continue
        if verdict == "no" and not reason:
            more = _ask("A reason is required for 'no': ")
            if more is None:
                return None
            reason = more.strip()
            if not reason:
                line = "no"
                continue
        return verdict, reason
    return None


def cmd_insights(args: argparse.Namespace) -> int:
    with client() as cl:
        r = cl.get("/insights", params={"teams": args.team} if args.team else {})
    _check(r, "insights")
    ins = r.json()
    print(render_insights(ins))
    if args.no_rate:
        return 0
    first = args.rate if args.rate is not None else _ask("\nGood? (yes/no) + reason: ")
    rating = get_rating(first)
    if rating is None:
        print("\nNo rating recorded.")
        return 0
    verdict, reason = rating
    body = {
        "run_id": ins.get("run_id") or None,
        "target": f"insights:{args.team}" if args.team else "insights",
        "verdict": verdict,
        "reason": reason,
        "insights_sha256": insights_hash(ins),
    }
    with client() as cl:
        r = cl.post("/ledger", json=body)
    _check(r, "ledger")
    e = r.json()
    print(green(f"Logged {e['verdict']}") + (f" - {e['reason']}" if e.get("reason") else ""))
    return 0


# ---- ledger, health ------------------------------------------------------------------------

def cmd_ledger(args: argparse.Namespace) -> int:
    with client() as cl:
        r = cl.get("/ledger", params={"limit": args.limit})
    _check(r, "ledger")
    rows = r.json()
    if not rows:
        print("Ledger is empty.")
        return 0
    for e in rows:
        try:
            when = datetime.fromisoformat(e["ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            when = str(e.get("ts", ""))[:16].replace("T", " ")
        print(f"{when}  {e.get('verdict', ''):<3}  {e.get('target', ''):<22} run {e.get('run_id') or '-':<9} {e.get('reason', '')}")
    return 0


def cmd_health(_: argparse.Namespace) -> int:
    with client() as cl:
        r = cl.get("/health")
    _check(r, "health")
    h = r.json()
    print(f"{h['status']}  mode={h['mode']}  feedback={h['feedback_count']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Feedback Insights CLI")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    a = sub.add_parser("ingest", help="load a JSON or CSV file of feedback")
    a.add_argument("path")
    a = sub.add_parser("analyze", help="run the analysis and watch each step live")
    a.add_argument("--mode", choices=["new", "all"], default="new", help="new rows only (default) or everything")
    a = sub.add_parser("insights", help="show urgent issues, themes and at-risk accounts, then rate them")
    a.add_argument("--team", choices=sorted(TEAMS), help="only items for one team")
    a.add_argument("--rate", metavar="'yes|no reason'", help="answer the rating prompt up front")
    a.add_argument("--no-rate", action="store_true", help="print only, no rating prompt")
    a = sub.add_parser("ledger", help="show recent ratings")
    a.add_argument("--limit", type=int, default=10)
    sub.add_parser("health", help="API status")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"ingest": cmd_ingest, "analyze": cmd_analyze, "insights": cmd_insights,
                "ledger": cmd_ledger, "health": cmd_health}
    try:
        return handlers[args.cmd](args)
    except CliError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 1
    except httpx.ConnectError:
        print(red(f"Cannot reach the API at {get_settings().api_url}. Start it with ./run.sh"), file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(red(f"HTTP error: {exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
