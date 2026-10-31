"""IC Partner agent — makes the final Back / Watch / Pass / Human Review verdict.

Rules-first decision logic ensures rigorous adherence to the fund thesis thresholds.
The LLM writes a 3-sentence partner rationale citing specific score parts and Fact IDs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from app.anonymize import Anonymizer
from app.events import EventBus
from app.ledger import LedgerView
from app.llm import LLMFailed, call_json
from app.models import AgentTask, Challenge, Event, Fact, Memo, Score, Verdict
from app.verifier import verify_ic_rationale

logger = logging.getLogger(__name__)

_AGENT = "ic"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _build_fallback_rationale(decision: str, score: Score, company_name: str) -> str:
    """Deterministic, verified 3-sentence IC rationale template."""
    if decision == "back":
        s1 = f"We recommend a Back call on {company_name} driven by an overall thesis fit score of {score.total:.1f}/100."
        s2 = f"The founding team demonstrated strong operator pedigree with a founders sub-score of {score.parts.get('founders', 0):.1f}/100."
        s3 = f"Signals and market momentum align with our target deployment criteria with {score.confidence:.0%} data confidence."
        return f"{s1} {s2} {s3}"
    elif decision == "watch":
        s1 = f"We recommend placing {company_name} on Watch status with an overall score of {score.total:.1f}/100."
        s2 = f"While thesis fit is promising ({score.parts.get('thesis_fit', 0):.1f}/100), key hiring or founder signals require further maturation."
        s3 = f"We will re-evaluate on subsequent inflection points with current confidence at {score.confidence:.0%}."
        return f"{s1} {s2} {s3}"
    elif decision == "human_review":
        s1 = f"{company_name} requires human review due to data ambiguity or low confidence ({score.confidence:.0%})."
        s2 = f"Automated scoring reached {score.total:.1f}/100 but key founder or entity identity facts could not be autonomously confirmed."
        s3 = f"An analyst must manually verify corporate identity and founder employment records before capital allocation."
        return f"{s1} {s2} {s3}"
    else:  # pass
        s1 = f"We recommend a Pass on {company_name} with an overall thesis score of {score.total:.1f}/100."
        s2 = f"The company does not meet our minimum threshold requirements across target founder or market momentum dimensions."
        s3 = f"We will archive this profile and monitor for major strategic pivots."
        return f"{s1} {s2} {s3}"


async def run(
    task: AgentTask,
    score: Score,
    memo: Memo,
    challenges: list[Challenge],
    ledger_view: LedgerView,
    thesis: dict,
    bus: EventBus,
    unknowns: list[str],
    *,
    anonymizer: Anonymizer | None = None,
) -> Verdict:
    """Decide investment verdict (Back / Watch / Pass / Human Review)."""
    company_id = task.company_id
    run_id = task.run_id

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=company_id,
            kind="start",
            message=f"Investment Committee convening for verdict on {company_id}",
        )
    )

    # Resolve company name
    visible_facts = ledger_view.facts(company_id)
    name_facts = [f for f in visible_facts if f.key == "company.name"]
    company_name = str(name_facts[0].value) if name_facts else company_id

    # 1. Deterministic rules
    min_confidence = float(thesis.get("min_confidence_for_auto_verdict", 0.6))
    thresholds = thesis.get("verdict_thresholds", {"back": 70, "watch": 50})
    back_thresh = float(thresholds.get("back", 70))
    watch_thresh = float(thresholds.get("watch", 50))

    flags: list[str] = []

    # Flag evaluations
    if score.confidence < 0.7:
        flags.append("low_confidence")

    founder_facts = [f for f in visible_facts if f.subject.startswith("founder") or "founder" in f.key]
    if len(founder_facts) < 2:
        flags.append("sparse_founder_data")

    openings_facts = [f for f in visible_facts if f.key == "openings.last_90d"]
    if not openings_facts or openings_facts[0].value == 0:
        flags.append("no_hiring_signal")

    rounds_facts = [f for f in visible_facts if f.key == "company.funding_rounds_count"]
    hc_facts = [f for f in visible_facts if f.key == "company.headcount_total"]
    if rounds_facts and rounds_facts[0].value == 0:
        if hc_facts:
            try:
                if float(hc_facts[0].value) < 5:
                    flags.append("pre_revenue_signal")
            except (ValueError, TypeError):
                pass

    if challenges:
        flags.append("skeptic_challenges_flagged")

    # Determine Decision
    decision: Literal["back", "watch", "pass", "human_review"]
    if (
        score.confidence < min_confidence
        or "founders_unidentified" in unknowns
        or "ambiguous_identity" in unknowns
        or (score.parts.get("founders", 0) < 20 and score.confidence < 0.5)
    ):
        decision = "human_review"
    elif score.total >= back_thresh:
        decision = "back"
    elif score.total >= watch_thresh:
        decision = "watch"
    else:
        decision = "pass"

    # 2. Draft Rationale
    rationale = _build_fallback_rationale(decision, score, company_name)

    prompt = f"""You are the Managing Partner of an elite venture fund writing the final Investment Committee verdict rationale.
Company: {company_name}
Decision: {decision.upper()}
Total Score: {score.total}/100
Confidence: {score.confidence:.0%}
Sub-scores: {score.parts}
Key Reasons: {score.reasons[:4]}
Flags: {flags}

Write EXACTLY 3 clear, professional sentences explaining this {decision.upper()} decision.
Be precise and cite the key scores and reasons. Do NOT invent numbers.
Return JSON with 'rationale' (string) and 'used_fact_ids' (list of strings).
"""

    schema = {
        "type": "object",
        "properties": {
            "rationale": {"type": "string"},
            "used_fact_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["rationale"],
    }

    try:
        data = await call_json(prompt, schema=schema, run_id=run_id)
        llm_rationale = str(data.get("rationale", "")).strip()
        used_fids = [str(f) for f in data.get("used_fact_ids", [])]

        if anonymizer:
            llm_rationale = anonymizer.deanonymize(llm_rationale)

        # Verify rationale
        passed, violations = verify_ic_rationale(
            llm_rationale,
            used_fids,
            ledger_view,
            as_of=ledger_view.as_of,
        )
        if passed and len(llm_rationale.split(".")) >= 2:
            rationale = llm_rationale
        else:
            logger.info("LLM IC rationale failed verification (%s), using verified template", violations)

    except (LLMFailed, Exception) as exc:
        logger.info("LLM IC rationale generation failed: %s", exc)

    verdict = Verdict(
        company_id=company_id,
        name=company_name,
        decision=decision,
        score=score,
        memo=memo,
        rationale=rationale,
        flags=flags,
        check_size=None,
    )

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=company_id,
            kind="verdict",
            message=f"FINAL VERDICT: {decision.upper()} ({score.total:.1f}/100, confidence {score.confidence:.0%})",
            data={
                "decision": decision,
                "score": score.total,
                "confidence": score.confidence,
                "flags": flags,
                "rationale": rationale,
            },
        )
    )

    return verdict
