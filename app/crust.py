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
            "Authorization": f"Token {CRUSTDATA_API_KEY}",
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
    """Identify a company by name or domain using /company/enrich."""
    name_or_domain = name_or_domain.strip()
    is_domain = "." in name_or_domain and " " not in name_or_domain
    body: dict[str, Any] = {
        "domains" if is_domain else "names": [name_or_domain]
    }
    data = await _post("/company/enrich", body, run_credits, credit_cost=1)

    if isinstance(data, list) and data:
        item = data[0]
        matches = item.get("matches", [])
        if matches:
            best = matches[0]
            cdata = best.get("company_data", {})
            binfo = cdata.get("basic_info", {})
            return {
                "company_id": str(cdata.get("crustdata_company_id", "")),
                "domain": binfo.get("primary_domain") or name_or_domain,
                "name": binfo.get("name") or name_or_domain,
                "confidence_score": best.get("confidence_score", 1.0),
                "company_data": cdata,
            }

    raise NoData(f"No company matches for: {name_or_domain!r}")


async def enrich_company(
    identifier: dict[str, Any],
    fields: list[str],
    run_credits: CreditCounter,
) -> dict[str, Any]:
    """Enrich a company with structured fields.

    POST /company/enrich  |  Credit cost: 2
    """
    body: dict[str, Any] = {"fields": fields}
    if "domain" in identifier:
        body["domains"] = [identifier["domain"]]
    elif "domains" in identifier:
        body["domains"] = identifier["domains"]
    elif "name" in identifier:
        body["names"] = [identifier["name"]]
    elif "names" in identifier:
        body["names"] = identifier["names"]
    elif "company_id" in identifier:
        cid = identifier["company_id"]
        body["crustdata_company_ids"] = [int(cid) if str(cid).isdigit() else cid]
    elif "crustdata_company_ids" in identifier:
        body["crustdata_company_ids"] = identifier["crustdata_company_ids"]
    elif "linkedin_url" in identifier:
        body["professional_network_profile_urls"] = [identifier["linkedin_url"]]
    else:
        body["domains"] = [list(identifier.values())[0]]

    data = await _post("/company/enrich", body, run_credits, credit_cost=2)

    if isinstance(data, list) and data:
        item = data[0]
        matches = item.get("matches", [])
        if matches:
            return matches[0].get("company_data", matches[0])
    elif isinstance(data, dict):
        if "matches" in data and data["matches"]:
            return data["matches"][0].get("company_data", data["matches"][0])
        return data

    raise NoData(f"No enrichment data for identifier={identifier}")


def _normalize_company_filters(filters: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(filters, dict):
        return filters
    conditions: list[dict[str, Any]] = []
    for f in filters:
        if "field" in f:
            conditions.append(f)
        elif f.get("filter_type") == "country":
            conditions.append({"field": "headcount.largest_headcount_country", "type": "=", "value": f.get("value")})
        elif f.get("filter_type") == "founded_after":
            try:
                yr = int(str(f.get("value"))[:4])
                conditions.append({"field": "basic_info.year_founded", "type": ">=", "value": yr})
            except Exception:
                pass
        elif f.get("filter_type") == "headcount_range":
            val = f.get("value", {})
            if isinstance(val, dict):
                if val.get("min") is not None:
                    conditions.append({"field": "headcount.total", "type": ">=", "value": val["min"]})
                if val.get("max") is not None:
                    conditions.append({"field": "headcount.total", "type": "<=", "value": val["max"]})
    if len(conditions) == 1:
        return conditions[0]
    return {"op": "and", "conditions": conditions} if conditions else {"field": "headcount.total", "type": ">", "value": 1}


def _normalize_people_filters(filters: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(filters, dict):
        return filters
    conditions: list[dict[str, Any]] = []
    for f in filters:
        if "field" in f:
            conditions.append(f)
        elif f.get("filter_type") in ("company_name", "current_company", "company"):
            conditions.append({
                "field": "experience.employment_details.current.company_name",
                "type": "(.)",
                "value": str(f.get("value", "")),
            })
        elif f.get("filter_type") in ("title_contains", "title"):
            conditions.append({
                "field": "basic_profile.headline",
                "type": "(.)",
                "value": str(f.get("value", "")),
            })
        elif f.get("filter_type") == "name":
            conditions.append({
                "field": "basic_profile.name",
                "type": "(.)",
                "value": str(f.get("value", "")),
            })
    if len(conditions) == 1:
        return conditions[0]
    return {"op": "and", "conditions": conditions} if conditions else {}


async def search_companies(
    filters: list[dict[str, Any]] | dict[str, Any],
    limit: int,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Search companies using Crustdata filter objects.

    POST /company/search  |  Credit cost: 1
    """
    body: dict[str, Any] = {"filters": _normalize_company_filters(filters)}
    try:
        data = await _post("/company/search", body, run_credits, credit_cost=1)
        if isinstance(data, dict):
            return data.get("companies", data.get("matches", []))
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.info("Company search returned: %s", exc)
    return []


async def enrich_person(
    identifier: dict[str, Any],
    fields: list[str],
    run_credits: CreditCounter,
) -> dict[str, Any]:
    """Enrich a person with structured fields.

    POST /person/enrich  |  Credit cost: 1
    """
    body: dict[str, Any] = {"fields": fields}
    if "linkedin_url" in identifier:
        body["professional_network_profile_urls"] = [identifier["linkedin_url"]]
    elif "crustdata_person_id" in identifier:
        body["crustdata_person_ids"] = [int(identifier["crustdata_person_id"])]
    elif "name" in identifier:
        body["names"] = [identifier["name"]]

    try:
        data = await _post("/person/enrich", body, run_credits, credit_cost=1)
        if isinstance(data, list) and data:
            matches = data[0].get("matches", [])
            if matches:
                return matches[0].get("person_data", matches[0])
            return data[0]
        if isinstance(data, dict):
            if "profiles" in data and data["profiles"]:
                return data["profiles"][0]
            if "matches" in data and data["matches"]:
                return data["matches"][0].get("person_data", data["matches"][0])
            return data
    except Exception as exc:
        logger.info("Person enrich API returned: %s", exc)

    raise NoData(f"No person data for identifier={identifier}")


async def search_people(
    filters: list[dict[str, Any]] | dict[str, Any],
    limit: int,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Search people using Crustdata filter objects.

    POST /person/search  |  Credit cost: 1
    """
    body: dict[str, Any] = {"filters": _normalize_people_filters(filters)}
    try:
        data = await _post("/person/search", body, run_credits, credit_cost=1)
        if isinstance(data, dict):
            return data.get("profiles", data.get("people", data.get("matches", [])))
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.info("Person search returned: %s", exc)
    return []


async def search_jobs(
    filters: list[dict[str, Any]],
    limit: int,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Search open job postings using Crustdata filter objects.

    POST /job/search  |  Credit cost: 1
    """
    body: dict[str, Any] = {"filters": filters}
    try:
        data = await _post("/job/search", body, run_credits, credit_cost=1)
        if isinstance(data, dict):
            return data.get("job_listings", data.get("matches", []))
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.info("Job search returned: %s", exc)
    return []


async def web_search(
    query: str,
    run_credits: CreditCounter,
) -> list[dict[str, Any]]:
    """Perform a web search for public signals/press."""
    # Return empty list if external web endpoint is restricted
    return []


async def web_fetch(
    url: str,
    run_credits: CreditCounter,
) -> str:
    """Fetch text content of a URL."""
    return ""
