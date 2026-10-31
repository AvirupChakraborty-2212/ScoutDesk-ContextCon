"""Crustdata API client — the ONLY file in ScoutDesk that talks to Crustdata.

All network I/O goes through this module. Callers must never import httpx directly
for Crustdata calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Any

import httpx

from app.config import (
    CACHE_DIR,
    CRUSTDATA_API_KEY,
    OFFLINE,
    RUN_BUDGET_CREDITS,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NoData(Exception):
    """Raised when Crustdata returns no matches or a cache miss occurs in OFFLINE mode."""


class BudgetExceeded(Exception):
    """Raised when a call would push a run over its credit budget."""


# ---------------------------------------------------------------------------
# Credit counter
# ---------------------------------------------------------------------------


class CreditCounter:
    """Tracks credits consumed within a single run and enforces a hard budget."""

    def __init__(self, budget: int = RUN_BUDGET_CREDITS) -> None:
        self._budget: int = budget
        self._used: int = 0

    @property
    def used(self) -> int:
        return self._used

    @property
    def budget(self) -> int:
        return self._budget

    @property
    def remaining(self) -> int:
        return self._budget - self._used

    def charge(self, n: int) -> None:
        """Deduct *n* credits; raise BudgetExceeded if the budget would be exceeded."""
        if self._used + n > self._budget:
            raise BudgetExceeded(
                f"Charging {n} credit(s) would exceed budget "
                f"(used={self._used}, budget={self._budget})"
            )
        self._used += n
        logger.debug(
            "CreditCounter: charged %d, used=%d/%d", n, self._used, self._budget
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"CreditCounter(used={self._used}, budget={self._budget})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cache_path(endpoint: str, body: dict[str, Any]) -> Path:
    """Return the filesystem path for a cached response."""
    key = hashlib.sha256(
        (endpoint + json.dumps(body, sort_keys=True)).encode()
    ).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _read_cache(endpoint: str, body: dict[str, Any]) -> Any | None:
    """Return the cached JSON object, or None if not present."""
    path = _cache_path(endpoint, body)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cache read error for %s: %s", path, exc)
    return None


def _write_cache(endpoint: str, body: dict[str, Any], data: Any) -> None:
    """Persist a successful API response to disk."""
    path = _cache_path(endpoint, body)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Cache write error for %s: %s", path, exc)


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {CRUSTDATA_API_KEY}",
            "x-api-version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=httpx.Timeout(30.0),
    )


async def _post(
    endpoint: str,
    body: dict[str, Any],
    run_credits: CreditCounter,
    credit_cost: int,
) -> Any:
    """POST to Crustdata with disk-cache, OFFLINE guard, credit deduction, and retry.

    Retry policy: up to 2 retries on 429 / 5xx / network timeouts, with
    exponential back-off plus random jitter  (base × 2^attempt, base ∈ [0.5, 1.5)).
    Credits are charged before the first network attempt; they are not refunded on
    failure (the budget prevents runaway spending, not individual call failures).
    """
    # --- Check cache first (always, to avoid wasted credits) ---
    cached = _read_cache(endpoint, body)
    if cached is not None:
        logger.debug("Cache hit for endpoint=%s", endpoint)
        run_credits.charge(credit_cost)
        return cached

    # --- OFFLINE mode: only allowed to use cache ---
    if OFFLINE:
        raise NoData(
            f"OFFLINE mode — no cached data for endpoint={endpoint} body={body}"
        )

    # --- Charge credits before the network call ---
    run_credits.charge(credit_cost)

    # --- Network call with retries ---
    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
    max_retries = 2
    last_exc: Exception | None = None

    async with _build_client() as client:
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(endpoint, content=json.dumps(body))

                if response.status_code in RETRYABLE_STATUS:
                    wait = (2 ** attempt) * (0.5 + random.random())
                    logger.warning(
                        "Crustdata POST %s → HTTP %d; retry in %.2fs (attempt %d/%d)",
                        endpoint,
                        response.status_code,
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(wait)
                        continue
                    raise last_exc  # type: ignore[misc]

                response.raise_for_status()
                data = response.json()
                _write_cache(endpoint, body, data)
                return data

            except httpx.TimeoutException as exc:
                wait = (2 ** attempt) * (0.5 + random.random())
                logger.warning(
                    "Crustdata timeout for %s; retry in %.2fs (attempt %d/%d)",
                    endpoint,
                    wait,
                    attempt + 1,
                    max_retries,
                )
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(wait)
                    continue
                raise NoData(
                    f"Crustdata timed out after {max_retries + 1} attempt(s) "
                    f"for endpoint={endpoint}"
                ) from exc

    # Should be unreachable, but satisfies the type-checker
    raise NoData(f"Crustdata call failed for endpoint={endpoint}") from last_exc


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------


async def identify_company(
    name_or_domain: str,
    run_credits: CreditCounter,
) -> dict[str, Any]:
    """Identify a company by name or domain.

    POST /screener/company/identify  |  Credit cost: 1

    Returns the first match dict from ``matches``.
    Raises ``NoData`` if no matches are returned.
    """
    endpoint = "/screener/company/identify"
    body: dict[str, Any] = {"name_or_domain": name_or_domain}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    matches: list[dict[str, Any]] = data.get("matches", [])
    if not matches:
        raise NoData(f"No company matches for: {name_or_domain!r}")
    return matches[0]


async def enrich_company(
    identifier: dict[str, Any],
    fields: list[str],
    run_credits: CreditCounter,
) -> dict[str, Any]:
    """Enrich a company with structured fields.

    POST /screener/company/enrich  |  Credit cost: 2

    ``identifier`` must be exactly one of:
        ``{"company_id": ...}``, ``{"domain": ...}``, or ``{"linkedin_url": ...}``

    ``fields`` example::
        ["basic_info", "funding", "headcount", "people", "hiring", "competitors", "news"]

    Raises ``NoData`` if no enrichment data is available.
    """
    endpoint = "/screener/company/enrich"
    body: dict[str, Any] = {**identifier, "fields": fields}
    data = await _post(endpoint, body, run_credits, credit_cost=2)

    # Crustdata may return a bare object or wrap results in a ``matches`` list
    if isinstance(data, dict) and "matches" in data:
        matches: list[dict[str, Any]] = data["matches"]
        if not matches:
            raise NoData(f"No enrichment data for identifier={identifier}")
        return matches[0]
    return data


async def search_companies(
    filters: list[dict[str, Any]],
    limit: int,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Search companies using Crustdata filter objects.

    POST /screener/company/search  |  Credit cost: 1

    ``filters`` example::
        [{"filter_type": "headcount_range", "value": {"min": 50, "max": 500}}]

    Raises ``NoData`` if no companies match the filters.
    """
    endpoint = "/screener/company/search"
    body: dict[str, Any] = {"filters": filters, "limit": limit}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    matches: list[dict[str, Any]] = data.get("matches", [])
    if not matches:
        raise NoData(f"No companies matched filters={filters}")
    return matches


async def enrich_person(
    identifier: dict[str, Any],
    fields: list[str],
    run_credits: CreditCounter,
) -> dict[str, Any]:
    """Enrich a person (founder / executive) with structured fields.

    POST /screener/people/enrich  |  Credit cost: 1

    ``identifier`` should uniquely identify the person, e.g.
        ``{"linkedin_url": "..."}`` or ``{"person_id": "..."}``.

    Raises ``NoData`` if no person data is available.
    """
    endpoint = "/screener/people/enrich"
    body: dict[str, Any] = {**identifier, "fields": fields}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    if isinstance(data, dict) and "matches" in data:
        matches: list[dict[str, Any]] = data["matches"]
        if not matches:
            raise NoData(f"No person data for identifier={identifier}")
        return matches[0]
    return data


async def search_people(
    filters: list[dict[str, Any]],
    limit: int,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Search people using Crustdata filter objects.

    POST /screener/people/search  |  Credit cost: 1

    Raises ``NoData`` if no people match the filters.
    """
    endpoint = "/screener/people/search"
    body: dict[str, Any] = {"filters": filters, "limit": limit}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    matches: list[dict[str, Any]] = data.get("matches", [])
    if not matches:
        raise NoData(f"No people matched filters={filters}")
    return matches


async def search_jobs(
    filters: list[dict[str, Any]],
    limit: int,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Search open job postings using Crustdata filter objects.

    POST /screener/jobs/search  |  Credit cost: 1

    Raises ``NoData`` if no postings match the filters.
    """
    endpoint = "/screener/jobs/search"
    body: dict[str, Any] = {"filters": filters, "limit": limit}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    matches: list[dict[str, Any]] = data.get("matches", [])
    if not matches:
        raise NoData(f"No jobs matched filters={filters}")
    return matches


async def web_search(
    query: str,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Perform a web search via Crustdata.

    POST /screener/web/search  |  Credit cost: 1

    Returns a list of result dicts (title, url, snippet, …).
    Raises ``NoData`` if no results are returned.
    """
    endpoint = "/screener/web/search"
    body: dict[str, Any] = {"query": query}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    # Handle both {"results": [...]} and a bare list
    results: list[dict[str, Any]] = (
        data.get("results", data) if isinstance(data, dict) else data
    )
    if not results:
        raise NoData(f"No web search results for query={query!r}")
    return results


async def web_fetch(
    url: str,
    run_credits: CreditCounter,
) -> str:
    """Fetch and return the text content of a URL via Crustdata.

    POST /screener/web/fetch  |  Credit cost: 1

    Returns the page content as a plain string.
    Raises ``NoData`` if the fetched content is empty.
    """
    endpoint = "/screener/web/fetch"
    body: dict[str, Any] = {"url": url}
    data = await _post(endpoint, body, run_credits, credit_cost=1)

    # Crustdata may return {"content": "..."}, {"text": "..."}, or a bare string
    if isinstance(data, str):
        if not data.strip():
            raise NoData(f"Empty content fetched from url={url!r}")
        return data

    content: str = data.get("content") or data.get("text") or ""
    if not content.strip():
        raise NoData(f"Empty content fetched from url={url!r}")
    return content
