"""Scoring constants and pure functions, checked by hand against CLAUDE.md."""
import pytest

from app.analyze import scoring as s


def test_constants_are_recall_first():
    assert (s.URGENT_T, s.CRITICAL_T, s.AT_RISK_T) == (0.4, 0.35, 0.35)


@pytest.mark.parametrize("kw,want", [
    (dict(days_to_deadline=3), 1.0),
    (dict(days_to_deadline=14), 1.0),
    (dict(days_to_deadline=15), 0.8),
    (dict(has_deadline=True), 0.8),
    (dict(days_to_deadline=30, blocking=True), 0.8),
    (dict(blocking=True), 0.7),
    (dict(blocking=True, frustrated=True), 0.7),
    (dict(frustrated=True), 0.4),
    (dict(), 0.0),
])
def test_feedback_urgency(kw, want):
    assert s.feedback_urgency(**kw) == want


def test_theme_urgency_is_the_max():
    assert s.theme_urgency([0.4, 1.0, 0.7]) == 1.0
    assert s.theme_urgency([]) == 0.0


@pytest.mark.parametrize("accounts,items,want", [
    (1, 1, 0.16), (2, 2, 0.32), (3, 3, 0.48), (4, 5, 0.68), (5, 10, 1.0), (8, 30, 1.0),
])
def test_theme_criticality(accounts, items, want):
    assert s.theme_criticality(accounts, items) == pytest.approx(want)


@pytest.mark.parametrize("neg,urg,themes,want", [
    (1.0, 1.0, 3, 1.0), (0.0, 0.0, 0, 0.0), (1.0, 0.0, 0, 0.4), (0.0, 1.0, 0, 0.4),
    (0.5, 0.7, 1, 0.5 * 0.4 + 0.7 * 0.4 + 0.2 / 3),
])
def test_account_risk(neg, urg, themes, want):
    assert s.account_risk(neg, urg, themes) == pytest.approx(want)


@pytest.mark.parametrize("u,c,want", [
    (0.4, 0.35, "urgent+critical"), (1.0, 1.0, "urgent+critical"),
    (0.4, 0.34, "urgent"), (0.39, 0.35, "critical"), (0.39, 0.34, "watch"), (0.0, 0.0, "watch"),
])
def test_priority_label_thresholds_are_inclusive(u, c, want):
    assert s.priority_label(u, c) == want


def test_confidence_label_tiers():
    assert [s.confidence_label(x) for x in (1.0, 0.8, 0.79, 0.5, 0.49, 0.0)] == ["high", "high", "medium", "medium", "low", "low"]
