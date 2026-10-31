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
    """Construct a comprehensive, deterministic template memo from ledger facts and score reasons."""
    fact_map = {f.id: f for f in visible_facts}
    sections: dict[str, list[MemoSentence]] = {
        "summary": [],
        "founders": [],
        "market": [],
        "signals": [],
        "risks": [],
        "bear_case": [],
    }

    # 1. Summary
    name_facts = [f for f in visible_facts if f.key == "company.name"]
    name_fid = name_facts[0].id if name_facts else None
    c_name = str(name_facts[0].value) if name_facts else company_id

    s_template = f"Investment evaluation for {c_name} with thesis score of {score.total:.1f}/100 (confidence {score.confidence:.0%})."
    sections["summary"].append(MemoSentence(template=s_template, fact_ids=[name_fid] if name_fid else [], rendered=s_template))

    sector_facts = [f for f in visible_facts if f.key == "company.sector"]
    country_facts = [f for f in visible_facts if f.key == "company.country"]
    year_facts = [f for f in visible_facts if f.key == "company.founded_year"]
    desc_facts = [f for f in visible_facts if f.key == "company.description"]

    id_parts = []
    id_fids = []
    if sector_facts and sector_facts[0].value:
        id_parts.append(f"operates in {sector_facts[0].value}")
        id_fids.append(sector_facts[0].id)
    if country_facts and country_facts[0].value:
        id_parts.append(f"headquartered in {country_facts[0].value}")
        id_fids.append(country_facts[0].id)
    if year_facts and year_facts[0].value:
        id_parts.append(f"founded in {year_facts[0].value}")
        id_fids.append(year_facts[0].id)

    if id_parts:
        s_id = f"{c_name} {', '.join(id_parts)}."
        sections["summary"].append(MemoSentence(template=s_id, fact_ids=id_fids, rendered=s_id))

    if desc_facts and desc_facts[0].value:
        d_val = str(desc_facts[0].value)
        d_trunc = d_val[:220] + "..." if len(d_val) > 220 else d_val
        s_desc = f"Business profile: {d_trunc}"
        sections["summary"].append(MemoSentence(template=s_desc, fact_ids=[desc_facts[0].id], rendered=s_desc))

    # 2. Founders
    founder_subjects = sorted(list(set(f.subject for f in visible_facts if f.subject.startswith("founder:"))))
    if founder_subjects:
        for subj in founder_subjects:
            f_name_f = [f for f in visible_facts if f.subject == subj and f.key == "founder.name"]
            f_li_f = [f for f in visible_facts if f.subject == subj and f.key == "founder.linkedin_url"]
            f_hc_f = [f for f in visible_facts if f.subject == subj and f.key == "founder.prior_employer_headcount"]
            f_exp_f = [f for f in visible_facts if f.subject == subj and f.key == "founder.years_experience"]
            f_rf_f = [f for f in visible_facts if f.subject == subj and f.key == "founder.is_repeat_founder"]
            role_f = [f for f in visible_facts if f.subject == subj and "founder.role" in f.key]

            if not f_name_f:
                continue

            f_name = str(f_name_f[0].value)
            f_fids = [f_name_f[0].id]

            prior_desc = ""
            if role_f:
                roles_str = "; ".join(str(rf.value) for rf in role_f[:2])
                prior_desc += f" with prior background: {roles_str}"
                f_fids.extend(rf.id for rf in role_f[:2])

            if f_hc_f and f_hc_f[0].value:
                prior_desc += f" (prior organization scale: {int(float(f_hc_f[0].value)):,} employees)"
                f_fids.append(f_hc_f[0].id)

            if f_exp_f and f_exp_f[0].value:
                prior_desc += f" and {int(float(f_exp_f[0].value))} years professional experience"
                f_fids.append(f_exp_f[0].id)

            repeat_str = " (repeat founder)" if (f_rf_f and f_rf_f[0].value is True) else ""
            if f_rf_f:
                f_fids.append(f_rf_f[0].id)

            f_sentence = f"Executive leadership includes {f_name}{repeat_str}{prior_desc}."
            sections["founders"].append(MemoSentence(template=f_sentence, fact_ids=f_fids, rendered=f_sentence))
    else:
        # Check flat founder facts
        flat_f = [f for f in visible_facts if f.key == "company.founders"]
        if flat_f and flat_f[0].value:
            v = flat_f[0].value
            names = v if isinstance(v, list) else [v]
            f_sentence = f"Identified company founders from registry: {', '.join(str(n) for n in names)}."
            sections["founders"].append(MemoSentence(template=f_sentence, fact_ids=[flat_f[0].id], rendered=f_sentence))
        else:
            no_f = f"No executive founder profiles could be autonomously verified in registry data for {c_name}."
            sections["founders"].append(MemoSentence(template=no_f, fact_ids=[], rendered=no_f))

    # 3. Market & Capital
    funding_fids: list[str] = []
    funding_parts: list[str] = []

    raised_f = [f for f in visible_facts if f.key == "company.total_raised_usd"]
    if raised_f and raised_f[0].value:
        try:
            r_usd = float(raised_f[0].value)
            funding_parts.append(f"total funding raised of ${r_usd / 1_000_000:.1f}M USD")
            funding_fids.append(raised_f[0].id)
        except (ValueError, TypeError):
            pass

    rc_f = [f for f in visible_facts if f.key == "company.funding_rounds_count"]
    if rc_f and rc_f[0].value is not None:
        funding_parts.append(f"across {rc_f[0].value} funding round(s)")
        funding_fids.append(rc_f[0].id)

    if funding_parts:
        m_sentence = f"Capital structure: {c_name} has {', '.join(funding_parts)}."
        sections["market"].append(MemoSentence(template=m_sentence, fact_ids=funding_fids, rendered=m_sentence))

    # Competitor facts
    comp_f = [f for f in visible_facts if f.key.startswith("competitor.") and f.key.endswith(".name")]
    if comp_f:
        comp_names = [str(cf.value) for cf in comp_f[:4]]
        c_sent = f"Direct market competitors identified in sector: {', '.join(comp_names)}."
        sections["market"].append(MemoSentence(template=c_sent, fact_ids=[cf.id for cf in comp_f[:4]], rendered=c_sent))

    # 4. Signals & Traction
    traction_fids: list[str] = []
    traction_parts: list[str] = []

    hc_f = [f for f in visible_facts if f.key == "company.headcount_total"]
    if hc_f and hc_f[0].value:
        traction_parts.append(f"team size of {hc_f[0].value} employees")
        traction_fids.append(hc_f[0].id)

    yoy_f = [f for f in visible_facts if f.key == "company.headcount_yoy"]
    if yoy_f and yoy_f[0].value is not None:
        try:
            y_val = float(yoy_f[0].value)
            traction_parts.append(f"{y_val:+.1f}% YoY headcount growth")
            traction_fids.append(yoy_f[0].id)
        except (ValueError, TypeError):
            pass

    if traction_parts:
        t_sentence = f"Headcount traction: {c_name} reports {', '.join(traction_parts)}."
        sections["signals"].append(MemoSentence(template=t_sentence, fact_ids=traction_fids, rendered=t_sentence))

    openings_f = [f for f in visible_facts if f.key == "openings.last_90d" or f.key == "openings.count"]
    if openings_f and openings_f[0].value is not None:
        o_val = int(float(openings_f[0].value))
        o_sentence = f"Active hiring momentum: {o_val} job opening(s) recorded in the past 90 days."
        sections["signals"].append(MemoSentence(template=o_sentence, fact_ids=[openings_f[0].id], rendered=o_sentence))

    # 5. Risks & Score Reasons
    for reason in score.reasons:
        m = re.search(r"\[(.*?)\]", reason)
        cited_ids: list[str] = []
        clean_text = reason
        if m:
            cited_ids = [fid.strip() for fid in m.group(1).split(",") if fid.strip() in fact_map]
            clean_text = reason[:m.start()].strip()

        sentence = MemoSentence(template=clean_text, fact_ids=cited_ids, rendered=clean_text)
        r_lower = reason.lower()
        if "mismatch" in r_lower or "outside" in r_lower or "too many" in r_lower or "no signal" in r_lower:
            sections["risks"].append(sentence)
        elif "operator" in r_lower or "repeat founder" in r_lower:
            if not any(sentence.template == s.template for s in sections["founders"]):
                sections["founders"].append(sentence)
        elif "hiring" in r_lower or "news" in r_lower:
            if not any(sentence.template == s.template for s in sections["signals"]):
                sections["signals"].append(sentence)
        else:
            if not any(sentence.template == s.template for s in sections["summary"]):
                sections["summary"].append(sentence)

    if not sections["risks"]:
        r_text = f"Thesis alignment risk: early-stage venture execution requires ongoing verification of key milestones."
        sections["risks"].append(MemoSentence(template=r_text, fact_ids=[], rendered=r_text))

    # 6. Bear Case
    b_text = f"Bear case: Competitive saturation from incumbents and execution headwinds during stage expansion."
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

Return a JSON object with a 'sections' key:
{{
  "sections": {{
    "summary": [{{"template": "...", "fact_ids": ["..."]}}],
    "founders": [{{"template": "...", "fact_ids": ["..."]}}],
    "signals": [{{"template": "...", "fact_ids": ["..."]}}],
    "risks": [{{"template": "...", "fact_ids": ["..."]}}],
    "bear_case": [{{"template": "...", "fact_ids": ["..."]}}]
  }}
}}
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
        raw_sections = data.get("sections") if isinstance(data.get("sections"), dict) else data
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
