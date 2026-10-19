"""Happy-path eval handlers H1 to H10. Each takes (Case, Srv) and returns (ok, detail)."""
from __future__ import annotations

import math
import re

from harness import (EXPECTED, FIX, SEED, Acc, bad_quotes, best_theme, fbmap, norm, only_tag, shown_ids,
                     urgent_ids, verbatim)

DATE_RX = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
                     r"|\bby (?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)


def h1(c, srv):
    want, ins, fbm, a = EXPECTED[c.p["expected_key"]], srv.insights(), fbmap(srv), Acc()
    got = {x["account"]: x for x in ins["at_risk_accounts"]}
    miss = [x for x in want if x not in got]
    a.check(not miss, f"all {len(want)} accounts on the at-risk list", f"missing from the at-risk list {miss}")
    noq = [x for x in want if x in got and not any(verbatim(e["quote"], fbm[e["feedback_id"]]["text"]) for e in got[x]["evidence"])]
    a.check(not noq, "each has a verbatim quote", f"no verbatim quote for {noq}")
    section = srv.cli("insights", "--no-rate").stdout.split("AT-RISK ACCOUNTS", 1)[-1]
    a.check(all(x in section for x in want), "CLI prints them too", f"CLI at-risk section lacks {[x for x in want if x not in section]}")
    return a.result()


def h2(c, srv):
    want, ins, fbm, a = EXPECTED[c.p["expected_key"]], srv.insights(), fbmap(srv), Acc()
    issues = {u["feedback_id"]: u for u in ins["urgent_issues"]}
    miss = [i for i in want if i not in issues]
    a.check(not miss, f"{len(want) - len(miss)}/{len(want)} labelled-urgent items surfaced (recall {1 - len(miss) / len(want):.2f})", f"missing {miss}")
    dated = [i for i in want if i in issues and DATE_RX.search(fbm[i]["text"])]
    nodl = [i for i in dated if not issues[i]["deadline"] or norm(issues[i]["deadline"]) not in norm(issues[i]["quote"])]
    a.check(not nodl, f"deadline visible in the quote on all {len(dated)} dated items", f"deadline missing from the quote on {nodl}")
    return a.result()


def h3(c, srv):
    ins, a = srv.insights(), Acc()
    for name, g in EXPECTED[c.p["expected_key"]].items():
        th = best_theme(ins, g["ids"])
        if th is None:
            a.check(False, "", f"{name} has no theme at all")
            continue
        ov, need = len(set(th["related_feedback_ids"]) & set(g["ids"])), math.ceil(c.p["min_overlap_share"] * len(g["ids"]))
        ok = ov >= need and len(th["accounts"]) >= c.p["min_accounts"] and "critical" in th["priority"]
        a.check(ok, f"{name} in '{th['name']}' ({th['priority']}, {len(th['accounts'])} accounts)",
                f"{name} best theme '{th['name']}' holds {ov}/{len(g['ids'])} ids, {len(th['accounts'])} accounts, priority {th['priority']}")
    return a.result()


def h4(c, srv):
    team, tag, fbm, a = c.p["team"], c.p["hidden_when_only_tag"], fbmap(srv), Acc()
    ins = srv.insights(team)
    shown = shown_ids(ins)
    miss = [i for i in c.p["must_show"] if i not in shown]
    a.check(not miss, f"{c.p['must_show']} show", f"billing or renewal items missing from the {team} view {miss}")
    pure = [i for i, f in fbm.items() if only_tag(f, tag)]
    a.check(bool(pure), "", f"vacuous, no row is tagged only {tag}")
    leak = [i for i in pure if i in shown]
    a.check(not leak, f"{len(pure)} pure {tag} rows hidden", f"pure {tag} rows visible {leak}")
    out = srv.cli("insights", "--team", team, "--no-rate")
    a.check(out.returncode == 0 and f"INSIGHTS  {team}" in out.stdout, "CLI header names the team", "CLI output does not name the team")
    return a.result()


def h5(c, srv):
    team, tag, fbm, a = c.p["team"], c.p["hidden_when_only_tag"], fbmap(srv), Acc()
    shown = shown_ids(srv.insights(team))
    for t in c.p["must_show_tags"]:
        ids = [i for i, f in fbm.items() if t in f["tags"]]
        hit = [i for i in ids if i in shown]
        a.check(bool(ids) and bool(hit), f"{t} {len(hit)}/{len(ids)} show", f"no {t} item visible in the {team} view ({len(ids)} exist)")
    pure = [i for i, f in fbm.items() if only_tag(f, tag)]
    a.check(bool(pure), "", f"vacuous, no row is tagged only {tag}")
    leak = [i for i in pure if i in shown]
    a.check(not leak, f"{len(pure)} {tag}-only rows hidden", f"{tag}-only rows visible {leak}")
    return a.result()


def h6(c, srv):
    acct, ins, fbm, a = c.p["account"], srv.insights(), fbmap(srv), Acc()
    risk = next((x for x in ins["at_risk_accounts"] if x["account"] == acct), None)
    if not a.check(risk is not None, "", f"{acct} is not on the at-risk list"):
        return a.result()
    reasons = " | ".join(risk["reasons"])
    a.check(c.p["reason_word"] in reasons.lower(), f"reasons cite it ({reasons[:80]})", f"reasons never mention {c.p['reason_word']} (reasons {reasons})")
    ev = [e for e in risk["evidence"] if e["feedback_id"] == c.p["evidence_id"]]
    a.check(bool(ev) and all(verbatim(e["quote"], fbm[e["feedback_id"]]["text"]) for e in ev), f"{c.p['evidence_id']} quote is verbatim",
            f"no verbatim quote from {c.p['evidence_id']} (evidence ids {[e['feedback_id'] for e in risk['evidence']]})")
    return a.result()


def h7(c, srv):
    g, ins, fbm, a = EXPECTED["expected_critical_groups"][c.p["group"]], srv.insights(), fbmap(srv), Acc()
    th = best_theme(ins, g["ids"])
    if not a.check(th is not None, "", "no SSO theme found"):
        return a.result()
    bad = [(e["feedback_id"], e["quote"][:40]) for e in th["evidence"] if not verbatim(e["quote"], fbm[e["feedback_id"]]["text"])]
    a.check(bool(th["evidence"]) and not bad, f"{len(th['evidence'])} quotes verbatim in '{th['name']}'", f"quotes not verbatim {bad} or no evidence")
    named = [x for x in g["accounts"] if x in th["accounts"]]
    a.check(len(named) == len(g["accounts"]) >= c.p["min_accounts"], f"accounts named {named}", f"theme names {th['accounts']}, expected {g['accounts']}")
    a.check(th["name"] in srv.cli("insights", "--no-rate").stdout, "theme is in the CLI output", f"CLI output lacks '{th['name']}'")
    return a.result()


def h8(c, srv):
    got, a = urgent_ids(srv.insights()), Acc()
    miss = [i for i in c.p["ids"] if i not in got]
    a.check(not miss, f"all {len(c.p['ids'])} blocking items surface", f"blocking items missing {miss}")
    return a.result()


def h9(c, srv):
    a, new_ids = Acc(), c.p["new_ids"]
    before = {f["id"]: f["analyzed_at"] for f in srv.feedback()}
    ins0 = srv.insights()
    members_before = {t["id"]: set(t["related_feedback_ids"]) for t in ins0["themes"]}
    urgent_before = urgent_ids(ins0)
    r = srv.ingest(FIX / c.p["fixture"])
    a.check(r.returncode == 0 and f"Ingested {len(new_ids)}" in r.stdout, "", f"ingest of new tickets failed {r.stdout.strip()[:80]} {r.stderr.strip()[:80]}")
    out = srv.cli("analyze")
    lines = re.findall(r"^\S+\s+(\w+)\s+(.*)$", out.stdout, flags=re.M)
    steps = [s for s, _ in lines]
    order = ["start", "load", "classify", "cluster", "score", "verify", "persist", "answer", "done"]
    idx = [steps.index(s) for s in order if s in steps]
    a.check(out.returncode == 0 and set(order) <= set(steps) and idx == sorted(idx), "steps stream in order", f"steps were {steps}")
    load = next((m for s, m in lines if s == "load"), "")
    m = re.search(r"(\d+) to analyze", load)
    a.check(bool(m) and int(m.group(1)) == len(new_ids), f"only the {len(new_ids)} new rows analyzed", f"load step says '{load}'")
    after = {f["id"]: f for f in srv.feedback()}
    changed = [i for i, t in before.items() if after[i]["analyzed_at"] != t]
    a.check(not changed, "old rows untouched", f"{len(changed)} old rows were re-analyzed")
    a.check(all(after[i]["analyzed_at"] for i in new_ids), "", "new rows were not analyzed")
    ins = srv.insights()
    in_themes = {i for t in ins["themes"] for i in t["related_feedback_ids"]}
    refreshed = [t["name"] for t in ins["themes"] if set(t["related_feedback_ids"]) != members_before.get(t["id"], set())]
    a.check(set(new_ids) <= in_themes and bool(refreshed), f"themes refreshed {refreshed}, new rows joined them",
            f"new rows missing from themes {sorted(set(new_ids) - in_themes)} or no theme changed membership")
    gained = sorted(urgent_ids(ins) - urgent_before)
    a.check(bool(gained), f"newly urgent {gained}", "no newly urgent item although the new SAML ticket has a deadline")
    return a.result()


def h10(c, srv):
    a = Acc()
    r1 = srv.cli("insights", stdin=c.p["yes"] + "\n")
    a.check("Good? (yes/no) + reason:" in r1.stdout, "CLI asks Good? (yes/no) + reason", "CLI never asked the rating question")
    snap = srv.ledger_path().read_bytes() if srv.ledger_path().exists() else b""
    srv.cli("insights", stdin=c.p["no"] + "\n")
    srv.cli("insights", stdin=c.p["no_reason"] + "\n")
    rows = srv.ledger_lines()
    got = [(x["verdict"], x["reason"]) for x in rows]
    a.check(got == [("yes", ""), ("no", c.p["no"].split(" ", 1)[1])], "yes and no+reason saved, bare no refused",
            f"ledger holds {got}")
    a.check(srv.ledger_path().read_bytes().startswith(snap) and bool(snap), "append-only, earlier line unchanged", "ledger bytes changed")
    resp = srv.post("/ledger", json={"target": "insights", "verdict": "no", "reason": ""})
    a.check(resp.status_code == 422 and len(srv.ledger_lines()) == 2, "API refuses a no without a reason (422)",
            f"POST /ledger no+empty gave {resp.status_code}, ledger lines {len(srv.ledger_lines())}")
    return a.result()


HANDLERS = {"H1": h1, "H2": h2, "H3": h3, "H4": h4, "H5": h5, "H6": h6, "H7": h7, "H8": h8, "H9": h9, "H10": h10}
