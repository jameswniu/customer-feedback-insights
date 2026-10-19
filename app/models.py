"""Pydantic models shared by every module (contract: CLAUDE.md)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SENTIMENTS = ("negative", "neutral", "positive")
TAGS = (
    "bug", "performance", "usability", "feature_request", "integration", "security",
    "billing", "onboarding", "support_quality", "churn_risk", "renewal", "other",
)
TEAMS: dict[str, set[str]] = {
    "product": {"bug", "performance", "usability", "feature_request", "integration", "security", "other"},
    "customer_success": {"billing", "onboarding", "support_quality", "churn_risk", "renewal", "security", "other"},
}
STEPS = ("start", "load", "classify", "cluster", "score", "verify", "persist", "answer", "done", "error")

Confidence = Literal["low", "medium", "high"]
Priority = Literal["urgent+critical", "urgent", "critical", "watch"]


def now_iso() -> str:
    """Timezone-aware UTC, seconds precision (generated_at, ts, analyzed_at, updated_at)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


def _lower(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


class FeedbackIn(_Base):
    id: Optional[str] = None
    source: str = Field(min_length=1)
    account: str = Field(min_length=1)
    text: str = Field(min_length=1)
    timestamp: Optional[str] = None

    @field_validator("id", "timestamp", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return str(v)

    @field_validator("source", "account", "text", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class Feedback(_Base):
    id: str
    source: str
    account: str
    timestamp: Optional[str] = None
    text: str
    sentiment: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    urgency: float = 0.0
    deadline: Optional[str] = None
    theme_id: Optional[str] = None
    analyzed_at: Optional[str] = None


class Classification(_Base):
    id: str
    sentiment: str = "neutral"
    tags: list[str] = Field(default_factory=list)
    urgency: float = 0.0
    deadline: Optional[str] = None
    theme_label: str = "other"
    evidence_quote: str = ""


class Theme(_Base):
    id: str
    name: str
    description: str = ""
    urgency: float = 0.0
    criticality: float = 0.0
    related_feedback_ids: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    updated_at: Optional[str] = None

    _lc = field_validator("confidence", mode="before")(lambda cls, v: _lower(v))


class Evidence(_Base):
    feedback_id: str
    account: str = ""
    source: str = ""
    quote: str


class InsightTheme(Theme):
    priority: Priority = "watch"
    evidence: list[Evidence] = Field(default_factory=list)


class UrgentIssue(_Base):
    feedback_id: str
    account: str = ""
    source: str = ""
    quote: str
    deadline: Optional[str] = None
    theme_id: Optional[str] = None
    confidence: Confidence = "medium"

    _lc = field_validator("confidence", mode="before")(lambda cls, v: _lower(v))


class AtRiskAccount(_Base):
    account: str
    risk: float = 0.0
    confidence: Confidence = "medium"
    reasons: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    _lc = field_validator("confidence", mode="before")(lambda cls, v: _lower(v))


class Insights(_Base):
    run_id: str = ""
    generated_at: str = Field(default_factory=now_iso)
    team: Optional[str] = None
    mode: str = "offline"
    themes: list[InsightTheme] = Field(default_factory=list)
    urgent_issues: list[UrgentIssue] = Field(default_factory=list)
    at_risk_accounts: list[AtRiskAccount] = Field(default_factory=list)
    dropped_unverified: int = 0


class Event(_Base):
    """One pipeline step. `step` is one of STEPS (kept a plain str so a new step never crashes a run)."""
    step: str
    message: str = ""
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, step: str, message: str = "", **data: Any) -> "Event":
        return cls(step=step, message=message, data=data)


class AnalyzeResult(_Base):
    run_id: str
    analyzed: int = 0
    themes: int = 0
    dropped_unverified: int = 0
    duration_s: float = 0.0
    mode: str = "offline"


class LedgerIn(_Base):
    run_id: Optional[str] = None
    target: str = "insights"
    verdict: Literal["yes", "no"]
    reason: str = ""
    insights_sha256: Optional[str] = None

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v: Any) -> Any:
        v = _lower(v)
        return {"y": "yes", "n": "no"}.get(v, v)

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> Any:
        return (v or "").strip() if isinstance(v, (str, type(None))) else v

    @model_validator(mode="after")
    def _no_needs_reason(self) -> "LedgerIn":
        if self.verdict == "no" and not self.reason:
            raise ValueError("reason is required when verdict is 'no'")
        return self


class LedgerEntry(_Base):
    ts: str
    run_id: Optional[str] = None
    target: str = "insights"
    verdict: str
    reason: str = ""
    mode: str = "offline"
    insights_sha256: Optional[str] = None
