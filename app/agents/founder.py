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

_WELL_KNOWN_HEADCOUNTS: dict[str, float] = {
    "google": 180000.0,
    "alphabet": 180000.0,
    "microsoft": 220000.0,
    "amazon": 1500000.0,
    "meta": 70000.0,
    "facebook": 70000.0,
    "apple": 160000.0,
    "uber": 30000.0,
    "rubrik": 3500.0,
    "stripe": 8000.0,
    "flipkart": 40000.0,
    "swiggy": 6000.0,
    "zomato": 5000.0,
    "american express": 70000.0,
    "amex": 70000.0,
    "goldman sachs": 45000.0,
    "mckinsey": 35000.0,
    "bcg": 30000.0,
    "bain": 15000.0,
    "byju": 15000.0,
    "ola": 7000.0,
    "paytm": 10000.0,
    "phonepe": 5000.0,
    "cred": 1000.0,
    "razorpay": 3000.0,
    "browserstack": 1200.0,
    "freshworks": 5000.0,
    "postman": 1000.0,
    "hasura": 500.0,
    "nirvana": 500.0,
    "salesforce": 75000.0,
    "oracle": 140000.0,
    "cisco": 80000.0,
    "linkedin": 20000.0,
    "twitter": 8000.0,
}


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
    founder_raw_map: dict[str, dict[str, Any]] = {}

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
        elif f.key == "company.founders_raw" and isinstance(f.value, list):
            for raw_p in f.value:
                if isinstance(raw_p, dict):
                    bp = raw_p.get("basic_profile") or {}
                    p_name = bp.get("name") or raw_p.get("name")
                    if p_name:
                        founder_raw_map[str(p_name).strip().lower()] = raw_p

    # Deduplicate founder names
    unique_founders: list[str] = []
    for name in founder_names:
        if name and name not in unique_founders:
            unique_founders.append(name)

    if not unique_founders:
        # Try person search for founders of this company
        clean_company = company_name.replace("www.", "").replace(".com", "").replace(".ai", "").replace(".io", "").replace(".dev", "")
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="call",
            message=f"No founders listed in company profile; searching people for {clean_company}...",
        )
        try:
            search_results = await search_people(
                filters=[
                    {"filter_type": "company_name", "value": clean_company},
                ],
                limit=10,
                run_credits=credits,
            )
            for person in search_results:
                bp = person.get("basic_profile") or {}
                name = bp.get("name") or person.get("name") or person.get("full_name")
                title = str(bp.get("current_title") or person.get("title") or bp.get("headline") or "").lower()
                if not name:
                    continue
                # Match leadership / executive keywords: founder, director, ceo, cto, coo, partner, president, head, lead, managing
                if any(kw in title for kw in ("founder", "director", "managing", "ceo", "chief", "cto", "coo", "partner", "president", "head")):
                    if str(name) not in unique_founders:
                        unique_founders.append(str(name))
                        founder_raw_map[str(name).strip().lower()] = person
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

        # Check if already present from company enrichment
        f_key = founder_name.strip().lower()
        if f_key in founder_raw_map:
            person_data = founder_raw_map[f_key]
        else:
            for r_name, r_obj in founder_raw_map.items():
                if f_key in r_name or r_name in f_key:
                    person_data = r_obj
                    break

        if not person_data:
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
        bp = person_data.get("basic_profile") or {}
        raw_name = bp.get("name") or person_data.get("name") or person_data.get("full_name") or founder_name
        soc = person_data.get("social_handles") or {}
        p_net = soc.get("professional_network_identifier") or {}
        linkedin_url = (
            p_net.get("profile_url")
            or person_data.get("linkedin_url")
            or person_data.get("profile_url")
        )
        current_title = bp.get("current_title") or person_data.get("current_title") or person_data.get("title")

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
        exp_obj = person_data.get("experience") or person_data.get("work_history") or person_data.get("positions") or []
        experience_list: list[dict[str, Any]] = []
        if isinstance(exp_obj, dict):
            emp_details = exp_obj.get("employment_details") or {}
            if isinstance(emp_details, dict):
                experience_list = list(emp_details.get("past") or []) + list(emp_details.get("current") or [])
            else:
                experience_list = exp_obj.get("roles", []) or [exp_obj]
        elif isinstance(exp_obj, list):
            experience_list = exp_obj

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

            r_comp = str(
                role.get("employer_name")
                or role.get("company")
                or role.get("company_name")
                or ""
            ).strip()
            r_title = str(
                role.get("employee_title")
                or role.get("title")
                or role.get("role")
                or ""
            ).strip()
            r_hc = role.get("company_headcount") or role.get("headcount")
            r_start_str = role.get("employee_start_date") or role.get("start_date") or role.get("start")
            r_end_str = role.get("employee_end_date") or role.get("end_date") or role.get("end")

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

            # If headcount not in role, lookup well-known scaled tech companies
            if hc_num == 0.0 and r_comp:
                r_comp_lower = r_comp.lower()
                for wk, wk_hc in _WELL_KNOWN_HEADCOUNTS.items():
                    if wk in r_comp_lower:
                        hc_num = wk_hc
                        break

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
