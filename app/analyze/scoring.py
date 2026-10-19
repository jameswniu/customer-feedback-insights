"""Recall-first scoring constants and pure functions (see CLAUDE.md, Scoring)."""
URGENT_T = 0.4
CRITICAL_T = 0.35
AT_RISK_T = 0.35


def feedback_urgency(days_to_deadline=None, has_deadline=False, blocking=False, frustrated=False) -> float:
    """1.0 deadline within 14 days; 0.8 later or unresolved deadline; 0.7 blocked/outage/cancel/escalate; 0.4 frustration; else 0."""
    if has_deadline or days_to_deadline is not None:
        return 1.0 if (days_to_deadline is not None and days_to_deadline <= 14) else 0.8
    if blocking:
        return 0.7
    return 0.4 if frustrated else 0.0


def theme_urgency(item_urgencies) -> float:
    return max(list(item_urgencies) or [0.0])


def theme_criticality(n_accounts: int, n_items: int) -> float:
    return min(1.0, 0.6 * min(n_accounts / 5, 1) + 0.4 * min(n_items / 10, 1))


def account_risk(negative_share: float, max_urgency: float, n_themes: int) -> float:
    return min(1.0, 0.4 * negative_share + 0.4 * max_urgency + 0.2 * min(n_themes / 3, 1))


def priority_label(urgency: float, criticality: float) -> str:
    u, c = urgency >= URGENT_T, criticality >= CRITICAL_T
    return "urgent+critical" if u and c else "urgent" if u else "critical" if c else "watch"


def confidence_label(score: float) -> str:
    """How sure we are it matters: explicit dated deadline high, blocking keyword medium, frustration only low."""
    return "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
