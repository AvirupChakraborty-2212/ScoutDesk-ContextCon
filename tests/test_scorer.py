"""Tests for scorer.py — pure code thesis scoring and confidence calculation."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.models import Fact
from app.scorer import score


def _make_fact(id: str, company: str, subject: str, key: str, value: any, unit: str | None = None) -> Fact:
    return Fact(
        id=id,
        company_id=company,
        subject=subject,
        key=key,
        value=value,
        unit=unit,
        endpoint="test",
        field_path=key,
        derived_from=[],
        known_since=date(2024, 1, 1),
        fetched_at=datetime.now(timezone.utc),
        raw_excerpt="test",
    )


def test_scorer_all_dimensions():
    c_id = "test-operator.ai"
    thesis = {
        "geographies": ["India"],
        "sectors": ["B2B SaaS", "developer tools"],
        "company": {"founded_after": 2023, "headcount": {"min": 2, "max": 40}, "max_rounds": 0},
        "founders": {
            "operator_signal": {"min_prior_company_headcount": 500, "min_years": 3},
            "repeat_founder_bonus": True,
        },
        "weights": {"founders": 0.45, "thesis_fit": 0.25, "signals": 0.20, "momentum": 0.10},
        "verdict_thresholds": {"back": 70, "watch": 50},
        "min_confidence_for_auto_verdict": 0.6,
    }

    facts = [
        # Founder facts (Operator + Repeat Founder + 10 yrs exp)
        _make_fact("F1", c_id, "founder:0", "founder.prior_employer_headcount", 1200, "employees"),
        _make_fact("F2", c_id, "founder:0", "founder.years_at_prior", 4.5, "years"),
        _make_fact("F3", c_id, "founder:0", "founder.is_repeat_founder", True),
        _make_fact("F4", c_id, "founder:0", "founder.years_experience", 10.0, "years"),

        # Thesis fit facts
        _make_fact("F5", c_id, "company", "company.country", "India"),
        _make_fact("F6", c_id, "company", "company.sector", "B2B SaaS"),
        _make_fact("F7", c_id, "company", "company.founded_year", 2023),
        _make_fact("F8", c_id, "company", "company.headcount_total", 12, "employees"),
        _make_fact("F9", c_id, "company", "company.funding_rounds_count", 0),

        # Signals facts
        _make_fact("F10", c_id, "company", "openings.last_90d", 5, "openings"),
        _make_fact("F11", c_id, "company", "openings.share_gtm", 0.40, "ratio"),
        _make_fact("F12", c_id, "company", "signals.has_recent_news", True),

        # Momentum facts
        _make_fact("F13", c_id, "company", "company.headcount_yoy", 0.35, "%"),
        _make_fact("F14", c_id, "company", "company.headcount_mom", 0.08, "%"),
    ]

    sc = score(thesis, facts, c_id)

    # All sub-scores should be high
    assert sc.parts["founders"] == 100.0  # 60 (operator) + 25 (repeat) + 15 (10y exp)
    assert sc.parts["thesis_fit"] == 100.0  # 30 (sector) + 25 (geo) + 20 (founded) + 15 (hc) + 10 (rounds)
    assert sc.parts["signals"] == 90.0  # 40 (last_90d) + 20 (gtm>30%) + 30 (news)
    assert sc.parts["momentum"] == 100.0  # YoY > 20%

    assert sc.total >= 90.0
    assert sc.confidence >= 0.8
    assert len(sc.reasons) > 5


def test_scorer_weight_renormalisation():
    c_id = "sparse.ai"
    thesis = {
        "geographies": ["India"],
        "sectors": ["AI"],
        "company": {"founded_after": 2023, "headcount": {"min": 2, "max": 40}, "max_rounds": 0},
        "founders": {"operator_signal": {"min_prior_company_headcount": 500, "min_years": 3}},
        "weights": {"founders": 0.50, "thesis_fit": 0.30, "signals": 0.10, "momentum": 0.10},
    }

    # Only founder facts present — signals and momentum are missing
    facts = [
        _make_fact("F1", c_id, "founder:0", "founder.prior_employer_headcount", 800, "employees"),
        _make_fact("F2", c_id, "founder:0", "founder.years_at_prior", 4.0, "years"),
        _make_fact("F3", c_id, "company", "company.country", "India"),
    ]

    sc = score(thesis, facts, c_id)
    # Weights should renormalise between founders and thesis_fit
    assert sc.total > 0
    assert sc.confidence < 0.5  # low data confidence
