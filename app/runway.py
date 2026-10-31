"""Runway Watch — monitors portfolio companies for cash-out risk using headcount and hiring signals.

Clearly labels all calculations as proxy estimates based on public graph facts:
  burn_usd ≈ headcount × monthly_cost_per_head(country)
  cash_usd ≈ last_round_amount
  runway ≈ cash / burn
  months_left ≈ runway − months_since_last_raise
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from app.crust import (
    CreditCounter,
    NoData,
    enrich_company,
    identify_company,
)
from app.events import EventBus
from app.ledger import (
    KNOWN_SINCE_FUNDING,
    KNOWN_SINCE_REALTIME,
    FactLedger,
)
from app.models import Event, Fact, RunwayReport

logger = logging.getLogger(__name__)

_AGENT = "runway"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _today() -> date:
    return date.today()


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


def load_portfolio(portfolio_path: Path) -> list[str]:
    """Load portfolio company domains from portfolio.yaml."""
    if not portfolio_path.exists():
        return []
    with open(portfolio_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return []
    companies = data.get("portfolio", []) or data.get("companies", [])
    domains: list[str] = []
    for item in companies:
        if isinstance(item, str):
            domains.append(item.strip())
        elif isinstance(item, dict):
            d = item.get("domain") or item.get("name")
            if d:
                domains.append(str(d).strip())
    return domains


async def run_runway_watch(
    portfolio_domains: list[str],
    thesis: dict,
    ledger: FactLedger,
    bus: EventBus,
    credits: CreditCounter,
    run_id: str,
) -> list[RunwayReport]:
    """Run cash-out risk analysis across portfolio companies."""
    if run_id not in bus._queues:
        bus.create_run(run_id)

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="start",
            message=f"Runway Watch starting across {len(portfolio_domains)} portfolio companies",
        )
    )

    runway_cfg = thesis.get("runway", {})
    cost_map: dict[str, float] = runway_cfg.get(
        "monthly_cost_per_head_usd",
        {"India": 2500, "US": 15000, "default": 6000},
    )
    alert_months_threshold = float(runway_cfg.get("alert_months_left", 6))

    reports: list[RunwayReport] = []

    default_cost_map = {
        "India": 2500, "IND": 2500,
        "US": 15000, "USA": 15000,
        "Netherlands": 7000, "NLD": 7000,
        "UK": 8000, "GBR": 8000,
        "Germany": 7500, "DEU": 7500,
        "France": 7000, "FRA": 7000,
        "default": 6000,
    }
    merged_cost_map = {**default_cost_map, **cost_map}

    for domain in portfolio_domains:
        clean_domain = (
            domain.strip()
            .lower()
            .replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .split("/")[0]
        )
        if not clean_domain:
            continue

        company_id = clean_domain
        await bus.emit(
            Event(
                run_id=run_id,
                ts=_now(),
                agent=_AGENT,
                company_id=company_id,
                kind="call",
                message=f"Analyzing portfolio signals for {company_id}",
            )
        )

        company_data: dict[str, Any] = {}
        try:
            company_data = await enrich_company(
                identifier={"domain": clean_domain},
                fields=["basic_info", "funding", "headcount", "hiring", "people"],
                run_credits=credits,
            )
        except (NoData, Exception) as exc:
            logger.info("Enriching portfolio company %s directly failed, trying identify: %s", clean_domain, exc)
            try:
                ident = await identify_company(clean_domain, credits)
                cid = ident.get("company_id")
                if cid:
                    company_data = await enrich_company(
                        identifier={"company_id": cid},
                        fields=["basic_info", "funding", "headcount", "hiring", "people"],
                        run_credits=credits,
                    )
                elif "company_data" in ident:
                    company_data = ident["company_data"]
            except Exception as e2:
                logger.info("Identify fallback failed for %s: %s", clean_domain, e2)

        binfo = company_data.get("basic_info", {})
        hcount = company_data.get("headcount", {})
        funding = company_data.get("funding", {})
        hiring = company_data.get("hiring", {})

        raw_country = (
            hcount.get("largest_headcount_country")
            or binfo.get("country")
            or (binfo.get("location") or {}).get("country")
            or (binfo.get("location") or {}).get("country_code")
            or (binfo.get("location") or {}).get("raw")
            or "default"
        )
        country_str = str(raw_country).strip()
        country_aliases = {
            "IND": "India",
            "USA": "US",
            "NLD": "Netherlands",
            "GBR": "UK",
            "DEU": "Germany",
            "FRA": "France",
        }
        country = country_aliases.get(country_str, country_str)
        cost_per_head = merged_cost_map.get(country, merged_cost_map.get(country_str, merged_cost_map.get("default", 6000)))

        # Extract headcount
        headcount_val: float | None = None
        for k in ("total", "headcount", "total_headcount", "employees", "headcount_latest", "current_headcount"):
            if hcount.get(k) is not None:
                try:
                    headcount_val = float(hcount[k])
                    break
                except (ValueError, TypeError):
                    pass

        if headcount_val is None:
            rng = binfo.get("employee_count_range") or binfo.get("headcount_range")
            if rng and "-" in str(rng):
                try:
                    parts = str(rng).split("-")
                    headcount_val = (float(parts[0]) + float(parts[1])) / 2.0
                except (ValueError, IndexError):
                    pass

        # Headcount facts in ledger
        hc_fact_id: str | None = None
        if headcount_val is not None:
            hc_fact = Fact(
                id="",
                company_id=company_id,
                subject="company",
                key="company.headcount_total",
                value=headcount_val,
                unit="employees",
                endpoint="screener/company/enrich",
                field_path="headcount.total",
                derived_from=[],
                known_since=_today(),
                fetched_at=_now(),
                raw_excerpt=str({"headcount": headcount_val}),
            )
            hc_fact_id = ledger.add(hc_fact)

        # Extract last round amount and date
        rounds = funding.get("milestones") or funding.get("rounds") or funding.get("funding_rounds") or []
        last_round_amount: float | None = None
        last_round_date: date | None = None
        last_round_type: str | None = None
        funding_fact_id: str | None = None

        if isinstance(rounds, list) and rounds:
            valid_rounds = [r for r in rounds if isinstance(r, dict)]
            for r in valid_rounds:
                amt = r.get("amount_usd") or r.get("amount") or r.get("money_raised")
                r_dt_str = r.get("announced_on") or r.get("date") or r.get("round_date") or r.get("close_date")
                r_dt = _parse_date(r_dt_str)
                if r_dt and (last_round_date is None or r_dt > last_round_date):
                    last_round_date = r_dt
                if amt is not None and last_round_amount is None:
                    try:
                        val = float(amt)
                        if val > 0:
                            last_round_amount = val
                            last_round_type = r.get("round_type") or r.get("type") or r.get("series")
                    except (ValueError, TypeError):
                        pass

        # If no per-round details, try top-level funding fields
        if last_round_amount is None:
            for k in ("last_round_amount_usd", "total_investment_usd", "funds_total_usd", "total_funding_usd", "total_funding", "total_raised_usd", "total_raised"):
                tot = funding.get(k)
                try:
                    if tot is not None and float(tot) > 0:
                        last_round_amount = float(tot)
                        break
                except (ValueError, TypeError):
                    pass

        if last_round_date is None:
            for k in ("last_fundraise_date", "last_fund_date", "announced_on", "last_round_date"):
                dt_raw = funding.get(k)
                if dt_raw:
                    last_round_date = _parse_date(dt_raw)
                    if last_round_date:
                        break

        if last_round_type is None:
            last_round_type = str(funding.get("last_round_type") or funding.get("round_type") or "")

        if last_round_amount is not None:
            fund_fact = Fact(
                id="",
                company_id=company_id,
                subject="company",
                key="funding.last_round_amount",
                value=last_round_amount,
                unit="USD",
                endpoint="screener/company/enrich",
                field_path="funding.last_round.amount",
                derived_from=[],
                known_since=last_round_date or _today(),
                fetched_at=_now(),
                raw_excerpt=str({"amount": last_round_amount, "date": str(last_round_date)}),
            )
            funding_fact_id = ledger.add(fund_fact)

        # Warning signals
        warning_signals: list[str] = []
        assumptions: list[str] = [
            f"Monthly burn proxy = {int(headcount_val or 10)} headcount x ${cost_per_head:,.0f}/month ({country})",
        ]

        mom = hcount.get("growth_mom") or hcount.get("mom")
        qoq = hcount.get("growth_qoq") or hcount.get("qoq")
        if mom is not None:
            try:
                if float(mom) < 0:
                    warning_signals.append(f"Headcount contracted MoM ({float(mom):+.1%})")
            except (ValueError, TypeError):
                pass
        if qoq is not None:
            try:
                if float(qoq) < 0:
                    warning_signals.append(f"Headcount contracted QoQ ({float(qoq):+.1%})")
            except (ValueError, TypeError):
                pass

        openings_cnt = hiring.get("openings_count")
        if openings_cnt is None:
            openings_cnt = hiring.get("active_postings")
        if openings_cnt is not None:
            try:
                if float(openings_cnt) == 0:
                    warning_signals.append("Zero active job postings (hiring freeze detected)")
            except (ValueError, TypeError):
                pass
        elif hiring and hiring.get("recent_titles_csv") is None:
            warning_signals.append("Zero active job postings (hiring freeze detected)")

        months_since_raise: float = 0.0
        if last_round_date:
            days = (_today() - last_round_date).days
            months_since_raise = max(0.0, round(days / 30.4, 1))
            round_label = f" {last_round_type.upper()}" if last_round_type else ""
            assumptions.append(f"Last institutional round{round_label} dated {last_round_date} ({months_since_raise:.1f} months ago)")
            if months_since_raise > 18:
                warning_signals.append(f"Long fundraising gap: {months_since_raise:.1f} months since last round with no new capital")

        # Compute runway
        est_months_left: float | None = None
        level: Literal["ok", "watch", "alert", "unknown"]

        if headcount_val is None or last_round_amount is None:
            level = "unknown"
            est_months_left = None
            if not company_data or not binfo:
                warning_signals = [f"Entity not found: Crustdata has no corporate registry matches for '{company_id}'."]
                assumptions = [
                    f"Domain '{company_id}' returned 0 matches in business registry.",
                    "Try an active portfolio company: chargetrip.com, sarvam.ai, composio.dev, devzero.io, rupeecircle.com",
                ]
            else:
                assumptions.append("Missing headcount or funding capital baseline; cash-out date unknowable without guessing")
        else:
            monthly_burn = max(headcount_val * cost_per_head, 5000.0)
            total_runway_months = last_round_amount / monthly_burn
            est_months_left = max(0.0, round(total_runway_months - months_since_raise, 1))

            if est_months_left <= alert_months_threshold or len(warning_signals) >= 3:
                level = "alert"
            elif est_months_left <= alert_months_threshold * 2 or len(warning_signals) >= 1:
                level = "watch"
            else:
                level = "ok"

        report = RunwayReport(
            company_id=company_id,
            est_months_left=est_months_left,
            level=level,
            signals=warning_signals,
            assumptions=assumptions,
        )
        reports.append(report)

        await bus.emit(
            Event(
                run_id=run_id,
                ts=_now(),
                agent=_AGENT,
                company_id=company_id,
                kind="runway",
                message=f"Runway Assessment: {level.upper()} ({est_months_left or 'unknown'} months remaining)",
                data={
                    "level": level,
                    "est_months_left": est_months_left,
                    "signals": warning_signals,
                },
            )
        )

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=None,
            kind="done",
            message=f"Runway Watch complete for {len(reports)} companies.",
            data={"reports_count": len(reports)},
        )
    )

    try:
        bus.finish(run_id)
    except Exception:
        pass

    return reports
