"""Founder Analyst agent — researches founder histories, operator experience, and prior founding.

Only `crust.py` calls Crustdata; this module consumes those typed functions.
Facts extracted:
  - founder.{n}.name, founder.{n}.linkedin_url
  - founder.{n}.current_title, founder.{n}.current_company
  - founder.{n}.role.{m}.company, .headcount, .title, .start_date, .end_date
  - Derived:
      - founder.prior_employer_headcount (subject="founder:<n>", key="founder.prior_employer_headcount")
      - founder.years_at_prior
      - founder.is_repeat_founder
      - founder.years_experience
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from app.crust import (
    CreditCounter,
    NoData,
    enrich_person,
    search_people,
)
from app.events import EventBus
from app.ledger import (
    KNOWN_SINCE_DERIVED,
    KNOWN_SINCE_REALTIME,
    KNOWN_SINCE_ROLE,
    FactLedger,
    derive_known_since,
)
from app.models import AgentResult, AgentTask, Event, Fact

logger = logging.getLogger(__name__)

_AGENT = "founder"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _today() -> date:
    return date.today()


def _excerpt(data: Any, max_chars: int = 300) -> str:
    try:
        raw = json.dumps(data, default=str)
    except (TypeError, ValueError):
        raw = str(data)
    return raw[:max_chars]


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


def _make_fact(
    *,
    ledger: FactLedger,
    company_id: str,
    subject: str,
    key: str,
    value: Any,
    unit: str | None,
    endpoint: str,
    field_path: str,
    known_since: date | None,
    raw_excerpt: str,
    derived_from: list[str] | None = None,
) -> str:
    fact = Fact(
        id="",
        company_id=company_id,
        subject=subject,
        key=key,
        value=value,
        unit=unit,
        endpoint=endpoint,
        field_path=field_path,
        derived_from=derived_from or [],
        known_since=known_since,
        fetched_at=_now(),
        raw_excerpt=raw_excerpt,
    )
    return ledger.add(fact)


async def _emit_event(
    bus: EventBus,
    *,
    run_id: str,
    agent: str,
    company_id: str,
    kind: str,
    message: str,
    data: dict | None = None,
) -> None:
    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=agent,
            company_id=company_id,
            kind=kind,  # type: ignore[arg-type]
            message=message,
            data=data or {},
        )
    )


async def run(
    task: AgentTask,
    company_facts: list[Fact],
    ledger: FactLedger,
    bus: EventBus,
    credits: CreditCounter,
) -> AgentResult:
    """Run the Founder Analyst for a company.

    Steps
    -----
    1. Extract founder names from company_facts.
    2. Enrich up to 3 founders via `enrich_person` (fallback to `search_people`).
    3. Extract role histories, identify prior employers, compute operator metrics.
    4. Store facts in ledger, emit events, and return AgentResult.
    """
    company_id = task.company_id
    run_id = task.run_id
    start_credits = credits.used

    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="start",
        message=f"Founder Analyst starting for company: {company_id}",
    )

    fact_ids: list[str] = []
    findings: list[str] = []
    unknowns: list[str] = []

    # 1. Identify company name and founder names from company_facts
    company_name = company_id
    founder_names: list[str] = []

    for f in company_facts:
        if f.key == "company.name" and f.value:
            company_name = str(f.value)
        elif f.key == "company.founders":
            if isinstance(f.value, list):
                founder_names.extend([str(name).strip() for name in f.value if name])
            elif isinstance(f.value, str):
                try:
                    parsed = json.loads(f.value)
                    if isinstance(parsed, list):
                        founder_names.extend([str(name).strip() for name in parsed if name])
                except Exception:
                    if f.value.strip():
                        founder_names.append(f.value.strip())

    # Deduplicate founder names
    unique_founders: list[str] = []
    for name in founder_names:
        if name and name not in unique_founders:
            unique_founders.append(name)

    if not unique_founders:
        # Try person search for founders of this company
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="call",
            message=f"No founders listed in company profile; searching people for {company_name}...",
        )
        try:
            search_results = await search_people(
                filters=[
                    {"filter_type": "company_name", "value": company_name},
                    {"filter_type": "title_contains", "value": "founder"},
                ],
                limit=3,
                run_credits=credits,
            )
            for person in search_results:
                name = person.get("name") or person.get("full_name")
                if name and name not in unique_founders:
                    unique_founders.append(str(name))
        except (NoData, Exception) as exc:
            logger.info("Search people fallback returned no founders for %s: %s", company_name, exc)

    if not unique_founders:
        unknowns.append("founders_unidentified")
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="finding",
            message=f"No founders could be identified for {company_name}; flagged for human review",
            data={"flag": "founders_unidentified"},
        )
        return AgentResult(
            agent=_AGENT,
            company_id=company_id,
            fact_ids=fact_ids,
            findings=findings,
            unknowns=unknowns,
            credits_used=credits.used - start_credits,
            status="partial",
        )

    # 2. Enrich up to 3 founders
    for idx, founder_name in enumerate(unique_founders[:3]):
        founder_subject = f"founder:{idx}"
        person_data: dict[str, Any] | None = None

        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="call",
            message=f"Enriching founder {idx+1}/{min(len(unique_founders), 3)}: {founder_name}",
        )

        try:
            person_data = await enrich_person(
                identifier={"name": founder_name, "company": company_name},
                fields=["basic_info", "experience", "education"],
                run_credits=credits,
            )
        except (NoData, Exception) as exc:
            logger.info("Direct person enrich failed for %s, trying search fallback: %s", founder_name, exc)
            try:
                search_res = await search_people(
                    filters=[
                        {"filter_type": "name", "value": founder_name},
                        {"filter_type": "company_name", "value": company_name},
                    ],
                    limit=1,
                    run_credits=credits,
                )
                if search_res:
                    person_data = search_res[0]
            except Exception as search_exc:
                logger.info("Person search also failed for %s: %s", founder_name, search_exc)

        if not person_data:
            unknowns.append(f"founder_{idx}_details_missing")
            # Store at least the name we know
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=founder_subject,
                key="founder.name",
                value=founder_name,
                unit=None,
                endpoint="company/enrich",
                field_path=f"people.founders[{idx}]",
                known_since=date.today(),
                raw_excerpt=_excerpt({"name": founder_name}),
            )
            fact_ids.append(fid)
            continue

        # Extract founder basics
        raw_name = person_data.get("name") or person_data.get("full_name") or founder_name
        linkedin_url = person_data.get("linkedin_url") or person_data.get("profile_url")
        current_title = person_data.get("current_title") or person_data.get("title")

        fid_name = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject=founder_subject,
            key="founder.name",
            value=raw_name,
            unit=None,
            endpoint="screener/people/enrich",
            field_path="name",
            known_since=date.today(),
            raw_excerpt=_excerpt({"name": raw_name, "title": current_title}),
        )
        fact_ids.append(fid_name)

        if linkedin_url:
            fid_li = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=founder_subject,
                key="founder.linkedin_url",
                value=linkedin_url,
                unit=None,
                endpoint="screener/people/enrich",
                field_path="linkedin_url",
                known_since=date.today(),
                raw_excerpt=_excerpt({"linkedin_url": linkedin_url}),
            )
            fact_ids.append(fid_li)

        # 3. Analyze work history / roles
        experience_list = (
            person_data.get("experience")
            or person_data.get("work_history")
            or person_data.get("positions")
            or []
        )
        if isinstance(experience_list, dict):
            experience_list = experience_list.get("roles", []) or [experience_list]

        max_prior_hc: float = 0.0
        max_prior_hc_company: str = ""
        years_at_largest: float = 0.0
        is_repeat: bool = False
        total_exp_years: float = 0.0
        role_fact_ids: list[str] = []

        now_date = date.today()

        for role_idx, role in enumerate(experience_list):
            if not isinstance(role, dict):
                continue

            r_comp = str(role.get("company") or role.get("company_name") or "").strip()
            r_title = str(role.get("title") or role.get("role") or "").strip()
            r_hc = role.get("company_headcount") or role.get("headcount")
            r_start_str = role.get("start_date") or role.get("start")
            r_end_str = role.get("end_date") or role.get("end")

            start_dt = _parse_date(r_start_str)
            end_dt = _parse_date(r_end_str)
            effective_end = end_dt or now_date

            # Calculate tenure in years
            tenure_years: float = 1.0  # default assumption if dates missing
            if start_dt:
                days = (effective_end - start_dt).days
                if days > 0:
                    tenure_years = round(days / 365.25, 1)

            total_exp_years += tenure_years

            # Record role fact
            role_fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=founder_subject,
                key=f"founder.role.{role_idx}.company",
                value=f"{r_title} at {r_comp}" if r_title else r_comp,
                unit=None,
                endpoint="screener/people/enrich",
                field_path=f"experience[{role_idx}]",
                known_since=start_dt or date.today(),
                raw_excerpt=_excerpt(role),
            )
            fact_ids.append(role_fid)
            role_fact_ids.append(role_fid)

            # Check if this is a prior company (not current company being analyzed)
            is_current = (
                r_comp.lower() in company_name.lower()
                or company_name.lower() in r_comp.lower()
            )

            # Check for repeat founder
            if not is_current and r_comp:
                title_lower = r_title.lower()
                if "founder" in title_lower or "co-founder" in title_lower or "founding" in title_lower:
                    is_repeat = True

            # Check operator signal (prior employer headcount)
            try:
                hc_num = float(r_hc) if r_hc is not None else 0.0
            except (ValueError, TypeError):
                hc_num = 0.0

            if not is_current and hc_num > max_prior_hc:
                max_prior_hc = hc_num
                max_prior_hc_company = r_comp
                years_at_largest = tenure_years

        # Store derived founder metrics
        if max_prior_hc > 0:
            fid_hc = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=founder_subject,
                key="founder.prior_employer_headcount",
                value=max_prior_hc,
                unit="employees",
                endpoint="derived",
                field_path=f"max(experience.headcount)",
                known_since=date.today(),
                raw_excerpt=_excerpt({"company": max_prior_hc_company, "headcount": max_prior_hc}),
                derived_from=role_fact_ids,
            )
            fact_ids.append(fid_hc)

            fid_yr = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=founder_subject,
                key="founder.years_at_prior",
                value=years_at_largest,
                unit="years",
                endpoint="derived",
                field_path="experience.tenure",
                known_since=date.today(),
                raw_excerpt=_excerpt({"company": max_prior_hc_company, "years": years_at_largest}),
                derived_from=role_fact_ids,
            )
            fact_ids.append(fid_yr)

            findings.append(
                f"{raw_name}: Operator background at {max_prior_hc_company} "
                f"({int(max_prior_hc):,} employees, {years_at_largest:.1f} yrs)"
            )
        else:
            unknowns.append(f"founder_{idx}_prior_employer_headcount_unknown")

        # Repeat founder fact
        fid_repeat = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject=founder_subject,
            key="founder.is_repeat_founder",
            value=is_repeat,
            unit=None,
            endpoint="derived",
            field_path="experience.title_contains_founder",
            known_since=date.today(),
            raw_excerpt=_excerpt({"is_repeat_founder": is_repeat}),
            derived_from=role_fact_ids,
        )
        fact_ids.append(fid_repeat)
        if is_repeat:
            findings.append(f"{raw_name}: Repeat founder with prior startup leadership experience")

        # Total experience
        if total_exp_years > 0:
            fid_exp = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=founder_subject,
                key="founder.years_experience",
                value=round(total_exp_years, 1),
                unit="years",
                endpoint="derived",
                field_path="sum(experience.tenure)",
                known_since=date.today(),
                raw_excerpt=_excerpt({"total_experience_years": total_exp_years}),
                derived_from=role_fact_ids,
            )
            fact_ids.append(fid_exp)

    # Emit completion event
    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="done",
        message=f"Founder Analyst complete. Extracted {len(fact_ids)} facts across {len(unique_founders)} founders.",
        data={"fact_ids_count": len(fact_ids), "findings": findings},
    )

    return AgentResult(
        agent=_AGENT,
        company_id=company_id,
        fact_ids=fact_ids,
        findings=findings,
        unknowns=unknowns,
        credits_used=credits.used - start_credits,
        status="ok" if fact_ids else "partial",
    )
