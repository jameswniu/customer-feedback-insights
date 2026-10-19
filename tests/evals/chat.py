"""The eight everyday questions from docs/evals-questions.md (H1 to H8), asked as plain sentences. Each one goes through
POST /ask and through `python -m app.cli chat` with piped stdin, with the same pass conditions as the table and a control that must fail."""
from __future__ import annotations

import json
import re
import subprocess

from harness import EXPECTED, PY, ROOT, TEAMSET, Acc, fbmap, norm, only_tag

STEPS = ["understand", "fetch", "verify", "answer", "done"]
UNKNOWN = "What is the airspeed velocity of an unladen swallow?"
DATE_RX = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
                     r"|\bby (?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)


def ask(srv, q):
    r = srv.post("/ask", json={"question": q})
    if r.status_code != 200:
        raise RuntimeError(f"POST /ask answered {r.status_code}: {r.text[:120]}")
    ev = [json.loads(x[5:].strip()) for x in r.text.splitlines() if x.startswith("data:")]
    ans = next((e for e in ev if e["step"] == "answer"), None)
    if ans is None:
        raise RuntimeError("the stream has no answer event")
    return ev, ans


def chat(srv, q) -> str:
    r = subprocess.run([PY, "-m", "app.cli", "chat"], input=q + "\n\nexit\n", capture_output=True, text=True,
                       cwd=ROOT, env=srv.env, timeout=60)
    if r.returncode != 0 or "Traceback" in r.stderr:
        raise RuntimeError("chat command failed " + r.stderr[-120:])
    return r.stdout


def items(ans):
    return ans.get("data", {}).get("items") or []


def ids(ans):
    return {i["feedback_id"] for i in items(ans)}


def blob(ans):
    return (ans["message"] + " " + json.dumps(items(ans))).lower()


def bad(its, fbm):
    out = []
    for i in its:
        f, q = fbm.get(i.get("feedback_id")), i.get("quote") or ""
        if not (f and norm(q) and norm(q) in norm(f["text"]) and (not i.get("account") or i["account"] == f["account"])):
            out.append((i.get("feedback_id"), q[:40]))
    return out


def steps_ok(ev):
    s = [e["step"] for e in ev]
    idx = [s.index(x) for x in STEPS if x in s]
    return len(idx) == 5 and idx == sorted(idx) and "error" not in s and s[-1] == "done"


def cli_quotes_loose(out, fbm):
    texts = [norm(f["text"]) for f in fbm.values()]
    return [q for q in re.findall(r"[\"“]([^\"”]+)[\"”]", out) if not any(norm(q) in t for t in texts)]


def both(c, srv, q=None):
    """Ask once through each door. The stream must run in order and every quote printed must be verbatim."""
    q, fbm, a = q or c.p["question"], fbmap(srv), Acc()
    ev, ans = ask(srv, q)
    out = chat(srv, q)
    a.check(steps_ok(ev), "stream runs understand, fetch, verify, answer, done", f"steps were {[e['step'] for e in ev]}")
    a.check(not bad(items(ans), fbm), f"{len(items(ans))} quotes verbatim", f"quotes not verbatim {bad(items(ans), fbm)[:2]}")
    loose = cli_quotes_loose(out, fbm)
    a.check("Feedback Insights chat" in out and not loose, "chat command prints them verbatim too", f"chat printed quotes found in no ticket {loose[:2]} or no banner")
    return a, ans, out, fbm


def c1(c, srv):
    a, ans, out, fbm = both(c, srv)
    want = set(EXPECTED[c.p["expected_key"]])
    got = {i["account"] for i in items(ans)}
    a.check(want <= got, f"all {len(want)} accounts in the answer", f"accounts missing from the answer {sorted(want - got)}")
    a.check(all(x in out for x in want), "and in the chat output", f"chat output lacks {[x for x in want if x not in out]}")
    a.check(not want <= {i["account"] for i in items(ans) if i["account"] != "Acme Corp"}, "control, dropping one account fails the check", "control did not fail")
    a.check(bool(bad([dict(i, quote=i["quote"] + " and then they left") for i in items(ans)], fbm)), "control, an altered quote is caught", "control did not fail")
    return a.result()


def c2(c, srv):
    a, ans, out, fbm = both(c, srv)
    want = set(EXPECTED[c.p["expected_key"]])
    a.check(want <= ids(ans), f"{len(want)}/{len(want)} labelled-urgent items", f"urgent items missing {sorted(want - ids(ans))}")
    a.check(all(i in out for i in want), "all ids in the chat output", f"chat output lacks {[i for i in want if i not in out]}")
    by = {i["feedback_id"]: i for i in items(ans)}
    dated = [i for i in want if DATE_RX.search(fbm[i]["text"])]
    nodl = [i for i in dated if i in by and not DATE_RX.search(by[i]["quote"])]
    a.check(not nodl, f"deadline visible in the quote on all {len(dated)} dated items", f"deadline missing from the quote on {nodl}")
    a.check(not want <= {i for i in ids(ans) if i != "fb-014"}, "control, dropping one item fails the check", "control did not fail")
    return a.result()


def c3(c, srv):
    a, ans, out, fbm = both(c, srv)
    ins = srv.insights()
    themes = {t["id"]: t for t in ins["themes"]}
    text = blob(ans)
    for name, g in EXPECTED[c.p["expected_key"]].items():
        best = max(themes.values(), key=lambda t: len(set(t["related_feedback_ids"]) & set(g["ids"])))
        named = sum(x.lower() in text for x in best["accounts"])
        a.check(best["name"].lower() in text and best["name"].lower() in out.lower() and named >= 2,
                f"{name} in '{best['name']}' with {named} accounts named",
                f"{name}, theme '{best['name']}' or two of its accounts missing from the answer or the chat output")
    _, other = ask(srv, UNKNOWN)
    a.check(not any(t["name"].lower() in blob(other) for t in themes.values()), "control, an unrelated question names no theme", "control did not fail")
    return a.result()


def scoped(c, srv, team, must_show, pure_tag, wrong_ids_hint):
    a, ans, out, fbm = both(c, srv)
    shown = ids(ans)
    a.check(ans["data"].get("team") == team, f"answer is scoped to {team}", f"team is {ans['data'].get('team')!r}")
    miss = [i for i in must_show if i not in shown]
    a.check(not miss and all(i in out for i in must_show), f"{must_show} show in the answer and the chat output", f"missing {miss or 'from the chat output'}")
    leaks = [i for i in shown if not (set(fbm[i]["tags"] or ["other"]) & TEAMSET[team])]
    a.check(not leaks, "no item outside the team's tags", f"items outside the {team} tag set {leaks}")
    pure = [i for i, f in fbm.items() if only_tag(f, pure_tag)]
    a.check(bool(pure) and not (set(pure) & shown) and not any(i in out for i in pure), f"{len(pure)} {pure_tag}-only rows hidden",
            f"{pure_tag}-only rows shown {sorted(set(pure) & shown)} or no such row exists")
    other = "customer_success" if team == "product" else "product"
    _, allv = ask(srv, "What's urgent this week?")
    outside = [i for i in ids(allv) if not (set(fbm[i]["tags"] or ["other"]) & TEAMSET[team])]
    a.check(bool(outside) and not (set(outside) & shown), "control, the unscoped answer has items this scope hides", "control did not fail")
    return a, ans, out, fbm


def c4(c, srv):
    a, *_ = scoped(c, srv, "customer_success", c.p["must_show"], "feature_request", None)
    return a.result()


def c5(c, srv):
    a, ans, out, fbm = scoped(c, srv, "product", [], "billing", None)
    for tag in c.p["must_show_tags"]:
        a.check(any(tag in fbm[i]["tags"] for i in ids(ans)), f"{tag} item shows", f"no {tag} item in the product answer")
    return a.result()


def c6(c, srv):
    a, ans, out, fbm = both(c, srv)
    a.check(ans["data"].get("account") == c.p["account"], f"account matched {c.p['account']}", f"account is {ans['data'].get('account')!r}")
    a.check(c.p["evidence_id"] in ids(ans) and f"[{c.p['evidence_id']}]" in out, f"{c.p['evidence_id']} quoted in both", f"{c.p['evidence_id']} not quoted, ids {sorted(ids(ans))}")
    reasons = ans["message"]
    for q in [i["quote"] for i in items(ans)]:
        reasons = reasons.replace(q, " ")
    a.check(c.p["reason_word"] in (reasons + json.dumps([i.get("reasons") for i in items(ans)])).lower(), "reasons cite the Friday deadline, not only the quote",
            f"the deadline is only in the quote, reasons read {reasons[:120]!r}")
    _, other = ask(srv, "Why is Stark Industries at risk?")
    a.check(c.p["evidence_id"] not in ids(other), "control, another account's answer does not cite it", "control did not fail")
    return a.result()


def c7(c, srv):
    a, ans, out, fbm = both(c, srv)
    g = EXPECTED["expected_critical_groups"][c.p["group"]]
    a.check(len(ids(ans) & set(g["ids"])) >= 2, "quotes come from the SSO tickets", f"quotes are not from the SSO tickets {sorted(ids(ans))}")
    named = [x for x in g["accounts"] if x.lower() in blob(ans) and x in out]
    a.check(len(named) == len(g["accounts"]) >= c.p["min_accounts"], f"accounts named {named}", f"accounts named {named}, expected {g['accounts']}")
    _, none = ask(srv, "What's the evidence for the underwater basket weaving theme?")
    a.check(not items(none), "control, a theme that does not exist gets no quotes", f"quotes for a made-up theme {items(none)[:1]}")
    return a.result()


def c8(c, srv):
    a, ans, out, fbm = both(c, srv)
    want = set(c.p["ids"])
    a.check(want <= ids(ans) and all(i in out for i in want), f"all {len(want)} blocking items in both", f"blocking items missing {sorted(want - ids(ans))}")
    a.check(not ({"fb-022", "fb-025", "fb-037", "fb-038"} & ids(ans)), "control, positive-only tickets never listed as blocking", "a positive-only ticket is listed as blocking")
    return a.result()


HANDLERS = {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5, "C6": c6, "C7": c7, "C8": c8}
