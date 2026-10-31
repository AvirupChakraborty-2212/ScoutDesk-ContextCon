"""Skeptic agent — challenges weak claims, flags contradictions, and builds the bear case.

Acts as the dissenting partner in IC:
  - Catches overstated claims or mismatches between graph facts and narrative
  - Identifies unmentioned risks and competitive threats
  - Can trigger at most ONE follow-up lookup to test a specific doubt
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.anonymize import Anonymizer
from app.crust import CreditCounter, web_search
from app.events import EventBus
from app.ledger import LedgerView
from app.llm import LLMFailed, call_json
from app.models import AgentTask, Challenge, Event, Fact, Memo

logger = logging.getLogger(__name__)

_AGENT = "skeptic"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


async def run(
    task: AgentTask,
    memo: Memo,
    ledger_view: LedgerView,
    bus: EventBus,
    credits: CreditCounter,
    *,
    anonymizer: Anonymizer | None = None,
) -> list[Challenge]:
    """Challenge the draft memo and identify bear-case risks."""
    company_id = task.company_id
    run_id = task.run_id

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=company_id,
            kind="start",
            message=f"Skeptic reviewing draft memo for {company_id}",
        )
    )

    visible_facts = ledger_view.facts(company_id)
    fact_map = {f.id: f for f in visible_facts}

    # Format memo sentences
    memo_text_lines: list[str] = []
    for sec_name, sentences in memo.sections.items():
        memo_text_lines.append(f"[{sec_name.upper()}]")
        for idx, s in enumerate(sentences):
            memo_text_lines.append(f"  ({idx}) {s.rendered}")
    memo_text = "\n".join(memo_text_lines)

    fact_lines = [f"{{{f.id}}} {f.key}: {f.value} {f.unit or ''}" for f in visible_facts[:35]]
    facts_text = "\n".join(fact_lines)

    if anonymizer:
        memo_text = anonymizer.anonymize(memo_text)
        facts_text = anonymizer.anonymize(facts_text)

    prompt = f"""You are the Skeptic Partner on an Investment Committee.
Review this draft investment memo against the verified facts and identify critical weaknesses.

Look for:
1. Contradictions between narrative claims and graph facts.
2. Unaddressed bear-case risks: thin headcount, missing revenue proof, unverified founder tenure.
3. Overly optimistic adjectives (e.g. "rapid growth", "proven leadership") unsupported by raw numbers.

DRAFT MEMO:
{memo_text}

VERIFIED FACTS:
{facts_text}

Return JSON with a list of 'challenges'.
For each challenge, specify:
- 'sentence_index': index of the sentence being challenged (integer, 0 if general)
- 'section': memo section name ('summary', 'founders', 'signals', 'risks', 'bear_case')
- 'objection': precise critique explaining the vulnerability or contradiction
- 'counter_fact_ids': list of Fact IDs from the verified facts that support this challenge
- 'requested_lookup': optional web search query to investigate, or null
"""

    schema = {
        "type": "object",
        "properties": {
            "challenges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sentence_index": {"type": "integer"},
                        "section": {"type": "string"},
                        "objection": {"type": "string"},
                        "counter_fact_ids": {"type": "array", "items": {"type": "string"}},
                        "requested_lookup": {"type": ["string", "null"]},
                    },
                    "required": ["sentence_index", "section", "objection", "counter_fact_ids"],
                },
            }
        },
        "required": ["challenges"],
    }

    challenges: list[Challenge] = []

    try:
        data = await call_json(prompt, schema=schema, run_id=run_id)
        raw_challenges = data.get("challenges", [])

        lookup_performed = False

        for c in raw_challenges:
            s_idx = int(c.get("sentence_index", 0))
            sec = str(c.get("section", "risks"))
            obj = str(c.get("objection", ""))
            cfids = [str(fid) for fid in c.get("counter_fact_ids", []) if str(fid) in fact_map]
            req_lookup = c.get("requested_lookup")

            # Execute at most one follow-up web lookup if requested
            if req_lookup and not lookup_performed and credits.remaining > 0:
                lookup_performed = True
                try:
                    await bus.emit(
                        Event(
                            run_id=run_id,
                            ts=_now(),
                            agent=_AGENT,
                            company_id=company_id,
                            kind="call",
                            message=f"Skeptic testing thesis via web search: {req_lookup}",
                        )
                    )
                    await web_search(str(req_lookup), run_credits=credits)
                except Exception as lex:
                    logger.info("Skeptic follow-up lookup failed: %s", lex)

            challenge_obj = Challenge(
                sentence_index=s_idx,
                objection=obj,
                counter_fact_ids=cfids,
                requested_lookup=str(req_lookup) if req_lookup else None,
            )
            challenges.append(challenge_obj)

            await bus.emit(
                Event(
                    run_id=run_id,
                    ts=_now(),
                    agent=_AGENT,
                    company_id=company_id,
                    kind="challenge",
                    message=f"Skeptic Challenge: {obj[:120]}",
                    data={"section": sec, "sentence_index": s_idx, "counter_facts": cfids},
                )
            )

    except (LLMFailed, Exception) as exc:
        logger.info("LLM skeptic call failed (%s), running heuristic risk checks", exc)
        # Fallback heuristic challenges
        hc_facts = [f for f in visible_facts if f.key == "company.headcount_total"]
        if hc_facts:
            hc_val = hc_facts[0].value
            try:
                if float(hc_val) < 5:
                    c = Challenge(
                        sentence_index=0,
                        objection=f"Early team risk: team size is only {hc_val} employees; key person dependency is severe.",
                        counter_fact_ids=[hc_facts[0].id],
                        requested_lookup=None,
                    )
                    challenges.append(c)
            except (ValueError, TypeError):
                pass

        rounds_facts = [f for f in visible_facts if f.key == "company.funding_rounds_count"]
        if rounds_facts and rounds_facts[0].value == 0:
            c = Challenge(
                sentence_index=0,
                objection="Pre-round risk: no institutional capital raised to date; valuation and terms unbenchmarked.",
                counter_fact_ids=[rounds_facts[0].id],
                requested_lookup=None,
            )
            challenges.append(c)

    return challenges
