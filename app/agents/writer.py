"""Memo Writer agent — drafts investment memos where all numbers cite Fact IDs.

Enforces Design Rule #4: No LLM computes or edits a number.
Writers generate templates with {fact_id} placeholders that code fills in.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.anonymize import Anonymizer
from app.events import EventBus
from app.ledger import LedgerView
from app.llm import LLMFailed, call_json
from app.models import AgentTask, Challenge, Event, Fact, Memo, MemoSentence, Score

logger = logging.getLogger(__name__)

_AGENT = "writer"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _format_fact_value(fact: Fact) -> str:
    """Format a Fact's value with its unit for rendering."""
    val = fact.value
    if val is None:
        return "[N/A]"
    unit = fact.unit

    if unit == "%":
        try:
            fval = float(val)
            pct = fval * 100 if abs(fval) <= 1.0 else fval
            return f"{pct:+.1f}%" if fval > 0 else f"{pct:.1f}%"
        except (ValueError, TypeError):
            return f"{val}%"
    elif unit == "USD":
        try:
            fval = float(val)
            if fval >= 1_000_000_000:
                return f"${fval / 1_000_000_000:.2f}B"
            if fval >= 1_000_000:
                return f"${fval / 1_000_000:.2f}M"
            if fval >= 1_000:
                return f"${fval / 1_000:.1f}K"
            return f"${fval:,.0f}"
        except (ValueError, TypeError):
            return f"${val}"
    elif unit in ("years", "employees", "openings", "ratio"):
        try:
            fval = float(val)
            if unit == "ratio":
                return f"{fval:.0%}"
            return f"{int(fval) if fval.is_integer() else fval:.1f} {unit}"
        except (ValueError, TypeError):
            return f"{val} {unit}"

    return str(val)


def _render_sentence(template: str, fact_map: dict[str, Fact]) -> str:
    """Replace all {fact_id} placeholders with rendered values from fact_map."""
    def replacer(m: re.Match) -> str:
        fid = m.group(1)
        fact = fact_map.get(fid)
        if fact:
            return _format_fact_value(fact)
        return f"[{fid}]"
    return re.sub(r"\{([A-Za-z0-9_-]+)\}", replacer, template)


def _build_fallback_memo(company_id: str, score: Score, visible_facts: list[Fact]) -> Memo:
    """Construct a deterministic template memo from score reasons when LLM is unavailable."""
    fact_map = {f.id: f for f in visible_facts}
    sections: dict[str, list[MemoSentence]] = {
        "summary": [],
        "founders": [],
        "signals": [],
        "risks": [],
        "bear_case": [],
    }

    # Summary
    name_facts = [f for f in visible_facts if f.key == "company.name"]
    name_fid = name_facts[0].id if name_facts else None
    c_name = str(name_facts[0].value) if name_facts else company_id

    s_template = f"Investment evaluation for {c_name} with thesis score of {score.total:.1f}/100 (confidence {score.confidence:.0%})."
    sections["summary"].append(MemoSentence(template=s_template, fact_ids=[name_fid] if name_fid else [], rendered=s_template))

    # Incorporate reasons with fact citations
    for reason in score.reasons:
        # Extract [F1, F2] citations from reason string
        m = re.search(r"\[(.*?)\]", reason)
        cited_ids: list[str] = []
        clean_text = reason
        if m:
            cited_ids = [fid.strip() for fid in m.group(1).split(",") if fid.strip() in fact_map]
            clean_text = reason[:m.start()].strip()

        sentence = MemoSentence(template=clean_text, fact_ids=cited_ids, rendered=clean_text)

        r_lower = reason.lower()
        if "founder" in r_lower or "operator" in r_lower:
            sections["founders"].append(sentence)
        elif "hiring" in r_lower or "openings" in r_lower or "news" in r_lower:
            sections["signals"].append(sentence)
        elif "thesis" in r_lower or "headcount" in r_lower:
            sections["summary"].append(sentence)
        else:
            sections["risks"].append(sentence)

    if not sections["bear_case"]:
        b_text = f"Early-stage venture risk: pre-round evaluation subject to market execution and team retention."
        sections["bear_case"].append(MemoSentence(template=b_text, fact_ids=[], rendered=b_text))

    return Memo(company_id=company_id, sections=sections, removed_by_verifier=0, revisions=0)


async def run(
    task: AgentTask,
    score: Score,
    ledger_view: LedgerView,
    bus: EventBus,
    *,
    prior_challenges: list[Challenge] | None = None,
    anonymizer: Anonymizer | None = None,
) -> Memo:
    """Draft a cited investment committee memo."""
    company_id = task.company_id
    run_id = task.run_id

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=company_id,
            kind="start",
            message=f"Memo Writer drafting investment memo for {company_id}",
            data={"score": score.total, "confidence": score.confidence},
        )
    )

    visible_facts = ledger_view.facts(company_id)
    fact_map = {f.id: f for f in visible_facts}

    if not visible_facts:
        return _build_fallback_memo(company_id, score, [])

    # Format fact inventory for the prompt
    fact_lines: list[str] = []
    for f in visible_facts[:40]:  # limit context window
        val_str = _format_fact_value(f)
        fact_lines.append(f"Fact ID {{{f.id}}}: key={f.key}, value={val_str}")
    facts_context = "\n".join(fact_lines)

    # Anonymize if in backtest mode
    if anonymizer:
        facts_context = anonymizer.anonymize(facts_context)

    challenges_context = ""
    if prior_challenges:
        c_lines = [f"- {c.section}: {c.objection}" for c in prior_challenges]
        challenges_context = (
            "\nAddress the following skeptic challenges directly in the 'bear_case' section:\n"
            + "\n".join(c_lines)
        )

    prompt = f"""You are an Investment Committee Memo Writer for a top venture fund.
Draft an investment memo for {company_id}.

CRITICAL RULES:
1. Every numeric metric or statistic MUST use the exact placeholder {{{'{fact_id}'}}} from the Fact Inventory below.
   Example: "Headcount reached {{{'{fact_id}'}}} after growing {{{'{fact_id}'}}}."
2. Do NOT invent, extrapolate, or write any bare numbers ≥ 11 outside of {{{'{fact_id}'}}} placeholders.
3. For each sentence, provide the list of fact_ids referenced in that sentence.
4. If data is unknown or missing, explicitly state it is unknown.

SCORE SUMMARY:
- Total: {score.total}/100 | Confidence: {score.confidence:.0%}
- Sub-scores: {score.parts}
- Key reasons: {score.reasons}

FACT INVENTORY (Use only these Fact IDs):
{facts_context}
{challenges_context}

Return a JSON object with sections: 'summary', 'founders', 'signals', 'risks', 'bear_case'.
Each section contains a list of sentences, where each sentence has 'template' and 'fact_ids'.
"""

    schema = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "template": {"type": "string"},
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["template", "fact_ids"],
                        },
                    },
                    "founders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "template": {"type": "string"},
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["template", "fact_ids"],
                        },
                    },
                    "signals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "template": {"type": "string"},
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["template", "fact_ids"],
                        },
                    },
                    "risks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "template": {"type": "string"},
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["template", "fact_ids"],
                        },
                    },
                    "bear_case": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "template": {"type": "string"},
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["template", "fact_ids"],
                        },
                    },
                },
                "required": ["summary", "founders", "bear_case"],
            }
        },
        "required": ["sections"],
    }

    try:
        data = await call_json(prompt, schema=schema, run_id=run_id)
        raw_sections = data.get("sections", {})
        sections: dict[str, list[MemoSentence]] = {}

        for sec_name, sentences in raw_sections.items():
            sec_list: list[MemoSentence] = []
            for s in sentences:
                template = str(s.get("template", ""))
                # Deanonymize if needed
                if anonymizer:
                    template = anonymizer.deanonymize(template)
                fids = [str(fid) for fid in s.get("fact_ids", [])]

                # Render placeholders
                rendered = _render_sentence(template, fact_map)
                sec_list.append(MemoSentence(template=template, fact_ids=fids, rendered=rendered))
            sections[sec_name] = sec_list

        memo = Memo(
            company_id=company_id,
            sections=sections,
            removed_by_verifier=0,
            revisions=0,
        )

        await bus.emit(
            Event(
                run_id=run_id,
                ts=_now(),
                agent=_AGENT,
                company_id=company_id,
                kind="finding",
                message=f"Drafted memo with {sum(len(v) for v in sections.values())} sentences across {len(sections)} sections.",
            )
        )
        return memo

    except (LLMFailed, Exception) as exc:
        logger.warning("LLM memo drafting failed for %s, using deterministic fallback: %s", company_id, exc)
        return _build_fallback_memo(company_id, score, visible_facts)
