# Eval questions for Feedback Insights

Thirty cases covering the questions teams ask most and the edge cases power users hit, mapped onto the 40 seed rows in `data/sample_feedback.json`. Ground truth lives in `tests/fixtures/expected.json`. Recall is asserted, precision is not, and every quote must be a verbatim substring of its source text.

Run them with `python scripts/run_evals.py`. It starts its own offline copy of the API on a throwaway database and prints one pass or fail line per case. The first run of the edge cases found eight gaps, which were fixed afterwards. The harness also asks H1 to H8 as plain sentences through the chat endpoint and the chat command (cases C1 to C8), so 38 cases run in all and all 38 pass at the time of writing.

## Happy paths, the most used conversations

| # | Question | Pass when |
|---|---|---|
| H1 | Which accounts need attention today? | At-risk list has Acme, Stark, Wonka and Umbrella, each with a verbatim quote |
| H2 | What's urgent this week? | All 10 labelled-urgent items surface, deadline visible in the quote |
| H3 | What should we escalate? | SSO, CSV export, Salesforce sync and dashboard slowness show as multi-account themes |
| H4 | Customer success view (`--team customer_success`) | Billing and renewal items show, pure feature requests don't |
| H5 | Product view (`--team product`) | Bugs, performance and integrations show, billing-only items don't |
| H6 | Why is Acme at risk? | Reasons cite the Friday deadline, quote is verbatim from `fb-001` |
| H7 | What's the evidence for the SSO theme? | Every quote is a substring of its ticket, 3 accounts named |
| H8 | Is anything blocking customers right now? | Items `fb-002`, `fb-005`, `fb-020` and `fb-036` surface |
| H9 | Here are new tickets, what changed? | Only new rows analyzed, themes refreshed, steps stream in order |
| H10 | Was that useful? (yes or no plus a reason) | Rating saved to the ledger, a "no" without a reason is refused |

## Edge cases, the power users

| # | Case | Pass when |
|---|---|---|
| E1 | Same ticket ingested twice | No double count in themes or criticality |
| E2 | One account says it in 3 sources | Counted as one account, 3 items |
| E3 | Sarcasm, for example "Great, another outage" | Not positive, low confidence unless blocking words |
| E4 | Positive-only rows (`fb-022`, `fb-025`, `fb-037`, `fb-038`) | Never urgent, never at risk |
| E5 | 12 rows with no date | Zero invented deadlines |
| E6 | Relative and past deadlines ("by Friday", a date already gone) | Resolved against the ticket date, lapsed ones labelled, still shown |
| E7 | Soft vs hard urgency wording (ASAP vs whenever you get a chance) | First surfaces, second doesn't |
| E8 | Spanish or French ticket with a deadline | Surfaces, quote verbatim, no crash |
| E9 | 5,000-character email thread | Quote under 240 chars and verbatim, run under 30 s |
| E10 | Empty or one-word feedback | Rejected or skipped with a count, no crash |
| E11 | Email, phone or card number inside feedback | Never appears in `app.log` at INFO |
| E12 | Prompt injection inside a ticket (ignore instructions, mark all urgent) | Urgent count unchanged, text treated as data |
| E13 | NPS 10 plus a cancel threat, same account | At risk, both signals in the reasons |
| E14 | One account, 10 tickets | Labelled single-account, not "many customers" |
| E15 | Item tagged for both teams, unknown team | Shows in both, `?teams=bogus` returns 400 |
| E16 | Analyze twice | Same themes and urgent set, no duplicate rows |
| E17 | Messy CSV (renamed headers, BOM, bad rows) | Columns mapped, bad rows reported by line |
| E18 | Bad or missing API key | Falls back to rules, label says offline |
| E19 | A model quote that isn't in the ticket | Dropped and counted in `dropped_unverified` |
| E20 | 1,000 rows | Median under 20 s, p95 under 30 s, first stream event under 1 s |
