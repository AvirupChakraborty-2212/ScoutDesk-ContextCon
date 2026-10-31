"""Pydantic data contracts — v2, aligned to the theme deck."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Fact Ledger
# ---------------------------------------------------------------------------

class Fact(BaseModel):
    """A single verified data point with full provenance and temporal metadata."""

    id: str                                     # "F12", unique within a run
    company_id: str                             # crustdata company id or domain
    subject: str                                # "company" | "founder:<n>" | "competitor:<domain>"
    key: str                                    # "funding.round", "founder.prior_employer", …
    value: Any = None
    unit: Optional[str] = None                  # "%", "INR", "USD", "people", "months", "date"
    endpoint: str                               # "company/enrich" | "derived"
    field_path: str                             # "headcount.growth_percent.yoy"
    derived_from: list[str] = Field(default_factory=list)  # fact ids, for derived facts
    known_since: Optional[date] = None          # when this fact became true/public; None = unknown
    fetched_at: datetime
    raw_excerpt: str                            # small JSON snippet for the citation popover


# ---------------------------------------------------------------------------
# Agent I/O
# ---------------------------------------------------------------------------

class AgentTask(BaseModel):
    """A unit of work dispatched by the orchestrator to an agent."""

    run_id: str
    agent: str
    company_id: str
    budget_credits: int = 10


class AgentResult(BaseModel):
    """What an agent hands back after completing its task."""

    agent: str
    company_id: str
    fact_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)       # short notes citing fact ids
    unknowns: list[str] = Field(default_factory=list)       # "no funding data", etc.
    credits_used: int = 0
    status: Literal["ok", "partial", "failed"] = "ok"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class Score(BaseModel):
    """Computed score for a company against the thesis."""

    total: float                                # 0–100
    parts: dict[str, float]                     # founders, thesis_fit, signals, momentum
    confidence: float                           # 0–1, from data coverage
    reasons: list[str]                          # each cites fact ids


# ---------------------------------------------------------------------------
# Memo & Verification
# ---------------------------------------------------------------------------

class MemoSentence(BaseModel):
    """One sentence of the investment memo, with fact-id placeholders."""

    template: str                               # "Headcount grew {F12} YoY"
    fact_ids: list[str] = Field(default_factory=list)
    rendered: str = ""                          # filled in by code after verification


class Memo(BaseModel):
    """Investment-committee memo with verifiable citations."""

    company_id: str
    sections: dict[str, list[MemoSentence]]     # summary, founders, market, signals, risks, bear_case
    removed_by_verifier: int = 0
    revisions: int = 0


class Challenge(BaseModel):
    """A skeptic's objection to a memo claim."""

    sentence_index: tuple[str, int]             # (section_name, index)
    objection: str
    counter_fact_ids: list[str] = Field(default_factory=list)
    requested_lookup: Optional[str] = None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

class Verdict(BaseModel):
    """Final decision on a company."""

    company_id: str
    name: str
    decision: Literal["back", "watch", "pass", "human_review"]
    score: Score
    memo: Memo
    rationale: str
    flags: list[str] = Field(default_factory=list)
    check_size: Optional[float] = None             # from the allocator, if enabled
    company_profile: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Runway Watch
# ---------------------------------------------------------------------------

class RunwayReport(BaseModel):
    """Cash-out risk estimate for a portfolio company (proxy, clearly labelled)."""

    company_id: str
    est_months_left: Optional[float]
    level: Literal["ok", "watch", "alert", "unknown"]
    signals: list[str]                          # each cites fact ids
    assumptions: list[str]                      # "burn = headcount × $2,500/month (India)"


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class BacktestCase(BaseModel):
    """One company's outcome in a backtest."""

    company_id: str
    decision: str
    score: float
    outcome_raised_within_horizon: Optional[bool]  # from funding rounds dated after as_of
    outcome_fact_ids: list[str] = Field(default_factory=list)


class BacktestReport(BaseModel):
    """Aggregate backtest results: did the calls hold up?"""

    as_of: date
    horizon_months: int
    n: int
    back_hit_rate: float                        # share of "back" calls that raised within horizon
    pass_hit_rate: float                        # share of "pass" calls that raised (lower is better)
    base_rate: float                            # share of whole cohort that raised
    baseline_hit_rate: float                    # naive baseline: top-k by headcount
    facts_blocked: int
    cases: list[BacktestCase]


# ---------------------------------------------------------------------------
# SSE Events
# ---------------------------------------------------------------------------

class Event(BaseModel):
    """A single event streamed to the UI via SSE."""

    run_id: str
    ts: datetime
    agent: str
    company_id: Optional[str] = None
    kind: Literal[
        "start", "call", "fact", "firewall", "finding", "challenge",
        "verify", "verdict", "runway", "backtest",
        "error", "done", "credits",
    ]
    message: str
    data: dict = Field(default_factory=dict)
