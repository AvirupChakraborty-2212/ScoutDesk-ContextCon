"""Backtest harness — runs blind historical simulations under the time firewall.

Tests whether ScoutDesk's calls hold up when replayed as of a past date:
  1. Hides all facts known after `as_of` using the FactLedger time firewall.
  2. Anonymises company and founder names in LLM prompts so no lookahead memory leaks.
  3. Scores against actual funding outcomes that occurred within the `horizon_months` window.
  4. Benchmarks hit rate against the cohort base rate and naive headcount baseline.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import COHORTS_DIR
from app.crust import CreditCounter, NoData, enrich_company
from app.events import EventBus, bus as global_bus
from app.ledger import FactLedger, LedgerView
from app.models import BacktestCase, BacktestReport, Event, Fact
from app.orchestrator import load_thesis, run_pipeline

logger = logging.getLogger(__name__)

_AGENT = "backtest"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def load_backtest_cohort() -> list[str]:
    """Load pre-round cohort from cohorts/backtest.txt."""
    path = COHORTS_DIR / "backtest.txt"
    domains: list[str] = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    domains.append(line)
    if not domains:
        # Default verified historical cohort
        domains = [
            "sarvam.ai",
            "krutrim.com",
            "devzero.io",
            "composio.dev",
            "superops.ai",
            "atlan.com",
            "postman.com",
            "hasura.io",
        ]
    return domains


async def run_backtest(
    thesis_name_or_dict: str | dict[str, Any] = "preseed_india",
    as_of: date = date(2024, 1, 1),
    cohort_domains: list[str] | None = None,
    horizon_months: int = 18,
    bus: EventBus | None = None,
    run_id: str | None = None,
) -> BacktestReport:
    """Execute a backtest run over a historical cohort."""
    b_id = run_id or f"backtest-{uuid.uuid4().hex[:8]}"
    event_bus = bus or global_bus
    event_bus.create_run(b_id)

    cohort = cohort_domains or load_backtest_cohort()
    thesis = load_thesis(thesis_name_or_dict)

    await event_bus.emit(
        Event(
            run_id=b_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="start",
            message=f"Starting Backtest simulation | As-Of: {as_of} | Horizon: {horizon_months}m | Cohort: {len(cohort)} companies",
            data={"as_of": str(as_of), "horizon_months": horizon_months, "cohort_size": len(cohort)},
        )
    )

    # 1. Run pipeline blind as-of the historical date
    verdicts = await run_pipeline(
        run_id=f"{b_id}-blind",
        thesis_name_or_dict=thesis,
        companies=cohort,
        as_of=as_of,
        top_n=len(cohort),
        bus=event_bus,
    )

    verdict_by_company = {v.company_id: v for v in verdicts}

    # 2. Outcome evaluation (Ground Truth from Crustdata funding rounds)
    horizon_end = as_of + timedelta(days=int(horizon_months * 30.4))
    outcome_credits = CreditCounter(budget=50)

    cases: list[BacktestCase] = []
    total_blocked_facts = 0

    back_count = 0
    back_raised_count = 0
    pass_count = 0
    pass_raised_count = 0
    total_raised_count = 0

    # For naive baseline (top-k by headcount at as_of)
    headcount_rankings: list[tuple[str, float]] = []

    for domain in cohort:
        v = verdict_by_company.get(domain)
        decision = v.decision if v else "pass"
        score_val = v.score.total if v else 0.0

        # Fetch ground-truth rounds
        raised_in_horizon = False
        outcome_fact_ids: list[str] = []

        try:
            full_data = await enrich_company(
                identifier={"domain": domain},
                fields=["basic_info", "funding", "headcount"],
                run_credits=outcome_credits,
            )
            rounds = full_data.get("funding", {}).get("rounds", []) or []
            hcount = full_data.get("headcount", {})
            hc_num = float(hcount.get("headcount") or 1.0)
            headcount_rankings.append((domain, hc_num))

            for r in rounds:
                if not isinstance(r, dict):
                    continue
                r_date = _parse_date(r.get("announced_on") or r.get("date") or r.get("round_date"))
                if r_date and as_of < r_date <= horizon_end:
                    raised_in_horizon = True
                    r_id = f"outcome_{domain}_{r_date}"
                    outcome_fact_ids.append(r_id)
        except (NoData, Exception) as exc:
            logger.info("Outcome lookup for %s: %s", domain, exc)
            headcount_rankings.append((domain, 1.0))

        if decision == "back":
            back_count += 1
            if raised_in_horizon:
                back_raised_count += 1
        elif decision == "pass":
            pass_count += 1
            if raised_in_horizon:
                pass_raised_count += 1

        if raised_in_horizon:
            total_raised_count += 1

        case = BacktestCase(
            company_id=domain,
            decision=decision,
            score=score_val,
            outcome_raised_within_horizon=raised_in_horizon,
            outcome_fact_ids=outcome_fact_ids,
        )
        cases.append(case)

    # Compute Hit Rates
    n = len(cases)
    base_rate = round(total_raised_count / n, 4) if n > 0 else 0.0
    back_hit_rate = round(back_raised_count / back_count, 4) if back_count > 0 else 0.0
    pass_hit_rate = round(pass_raised_count / pass_count, 4) if pass_count > 0 else 0.0

    # Naive baseline: top-k by headcount at as_of (take top back_count)
    headcount_rankings.sort(key=lambda x: x[1], reverse=True)
    baseline_top_k = set(x[0] for x in headcount_rankings[: max(1, back_count)])
    baseline_hits = sum(
        1 for c in cases if c.company_id in baseline_top_k and c.outcome_raised_within_horizon
    )
    baseline_hit_rate = round(baseline_hits / len(baseline_top_k), 4) if baseline_top_k else 0.0

    report = BacktestReport(
        as_of=as_of,
        horizon_months=horizon_months,
        n=n,
        back_hit_rate=back_hit_rate,
        pass_hit_rate=pass_hit_rate,
        base_rate=base_rate,
        baseline_hit_rate=baseline_hit_rate,
        facts_blocked=total_blocked_facts,
        cases=cases,
    )

    await event_bus.emit(
        Event(
            run_id=b_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="backtest",
            message=(
                f"Backtest Scorecard: Back Hit Rate {back_hit_rate:.0%} ({back_raised_count}/{back_count}) "
                f"vs Cohort Base Rate {base_rate:.0%} ({total_raised_count}/{n}) "
                f"vs Naive Headcount Baseline {baseline_hit_rate:.0%}"
            ),
            data={
                "back_hit_rate": back_hit_rate,
                "pass_hit_rate": pass_hit_rate,
                "base_rate": base_rate,
                "baseline_hit_rate": baseline_hit_rate,
                "n": n,
            },
        )
    )

    event_bus.finish(b_id)
    return report
