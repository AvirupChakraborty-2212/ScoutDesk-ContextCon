"""Company Analyst agent — enriches a company and extracts structured Facts.

Only `crust.py` calls Crustdata; this module consumes those typed functions.
No LLM is used here — all values come directly from API responses.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from app.crust import (
    CreditCounter,
    NoData,
    enrich_company,
    identify_company,
)
from app.events import EventBus
from app.ledger import (
    KNOWN_SINCE_FUNDING,
    KNOWN_SINCE_NEWS,
    KNOWN_SINCE_REALTIME,
    FactLedger,
)
from app.models import AgentResult, AgentTask, Event, Fact

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_AGENT = "company"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _today() -> date:
    return date.today()


def _excerpt(data: Any, max_chars: int = 300) -> str:
    """Return a compact JSON snippet suitable for a citation popover."""
    try:
        raw = json.dumps(data, default=str)
    except (TypeError, ValueError):
        raw = str(data)
    return raw[:max_chars]


def _parse_date(value: Any) -> date | None:
    """Best-effort parse of a date string into a :class:`date`."""
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
) -> str:
    """Construct a :class:`Fact`, store it in *ledger*, and return its id."""
    fact = Fact(
        id="",
        company_id=company_id,
        subject=subject,
        key=key,
        value=value,
        unit=unit,
        endpoint=endpoint,
        field_path=field_path,
        derived_from=[],
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run(
    task: AgentTask,
    ledger: FactLedger,
    bus: EventBus,
    credits: CreditCounter,
) -> AgentResult:
    """Run the Company Analyst for *task.company_id*.

    Steps
    -----
    1. Emit ``start`` event.
    2. Identify the company via Crustdata to get canonical id + domain.
    3. Enrich the company across all relevant field groups.
    4. Extract structured :class:`Fact` objects and store them in *ledger*.
    5. Emit ``fact`` and ``finding`` events.
    6. Handle :class:`NoData` gracefully — record unknowns, emit partial status.
    7. Return :class:`AgentResult`.
    """
    run_id = task.run_id
    company_id = task.company_id
    fact_ids: list[str] = []
    findings: list[str] = []
    unknowns: list[str] = []
    status = "ok"

    ENRICH_ENDPOINT = "/screener/company/enrich"

    # Step 1 — start event
    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="start",
        message=f"Company Analyst starting for {company_id}",
    )

    # Step 2 — identify company
    try:
        identifier_data = await identify_company(company_id, credits)
    except NoData as exc:
        logger.warning("company.identify failed: %s", exc)
        unknowns.append(f"company_not_found: {exc}")
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="error",
            message=f"Could not identify company: {exc}",
        )
        return AgentResult(
            agent=_AGENT,
            company_id=company_id,
            fact_ids=fact_ids,
            findings=findings,
            unknowns=unknowns,
            credits_used=credits.used,
            status="failed",
        )

    # Build identifier for enrich: prefer company_id field, fall back to domain
    canonical_company_id: str = str(
        identifier_data.get("company_id")
        or identifier_data.get("id")
        or company_id
    )
    domain: str = str(identifier_data.get("domain") or "")
    identifier: dict[str, Any] = (
        {"company_id": canonical_company_id}
        if identifier_data.get("company_id") or identifier_data.get("id")
        else {"domain": domain or company_id}
    )

    # Store company.domain immediately from identify response
    if domain:
        fid = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject="company",
            key="company.domain",
            value=domain,
            unit=None,
            endpoint="/screener/company/identify",
            field_path="matches[0].domain",
            known_since=_today(),
            raw_excerpt=_excerpt(identifier_data),
        )
        fact_ids.append(fid)
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="fact",
            message=f"company.domain = {domain}",
            data={"fact_id": fid},
        )

    # Step 3 — enrich company
    try:
        enriched = await enrich_company(
            identifier,
            ["basic_info", "funding", "headcount", "people", "hiring", "competitors", "news"],
            credits,
        )
    except NoData as exc:
        logger.warning("company.enrich failed: %s", exc)
        unknowns.append(f"enrich_failed: {exc}")
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="error",
            message=f"Enrichment failed: {exc}",
        )
        return AgentResult(
            agent=_AGENT,
            company_id=company_id,
            fact_ids=fact_ids,
            findings=findings,
            unknowns=unknowns,
            credits_used=credits.used,
            status="partial",
        )

    # ------------------------------------------------------------------
    # Step 4 — extract facts
    # ------------------------------------------------------------------

    today = _today()

    # --- basic_info ---
    basic_info: dict[str, Any] = enriched.get("basic_info") or {}
    if basic_info:
        _basic_map: list[tuple[str, str, str | None]] = [
            ("name", "company.name", None),
            ("domain", "company.domain", None),
            ("country", "company.country", None),
            ("founded_year", "company.founded_year", "year"),
            ("description", "company.description", None),
            ("sector", "company.sector", None),
        ]
        for api_field, fact_key, unit in _basic_map:
            raw_val = basic_info.get(api_field)
            if raw_val is None:
                unknowns.append(f"missing: {fact_key}")
                continue
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key=fact_key,
                value=raw_val,
                unit=unit,
                endpoint=ENRICH_ENDPOINT,
                field_path=f"basic_info.{api_field}",
                known_since=today,
                raw_excerpt=_excerpt({api_field: raw_val}),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"{fact_key} = {raw_val}",
                data={"fact_id": fid},
            )
        # Update domain if freshly obtained from basic_info and not already stored
        if basic_info.get("domain") and not domain:
            domain = str(basic_info["domain"])
    else:
        unknowns.append("missing: basic_info")
        status = "partial"

    # --- headcount ---
    headcount: dict[str, Any] = enriched.get("headcount") or {}
    if headcount:
        _hc_map: list[tuple[str, str, str | None]] = [
            ("total", "company.headcount_total", "people"),
            ("mom_growth_percent", "company.headcount_mom", "%"),
            ("qoq_growth_percent", "company.headcount_qoq", "%"),
            ("yoy_growth_percent", "company.headcount_yoy", "%"),
        ]
        for api_field, fact_key, unit in _hc_map:
            raw_val = headcount.get(api_field)
            if raw_val is None:
                unknowns.append(f"missing: {fact_key}")
                continue
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key=fact_key,
                value=raw_val,
                unit=unit,
                endpoint=ENRICH_ENDPOINT,
                field_path=f"headcount.{api_field}",
                known_since=today,
                raw_excerpt=_excerpt({api_field: raw_val}),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"{fact_key} = {raw_val}",
                data={"fact_id": fid},
            )
        # Finding: headcount momentum
        yoy = headcount.get("yoy_growth_percent")
        total = headcount.get("total")
        if yoy is not None and total is not None:
            finding = f"Headcount is {total} people with {yoy:+.1f}% YoY growth"
            findings.append(finding)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="finding",
                message=finding,
            )
    else:
        unknowns.append("missing: headcount")
        status = "partial"

    # --- funding ---
    funding: dict[str, Any] = enriched.get("funding") or {}
    if funding:
        rounds_count = funding.get("rounds_count") or funding.get("total_rounds")
        total_raised = funding.get("total_raised_usd") or funding.get("total_raised")

        if rounds_count is not None:
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key="company.funding_rounds_count",
                value=rounds_count,
                unit=None,
                endpoint=ENRICH_ENDPOINT,
                field_path="funding.rounds_count",
                known_since=today,
                raw_excerpt=_excerpt({"rounds_count": rounds_count}),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"company.funding_rounds_count = {rounds_count}",
                data={"fact_id": fid},
            )

        if total_raised is not None:
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key="company.total_raised_usd",
                value=total_raised,
                unit="USD",
                endpoint=ENRICH_ENDPOINT,
                field_path="funding.total_raised_usd",
                known_since=today,
                raw_excerpt=_excerpt({"total_raised_usd": total_raised}),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"company.total_raised_usd = {total_raised}",
                data={"fact_id": fid},
            )

        # Per-round facts: known_since = round close/announce date (KNOWN_SINCE_FUNDING)
        rounds: list[dict[str, Any]] = funding.get("rounds") or []
        for n, rnd in enumerate(rounds):
            round_date_raw = rnd.get("date") or rnd.get("announced_date") or rnd.get("close_date")
            round_date: date | None = _parse_date(round_date_raw)
            round_amount = rnd.get("amount_usd") or rnd.get("amount")
            round_type = rnd.get("type") or rnd.get("round_type") or rnd.get("series")

            prefix = f"funding.round_{n}"

            if round_amount is not None:
                fid = _make_fact(
                    ledger=ledger,
                    company_id=company_id,
                    subject="funding",
                    key=f"{prefix}.amount",
                    value=round_amount,
                    unit="USD",
                    endpoint=ENRICH_ENDPOINT,
                    field_path=f"funding.rounds[{n}].amount",
                    known_since=round_date,  # KNOWN_SINCE_FUNDING
                    raw_excerpt=_excerpt(rnd),
                )
                fact_ids.append(fid)
                await _emit_event(
                    bus,
                    run_id=run_id,
                    agent=_AGENT,
                    company_id=company_id,
                    kind="fact",
                    message=f"{prefix}.amount = {round_amount} USD",
                    data={"fact_id": fid},
                )

            if round_type is not None:
                fid = _make_fact(
                    ledger=ledger,
                    company_id=company_id,
                    subject="funding",
                    key=f"{prefix}.type",
                    value=round_type,
                    unit=None,
                    endpoint=ENRICH_ENDPOINT,
                    field_path=f"funding.rounds[{n}].type",
                    known_since=round_date,  # KNOWN_SINCE_FUNDING
                    raw_excerpt=_excerpt(rnd),
                )
                fact_ids.append(fid)

            if round_date is not None:
                fid = _make_fact(
                    ledger=ledger,
                    company_id=company_id,
                    subject="funding",
                    key=f"{prefix}.date",
                    value=str(round_date),
                    unit="date",
                    endpoint=ENRICH_ENDPOINT,
                    field_path=f"funding.rounds[{n}].date",
                    known_since=round_date,  # KNOWN_SINCE_FUNDING
                    raw_excerpt=_excerpt(rnd),
                )
                fact_ids.append(fid)

            # Finding per round
            if round_amount and round_type and round_date:
                amount_m = round_amount / 1_000_000 if isinstance(round_amount, (int, float)) else round_amount
                finding = (
                    f"Raised ${amount_m:.1f}M {round_type} in {round_date.strftime('%b %Y')}"
                )
                findings.append(finding)
                await _emit_event(
                    bus,
                    run_id=run_id,
                    agent=_AGENT,
                    company_id=company_id,
                    kind="finding",
                    message=finding,
                )
    else:
        unknowns.append("missing: funding")
        status = "partial"

    # --- hiring / openings ---
    hiring: dict[str, Any] = enriched.get("hiring") or {}
    if hiring:
        _hiring_map: list[tuple[str, str, str | None]] = [
            ("openings_count", "openings.count", None),
            ("openings_growth_percent", "openings.growth_percent", "%"),
            ("openings_last_90d", "openings.last_90d", None),
        ]
        for api_field, fact_key, unit in _hiring_map:
            raw_val = hiring.get(api_field)
            if raw_val is None:
                # Try alternate field names Crustdata may use
                alternates = {
                    "openings_count": ["total_openings", "open_roles", "count"],
                    "openings_growth_percent": ["growth_percent", "openings_growth"],
                    "openings_last_90d": ["last_90_days", "openings_90d"],
                }
                for alt in alternates.get(api_field, []):
                    raw_val = hiring.get(alt)
                    if raw_val is not None:
                        break
            if raw_val is None:
                unknowns.append(f"missing: {fact_key}")
                continue
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key=fact_key,
                value=raw_val,
                unit=unit,
                endpoint=ENRICH_ENDPOINT,
                field_path=f"hiring.{api_field}",
                known_since=today,
                raw_excerpt=_excerpt({api_field: raw_val}),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"{fact_key} = {raw_val}",
                data={"fact_id": fid},
            )
    else:
        unknowns.append("missing: hiring")
        status = "partial"

    # --- people / founders ---
    people: dict[str, Any] = enriched.get("people") or {}
    founders_list: list[str] = []

    if people:
        # Crustdata may structure people as a list or a dict with roles
        people_entries: list[dict[str, Any]] = []
        if isinstance(people, list):
            people_entries = people
        elif isinstance(people, dict):
            people_entries = people.get("founders") or people.get("employees") or []

        for entry in people_entries:
            title: str = str(entry.get("title") or entry.get("role") or "")
            name: str = str(entry.get("name") or entry.get("full_name") or "")
            if not name:
                continue
            title_lower = title.lower()
            if any(kw in title_lower for kw in ("founder", "co-founder", "ceo", "cto", "coo")):
                founders_list.append(name)

        if founders_list:
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key="company.founders",
                value=json.dumps(founders_list),
                unit=None,
                endpoint=ENRICH_ENDPOINT,
                field_path="people",
                known_since=today,
                raw_excerpt=_excerpt(people_entries[:5]),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"company.founders = {founders_list}",
                data={"fact_id": fid},
            )
            findings.append(f"Founders identified: {', '.join(founders_list)}")
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="finding",
                message=f"Founders identified: {', '.join(founders_list)}",
            )
        else:
            unknowns.append("missing: founders")
    else:
        unknowns.append("missing: people")
        status = "partial"

    # --- competitors ---
    competitors: list[dict[str, Any]] = enriched.get("competitors") or []
    if competitors:
        for comp in competitors[:5]:
            comp_domain: str = str(comp.get("domain") or comp.get("website") or "")
            comp_name: str = str(comp.get("name") or comp.get("company_name") or "")
            if not comp_domain:
                continue
            fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=f"competitor:{comp_domain}",
                key=f"competitor.{comp_domain}.name",
                value=comp_name or comp_domain,
                unit=None,
                endpoint=ENRICH_ENDPOINT,
                field_path="competitors[].name",
                known_since=today,
                raw_excerpt=_excerpt(comp),
            )
            fact_ids.append(fid)
            await _emit_event(
                bus,
                run_id=run_id,
                agent=_AGENT,
                company_id=company_id,
                kind="fact",
                message=f"competitor.{comp_domain}.name = {comp_name}",
                data={"fact_id": fid},
            )
    else:
        unknowns.append("missing: competitors")

    # --- news ---
    news_items: list[dict[str, Any]] = enriched.get("news") or []
    if news_items:
        for n, item in enumerate(news_items):
            news_title: str = str(item.get("title") or "")
            news_url: str = str(item.get("url") or item.get("link") or "")
            news_date_raw = item.get("date") or item.get("published_at") or item.get("published_date")
            news_date: date | None = _parse_date(news_date_raw)

            if news_title:
                fid = _make_fact(
                    ledger=ledger,
                    company_id=company_id,
                    subject="company",
                    key=f"news.{n}.title",
                    value=news_title,
                    unit=None,
                    endpoint=ENRICH_ENDPOINT,
                    field_path=f"news[{n}].title",
                    known_since=news_date,  # KNOWN_SINCE_NEWS
                    raw_excerpt=_excerpt(item),
                )
                fact_ids.append(fid)
                await _emit_event(
                    bus,
                    run_id=run_id,
                    agent=_AGENT,
                    company_id=company_id,
                    kind="fact",
                    message=f"news.{n}.title = {news_title[:80]}",
                    data={"fact_id": fid},
                )

            if news_date:
                fid = _make_fact(
                    ledger=ledger,
                    company_id=company_id,
                    subject="company",
                    key=f"news.{n}.date",
                    value=str(news_date),
                    unit="date",
                    endpoint=ENRICH_ENDPOINT,
                    field_path=f"news[{n}].date",
                    known_since=news_date,  # KNOWN_SINCE_NEWS
                    raw_excerpt=_excerpt(item),
                )
                fact_ids.append(fid)

            if news_url:
                fid = _make_fact(
                    ledger=ledger,
                    company_id=company_id,
                    subject="company",
                    key=f"news.{n}.url",
                    value=news_url,
                    unit=None,
                    endpoint=ENRICH_ENDPOINT,
                    field_path=f"news[{n}].url",
                    known_since=news_date,  # KNOWN_SINCE_NEWS
                    raw_excerpt=_excerpt(item),
                )
                fact_ids.append(fid)
    else:
        unknowns.append("missing: news")

    # Emit credits snapshot
    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="credits",
        message=f"Credits used: {credits.used}/{credits.budget}",
        data={"used": credits.used, "budget": credits.budget},
    )

    return AgentResult(
        agent=_AGENT,
        company_id=company_id,
        fact_ids=fact_ids,
        findings=findings,
        unknowns=unknowns,
        credits_used=credits.used,
        status=status,
    )
