# CLAUDE.md: Feedback Insights

Agent harness. An agent with no memory must be able to rebuild this repo from this file alone. Read all of it, then `TASKS.md` (the build board), before editing. Contract changes go through the architect session.

## Time box
Built in a 45-minute exercise. A working demo beats more features, so cut scope before missing the clock.

## What we are building
Internal Customer Success (CS) and Product teams get customer feedback from many sources. The tool tells them what matters: urgent issues, themes to escalate, accounts at risk.
Form: Python API (FastAPI) plus a terminal CLI. A web UI is out of scope.
User: an internal CS manager or product manager in a terminal.
Flow: ingest feedback, analyze, read insights (optionally for one team), rate the result "Good? yes/no + reason", the rating lands in a ledger.

## Design rules (from the user, binding)
1. Recall over precision. Users are internal and a human decides whether to escalate, so surface anything plausibly urgent or critical and label `confidence` (low, medium, high) instead of hiding it.
2. Every claim is verifiable. Each theme, urgent issue and at-risk account carries evidence: feedback ids plus a quote that is a verbatim substring of that feedback's text (whitespace and case normalised). Code checks it. A claim whose quote fails is dropped and counted in `dropped_unverified`. No evidence, no claim. Never invent a deadline: a deadline must appear in the quote.
3. URGENT means a time deadline (renewal date, launch, contract end, "by Friday", outage, "blocked"). CRITICAL means multiple stakeholders affected (several accounts, or several items from one account across sources). Both, either, or neither. Priority label: `urgent+critical`, `urgent`, `critical`, `watch`.
4. Latency target, suggested not hard: POST /analyze p50 < 20 s, p95 < 30 s on the seed set.
5. The bot streams its reasoning: every real pipeline step is an event, printed live by the CLI, then the answer.
6. Observability: `./run.sh` opens a second Terminal window tailing `logs/app.log`.
7. Offline mode (rule-based) must work with no API key. LLM mode is optional, on when `ANTHROPIC_API_KEY` is set.
8. Scalable and secure are documented design points (README), not prototype features. Never log secrets.

## Stack
Python 3.11+, FastAPI, uvicorn, sqlite3 (stdlib), pydantic v2, httpx, pytest, `anthropic` SDK (optional import, default model `claude-haiku-5-5`). Nothing else heavy (no pandas, scikit-learn, vector DB).

## Layout and owners (one owner per file, never edit another owner's file)
| Path | Owner | Purpose |
|---|---|---|
| `app/models.py`, `app/config.py`, `app/db.py`, `app/ingest.py` | `eng1` | Pydantic models, env settings, SQLite repo, load JSON or CSV |
| `app/main.py`, `app/ledger.py`, `app/cli.py` | `eng1` | Routes with SSE, JSONL ledger, CLI |
| `requirements.txt`, `.env.example`, `.gitignore` | `eng1` | Deps and env template |
| `app/analyze/*` | `eng2` | Classify, group, score, orchestrate, verify evidence |
| `app/insights.py`, `app/logging_setup.py` | `eng2` | Build Insights, JSON logs |
| `tests/*`, `data/sample_feedback.json` | `qa` | Seed data (about 40 rows), tests, expected-urgent labels |
| `run.sh`, `scripts/*` | `qa` | Launcher, smoke, latency, debug bundle |
| `README.md`, `CLAUDE.md`, `TASKS.md` header, `docs/*` | `arch` | Docs, diagram, integration, review |

## Contracts
Data, SQLite `data/feedback.db` (JSON arrays stored as TEXT, timestamps ISO 8601):
```
feedback(id PK, source, account, timestamp, text, sentiment, tags, urgency REAL, deadline, theme_id, analyzed_at)
theme(id PK, name, description, urgency REAL, criticality REAL, related_feedback_ids, accounts, confidence, updated_at)
```
sentiment: negative | neutral | positive. Tag taxonomy: bug, performance, usability, feature_request, integration, security, billing, onboarding, support_quality, churn_risk, renewal, other.
Pydantic (`app/models.py`): FeedbackIn (source, account, text required; id, timestamp optional), Feedback (adds sentiment, tags, urgency, deadline, theme_id, analyzed_at), Classification {id, sentiment, tags, urgency, deadline, theme_label, evidence_quote}, Theme, Evidence {feedback_id, account, source, quote}, Event, Insights, LedgerEntry.

`app/db.py` functions (eng2 codes against these names):
```
init_db() -> None
insert_feedback(rows: list[FeedbackIn]) -> list[str]
list_feedback(account=None, limit=1000, only_unanalyzed=False) -> list[Feedback]
get_feedback(ids: list[str]) -> list[Feedback]
save_classifications(items: list[Classification]) -> None
save_themes(themes: list[Theme]) -> None   # replaces all themes
list_themes() -> list[Theme]
```

API (`app/main.py`):
- `GET /health` -> {"status","mode":"llm"|"offline","feedback_count"}
- `POST /feedback` (FeedbackIn or list) -> 201 {"ingested","ids"}
- `GET /feedback?account=&limit=` -> list[Feedback]
- `POST /analyze?mode=new|all&stream=true|false` -> SSE of Event, or JSON {"run_id","analyzed","themes","dropped_unverified","duration_s","mode"}
- `GET /insights?teams=product|customer_success` (omit = all, unknown = 400) -> Insights
- `POST /ledger` {run_id?, target, verdict:"yes"|"no", reason} -> 201 LedgerEntry; reason required when verdict is "no" (422 otherwise)
- `GET /ledger?limit=` -> list[LedgerEntry]

Event: {"step": start|load|classify|cluster|score|verify|persist|answer|done|error, "message", "ts", "data": {}}.
Insights: {"run_id","generated_at","team","mode","themes":[Theme + priority + evidence[]],"urgent_issues":[{feedback_id, account, source, quote, deadline, lapsed, theme_id, confidence}],"at_risk_accounts":[{account, risk, confidence, reasons[], evidence[]}],"dropped_unverified"}.
Teams: product = {bug, performance, usability, feature_request, integration, security}; customer_success = {billing, onboarding, support_quality, churn_risk, renewal, security}; `other` belongs to both. `teams=` keeps items whose tags intersect the team's set.

Analysis: `app.analyze.pipeline.run_analysis(mode) -> Iterator[Event]` is a plain sync generator (main.py wraps it in a threadpool). `app.insights.build_insights(team) -> Insights`. `nlp.classify_batch(items) -> list[Classification]`. LLM path: batches of 20, JSON-only output, one retry, then the offline classifier for that batch. Offline path: broad keyword rules.

Scoring (constants in `scoring.py`, recall-first):
- URGENT_T = 0.4, CRITICAL_T = 0.35, AT_RISK_T = 0.35.
- feedback.urgency: 1.0 deadline within 14 days of timestamp; 0.8 later deadline; 0.7 blocked, outage, cancel, escalate; 0.4 frustration with no date; else 0.
- theme.urgency = max urgency of its feedback. theme.criticality = min(1, 0.6*min(accounts/5, 1) + 0.4*min(items/10, 1)).
- account.risk = min(1, 0.4*negative_share + 0.4*max_urgency + 0.2*min(themes/3, 1)).

Ledger: append-only `data/ledger.jsonl`, one JSON object per line: {"ts","run_id","target","verdict","reason","mode","insights_sha256"}. Never rewritten.

CLI (`python -m app.cli`), talks to `API_URL` (default http://127.0.0.1:8000): `ingest <path>`, `analyze`, `ledger`, `insights [--team product|customer_success]` (prints, then asks `Good? (yes/no) + reason:` and POSTs /ledger).

Env: ANTHROPIC_API_KEY (optional), LLM_MODEL (claude-haiku-5-5), LLM_MODE (auto|offline), DB_PATH (data/feedback.db), LEDGER_PATH (data/ledger.jsonl), LOG_LEVEL (INFO), LOG_FILE (logs/app.log), PORT (8000), OPEN_LOG_WINDOW (1), API_URL.

Logging: JSON lines to LOG_FILE plus human lines to stderr; fields ts, level, logger, msg, request_id, step, duration_ms. LLM calls log model, latency_ms and token counts at INFO, full prompts only at DEBUG. `run.sh` opens the log window with `osascript` on macOS. `scripts/debug_bundle.sh` writes `debug/bundle-<timestamp>.tar.gz` (last 2000 log lines, redacted env, versions, row counts).

## Commands
```
./run.sh                 # venv, deps, start API on :8000, open log window, print CLI help
python -m app.cli --help
pytest -q
./scripts/smoke.sh       # temp DB and port, offline mode, ingest seed, analyze, check insights, kill server
python scripts/latency.py
```

## Sessions and board
Four sessions shared this directory, an architect, two engineers and a QA. The board is `TASKS.md`. Claim a row with `[~]`, strike it `[x]` only after its Accept check passed, and log bugs under Bugs. Milestones were ingest plus offline analyze end to end, then insights plus the CLI rating prompt, then a code freeze.

## Definition of done
- `./scripts/smoke.sh` passes offline.
- Every evidence quote in /insights is a substring of its feedback text (tests assert it).
- Recall >= 0.9 on qa's expected-urgent set (precision is not asserted).
- The CLI writes a ledger line for a yes and for a no with a reason.
- README steps work in a fresh shell without any AI assistant.

## Rebuild from zero (agent with no memory)
1. Read this file and `TASKS.md`. 2. Create the files in the owner table in task order, following the contracts verbatim. 3. `./run.sh`. 4. `./scripts/smoke.sh` and `pytest -q`. 5. Compare the README quick start against what you ran and fix the README.

## Gotchas
- SSE: `StreamingResponse(media_type="text/event-stream")`, headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`, flush per event.
- SQLite is touched from threadpool threads: open a connection per call, never share one.
- LLM JSON can be malformed or fenced: strip code fences, parse, retry once, fall back offline.
- `osascript` only exists on macOS: `run.sh` must degrade to printing the `tail -f` command.
