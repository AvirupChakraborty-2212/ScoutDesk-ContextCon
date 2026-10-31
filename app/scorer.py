"""Scorer — pure code, thesis-fit scoring with sub-scores and confidence."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.models import Fact, Score

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The eight key facts that drive the confidence denominator.
_EXPECTED_KEYS: list[str] = [
    "founder.prior_employer_headcount",
    "founder.years_experience",
    "company.headcount_total",
    "company.funding_rounds_count",
    "company.founded_year",
    "company.country",
    "openings.last_90d",
    "company.headcount_yoy",
]

_DIMENSION_KEYS: dict[str, list[str]] = {
    "founders": [
        "founder.prior_employer_headcount",
        "founder.years_at_prior",
        "founder.is_repeat_founder",
        "founder.years_experience",
    ],
    "thesis_fit": [
        "company.sector",
        "company.country",
        "company.founded_year",
        "company.headcount_total",
        "company.funding_rounds_count",
    ],
    "signals": [
        "openings.last_90d",
        "openings.share_ml",
        "openings.share_gtm",
        "signals.has_recent_news",
    ],
    "momentum": [
        "company.headcount_mom",
        "company.headcount_yoy",
    ],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _numeric(value: Any) -> float | None:
    """Coerce a fact value to float, returning None if not possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_val(value: Any) -> bool | None:
    """Coerce a fact value to bool."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return None


def _group_by_key(facts: list[Fact]) -> dict[str, list[Fact]]:
    """Index facts by their key."""
    index: dict[str, list[Fact]] = {}
    for f in facts:
        index.setdefault(f.key, []).append(f)
    return index


def _has_key(index: dict[str, list[Fact]], key: str) -> bool:
    return bool(index.get(key))


def _first_numeric(index: dict[str, list[Fact]], key: str) -> tuple[float | None, list[Fact]]:
    """Return (value, [facts]) for the first numeric fact matching key."""
    facts_for_key = index.get(key, [])
    for f in facts_for_key:
        v = _numeric(f.value)
        if v is not None:
            return v, [f]
    return None, []


def _fact_ids(facts: list[Fact]) -> str:
    """Format a list of facts as '[F1, F2]'."""
    if not facts:
        return ""
    return "[" + ", ".join(f.id for f in facts) + "]"


def _thesis_sectors(thesis: dict) -> list[str]:
    """Return lowercase sector strings from thesis."""
    raw = thesis.get("sectors", [])
    if isinstance(raw, list):
        return [s.lower() for s in raw]
    if isinstance(raw, str):
        return [raw.lower()]
    return []


def _thesis_geographies(thesis: dict) -> list[str]:
    raw = thesis.get("geographies", [])
    if isinstance(raw, list):
        return [g.lower() for g in raw]
    if isinstance(raw, str):
        return [raw.lower()]
    return []


def _has_ml_focus(thesis: dict) -> bool:
    """True when any thesis sector string contains 'ml', 'ai', 'tech', or 'developer'."""
    keywords = {"ml", "ai", "tech", "developer", "data"}
    for sector in _thesis_sectors(thesis):
        if any(kw in sector for kw in keywords):
            return True
    return False


def _has_b2b_focus(thesis: dict) -> bool:
    """True when any thesis sector string contains 'b2b'."""
    return any("b2b" in s for s in _thesis_sectors(thesis))


# ---------------------------------------------------------------------------
# Sub-scorers
# ---------------------------------------------------------------------------

def _score_founders(thesis: dict, facts: list[Fact]) -> tuple[float, list[str]]:
    """
    Score the founders dimension (0-100).

    Aggregates across all founder-scoped facts: for each unique founder
    subject prefix ("founder:0", "founder:1", …), compute the sub-components
    and take the best operator-signal result across founders.
    """
    cfg = thesis.get("founders", {})
    op_cfg = cfg.get("operator_signal", {})
    min_headcount: float = float(op_cfg.get("min_prior_company_headcount", 500))
    min_years: float = float(op_cfg.get("min_years", 3))
    repeat_bonus: bool = bool(cfg.get("repeat_founder_bonus", True))

    # Group facts by founder subject then by key
    # subject format: "founder:<n>" or "company" (for single founder)
    founder_facts: dict[str, dict[str, list[Fact]]] = {}
    other_facts: dict[str, list[Fact]] = {}

    for f in facts:
        subject = f.subject
        if subject.startswith("founder"):
            founder_facts.setdefault(subject, {}).setdefault(f.key, []).append(f)
        else:
            other_facts.setdefault(f.key, []).append(f)

    # If no founder-scoped facts but we have flat founder keys, treat as single founder
    if not founder_facts:
        flat_keys = [
            "founder.prior_employer_headcount",
            "founder.years_at_prior",
            "founder.is_repeat_founder",
            "founder.years_experience",
        ]
        flat_index: dict[str, list[Fact]] = {}
        for f in facts:
            if f.key in flat_keys:
                flat_index.setdefault(f.key, []).append(f)
        if flat_index:
            founder_facts["founder:0"] = flat_index

    reasons: list[str] = []

    # Per-founder scoring: collect operator signal pts across founders
    best_op_pts: float = 0.0
    best_op_reason: str = ""
    repeat_pts: float = 0.0
    repeat_reason: str = ""
    exp_pts: float = 0.0
    exp_reason: str = ""

    for subject, findex in founder_facts.items():
        # --- Operator signal: +60 pts if headcount >= min AND years_at_prior >= min_years
        hc_val, hc_facts = _first_numeric(findex, "founder.prior_employer_headcount")
        yr_val, yr_facts = _first_numeric(findex, "founder.years_at_prior")

        op_pts: float = 0.0
        if hc_val is not None and yr_val is not None:
            if hc_val >= min_headcount and yr_val >= min_years:
                op_pts = 60.0
                combined_facts = hc_facts + yr_facts
                candidate_reason = (
                    f"Operator signal: prior employer {int(hc_val):,} employees, "
                    f"{yr_val:.1f} yrs tenure {_fact_ids(combined_facts)}"
                )
                if op_pts > best_op_pts:
                    best_op_pts = op_pts
                    best_op_reason = candidate_reason
        elif hc_val is not None:
            # Partial: employer size known but years unknown — award partial if headcount qualifies
            if hc_val >= min_headcount:
                op_pts = 30.0  # half credit for partial data
                candidate_reason = (
                    f"Operator signal (partial — years unknown): prior employer "
                    f"{int(hc_val):,} employees {_fact_ids(hc_facts)}"
                )
                if op_pts > best_op_pts:
                    best_op_pts = op_pts
                    best_op_reason = candidate_reason

        # --- Repeat founder: +25 pts (take max across founders)
        if repeat_bonus:
            rf_facts = findex.get("founder.is_repeat_founder", [])
            for f in rf_facts:
                val = _bool_val(f.value)
                if val is True:
                    if repeat_pts < 25.0:
                        repeat_pts = 25.0
                        repeat_reason = f"Repeat founder {_fact_ids([f])}"

        # --- Years experience: scale 0-15 pts per founder (take max)
        exp_val, exp_facts_list = _first_numeric(findex, "founder.years_experience")
        if exp_val is not None:
            scaled = min(exp_val / 10.0, 1.0) * 15.0
            if scaled > exp_pts:
                exp_pts = scaled
                exp_reason = (
                    f"Experience: {exp_val:.0f} yrs → {scaled:.1f}/15 pts "
                    f"{_fact_ids(exp_facts_list)}"
                )

    # Assemble
    total: float = best_op_pts + repeat_pts + exp_pts
    total = min(total, 100.0)

    if best_op_reason:
        reasons.append(best_op_reason)
    if repeat_reason:
        reasons.append(repeat_reason)
    if exp_reason:
        reasons.append(exp_reason)
    if not reasons:
        reasons.append("Founders: no signal data available")

    return total, reasons


def _score_thesis_fit(thesis: dict, facts: list[Fact]) -> tuple[float, list[str]]:
    """Score the thesis_fit dimension (0-100)."""
    company_cfg = thesis.get("company", {})
    hc_cfg = company_cfg.get("headcount", {})
    hc_min: float = float(hc_cfg.get("min", 2))
    hc_max: float = float(hc_cfg.get("max", 40))
    max_rounds: int = int(company_cfg.get("max_rounds", 0))

    # Parse founded_after
    founded_after_raw = company_cfg.get("founded_after")
    founded_after_year: int | None = None
    if founded_after_raw:
        if isinstance(founded_after_raw, date):
            founded_after_year = founded_after_raw.year
        elif isinstance(founded_after_raw, str):
            try:
                founded_after_year = date.fromisoformat(str(founded_after_raw)).year
            except ValueError:
                pass
        elif isinstance(founded_after_raw, int):
            founded_after_year = founded_after_raw

    sectors = _thesis_sectors(thesis)
    geographies = _thesis_geographies(thesis)

    index = _group_by_key(facts)
    total: float = 0.0
    reasons: list[str] = []

    # --- Sector match: +30 pts
    sector_facts = index.get("company.sector", [])
    sector_desc_facts = index.get("company.description", [])
    sector_matched = False
    if sectors:
        text_facts = sector_facts or sector_desc_facts
        for f in text_facts:
            txt = str(f.value or "").lower()
            if any(s in txt for s in sectors):
                sector_matched = True
                total += 30.0
                reasons.append(f"Sector match: '{f.value}' fits thesis {_fact_ids([f])}")
                break
        if not sector_matched and text_facts:
            reasons.append(f"Sector mismatch: '{text_facts[0].value}' not in thesis sectors")

    # --- Geo match: +25 pts
    country_facts = index.get("company.country", [])
    if geographies and country_facts:
        country_val = str(country_facts[0].value or "").lower()
        if any(g in country_val or country_val in g for g in geographies):
            total += 25.0
            reasons.append(f"Geo match: {country_facts[0].value} {_fact_ids(country_facts[:1])}")
        else:
            reasons.append(
                f"Geo mismatch: {country_facts[0].value} not in {thesis.get('geographies')}"
            )

    # --- Founded after: +20 pts
    founded_facts = index.get("company.founded_year", [])
    if founded_after_year is not None and founded_facts:
        fy_val, fy_fs = _first_numeric(index, "company.founded_year")
        if fy_val is not None:
            if int(fy_val) >= founded_after_year:
                total += 20.0
                reasons.append(
                    f"Founded {int(fy_val)} ≥ {founded_after_year} {_fact_ids(fy_fs)}"
                )
            else:
                reasons.append(
                    f"Founded {int(fy_val)} < {founded_after_year} (too early) {_fact_ids(fy_fs)}"
                )

    # --- Headcount in range: +15 pts
    hc_val, hc_fs = _first_numeric(index, "company.headcount_total")
    if hc_val is not None:
        if hc_min <= hc_val <= hc_max:
            total += 15.0
            reasons.append(
                f"Headcount {int(hc_val)} in [{int(hc_min)}, {int(hc_max)}] "
                f"{_fact_ids(hc_fs)}"
            )
        else:
            reasons.append(
                f"Headcount {int(hc_val)} outside [{int(hc_min)}, {int(hc_max)}] "
                f"{_fact_ids(hc_fs)}"
            )

    # --- Rounds OK: +10 pts
    rounds_val, rounds_fs = _first_numeric(index, "company.funding_rounds_count")
    if rounds_val is not None:
        if rounds_val <= max_rounds:
            total += 10.0
            reasons.append(
                f"Funding rounds {int(rounds_val)} ≤ {max_rounds} (pre-seed) "
                f"{_fact_ids(rounds_fs)}"
            )
        else:
            reasons.append(
                f"Funding rounds {int(rounds_val)} > {max_rounds} (too many) "
                f"{_fact_ids(rounds_fs)}"
            )

    if not reasons:
        reasons.append("Thesis fit: no company facts available")

    return min(total, 100.0), reasons


def _score_signals(thesis: dict, facts: list[Fact]) -> tuple[float, list[str]]:
    """Score the signals dimension (0-100)."""
    has_ml = _has_ml_focus(thesis)
    has_b2b = _has_b2b_focus(thesis)

    index = _group_by_key(facts)
    total: float = 0.0
    reasons: list[str] = []

    # --- Hiring last 90d: +40 pts
    openings_val, openings_fs = _first_numeric(index, "openings.last_90d")
    if openings_val is not None:
        if openings_val > 0:
            total += 40.0
            reasons.append(
                f"Hiring signal: {int(openings_val)} openings in last 90 days "
                f"{_fact_ids(openings_fs)}"
            )
        else:
            reasons.append(f"No openings in last 90 days {_fact_ids(openings_fs)}")

    # --- Hiring mix: +30 pts (ML/tech focus) or +20 pts (B2B/GTM focus)
    ml_share_val, ml_fs = _first_numeric(index, "openings.share_ml")
    gtm_share_val, gtm_fs = _first_numeric(index, "openings.share_gtm")

    if has_ml and ml_share_val is not None:
        if ml_share_val > 0.3:
            total += 30.0
            reasons.append(
                f"ML hiring mix {ml_share_val:.0%} > 30% (tech thesis aligned) "
                f"{_fact_ids(ml_fs)}"
            )
        else:
            reasons.append(
                f"ML hiring mix {ml_share_val:.0%} ≤ 30% {_fact_ids(ml_fs)}"
            )
    elif has_b2b and gtm_share_val is not None:
        if gtm_share_val > 0.3:
            total += 20.0
            reasons.append(
                f"GTM hiring mix {gtm_share_val:.0%} > 30% (B2B thesis aligned) "
                f"{_fact_ids(gtm_fs)}"
            )
        else:
            reasons.append(
                f"GTM hiring mix {gtm_share_val:.0%} ≤ 30% {_fact_ids(gtm_fs)}"
            )

    # --- Recent news: +30 pts
    news_facts = index.get("signals.has_recent_news", [])
    if news_facts:
        f0 = news_facts[0]
        has_news = _bool_val(f0.value)
        if has_news is True:
            total += 30.0
            reasons.append(f"Recent news/press activity {_fact_ids([f0])}")
        elif has_news is False:
            reasons.append(f"No recent news in last 90 days {_fact_ids([f0])}")

    if not reasons:
        reasons.append("Signals: no hiring/news data available")

    return min(total, 100.0), reasons


def _score_momentum(facts: list[Fact]) -> tuple[float, list[str]]:
    """Score the momentum dimension (0-100)."""
    index = _group_by_key(facts)
    total: float = 0.0
    reasons: list[str] = []

    # --- Headcount YoY: beats MoM for signal strength; check first
    yoy_val, yoy_fs = _first_numeric(index, "company.headcount_yoy")
    if yoy_val is not None:
        if yoy_val > 0.2:
            total = 100.0
            reasons.append(
                f"Headcount YoY growth {yoy_val:.0%} > 20% — strong momentum "
                f"{_fact_ids(yoy_fs)}"
            )
        elif yoy_val > 0:
            total = max(total, 80.0)
            reasons.append(
                f"Headcount YoY growth {yoy_val:.0%} — moderate momentum "
                f"{_fact_ids(yoy_fs)}"
            )
        else:
            reasons.append(
                f"Headcount YoY growth {yoy_val:.0%} — flat/negative {_fact_ids(yoy_fs)}"
            )

    # --- Headcount MoM
    mom_val, mom_fs = _first_numeric(index, "company.headcount_mom")
    if mom_val is not None:
        if mom_val > 0.05:
            if total < 80.0:
                total = max(total, 80.0)
                reasons.append(
                    f"Headcount MoM growth {mom_val:.0%} > 5% {_fact_ids(mom_fs)}"
                )
        elif mom_val > 0:
            if total < 50.0:
                total = max(total, 50.0)
                reasons.append(
                    f"Headcount MoM growth {mom_val:.0%} — positive {_fact_ids(mom_fs)}"
                )
        else:
            reasons.append(
                f"Headcount MoM growth {mom_val:.0%} — flat/negative {_fact_ids(mom_fs)}"
            )

    if not reasons:
        reasons.append("Momentum: no headcount growth data available")

    return min(total, 100.0), reasons


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def _compute_confidence(facts: list[Fact]) -> float:
    """
    Confidence = available_input_count / expected_input_count (8 key facts).

    A key is "available" if at least one fact with that key exists in the list
    and has a non-None value.
    """
    present: set[str] = set()
    for f in facts:
        if f.key in _EXPECTED_KEYS and f.value is not None:
            present.add(f.key)
    return len(present) / len(_EXPECTED_KEYS)


# ---------------------------------------------------------------------------
# Weight renormalisation
# ---------------------------------------------------------------------------

def _active_dimensions(
    thesis_weights: dict[str, float],
    facts: list[Fact],
) -> dict[str, float]:
    """
    Drop any dimension that has zero matching facts and renormalise the
    remaining weights to sum to 1.0.
    """
    index = _group_by_key(facts)
    active: dict[str, float] = {}

    for dim, weight in thesis_weights.items():
        dim_keys = _DIMENSION_KEYS.get(dim, [])
        has_any = any(_has_key(index, k) for k in dim_keys)
        if has_any:
            active[dim] = weight

    if not active:
        # Fall back to the full weight dict if somehow nothing matched
        return dict(thesis_weights)

    total_weight = sum(active.values())
    return {dim: w / total_weight for dim, w in active.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score(thesis: dict, facts: list[Fact], company_id: str) -> Score:
    """
    Compute a thesis-fit Score for *company_id* using the provided *facts*.

    The caller is responsible for pre-filtering facts through the time firewall
    before passing them here (only pass facts with known_since <= as_of).

    Parameters
    ----------
    thesis:
        Parsed thesis YAML as a plain dict.
    facts:
        All Fact objects relevant to this company (founders + company-level).
    company_id:
        Identifier for the company being scored (used only for bookkeeping).

    Returns
    -------
    Score
        total (0-100), parts dict, confidence (0-1), reasons list.
    """
    # Filter to just this company's facts plus its founders/competitors
    company_facts = [f for f in facts if f.company_id == company_id]
    # If caller already pre-filtered, use all provided facts
    if not company_facts:
        company_facts = facts

    # --- Raw weights from thesis
    raw_weights: dict[str, float] = thesis.get(
        "weights",
        {"founders": 0.45, "thesis_fit": 0.25, "signals": 0.20, "momentum": 0.10},
    )

    # --- Compute each dimension's raw score
    founders_raw, founders_reasons = _score_founders(thesis, company_facts)
    thesis_fit_raw, thesis_fit_reasons = _score_thesis_fit(thesis, company_facts)
    signals_raw, signals_reasons = _score_signals(thesis, company_facts)
    momentum_raw, momentum_reasons = _score_momentum(company_facts)

    part_scores: dict[str, float] = {
        "founders": founders_raw,
        "thesis_fit": thesis_fit_raw,
        "signals": signals_raw,
        "momentum": momentum_raw,
    }

    # --- Renormalise weights for dimensions with data
    effective_weights = _active_dimensions(raw_weights, company_facts)

    # --- Weighted total
    total: float = sum(
        part_scores[dim] * w for dim, w in effective_weights.items()
    )
    total = round(min(max(total, 0.0), 100.0), 2)

    # --- Confidence
    confidence: float = round(_compute_confidence(company_facts), 4)

    # --- Assemble reasons (labelled by dimension)
    all_reasons: list[str] = (
        founders_reasons
        + thesis_fit_reasons
        + signals_reasons
        + momentum_reasons
    )

    return Score(
        total=total,
        parts=part_scores,
        confidence=confidence,
        reasons=all_reasons,
    )
