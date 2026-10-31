"""Managing Partner — orchestrates the end-to-end investment evaluation pipeline.

Coordinates:
  Sourcing → Triage → Parallel Analysts (Company, Founder, Signals, Market)
  → Time Firewall → Pure-Code Scorer
  → Memo Writer → Skeptic Challenge → Code Verifier
  → IC Partner Verdict → Capital Allocator
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.agents import (
    company as company_agent,
    founder as founder_agent,
    ic as ic_agent,
    market as market_agent,
    signals as signals_agent,
    skeptic as skeptic_agent,
    sourcing as sourcing_agent,
    writer as writer_agent,
)
from app.allocator import allocate
from app.anonymize import Anonymizer
from app.config import RUN_BUDGET_CREDITS, RUNS_DIR, THESES_DIR, TOP_N
from app.crust import CreditCounter
from app.events import EventBus, bus as global_bus
from app.ledger import FactLedger, LedgerView
from app.models import AgentTask, Event, Fact, Score, Verdict
from app.scorer import score as compute_score
from app.verifier import verify as verify_memo

logger = logging.getLogger(__name__)

_AGENT = "orchestrator"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def load_thesis(thesis_name_or_dict: str | dict[str, Any]) -> dict[str, Any]:
    """Load a thesis YAML by filename/stem or return dict directly."""
    if isinstance(thesis_name_or_dict, dict):
        return thesis_name_or_dict

    name = str(thesis_name_or_dict).strip()
    if not name.endswith(".yaml") and not name.endswith(".yml"):
        name = f"{name}.yaml"

    path = THESES_DIR / name
    if not path.exists():
        # Fallback to preseed_india.yaml
        path = THESES_DIR / "preseed_india.yaml"

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def _run_company_pipeline(
    company_id: str,
    run_id: str,
    thesis: dict[str, Any],
    ledger: FactLedger,
    as_of: date | None,
    bus: EventBus,
    credits: CreditCounter,
    anonymizer: Anonymizer | None = None,
) -> Verdict:
    """Deep-dive investigation for a single company."""
    task = AgentTask(
        run_id=run_id,
        agent="pipeline",
        company_id=company_id,
        budget_credits=max(10, credits.remaining // 4),
    )

    all_unknowns: list[str] = []

    # 1. Company Analyst
    comp_result = await company_agent.run(task, ledger, bus, credits)
    all_unknowns.extend(comp_result.unknowns)

    # Gather company facts for downstream agents
    comp_facts = [f for f in ledger.by_company(company_id) if f.id in comp_result.fact_ids]

    # Resolve company name and domain
    c_name = company_id
    c_domain = company_id
    competitor_domains: list[str] = []

    for f in comp_facts:
        if f.key == "company.name" and f.value:
            c_name = str(f.value)
        elif f.key == "company.domain" and f.value:
            c_domain = str(f.value)
        elif f.key.startswith("competitor.") and f.key.endswith(".name"):
            # Subject or key has competitor domain
            parts = f.key.split(".")
            if len(parts) >= 2:
                competitor_domains.append(parts[1])

    # 2. Parallel Analysts: Founder, Signals, Market
    allocated_credits = max(5, credits.remaining // 4)
    f_task = AgentTask(run_id=run_id, agent="founder", company_id=company_id, budget_credits=allocated_credits)
    s_task = AgentTask(run_id=run_id, agent="signals", company_id=company_id, budget_credits=allocated_credits)
    m_task = AgentTask(run_id=run_id, agent="market", company_id=company_id, budget_credits=allocated_credits)

    results = await asyncio.gather(
        founder_agent.run(f_task, comp_facts, ledger, bus, credits),
        signals_agent.run(s_task, c_domain, c_name, ledger, bus, credits),
        market_agent.run(m_task, competitor_domains, ledger, bus, credits),
        return_exceptions=True,
    )

    for res in results:
        if isinstance(res, Exception):
            logger.error("Agent failed during company pipeline: %s", res)
        elif hasattr(res, "unknowns"):
            all_unknowns.extend(res.unknowns)

    # 3. Time Firewall view
    view: LedgerView = ledger.view(as_of)

    if as_of and view.blocked_count > 0:
        await bus.emit(
            Event(
                run_id=run_id,
                ts=_now(),
                agent=_AGENT,
                company_id=company_id,
                kind="firewall",
                message=f"Time firewall active (as of {as_of}): {view.blocked_count} facts hidden from agents.",
                data={"blocked_count": view.blocked_count, "as_of": str(as_of)},
            )
        )

    # 4. Pure-Code Scorer
    visible_facts = view.facts(company_id)
    score: Score = compute_score(thesis, visible_facts, company_id)

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent="scorer",
            company_id=company_id,
            kind="finding",
            message=f"Thesis fit scored: {score.total:.1f}/100 (Confidence: {score.confidence:.0%})",
            data={"score": score.total, "parts": score.parts, "confidence": score.confidence},
        )
    )

    # 5. Writer -> Skeptic -> Verifier loop
    w_task = AgentTask(run_id=run_id, agent="writer", company_id=company_id, budget_credits=0)
    memo = await writer_agent.run(
        w_task,
        score,
        view,
        bus,
        prior_challenges=None,
        anonymizer=anonymizer,
    )

    # Skeptic challenge
    sk_task = AgentTask(run_id=run_id, agent="skeptic", company_id=company_id, budget_credits=allocated_credits)
    challenges = await skeptic_agent.run(
        sk_task,
        memo,
        view,
        bus,
        credits,
        anonymizer=anonymizer,
    )

    # Second pass if challenges arose
    if challenges:
        memo = await writer_agent.run(
            w_task,
            score,
            view,
            bus,
            prior_challenges=challenges,
            anonymizer=anonymizer,
        )
        memo.revisions = 1

    # Verifier check
    verified_memo = verify_memo(memo, view, as_of=as_of)
    if verified_memo.removed_by_verifier > 0:
        await bus.emit(
            Event(
                run_id=run_id,
                ts=_now(),
                agent="verifier",
                company_id=company_id,
                kind="verify",
                message=f"Verifier removed {verified_memo.removed_by_verifier} unsupported sentences from memo.",
                data={"removed_count": verified_memo.removed_by_verifier},
            )
        )

    # 6. IC Partner Verdict
    ic_task = AgentTask(run_id=run_id, agent="ic", company_id=company_id, budget_credits=0)
    verdict = await ic_agent.run(
        ic_task,
        score,
        verified_memo,
        challenges,
        view,
        thesis,
        bus,
        all_unknowns,
        anonymizer=anonymizer,
    )

    return verdict


async def run_pipeline(
    run_id: str,
    thesis_name_or_dict: str | dict[str, Any] = "preseed_india",
    companies: list[str] | None = None,
    as_of: date | None = None,
    top_n: int = TOP_N,
    bus: EventBus | None = None,
) -> list[Verdict]:
    """Execute the full ScoutDesk multi-agent investment evaluation run.

    Parameters
    ----------
    run_id:
        Unique run tracking identifier.
    thesis_name_or_dict:
        Name of thesis YAML or pre-loaded dict.
    companies:
        Optional explicit list of target company domains or names.
    as_of:
        Optional past date for historical backtest simulation (activates time firewall).
    top_n:
        Maximum number of companies to put through full deep-dive.
    bus:
        EventBus instance (defaults to module singleton).
    """
    event_bus = bus or global_bus
    event_bus.create_run(run_id)

    ledger = FactLedger()
    credits = CreditCounter(budget=RUN_BUDGET_CREDITS)
    thesis = load_thesis(thesis_name_or_dict)

    await event_bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="start",
            message=f"Starting ScoutDesk run {run_id} | Thesis: {thesis.get('name', 'B2B Pre-seed')} | As-of: {as_of or 'LIVE'}",
            data={"as_of": str(as_of) if as_of else None, "top_n": top_n},
        )
    )

    # Setup Anonymizer for backtest mode
    anonymizer: Anonymizer | None = None

    # Step 1: Candidate sourcing
    candidate_domains: list[str] = []
    if companies:
        candidate_domains = [c.strip() for c in companies if c.strip()]
    else:
        candidate_domains = await sourcing_agent.run(
            thesis=thesis,
            ledger=ledger,
            bus=event_bus,
            credits=credits,
            run_id=run_id,
            limit=top_n * 2,
        )

    # Filter to top_n
    selected_cohort = candidate_domains[:top_n]

    if as_of:
        # In as_of backtest mode, anonymize names to prevent LLM lookahead leakage
        anonymizer = Anonymizer(company_names=selected_cohort, founder_names=[])

    await event_bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="finding",
            message=f"Advancing top {len(selected_cohort)} companies to deep-dive research: {', '.join(selected_cohort)}",
            data={"cohort": selected_cohort},
        )
    )

    # Step 2: Run pipeline for each company
    verdicts: list[Verdict] = []
    for company_id in selected_cohort:
        try:
            v = await _run_company_pipeline(
                company_id=company_id,
                run_id=run_id,
                thesis=thesis,
                ledger=ledger,
                as_of=as_of,
                bus=event_bus,
                credits=credits,
                anonymizer=anonymizer,
            )
            verdicts.append(v)
        except Exception as exc:
            logger.error("Failed pipeline for %s: %s", company_id, exc, exc_info=True)
            await event_bus.emit(
                Event(
                    run_id=run_id,
                    ts=_now(),
                    agent=_AGENT,
                    company_id=company_id,
                    kind="error",
                    message=f"Pipeline error for {company_id}: {exc}",
                )
            )

    # Step 3: Capital Allocation across Back verdicts
    allocated_verdicts = allocate(verdicts)

    # Step 4: Persist run dump
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        dump_path = RUNS_DIR / f"{run_id}.json"
        ledger.dump(dump_path)
    except Exception as exc:
        logger.warning("Could not dump run facts to disk: %s", exc)

    # Finish run
    await event_bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="done",
            message=f"ScoutDesk run complete. {len(allocated_verdicts)} verdicts generated. Credits used: {credits.used}/{credits.budget}",
            data={
                "verdicts_count": len(allocated_verdicts),
                "credits_used": credits.used,
                "decisions": {v.company_id: v.decision for v in allocated_verdicts},
            },
        )
    )

    event_bus.finish(run_id)
    return allocated_verdicts
