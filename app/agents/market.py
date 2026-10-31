"""Market Analyst agent — compares company metrics against competitor and peer medians.

Enriches identified competitors, computes peer benchmarks in pure code,
and records comparative market signals in the FactLedger.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime, timezone
from typing import Any

from app.crust import (
    CreditCounter,
    NoData,
    enrich_company,
)
from app.events import EventBus
from app.ledger import (
    KNOWN_SINCE_DERIVED,
    KNOWN_SINCE_REALTIME,
    FactLedger,
)
from app.models import AgentResult, AgentTask, Event, Fact

logger = logging.getLogger(__name__)

_AGENT = "market"


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
    competitor_domains: list[str],
    ledger: FactLedger,
    bus: EventBus,
    credits: CreditCounter,
) -> AgentResult:
    """Run the Market Analyst for competitor benchmarking."""
    company_id = task.company_id
    run_id = task.run_id
    start_credits = credits.used

    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="start",
        message=f"Market Analyst starting for {company_id} ({len(competitor_domains)} competitors provided)",
    )

    fact_ids: list[str] = []
    findings: list[str] = []
    unknowns: list[str] = []

    if not competitor_domains:
        unknowns.append("no_competitors_identified")
        findings.append("No direct competitors available in graph for peer benchmark")
        return AgentResult(
            agent=_AGENT,
            company_id=company_id,
            fact_ids=fact_ids,
            findings=findings,
            unknowns=unknowns,
            credits_used=0,
            status="partial",
        )

    peer_yoys: list[float] = []
    peer_moms: list[float] = []
    peer_fact_ids: list[str] = []

    # Enrich up to 4 competitors
    for domain in competitor_domains[:4]:
        clean_domain = str(domain).strip().lower()
        if not clean_domain:
            continue

        subject = f"competitor:{clean_domain}"
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            company_id=company_id,
            kind="call",
            message=f"Benchmarking competitor: {clean_domain}",
        )

        try:
            comp_data = await enrich_company(
                identifier={"domain": clean_domain},
                fields=["basic_info", "headcount"],
                run_credits=credits,
            )
            binfo = comp_data.get("basic_info", {})
            hcount = comp_data.get("headcount", {})

            comp_name = binfo.get("name") or clean_domain
            mom = hcount.get("growth_mom") or hcount.get("mom")
            yoy = hcount.get("growth_yoy") or hcount.get("yoy")

            fid_name = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject=subject,
                key=f"competitor.{clean_domain}.name",
                value=comp_name,
                unit=None,
                endpoint="screener/company/enrich",
                field_path="basic_info.name",
                known_since=_today(),
                raw_excerpt=_excerpt({"name": comp_name, "domain": clean_domain}),
            )
            fact_ids.append(fid_name)

            if yoy is not None:
                try:
                    yoy_float = float(yoy)
                    peer_yoys.append(yoy_float)
                    fid_yoy = _make_fact(
                        ledger=ledger,
                        company_id=company_id,
                        subject=subject,
                        key=f"competitor.{clean_domain}.headcount_yoy",
                        value=yoy_float,
                        unit="%",
                        endpoint="screener/company/enrich",
                        field_path="headcount.yoy",
                        known_since=_today(),
                        raw_excerpt=_excerpt({"domain": clean_domain, "yoy": yoy_float}),
                    )
                    fact_ids.append(fid_yoy)
                    peer_fact_ids.append(fid_yoy)
                except (ValueError, TypeError):
                    pass

            if mom is not None:
                try:
                    mom_float = float(mom)
                    peer_moms.append(mom_float)
                    fid_mom = _make_fact(
                        ledger=ledger,
                        company_id=company_id,
                        subject=subject,
                        key=f"competitor.{clean_domain}.headcount_mom",
                        value=mom_float,
                        unit="%",
                        endpoint="screener/company/enrich",
                        field_path="headcount.mom",
                        known_since=_today(),
                        raw_excerpt=_excerpt({"domain": clean_domain, "mom": mom_float}),
                    )
                    fact_ids.append(fid_mom)
                except (ValueError, TypeError):
                    pass

        except (NoData, Exception) as exc:
            logger.info("Enriching competitor %s failed: %s", clean_domain, exc)
            unknowns.append(f"competitor_{clean_domain}_enrich_failed")

    # Compute derived peer medians
    if peer_yoys:
        median_yoy = round(statistics.median(peer_yoys), 4)
        fid_med_yoy = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject="company",
            key="market.peer_headcount_yoy_median",
            value=median_yoy,
            unit="%",
            endpoint="derived",
            field_path="median(competitors.yoy)",
            known_since=_today(),
            raw_excerpt=_excerpt({"peer_yoys": peer_yoys, "median": median_yoy}),
            derived_from=peer_fact_ids,
        )
        fact_ids.append(fid_med_yoy)
        findings.append(f"Peer median headcount YoY growth: {median_yoy:+.1%}")

    if peer_moms:
        median_mom = round(statistics.median(peer_moms), 4)
        fid_med_mom = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject="company",
            key="market.peer_headcount_mom_median",
            value=median_mom,
            unit="%",
            endpoint="derived",
            field_path="median(competitors.mom)",
            known_since=_today(),
            raw_excerpt=_excerpt({"peer_moms": peer_moms, "median": median_mom}),
            derived_from=peer_fact_ids,
        )
        fact_ids.append(fid_med_mom)

    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="done",
        message=f"Market Analyst complete. Benchmarked {len(peer_yoys)} peers.",
        data={"fact_ids_count": len(fact_ids), "peer_count": len(peer_yoys)},
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
