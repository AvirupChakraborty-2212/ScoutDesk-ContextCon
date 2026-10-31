"""Signals Analyst agent — analyzes hiring momentum, hiring mix, and public signals.

Extracts dated job postings, classifies roles in pure code, and gathers recent news.
No LLM is used for classification or math.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.crust import (
    CreditCounter,
    NoData,
    search_jobs,
    web_search,
)
from app.events import EventBus
from app.ledger import (
    KNOWN_SINCE_DERIVED,
    KNOWN_SINCE_NEWS,
    KNOWN_SINCE_POSTING,
    KNOWN_SINCE_REALTIME,
    FactLedger,
)
from app.models import AgentResult, AgentTask, Event, Fact

logger = logging.getLogger(__name__)

_AGENT = "signals"


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


# Role classification keywords
_ML_KEYWORDS = ["machine learning", "ml", "ai", "data scientist", "nlp", "llm", "deep learning", "researcher", "computer vision"]
_ENG_KEYWORDS = ["engineer", "developer", "software", "backend", "frontend", "fullstack", "sre", "devops", "architect", "qa", "platform"]
_GTM_KEYWORDS = ["sales", "marketing", "growth", "account", "customer success", "business development", "partnerships", "bdr", "sdr", "gtm"]
_OPS_KEYWORDS = ["operations", "finance", "hr", "recruiting", "legal", "admin", "people", "talent", "compliance"]


def _classify_role(title: str) -> str:
    t = title.lower()
    if any(kw in t for kw in _ML_KEYWORDS):
        return "ml"
    if any(kw in t for kw in _ENG_KEYWORDS):
        return "eng"
    if any(kw in t for kw in _GTM_KEYWORDS):
        return "gtm"
    if any(kw in t for kw in _OPS_KEYWORDS):
        return "ops"
    return "other"


async def run(
    task: AgentTask,
    company_domain: str,
    company_name: str,
    ledger: FactLedger,
    bus: EventBus,
    credits: CreditCounter,
) -> AgentResult:
    """Run the Signals Analyst.

    Investigates hiring activity (mix, velocity) and recent news.
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
        message=f"Signals Analyst starting for {company_name or company_domain}",
    )

    fact_ids: list[str] = []
    findings: list[str] = []
    unknowns: list[str] = []

    today_dt = _today()
    cutoff_90d = today_dt - timedelta(days=90)

    # 1. Search jobs
    jobs: list[dict[str, Any]] = []
    try:
        filter_val = company_domain or company_name
        jobs = await search_jobs(
            filters=[{"filter_type": "company_domain", "value": filter_val}],
            limit=50,
            run_credits=credits,
        )
    except (NoData, Exception) as exc:
        logger.info("Search jobs by domain failed for %s, trying company_name: %s", filter_val, exc)
        if company_name and company_name != filter_val:
            try:
                jobs = await search_jobs(
                    filters=[{"filter_type": "company_name", "value": company_name}],
                    limit=50,
                    run_credits=credits,
                )
            except Exception as name_exc:
                logger.info("Search jobs by name also failed: %s", name_exc)

    ml_count = 0
    eng_count = 0
    gtm_count = 0
    ops_count = 0
    other_count = 0
    last_90d_count = 0
    job_fact_ids: list[str] = []

    for idx, job in enumerate(jobs):
        title = str(job.get("title") or job.get("job_title") or "Unknown Role")
        posted_str = job.get("posted_at") or job.get("posted_date") or job.get("date_posted")
        posted_date = _parse_date(posted_str)

        category = _classify_role(title)
        if category == "ml":
            ml_count += 1
        elif category == "eng":
            eng_count += 1
        elif category == "gtm":
            gtm_count += 1
        elif category == "ops":
            ops_count += 1
        else:
            other_count += 1

        is_recent = False
        if posted_date and posted_date >= cutoff_90d:
            last_90d_count += 1
            is_recent = True
        elif not posted_date:
            # If no posted date, assume recent if returned by active jobs API
            last_90d_count += 1

        # Store individual job fact (for up to 10 jobs)
        if idx < 10:
            j_fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key=f"openings.role.{idx}",
                value=f"{title} ({category})",
                unit=None,
                endpoint="screener/jobs/search",
                field_path=f"jobs[{idx}].title",
                known_since=posted_date or today_dt,
                raw_excerpt=_excerpt(job),
            )
            fact_ids.append(j_fid)
            job_fact_ids.append(j_fid)

    total_jobs = len(jobs)

    # Store openings metrics
    fid_90d = _make_fact(
        ledger=ledger,
        company_id=company_id,
        subject="company",
        key="openings.last_90d",
        value=float(last_90d_count),
        unit="openings",
        endpoint="derived" if total_jobs > 0 else "screener/jobs/search",
        field_path="count(jobs.posted_at >= today-90d)",
        known_since=today_dt,
        raw_excerpt=_excerpt({"total_jobs": total_jobs, "last_90d": last_90d_count}),
        derived_from=job_fact_ids,
    )
    fact_ids.append(fid_90d)

    if total_jobs > 0:
        ml_share = round(ml_count / total_jobs, 4)
        gtm_share = round(gtm_count / total_jobs, 4)
        eng_share = round(eng_count / total_jobs, 4)

        fid_ml = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject="company",
            key="openings.share_ml",
            value=ml_share,
            unit="ratio",
            endpoint="derived",
            field_path="count(ml_jobs)/total_jobs",
            known_since=today_dt,
            raw_excerpt=_excerpt({"ml_count": ml_count, "total": total_jobs, "share": ml_share}),
            derived_from=job_fact_ids,
        )
        fact_ids.append(fid_ml)

        fid_gtm = _make_fact(
            ledger=ledger,
            company_id=company_id,
            subject="company",
            key="openings.share_gtm",
            value=gtm_share,
            unit="ratio",
            endpoint="derived",
            field_path="count(gtm_jobs)/total_jobs",
            known_since=today_dt,
            raw_excerpt=_excerpt({"gtm_count": gtm_count, "total": total_jobs, "share": gtm_share}),
            derived_from=job_fact_ids,
        )
        fact_ids.append(fid_gtm)

        findings.append(
            f"Hiring activity: {last_90d_count} openings in last 90 days "
            f"({ml_share:.0%} ML, {eng_share:.0%} Eng, {gtm_share:.0%} GTM)"
        )
    else:
        findings.append("No active job postings found")

    # 2. Web search for recent news/announcements
    has_recent_news = False
    news_fact_ids: list[str] = []
    try:
        search_query = f"{company_name or company_domain} startup funding OR launch OR announcement"
        news_results = await web_search(search_query, run_credits=credits)
        for n_idx, item in enumerate(news_results[:3]):
            n_title = item.get("title") or item.get("snippet") or "News Item"
            n_date_str = item.get("date") or item.get("published_date")
            n_date = _parse_date(n_date_str)
            if n_date and n_date >= cutoff_90d:
                has_recent_news = True

            n_fid = _make_fact(
                ledger=ledger,
                company_id=company_id,
                subject="company",
                key=f"signals.news.{n_idx}.title",
                value=str(n_title)[:120],
                unit=None,
                endpoint="screener/web/search",
                field_path=f"news[{n_idx}].title",
                known_since=n_date or today_dt,
                raw_excerpt=_excerpt(item),
            )
            fact_ids.append(n_fid)
            news_fact_ids.append(n_fid)
    except (NoData, Exception) as exc:
        logger.info("Web search for news returned no results for %s: %s", company_name, exc)

    fid_news = _make_fact(
        ledger=ledger,
        company_id=company_id,
        subject="company",
        key="signals.has_recent_news",
        value=has_recent_news,
        unit=None,
        endpoint="derived",
        field_path="any(news.date >= today-90d)",
        known_since=today_dt,
        raw_excerpt=_excerpt({"has_recent_news": has_recent_news}),
        derived_from=news_fact_ids,
    )
    fact_ids.append(fid_news)

    if has_recent_news:
        findings.append("Recent press/launch activity detected in last 90 days")

    await _emit_event(
        bus,
        run_id=run_id,
        agent=_AGENT,
        company_id=company_id,
        kind="done",
        message=f"Signals Analyst complete: {len(jobs)} jobs, {len(news_fact_ids)} news items recorded.",
        data={"fact_ids_count": len(fact_ids), "last_90d_openings": last_90d_count},
    )

    return AgentResult(
        agent=_AGENT,
        company_id=company_id,
        fact_ids=fact_ids,
        findings=findings,
        unknowns=unknowns,
        credits_used=credits.used - start_credits,
        status="ok",
    )
