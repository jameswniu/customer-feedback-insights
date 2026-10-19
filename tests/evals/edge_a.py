"""Edge-case eval handlers E1 to E10. Each takes (Case, Srv) and returns (ok, detail)."""
from __future__ import annotations

import json
import re
import time

from harness import (EXPECTED, FIX, SEED, Acc, best_theme, fbmap, norm, theme_snapshot, urgent_ids, verbatim)

LAPSED_RX = re.compile(r"\b(lapsed|overdue|past due|expired|already passed|has passed|in the past)\b", re.I)


def e1(c, srv):
    a = Acc()
    n0, snap = len(srv.feedback()), theme_snapshot(srv.insights())
    srv.ingest(SEED)
    srv.analyze("all")
    n1, snap2 = len(srv.feedback()), theme_snapshot(srv.insights())
    a.check(n1 == n0 and snap == snap2, f"re-ingesting the same file changes nothing ({n0} rows, {len(snap)} themes)",
            f"after a re-ingest rows went {n0} to {n1}, themes changed {sorted(k for k in snap2 if snap2[k] != snap.get(k))}")
    row = c.p["row"]
    i1, i2 = srv.post("/feedback", json=row).json(), srv.post("/feedback", json=row).json()
    n2 = len(srv.feedback())
    a.check(i1["ids"] == i2["ids"] and n2 == n1 + 1, "same ticket without an id counts once",
            f"same ticket sent twice gave rows {n1} to {n2}, ids {i1['ids']} and {i2['ids']}")
    srv.analyze("all")
    every = [i for t in srv.insights()["themes"] for i in t["related_feedback_ids"]]
    a.check(len(every) == len(set(every)) == n2, f"every ticket sits in exactly one theme, once ({n2})",
            f"{len(every)} theme slots for {n2} rows, {len(every) - len(set(every))} repeats")
    return a.result()


def e2(c, srv):
    a, rows = Acc(), c.rows()
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    ids, ins = [r["id"] for r in rows], srv.insights()
    th = best_theme(ins, ids)
    if not a.check(th is not None and set(ids) <= set(th["related_feedback_ids"]), f"{len(ids)} items from {len({r['source'] for r in rows})} sources in one theme",
                   f"the {len(ids)} items did not land in one theme (best {th['name'] if th else None})"):
        return a.result()
    a.check(th["accounts"] == [c.p["account"]], "counted as one account", f"accounts {th['accounts']}")
    crit = round(min(1.0, 0.6 * min(1 / 5, 1) + 0.4 * min(len(ids) / 10, 1)), 2)
    a.check(abs(th["criticality"] - crit) < 0.011, f"criticality {th['criticality']} is what 1 account and {len(ids)} items give",
            f"criticality {th['criticality']}, expected {crit} for 1 account and {len(ids)} items")
    return a.result()


def e3(c, srv):
    a = Acc()
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    fbm, ins = fbmap(srv), srv.insights()
    issues = {u["feedback_id"]: u for u in ins["urgent_issues"]}
    for i in c.p["blocking_ids"] + c.p["plain_ids"]:
        s = fbm[i]["sentiment"]
        a.check(s != "positive", f"{i} read as {s}", f"{i} \"{fbm[i]['text'][:48]}...\" was read as {s}")
    for i in c.p["plain_ids"]:
        if i in issues:
            a.check(issues[i]["confidence"] == "low", f"{i} surfaced with low confidence",
                    f"{i} has no blocking word but surfaced with confidence {issues[i]['confidence']}")
    return a.result()


def e4(c, srv):
    a = Acc()
    ids = EXPECTED[c.p["expected_key"]]
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    fbm, ins = fbmap(srv), srv.insights()
    pos = ids + [r["id"] for r in c.rows()]
    hot = [i for i in pos if fbm[i]["urgency"] > 0 or i in urgent_ids(ins)]
    a.check(not hot, f"{len(pos)} positive-only rows never urgent", f"positive-only rows scored urgent {hot}")
    cited = sorted({e["feedback_id"] for x in ins["at_risk_accounts"] for e in x["evidence"] if e["feedback_id"] in pos})
    a.check(not cited, "never cited as at-risk evidence", f"positive-only rows cited as at-risk evidence {cited}")
    a.check(c.p["account"] not in {x["account"] for x in ins["at_risk_accounts"]}, f"{c.p['account']} (positive rows only) is not at risk",
            f"{c.p['account']} has only positive rows but is listed at risk")
    return a.result()


def e5(c, srv):
    a, ids, fbm, ins = Acc(), EXPECTED[c.p["expected_key"]], fbmap(srv), srv.insights()
    inv = [(i, fbm[i]["deadline"]) for i in ids if fbm[i]["deadline"]]
    a.check(not inv, f"{len(ids)} no-date rows carry no deadline", f"invented deadlines {inv}")
    urg = [(u["feedback_id"], u["deadline"]) for u in ins["urgent_issues"] if u["feedback_id"] in ids and u["deadline"]]
    a.check(not urg, "none in urgent_issues either", f"urgent issues with a deadline on no-date rows {urg}")
    ghost = [(i, f["deadline"]) for i, f in fbm.items() if f["deadline"] and norm(f["deadline"]) not in norm(f["text"])]
    a.check(not ghost, "every deadline on any row appears in its text", f"deadline not in its text {ghost}")
    return a.result()


def e6(c, srv):
    a, p = Acc(), c.p
    srv.ingest(FIX / p["fixture"])
    srv.analyze("all")
    fbm, ins = fbmap(srv), srv.insights()
    rel, past, later = fbm[p["relative_id"]], fbm[p["past_id"]], fbm[p["later_id"]]
    a.check("friday" in (rel["deadline"] or "").lower() and rel["urgency"] == 1.0, "by Friday resolved against the ticket date (4 days out, urgency 1.0)",
            f"'by Friday' on an Oct 5 ticket gave deadline {rel['deadline']!r} urgency {rel['urgency']}")
    a.check(later["urgency"] == 0.8, "Oct 30 on an Oct 5 ticket is 25 days out (0.8, not the 1.0 that today would give)",
            f"Oct 30 on an Oct 5 ticket got urgency {later['urgency']}, expected 0.8")
    shown = urgent_ids(ins)
    gone = [i for i in (p["relative_id"], p["past_id"]) if i not in shown]
    a.check(not gone, "lapsed items are still shown", f"lapsed items dropped from urgent_issues {gone}")
    flags = {u["feedback_id"]: u.get("lapsed") for u in ins["urgent_issues"]}
    unflagged = [i for i in (p["relative_id"], p["past_id"]) if flags.get(i) is not True]
    reasons = json.dumps([x["reasons"] for x in ins["at_risk_accounts"] if x["account"] == "Gringotts Bank"])
    a.check(not unflagged and bool(LAPSED_RX.search(reasons)), "lapsed items are labelled on the issue and in the at-risk reasons",
            f"the lapsed deadlines (Oct 9 and Sep 28, today is {p['today']}) are not all marked, issues without lapsed=true {unflagged}, "
            f"at-risk reasons mention it {bool(LAPSED_RX.search(reasons))}")
    return a.result()


def e7(c, srv):
    a = Acc()
    srv.ingest(FIX / c.p["fixture"])
    srv.analyze("all")
    got = urgent_ids(srv.insights())
    a.check(all(i in got for i in c.p["hard"]), f"hard wording surfaces {c.p['hard']}", f"hard wording missing from urgent {[i for i in c.p['hard'] if i not in got]}")
    a.check(not any(i in got for i in c.p["soft"]), f"soft wording stays out {c.p['soft']}", f"soft wording surfaced {[i for i in c.p['soft'] if i in got]}")
    return a.result()


def e8(c, srv):
    a = Acc()
    r = srv.ingest(FIX / c.p["fixture"])
    a.check(r.returncode == 0, "", f"ingest failed {r.stderr.strip()[:100]}")
    srv.analyze("all")
    fbm, ins = fbmap(srv), srv.insights()
    issues = {u["feedback_id"]: u for u in ins["urgent_issues"]}
    miss = [i for i in c.p["ids"] if i not in issues]
    a.check(not miss, f"{len(c.p['ids'])}/{len(c.p['ids'])} foreign-language tickets surface, no crash",
            f"{len(miss)}/{len(c.p['ids'])} foreign-language tickets with a deadline did not surface {miss}, urgency {[fbm[i]['urgency'] for i in miss]}, deadline {[fbm[i]['deadline'] for i in miss]}")
    bad = [i for i in c.p["ids"] if i in issues and not verbatim(issues[i]["quote"], fbm[i]["text"])]
    a.check(not bad, "quotes verbatim", f"quotes not verbatim {bad}")
    return a.result()


def e9(c, srv):
    a, p = Acc(), c.p
    srv.ingest(FIX / p["fixture"])
    t0 = time.perf_counter()
    srv.analyze("all")
    dur = time.perf_counter() - t0
    a.check(dur < p["max_seconds"], f"run took {dur:.1f}s", f"run took {dur:.1f}s")
    fbm, ins = fbmap(srv), srv.insights()
    text = fbm[p["id"]]["text"]
    a.check(len(text) >= 4900, f"input is {len(text)} chars", f"fixture is only {len(text)} chars")
    issue = next((u for u in ins["urgent_issues"] if u["feedback_id"] == p["id"]), None)
    if not a.check(issue is not None, "", "the long thread did not surface as urgent"):
        return a.result()
    q = issue["quote"]
    a.check(len(q) <= p["max_quote"], f"quote is {len(q)} chars", f"quote is {len(q)} chars, cap {p['max_quote']}")
    a.check(verbatim(q, text), "quote is verbatim", "quote is not verbatim")
    a.check(not issue["deadline"] or norm(issue["deadline"]) in norm(q), "deadline sits inside the quote",
            f"deadline {issue['deadline']!r} is not in the {len(q)}-char quote, which stops at '...{q[-40:]}'")
    return a.result()


def e10(c, srv):
    a, p = Acc(), c.p
    r = srv.ingest(FIX / p["fixture"])
    out = r.stdout + r.stderr
    a.check(r.returncode == 0 and "Traceback" not in out, "", f"ingest crashed or failed {out.strip()[:120]}")
    m = re.search(r"\((\d+) skipped", out)
    a.check(bool(m) and int(m.group(1)) == p["skipped"], f"skipped count reported ({m.group(0) if m else ''})",
            f"CLI did not report {p['skipped']} skipped rows, it said {out.strip()[:100]!r}")
    srv.analyze("all")
    hot = [i for i in p["one_word_ids"] if i in urgent_ids(srv.insights())]
    a.check(not hot, "analysis ran, one-word rows not urgent", f"one-word rows surfaced as urgent {hot}")
    resp = srv.post("/feedback", json={"source": "email", "account": "X", "text": ""})
    a.check(resp.status_code == 422, "API rejects empty text with 422", f"empty text gave {resp.status_code}")
    return a.result()


HANDLERS = {"E1": e1, "E2": e2, "E3": e3, "E4": e4, "E5": e5, "E6": e6, "E7": e7, "E8": e8, "E9": e9, "E10": e10}
