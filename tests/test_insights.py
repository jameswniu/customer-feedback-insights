"""Insights contract, scoring formulas, evidence verification, recall and team filter (read-only, in-process)."""
import pytest

from conftest import CS_TAGS, PRODUCT_TAGS, norm

URGENT_T, CRITICAL_T, AT_RISK_T = 0.4, 0.35, 0.35
PRIORITIES = {"urgent+critical", "urgent", "critical", "watch"}
CONF = {"low", "medium", "high"}
TAXONOMY = {"bug", "performance", "usability", "feature_request", "integration", "security",
            "billing", "onboarding", "support_quality", "churn_risk", "renewal", "other"}
TEAM_TAGS = {"product": PRODUCT_TAGS | {"other"}, "customer_success": CS_TAGS | {"other"}}


def label(u, c):
    urgent, critical = u >= URGENT_T, c >= CRITICAL_T
    return "urgent+critical" if urgent and critical else "urgent" if urgent else "critical" if critical else "watch"


def claims(ins):
    """Every (kind, key, evidence dict) the insights make."""
    for t in ins["themes"]:
        assert t["evidence"], f"theme {t['id']} has no evidence"
        for e in t["evidence"]:
            yield "theme", t["id"], e
    for u in ins["urgent_issues"]:
        yield "urgent", u["feedback_id"], {k: u.get(k) for k in ("feedback_id", "account", "source", "quote")}
    for a in ins["at_risk_accounts"]:
        assert a["evidence"], f"at-risk account {a['account']} has no evidence"
        for e in a["evidence"]:
            yield "account", a["account"], e


# ---- shape ----------------------------------------------------------------------------------

def test_insights_shape(insights):
    for k in ("run_id", "generated_at", "team", "mode", "themes", "urgent_issues", "at_risk_accounts", "dropped_unverified"):
        assert k in insights, k
    assert insights["mode"] == "offline"
    assert isinstance(insights["dropped_unverified"], int) and insights["dropped_unverified"] >= 0
    assert insights["themes"], "no themes at all on the seed set"
    for t in insights["themes"]:
        for k in ("id", "name", "description", "urgency", "criticality", "related_feedback_ids", "accounts",
                  "confidence", "priority", "evidence"):
            assert k in t, (k, t.get("id"))
        assert t["priority"] in PRIORITIES and t["confidence"] in CONF
    for u in insights["urgent_issues"]:
        for k in ("feedback_id", "account", "source", "quote", "deadline", "theme_id", "confidence"):
            assert k in u, k
        assert u["confidence"] in CONF
    for a in insights["at_risk_accounts"]:
        for k in ("account", "risk", "confidence", "reasons", "evidence"):
            assert k in a, k
        assert a["confidence"] in CONF


def test_feedback_fields_follow_taxonomy(all_feedback):
    for f in all_feedback:
        assert f["sentiment"] in ("negative", "neutral", "positive"), f["id"]
        assert set(f["tags"]) <= TAXONOMY, (f["id"], f["tags"])
        assert f["analyzed_at"], f["id"]


# ---- scoring formulas (black box, from the contract constants) -------------------------------

def test_feedback_urgency_values_follow_contract(all_feedback):
    allowed = {0.0, 0.4, 0.7, 0.8, 1.0}
    for f in all_feedback:
        assert round(f["urgency"], 2) in allowed, (f["id"], f["urgency"])
        if f["deadline"]:
            assert round(f["urgency"], 2) in (0.8, 1.0), (f["id"], f["deadline"], f["urgency"])


def test_theme_scores_follow_contract(insights, by_id):
    for t in insights["themes"]:
        ids = t["related_feedback_ids"]
        assert ids, t["id"]
        accts = {by_id[i]["account"] for i in ids}
        assert set(t["accounts"]) == accts, (t["id"], t["accounts"], accts)
        crit = min(1.0, 0.6 * min(len(accts) / 5, 1) + 0.4 * min(len(ids) / 10, 1))
        assert t["criticality"] == pytest.approx(crit, abs=0.01), (t["id"], t["criticality"], crit)
        assert t["urgency"] == pytest.approx(max(by_id[i]["urgency"] for i in ids), abs=0.01), t["id"]
        assert t["priority"] == label(t["urgency"], t["criticality"]), (t["id"], t["priority"])


def test_account_risk_follows_contract(insights, all_feedback):
    per = {}
    for f in all_feedback:
        per.setdefault(f["account"], []).append(f)
    assert insights["at_risk_accounts"], "no at-risk account on a seed set with churn language"
    for a in insights["at_risk_accounts"]:
        fs = per[a["account"]]
        neg = sum(f["sentiment"] == "negative" for f in fs) / len(fs)
        themes = len({f["theme_id"] for f in fs if f["theme_id"]})
        risk = min(1.0, 0.4 * neg + 0.4 * max(f["urgency"] for f in fs) + 0.2 * min(themes / 3, 1))
        assert a["risk"] == pytest.approx(risk, abs=0.05), (a["account"], a["risk"], risk)
        assert a["risk"] >= AT_RISK_T, a["account"]


# ---- every claim is verifiable ---------------------------------------------------------------

@pytest.mark.parametrize("team", [None, "product", "customer_success"])
def test_every_quote_is_a_substring_of_its_feedback(client, by_id, team):
    r = client.get("/insights", params={"teams": team} if team else None)
    assert r.status_code == 200
    n, bad = 0, []
    for kind, key, e in claims(r.json()):
        n += 1
        f = by_id.get(e["feedback_id"])
        if f is None:
            bad.append((kind, key, "unknown feedback id", e["feedback_id"]))
        elif not (e["quote"] or "").strip() or norm(e["quote"]) not in norm(f["text"]):
            bad.append((kind, key, e["feedback_id"], e["quote"]))
        elif e.get("account") and e["account"] != f["account"]:
            bad.append((kind, key, "account mismatch", e["account"], f["account"]))
        elif e.get("source") and e["source"] != f["source"]:
            bad.append((kind, key, "source mismatch", e["source"], f["source"]))
    assert n > 0
    assert not bad, bad[:5]


def test_deadlines_appear_in_the_quote(insights, by_id):
    bad = [(u["feedback_id"], u["deadline"], u["quote"]) for u in insights["urgent_issues"]
           if u["deadline"] and norm(u["deadline"]) not in norm(u["quote"])]
    assert not bad, f"deadline not in its quote: {bad[:3]}"


def test_no_deadline_invented(all_feedback, by_id, expected):
    for i in expected["no_deadline_ids"]:
        assert by_id[i]["deadline"] is None, (i, by_id[i]["deadline"], by_id[i]["text"])
    for f in all_feedback:
        if f["deadline"]:
            assert norm(f["deadline"]) in norm(f["text"]), (f["id"], f["deadline"], f["text"])


# ---- recall over precision -------------------------------------------------------------------

def test_recall_on_expected_urgent(insights, expected):
    urgent = {u["feedback_id"] for u in insights["urgent_issues"]}
    exp = set(expected["expected_urgent"])
    recall = len(exp & urgent) / len(exp)
    precision = len(exp & urgent) / len(urgent) if urgent else 0.0
    print(f"urgent recall={recall:.2f} precision={precision:.2f} (precision is never asserted) missed={sorted(exp - urgent)}")
    assert recall >= 0.9, f"missed {sorted(exp - urgent)}"


def test_recall_on_expected_critical(insights, expected):
    in_critical = {i for t in insights["themes"] if "critical" in t["priority"] for i in t["related_feedback_ids"]}
    exp = set(expected["expected_critical"])
    recall = len(exp & in_critical) / len(exp)
    print(f"critical recall={recall:.2f} missed={sorted(exp - in_critical)}")
    assert recall >= 0.7, f"missed {sorted(exp - in_critical)}"


def test_recall_on_expected_at_risk_accounts(insights, expected):
    got = {a["account"] for a in insights["at_risk_accounts"]}
    exp = set(expected["expected_at_risk_accounts"])
    recall = len(exp & got) / len(exp)
    print(f"at-risk recall={recall:.2f} missed={sorted(exp - got)}")
    assert recall >= 0.75, f"missed {sorted(exp - got)}"


# ---- team filter -----------------------------------------------------------------------------

@pytest.mark.parametrize("team", ["product", "customer_success"])
def test_team_filter_neither_leaks_nor_loses(client, insights, by_id, team):
    allowed = TEAM_TAGS[team]
    r = client.get("/insights", params={"teams": team})
    assert r.status_code == 200
    ins = r.json()
    assert ins["team"] == team

    def hit(ids):
        return any(set(by_id[i]["tags"]) & allowed for i in ids)

    got_themes = {t["id"] for t in ins["themes"]}
    want_themes = {t["id"] for t in insights["themes"] if hit(t["related_feedback_ids"])}
    assert got_themes == want_themes, (team, "leak", sorted(got_themes - want_themes), "lost", sorted(want_themes - got_themes))
    got_urgent = {u["feedback_id"] for u in ins["urgent_issues"]}
    want_urgent = {u["feedback_id"] for u in insights["urgent_issues"] if hit([u["feedback_id"]])}
    assert got_urgent == want_urgent, (team, "leak", sorted(got_urgent - want_urgent), "lost", sorted(want_urgent - got_urgent))
    for a in ins["at_risk_accounts"]:
        assert hit([f["id"] for f in by_id.values() if f["account"] == a["account"]]), (team, "at-risk leak", a["account"])


def test_unknown_team_is_400(client):
    assert client.get("/insights", params={"teams": "bogus"}).status_code == 400
    assert client.get("/insights", params={"teams": "product"}).status_code == 200
    assert client.get("/insights", params={"teams": "customer_success"}).status_code == 200
