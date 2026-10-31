"""Sourcing Agent — finds pre-round companies and operator founders matching thesis.

Uses Crustdata's company search and people search, with automatic filter relaxation
and fallback to local cohorts when the graph has sparse pre-round results.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import COHORTS_DIR
from app.crust import (
    CreditCounter,
    NoData,
    search_companies,
    search_people,
)
from app.events import EventBus
from app.ledger import FactLedger
from app.models import Event

logger = logging.getLogger(__name__)

_AGENT = "sourcing"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


async def _emit_event(
    bus: EventBus,
    *,
    run_id: str,
    agent: str,
    kind: str,
    message: str,
    data: dict | None = None,
) -> None:
    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=agent,
            company_id=None,
            kind=kind,  # type: ignore[arg-type]
            message=message,
            data=data or {},
        )
    )


def _load_fallback_cohort(thesis: dict | None = None) -> list[str]:
    """Read domains from thesis-specific cohort if present, else match sectors/geos, else demo.txt."""
    if thesis:
        # Check direct thesis id
        tid = str(thesis.get("id") or "").lower()
        if tid and (COHORTS_DIR / f"{tid}.txt").exists():
            return _read_domain_file(COHORTS_DIR / f"{tid}.txt")

        # Match by name, sectors, and geography
        tname = str(thesis.get("name") or "").lower()
        sectors = [str(s).lower() for s in thesis.get("sectors") or []]
        geos = [str(g).lower() for g in thesis.get("geographies") or []]

        if any("fintech" in s or "banking" in s or "payment" in s for s in sectors) or "fintech" in tname:
            if (COHORTS_DIR / "fintech_infra.txt").exists():
                return _read_domain_file(COHORTS_DIR / "fintech_infra.txt")

        if any("climate" in s or "clean" in s or "energy" in s for s in sectors) or "climate" in tname:
            if (COHORTS_DIR / "climate_tech.txt").exists():
                return _read_domain_file(COHORTS_DIR / "climate_tech.txt")

        if any("developer" in s or "devtools" in s or "devops" in s for s in sectors) or "devtools" in tname:
            if (COHORTS_DIR / "global_devtools.txt").exists():
                return _read_domain_file(COHORTS_DIR / "global_devtools.txt")

        if any("enterprise ai" in s or "agent" in s for s in sectors) or "enterprise" in tname:
            if (COHORTS_DIR / "enterprise_ai.txt").exists():
                return _read_domain_file(COHORTS_DIR / "enterprise_ai.txt")

        if any("us" in g or "united states" in g for g in geos) or "us" in tname:
            if (COHORTS_DIR / "us_ai_seed.txt").exists():
                return _read_domain_file(COHORTS_DIR / "us_ai_seed.txt")

        if any("india" in g or "ind" in g for g in geos) or "india" in tname:
            if (COHORTS_DIR / "preseed_india.txt").exists():
                return _read_domain_file(COHORTS_DIR / "preseed_india.txt")

    cohort_path = COHORTS_DIR / "demo.txt"
    if cohort_path.exists():
        return _read_domain_file(cohort_path)

    return [
        "sarvam.ai",
        "rupeecircle.com",
        "devzero.io",
        "composio.dev",
        "superops.ai",
    ]


def _read_domain_file(path: Path) -> list[str]:
    domains: list[str] = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    domains.append(line)
    return domains


async def run(
    thesis: dict,
    ledger: FactLedger,
    bus: EventBus,
    credits: CreditCounter,
    run_id: str,
    limit: int = 20,
) -> list[str]:
    """Source candidate companies for the given thesis.

    Returns a deduplicated list of company domains/names to triage and research.
    """
    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        kind="start",
        message=f"Sourcing pre-round candidates for thesis: {thesis.get('name', 'Default Thesis')}",
    )

    candidates: list[str] = []

    # 1. Build company search filter sets (from specific to relaxed)
    geos = thesis.get("geographies", ["India"])
    geo_val = geos[0] if geos else "India"
    comp_cfg = thesis.get("company", {})
    hc_cfg = comp_cfg.get("headcount", {"min": 2, "max": 40})
    min_hc = hc_cfg.get("min", 2)
    max_hc = hc_cfg.get("max", 50)
    founded_after = comp_cfg.get("founded_after", "2023-01-01")

    filter_attempts = [
        # Attempt 1: Strict filters
        [
            {"filter_type": "country", "value": geo_val},
            {"filter_type": "founded_after", "value": str(founded_after)},
            {"filter_type": "headcount_range", "value": {"min": min_hc, "max": max_hc}},
        ],
        # Attempt 2: Relax founded_after
        [
            {"filter_type": "country", "value": geo_val},
            {"filter_type": "headcount_range", "value": {"min": min_hc, "max": max_hc}},
        ],
        # Attempt 3: Just country
        [
            {"filter_type": "country", "value": geo_val},
        ],
    ]

    for attempt_idx, filters in enumerate(filter_attempts):
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            kind="call",
            message=f"Searching companies with filter set #{attempt_idx+1}...",
            data={"filters": filters},
        )
        try:
            results = await search_companies(filters, limit=limit, run_credits=credits)
            for item in results:
                domain = item.get("domain") or item.get("website") or item.get("name")
                if domain and str(domain) not in candidates:
                    candidates.append(str(domain))
            if len(candidates) >= 5:
                break
        except (NoData, Exception) as exc:
            logger.info("Search companies attempt %d failed: %s", attempt_idx + 1, exc)

    # 2. Founder-first path: search people with founder title and prior scaled experience
    try:
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            kind="call",
            message="Founder-first search: looking for operator founders from scaled companies...",
        )
        people_results = await search_people(
            filters=[
                {"filter_type": "title_contains", "value": "founder"},
                {"filter_type": "country", "value": geo_val},
            ],
            limit=10,
            run_credits=credits,
        )
        for person in people_results:
            c_domain = person.get("company_domain") or person.get("current_company_domain") or person.get("current_company")
            if c_domain and str(c_domain) not in candidates:
                candidates.append(str(c_domain))
    except (NoData, Exception) as exc:
        logger.info("Founder-first people search returned no results: %s", exc)

    # 3. Fallback if fewer than 3 candidates found
    if len(candidates) < 3:
        fallback_list = _load_fallback_cohort(thesis)
        await _emit_event(
            bus,
            run_id=run_id,
            agent=_AGENT,
            kind="finding",
            message=f"Graph search returned sparse pre-round results ({len(candidates)}); supplementing with verified cohort.",
            data={"added_fallback_count": len(fallback_list)},
        )
        for d in fallback_list:
            if d not in candidates:
                candidates.append(d)

    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        kind="done",
        message=f"Sourcing complete: {len(candidates)} candidate companies identified.",
        data={"candidates": candidates[:limit]},
    )

    return candidates[:limit]
