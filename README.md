# Customer Feedback Insights

Turns customer feedback from many sources into what a team can act on, the urgent issues, the themes worth escalating and the accounts at risk, each backed by the customer's own words.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue) ![FastAPI](https://img.shields.io/badge/API-FastAPI-009688) ![Tests](https://img.shields.io/badge/tests-134%20passing-brightgreen) ![Built in](https://img.shields.io/badge/built%20in-45%20minutes-orange)

![A customer success manager loads feedback, watches the analysis, asks a question and rates the answer](docs/demo.gif)

I built this in a 45-minute exercise for internal customer success and product teams. They collect feedback in support tickets, surveys, call notes, Slack and email, and need to know what matters today.

## Try it

You need Python 3.11 or newer on macOS or Linux. No API key and no AI assistant is needed.

```bash
git clone https://github.com/jameswniu/customer-feedback-insights
cd customer-feedback-insights
./run.sh
```

`run.sh` sets up a virtual environment, installs the dependencies and starts the API on port 8000. This window then says the API is on. On macOS it also opens two Terminal windows, one with the chat and one that follows the logs. On Linux, run `./scripts/chat.sh` and `tail -f logs/app.log` in two more terminals.

The first time, the chat loads the sample feedback and analyzes it, so you can ask a question at once. Use this window for the other commands.

```bash
python -m app.cli ingest data/sample_feedback.json   # the chat window already did this on a first run
python -m app.cli analyze
python -m app.cli chat                               # the same chat, here
python -m app.cli insights --team customer_success
python -m app.cli ledger
```

![The analysis prints each step as it runs](docs/analyze-steps.png)

`analyze` prints every step as it happens. `insights` prints the full results, then asks `Good? (yes/no) + reason:` and saves your answer. `ledger` shows the saved answers, and a "no" must come with a reason.

`chat` takes questions in plain sentences, such as `Which accounts need attention today?`, and streams its steps before an answer that quotes the customer. If a question is unclear it asks which one you mean, and you type 1, 2 or 3.

## How it decides what matters

An item is urgent when it carries a time deadline, such as a renewal date, a launch or a blocking outage. It is critical when it affects several stakeholders, meaning several accounts or one account across sources. It can be both, either or neither, and each theme gets a label (`urgent+critical`, `urgent`, `critical` or `watch`).

Two rules shape the results. I favour recall over precision, because a person decides whether to escalate, so borderline items appear with a low confidence label instead of being hidden. And the tool never states what it cannot show. Each claim quotes the customer word for word, code checks that the quote really appears in the source text, and any claim that fails the check is dropped and counted.

![A chat answer with the customer's own quotes, then the rating prompt](docs/chat-answer.png)

## Under the hood

```mermaid
flowchart TB
    CLI["Terminal CLI<br/>ingest, analyze, chat,<br/>insights, ledger"]:::ui

    CLI -->|load| ING["Ingest<br/>POST /feedback"]:::api
    CLI -->|analyze| ANA["Analyze<br/>POST /analyze"]:::api
    CLI -->|read or ask| INS["Insights and chat<br/>GET /insights<br/>POST /ask"]:::api
    CLI -->|rate| RAT["Ratings<br/>POST /ledger"]:::api

    subgraph PIPE["Analysis pipeline"]
        direction TB
        CLS["Classify<br/>rules, or a model<br/>if a key is set"]:::model --> SCO["Group and score<br/>urgent, critical,<br/>at risk"]:::api --> VER["Verify quotes<br/>drop any claim<br/>it cannot prove"]:::api
    end

    ANA --> PIPE
    ANA -. "live steps" .-> CLI
    ING --> DB
    PIPE --> DB[("SQLite<br/>feedback and themes")]:::data
    INS -->|reads| DB
    RAT --> LED[("Ratings log<br/>append-only file")]:::data

    classDef ui fill:#ff8787,stroke:#1e1e1e,color:#1e1e1e,stroke-width:2px;
    classDef api fill:#a5d8ff,stroke:#1e1e1e,color:#1e1e1e,stroke-width:2px;
    classDef model fill:#eebefa,stroke:#1e1e1e,color:#1e1e1e,stroke-width:2px;
    classDef data fill:#b2f2bb,stroke:#1e1e1e,color:#1e1e1e,stroke-width:2px;
```

View the full-size diagram as [a plain image](docs/architecture-full.png), or [in Excalidraw](https://link.excalidraw.com/l/47sur6M8pSM/87MyRB4UsBd), where anyone can open it as a read-only guest.

The CLI talks to a FastAPI service. Feedback lands in SQLite, a pipeline classifies each item, groups items into themes and scores urgency and criticality, and every quote is verified before anything is saved. The insights endpoint reads the result and filters it by team.

Without an API key a rule-based classifier does the work. With `ANTHROPIC_API_KEY` set in `.env`, a model (Claude Haiku by default) classifies in batches of 20 and any batch it cannot parse falls back to the rules. `.env.example` lists every setting, and `LLM_MODE=offline` forces the rules.

## Debugging

Every run writes JSON lines to `logs/app.log`, with a request id and step timing on each line. The log window shows the same stream.

```bash
LOG_LEVEL=DEBUG ./run.sh        # also logs model prompts and replies, never the API key
tail -f logs/app.log | jq .     # follow and pretty-print
grep 3f9a1c logs/app.log        # trace one request by its id
./scripts/debug_bundle.sh       # last 2000 log lines, versions, row counts, secrets redacted
```

## Tests

```bash
pytest -q                       # 134 tests, all offline
python scripts/run_evals.py     # 38 conversation-style cases, about 25 seconds
./scripts/smoke.sh              # the whole flow on a throwaway database
python scripts/latency.py       # median and p95 time for an analysis
```

The tests check recall on the labelled urgent items and that every quote appears in its source. They never assert precision, on purpose. `docs/evals-questions.md` explains the eval cases, 10 everyday conversations and 20 edge cases such as sarcasm, French and Spanish tickets, messy CSV files and a ticket that tries to give the tool orders. Eight more ask the everyday questions as chat sentences. All 38 pass today.

## How I built it

I split the 45 minutes across four AI coding sessions working from one task board (`TASKS.md`), an architect, two engineers and a QA that tried to break the claims. `CLAUDE.md` is the contract that kept them consistent, and it is enough for an agent with no memory to rebuild the repo. The chat command came in a second pass after the exercise.

SQLite and one process keep this easy to run. To productionize it I would put the API behind single sign-on with team-scoped queries and move analysis onto a queue with workers. I would also store themes in Postgres with embeddings for clustering and redact personal data before any model call. With more time I would use the saved ratings to tune thresholds and prompts.

Known limits, left alone to stay inside the 45 minutes:

- The quote check ignores case and spacing, so it proves a quote is present rather than character-exact.
- French and Spanish are handled with keyword cues, not a language model, so other languages need the API key.
- Chat uses keyword rules, so unusual phrasing gets a question back instead of an answer.
- A date with no year that is more than 30 days before the ticket is read as next year, which can show a lapsed deadline as live.
- Results are saved in two writes instead of one transaction, and insights and chat read at most 10,000 rows.
- A failure during a chat answer prints as a red error line, but the command still exits with success.
- Two analyses started at once can overwrite each other.
- The launcher trusts a loopback address and a saved process id, so a tunnel to another machine can fool it. `LOAD_SAMPLE=0` stops the sample load.
- The saved ratings trust the run id the CLI sends, and record the engine of the latest run, so rating an older result after a setting change can mislabel it.
