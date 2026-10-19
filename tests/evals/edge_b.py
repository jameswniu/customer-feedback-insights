"""Edge-case eval handlers E11 to E20. Each takes (Case, Srv) and returns (ok, detail)."""
from __future__ import annotations

import json
import math
import re
import statistics
import time

from harness import (EXPECTED, FIX, SEED, TEAMSET, Acc, bad_quotes, best_theme, fbmap, theme_snapshot, urgent_ids, verbatim)


def e11(c, srv):
    a = Acc()
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    srv.insights()
    srv.cli("insights", "--no-rate")
    srv.cli("analyze", "--mode", "all")
    logs = srv.log_text()
    a.check(len(logs) > 500, f"read {len(logs)} chars of app.log and server output", f"logs are nearly empty ({len(logs)} chars), the check would be vacuous")
    leaks = [s for s in c.p["secrets"] if s in logs]
    a.check(not leaks, f"none of {len(c.p['secrets'])} email, phone and card strings in the logs", f"found in the logs {leaks}")
    return a.result()


def e12(c, srv):
    a = Acc()
    seed_ids = {r["id"] for r in json.loads(SEED.read_text())}
    before, ins0 = fbmap(srv), srv.insights()
    u_before = urgent_ids(ins0)
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    after, ins = fbmap(srv), srv.insights()
    u_after = urgent_ids(ins)
    a.check(u_after & seed_ids == u_before, f"urgent set over the 40 seed rows unchanged ({len(u_before)})",
            f"urgent set changed, gained {sorted(u_after & seed_ids - u_before)}, lost {sorted(u_before - u_after)}")
    key = lambda f: (f["urgency"], f["sentiment"], f["deadline"], tuple(f["tags"]))  # noqa: E731
    moved = sorted(i for i in seed_ids if key(before[i]) != key(after[i]))
    a.check(not moved, "no seed row re-scored", f"seed rows re-scored {moved}")
    inj = [u for u in ins["urgent_issues"] if u["feedback_id"] in c.p["ids"]]
    a.check(all(not u["deadline"] and u["confidence"] != "high" and verbatim(u["quote"], after[u["feedback_id"]]["text"]) for u in inj),
            f"injected rows invent no deadline, never high confidence, quotes verbatim ({len(inj)} of {len(c.p['ids'])} surfaced on their own words)",
            f"an injected row carries a deadline, high confidence or a non-verbatim quote {inj}")
    return a.result()


def e13(c, srv):
    a = Acc()
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    risk = next((x for x in srv.insights()["at_risk_accounts"] if x["account"] == c.p["account"]), None)
    if not a.check(risk is not None, f"{c.p['account']} is at risk", f"{c.p['account']} is not on the at-risk list"):
        return a.result()
    reasons = " | ".join(risk["reasons"]).lower()
    a.check("cancel" in reasons, "reasons cite the cancel threat", f"reasons do not cite the cancel threat ({reasons})")
    a.check(bool(re.search(r"\bnps\b|score|promoter|positive|\b10\b", reasons)), "reasons cite the NPS 10",
            f"reasons never mention the NPS 10, they say '{reasons}' (the NPS quote is only in the evidence list)")
    return a.result()


def e14(c, srv):
    a, p = Acc(), c.p
    srv.ingest(FIX / p["fixture"])
    srv.analyze("all")
    ids, ins = [r["id"] for r in c.rows()], srv.insights()
    th = best_theme(ins, ids)
    if not a.check(th is not None and len(set(ids) & set(th["related_feedback_ids"])) >= 8, f"{len(ids)} tickets in one theme",
                   f"the {len(ids)} tickets are spread over several themes (best '{th['name'] if th else None}')"):
        return a.result()
    a.check(th["accounts"] == [p["account"]], "theme names one account", f"theme accounts {th['accounts']}")
    line = next((x for x in srv.cli("insights", "--no-rate").stdout.splitlines() if f"{len(th['related_feedback_ids'])} items" in x), "")
    a.check(bool(re.search(r"\b1 accounts?\b", line)), f"CLI says '{line.strip()}'", f"CLI theme line does not show one account '{line.strip()}'")
    a.check(not re.search(r"\b(?:[2-9]|\d\d+) (?:accounts|customers)\b", th["description"] + line), "never says many customers",
            f"theme says many customers '{th['description']}'")
    return a.result()


def e15(c, srv):
    a, fbm, ins = Acc(), fbmap(srv), srv.insights()
    teams = c.p["teams"]
    views = {t: urgent_ids(srv.insights(t)) for t in teams}
    both = [u for u in urgent_ids(ins) if all(set(fbm[u]["tags"] or ["other"]) & TEAMSET[t] for t in teams)]
    a.check(bool(both), "", "vacuous, no urgent item is tagged for both teams")
    miss = [(i, t) for i in both for t in teams if i not in views[t]]
    a.check(not miss, f"{len(both)} urgent items tagged for both teams show in both views", f"items missing from a team view {miss}")
    code = srv.get("/insights", teams=c.p["bogus"]).status_code
    a.check(code == 400, "?teams=bogus returns 400", f"?teams=bogus returned {code}")
    return a.result()


def e16(c, srv):
    a = Acc()
    ins1, fb1 = srv.insights(), fbmap(srv)
    srv.analyze("all")
    srv.analyze("new")
    ins2, fb2 = srv.insights(), fbmap(srv)
    a.check(theme_snapshot(ins1) == theme_snapshot(ins2), f"same {len(ins1['themes'])} themes after analyzing again", "themes differ after a second analyze")
    a.check(urgent_ids(ins1) == urgent_ids(ins2), f"same urgent set ({len(urgent_ids(ins1))})", f"urgent set changed {sorted(urgent_ids(ins1) ^ urgent_ids(ins2))}")
    a.check([x["account"] for x in ins1["at_risk_accounts"]] == [x["account"] for x in ins2["at_risk_accounts"]], "same at-risk accounts", "at-risk accounts changed")
    key = lambda f: (f["urgency"], f["sentiment"], f["deadline"], tuple(f["tags"]))  # noqa: E731
    a.check(fb1.keys() == fb2.keys() and len(fb2) == 40 and all(key(fb1[i]) == key(fb2[i]) for i in fb1), "no duplicate rows, 40 rows unchanged",
            f"rows {len(fb1)} to {len(fb2)} or scores changed")
    return a.result()


def e17(c, srv):
    a, p = Acc(), c.p
    r = srv.ingest(FIX / p["fixture"])
    out = r.stdout + r.stderr
    rows = srv.feedback()
    accts = sorted({f["account"] for f in rows})
    a.check(r.returncode == 0 and accts == sorted(p["good_accounts"]), f"columns mapped, accounts {accts}", f"ingest rc {r.returncode}, accounts after ingest {accts}")
    a.check({"Zendesk", "Email", "Call notes"} <= {f["source"] for f in rows}, "Channel mapped to source", f"sources {sorted({f['source'] for f in rows})}")
    reported = {int(x) for x in re.findall(r"(?:line|row)s?\s*#?(\d+)", out, flags=re.I)}
    data_rows = [n - 1 for n in p["bad_lines"]]
    a.check(set(p["bad_lines"]) <= reported or set(data_rows) <= reported, f"bad rows reported by line {sorted(reported)}",
            f"bad rows are not reported by line, wanted lines {p['bad_lines']}, CLI said {out.strip()[:110]!r}")
    srv.analyze("all")
    a.check(len(srv.feedback()) == len(p["good_accounts"]), "analysis ran on the good rows", "row count changed")
    return a.result()


def e18(c, srv):
    a, key, fake = Acc(), c.p["fake_key"], srv.fake
    res = srv.analyze("all")
    a.check(res["analyzed"] == 40 and len(fake.hits) >= 1 and all(h["key"] == key for h in fake.hits),
            f"model tried {len(fake.hits)}x with the bad key, then rules took over (40 analyzed)",
            f"scenario did not run, analyzed {res['analyzed']}, model calls {len(fake.hits)}")
    ins = srv.insights()
    a.check(res["mode"] == "offline" and ins["mode"] == "offline", "analyze and insights label the run offline",
            f"label says analyze {res['mode']}, insights {ins['mode']} although the rules answered")
    got = urgent_ids(ins)
    want = set(EXPECTED["expected_urgent"])
    a.check(len(want & got) / len(want) >= 0.9 and not bad_quotes(ins, fbmap(srv)), f"recall {len(want & got) / len(want):.2f}, quotes verbatim",
            f"fallback result is weaker, recall {len(want & got) / len(want):.2f}, bad quotes {bad_quotes(ins, fbmap(srv))[:2]}")
    srv.cli("insights", stdin="yes\n")
    last = srv.ledger_lines()[-1]
    a.check(last["mode"] == "offline", "ledger line says offline", f"ledger line says {last['mode']}")
    blob = srv.log_text() + json.dumps(ins) + json.dumps(res)
    a.check(key not in blob and "EVAL-FAKE" not in blob, "bad key never appears in logs or responses", "the bad key leaked into logs or a response")
    health = srv.get("/health").json()["mode"]
    with c.ctx.fresh(seed=True, analyze=False, env={"LLM_MODE": "auto"}) as s2:
        r2 = s2.analyze("all")
        modes = (r2["mode"], s2.insights()["mode"], s2.get("/health").json()["mode"])
    a.check(modes == ("offline",) * 3, f"no key at all reads offline everywhere (bad key /health says {health}, by design)",
            f"with no key the labels say {modes}")
    return a.result()


def e19(c, srv):
    a, p = Acc(), c.p
    res = srv.analyze("all")
    a.check(len(srv.fake.hits) >= 1, f"model path ran ({len(srv.fake.hits)} calls)", "the model path never ran, the case is vacuous")
    ins, fbm = srv.insights(), fbmap(srv)
    a.check(p["fake_quote"] not in json.dumps(ins), "fabricated quote never reaches the insights", "the fabricated quote is in the insights")
    a.check(p["fake_deadline"] not in json.dumps(ins) and fbm[p["target"]]["deadline"] != p["fake_deadline"], "fabricated deadline never reaches the insights",
            "the fabricated deadline is in the insights")
    a.check(not bad_quotes(ins, fbm), "every quote shown is verbatim", f"non-verbatim quotes shown {bad_quotes(ins, fbm)[:2]}")
    counted = max(res["dropped_unverified"], ins["dropped_unverified"])
    a.check(counted >= 1, f"dropped and counted ({counted})",
            f"the fabricated quote vanished but was not counted, dropped_unverified is {res['dropped_unverified']} in /analyze and {ins['dropped_unverified']} in /insights")
    return a.result()


def e20(c, srv):
    a, p = Acc(), c.p
    seed = json.loads(SEED.read_text())
    rows = [dict(r, id=f"bulk-{k:02d}-{r['id']}") for k in range(p["copies"]) for r in seed]
    n = srv.post("/feedback", json=rows).json()["ingested"]
    a.check(n == len(rows) == 1000, "1,000 rows ingested", f"ingested {n} of {len(rows)}")
    times = []
    for _ in range(p["runs"]):
        t0 = time.perf_counter()
        srv.analyze("all")
        times.append(time.perf_counter() - t0)
    med, p95 = statistics.median(times), sorted(times)[max(0, math.ceil(0.95 * len(times)) - 1)]
    a.check(med < p["median_s"] and p95 < p["p95_s"], f"median {med:.2f}s, p95 {p95:.2f}s over {p['runs']} runs",
            f"median {med:.2f}s (limit {p['median_s']}), p95 {p95:.2f}s (limit {p['p95_s']})")
    events, first = srv.stream_analyze("all")
    a.check(first is not None and first < p["first_event_s"] and events[-1]["step"] == "done", f"first stream event after {(first or 0) * 1000:.0f} ms, stream ends in done",
            f"first event after {first}s, last step {events[-1]['step'] if events else None}")
    t0 = time.perf_counter()
    srv.insights()
    a.check(True, f"insights over 1,000 rows took {time.perf_counter() - t0:.2f}s")
    return a.result()


HANDLERS = {"E11": e11, "E12": e12, "E13": e13, "E14": e14, "E15": e15, "E16": e16, "E17": e17, "E18": e18, "E19": e19, "E20": e20}
