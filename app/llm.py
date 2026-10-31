"""LLM provider wrapper — structured JSON calls with retries and fallback.

Supported providers
-------------------
* ``gemini``    — google-generativeai SDK, ``response_mime_type="application/json"``
* ``openai``    — openai async client, ``response_format={"type": "json_object"}``
* ``anthropic`` — anthropic async client, JSON parsed from text response

Retry / rate-limit policy
--------------------------
1. On malformed JSON: retry once with the validation error appended to the
   prompt so the LLM can self-correct.
2. On HTTP 429: exponential back-off, honouring the ``Retry-After`` header
   when present.  After 3 consecutive 429 responses switch to the fallback
   provider defined in ``config.py``.
3. If both primary and fallback providers fail: raise ``LLMFailed``.

Concurrency
-----------
A global ``asyncio.Semaphore(LLM_MAX_CONCURRENCY)`` prevents thundering-herd
against the provider APIs.

Temperature is always 0.0 — callers may pass a different value but it is
ignored in favour of deterministic, reproducible outputs.

Usage::

    from app.llm import call_json, LLMFailed

    result = await call_json(
        prompt="Extract the CEO name from the following ...",
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
        run_id="run-42",
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

try:
    import jsonschema
except ImportError:
    jsonschema = None

from app.config import (
    LLM_API_KEY,
    LLM_FALLBACK_API_KEY,
    LLM_FALLBACK_PROVIDER,
    LLM_MAX_CONCURRENCY,
    LLM_MODEL,
    LLM_PROVIDER,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class LLMFailed(Exception):
    """Raised when all LLM call attempts (primary + fallback) are exhausted."""


# ---------------------------------------------------------------------------
# Default model names per provider
# ---------------------------------------------------------------------------

_DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-1.5-pro-latest",
    "openai": "gpt-4o",
    "anthropic": "claude-3-5-sonnet-20241022",
}

# ---------------------------------------------------------------------------
# Global concurrency limiter
# ---------------------------------------------------------------------------

_semaphore: asyncio.Semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_model(provider: str, override: str | None) -> str:
    """Return the model name to use, preferring the config override."""
    if override:
        return override
    return _DEFAULT_MODELS.get(provider, "")


def _validate(data: dict, schema: dict) -> list[str]:
    """Return a list of validation error messages (empty → valid)."""
    if jsonschema is not None:
        validator = jsonschema.Draft7Validator(schema)
        return [e.message for e in validator.iter_errors(data)]
    # Fallback validation if jsonschema is not installed
    errors: list[str] = []
    required = schema.get("required", [])
    for key in required:
        if key not in data:
            errors.append(f"Missing required field: '{key}'")
    return errors


def _parse_retry_after(header_value: str | None, attempt: int) -> float:
    """Return how many seconds to wait given a Retry-After header value."""
    if header_value is not None:
        try:
            return float(header_value)
        except ValueError:
            pass
    # Exponential backoff: 1s, 2s, 4s, …
    return 2.0 ** attempt


# ---------------------------------------------------------------------------
# Provider-specific call implementations
# ---------------------------------------------------------------------------


async def _call_gemini(
    prompt: str,
    api_key: str,
    model: str,
) -> str:
    """Call the Gemini API and return the raw text response."""
    import google.generativeai as genai  # type: ignore[import]

    genai.configure(api_key=api_key)
    gen_model = genai.GenerativeModel(
        model_name=model,
        generation_config=genai.GenerationConfig(  # type: ignore[attr-defined]
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    # The google-generativeai SDK is synchronous; run in executor.
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: gen_model.generate_content(prompt),
    )
    return response.text


async def _call_openai(
    prompt: str,
    api_key: str,
    model: str,
) -> str:
    """Call the OpenAI API and return the raw text response."""
    from openai import AsyncOpenAI  # type: ignore[import]

    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    content = response.choices[0].message.content
    return content or ""


async def _call_anthropic(
    prompt: str,
    api_key: str,
    model: str,
) -> str:
    """Call the Anthropic API and return the raw text response."""
    import anthropic  # type: ignore[import]

    client = anthropic.AsyncAnthropic(api_key=api_key)
    # Instruct the model to respond with JSON only.
    system_prompt = (
        "You are a JSON-only assistant. "
        "Respond with valid JSON and nothing else — no markdown, no prose."
    )
    message = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )
    # Content blocks → concatenate text blocks.
    parts: list[str] = []
    for block in message.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "".join(parts)


# Map provider name → async callable.
_PROVIDER_CALLABLES = {
    "gemini": _call_gemini,
    "openai": _call_openai,
    "anthropic": _call_anthropic,
}


async def _dispatch(
    prompt: str,
    provider: str,
    api_key: str,
    model: str,
    run_id: str | None,
) -> str:
    """Call the appropriate provider and return raw text.

    Handles 429 rate-limiting with exponential back-off and up to 3 retries.
    Raises ``LLMFailed`` after 3 consecutive 429 responses.
    """
    caller = _PROVIDER_CALLABLES.get(provider)
    if caller is None:
        raise LLMFailed(
            f"Unknown LLM provider '{provider}'. "
            f"Supported: {list(_PROVIDER_CALLABLES)}"
        )

    consecutive_429s = 0
    for attempt in range(3):
        try:
            raw = await caller(prompt, api_key, model)
            return raw
        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc)
            status_code: int | None = None

            # Extract HTTP status code from common SDK exceptions.
            if hasattr(exc, "status_code"):
                status_code = int(exc.status_code)
            elif hasattr(exc, "status"):
                status_code = int(exc.status)
            elif "429" in exc_str:
                status_code = 429

            if status_code == 429:
                consecutive_429s += 1
                retry_after_header: str | None = None
                if hasattr(exc, "response") and exc.response is not None:
                    retry_after_header = exc.response.headers.get("Retry-After")
                wait = _parse_retry_after(retry_after_header, attempt)
                logger.warning(
                    "LLM 429 (attempt %d/3, provider=%s, run_id=%s). "
                    "Sleeping %.1fs.",
                    attempt + 1,
                    provider,
                    run_id,
                    wait,
                )
                await asyncio.sleep(wait)
                if consecutive_429s >= 3:
                    raise LLMFailed(
                        f"Provider '{provider}' returned 429 three times in a row."
                    ) from exc
                continue

            # Non-429 error — re-raise immediately.
            raise LLMFailed(
                f"Provider '{provider}' call failed (attempt {attempt + 1}): {exc_str}"
            ) from exc

    raise LLMFailed(
        f"Provider '{provider}' exhausted all retry attempts."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def call_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    temperature: float = 0.0,  # always 0 for reproducibility; parameter kept for signature compat
    run_id: str | None = None,
) -> dict[str, Any]:
    """Call the configured LLM, validate the JSON output against *schema*.

    Retry policy
    ------------
    1. If the response is not valid JSON or fails schema validation, the
       validation error is appended to *prompt* and the call is retried once.
    2. On 3 consecutive 429s from the primary provider, the call switches to
       the fallback provider (if configured).
    3. If all attempts and both providers fail, ``LLMFailed`` is raised.

    Parameters
    ----------
    prompt:
        The full prompt text.
    schema:
        JSON Schema dict that the response object must satisfy.
    temperature:
        Accepted for API compatibility but always overridden to 0.0.
    run_id:
        Optional run identifier for structured logging.

    Returns
    -------
    dict
        Validated JSON object returned by the LLM.

    Raises
    ------
    LLMFailed
        If all retries are exhausted or an unrecoverable error occurs.
    """
    primary_provider = LLM_PROVIDER
    primary_key = LLM_API_KEY
    primary_model = _resolve_model(primary_provider, LLM_MODEL)

    fallback_provider = LLM_FALLBACK_PROVIDER
    fallback_key = LLM_FALLBACK_API_KEY
    fallback_model = _resolve_model(fallback_provider or "", None) if fallback_provider else ""

    async with _semaphore:
        # ---- Try primary provider (with one JSON-fix retry) ----
        primary_result = await _try_provider_with_json_retry(
            prompt=prompt,
            schema=schema,
            provider=primary_provider,
            api_key=primary_key,
            model=primary_model,
            run_id=run_id,
        )
        if primary_result is not None:
            return primary_result

        # ---- Primary failed; try fallback ----
        if fallback_provider and fallback_key:
            logger.warning(
                "Primary provider '%s' failed. Switching to fallback '%s'. run_id=%s",
                primary_provider,
                fallback_provider,
                run_id,
            )
            fallback_result = await _try_provider_with_json_retry(
                prompt=prompt,
                schema=schema,
                provider=fallback_provider,
                api_key=fallback_key,
                model=fallback_model,
                run_id=run_id,
            )
            if fallback_result is not None:
                return fallback_result

        raise LLMFailed(
            f"All LLM attempts exhausted (primary={primary_provider}, "
            f"fallback={fallback_provider}). run_id={run_id}"
        )


async def _try_provider_with_json_retry(
    *,
    prompt: str,
    schema: dict[str, Any],
    provider: str,
    api_key: str,
    model: str,
    run_id: str | None,
) -> dict[str, Any] | None:
    """Attempt one provider with up to two LLM calls (first + one JSON-fix retry).

    Returns the validated dict on success, or ``None`` if the provider fails
    so the caller can try the fallback.
    """
    current_prompt = prompt
    last_error: str = ""

    for attempt in range(2):  # attempt 0 = first try, attempt 1 = JSON-fix retry
        try:
            raw = await _dispatch(
                prompt=current_prompt,
                provider=provider,
                api_key=api_key,
                model=model,
                run_id=run_id,
            )
        except LLMFailed as exc:
            logger.error(
                "Provider '%s' dispatch failed (attempt %d): %s. run_id=%s",
                provider,
                attempt + 1,
                exc,
                run_id,
            )
            return None

        # ---- Parse JSON ----
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"JSON parse error: {exc}. Raw response: {raw[:500]}"
            logger.warning(
                "JSON parse error on attempt %d (provider=%s, run_id=%s): %s",
                attempt + 1,
                provider,
                run_id,
                last_error,
            )
            if attempt == 0:
                # Retry with self-correction instruction.
                current_prompt = (
                    f"{prompt}\n\n"
                    f"[CORRECTION REQUIRED] Your previous response was not valid JSON.\n"
                    f"Error: {last_error}\n"
                    f"Please respond with valid JSON only, matching this schema:\n"
                    f"{json.dumps(schema, indent=2)}"
                )
                continue
            return None

        # ---- Validate against schema ----
        errors = _validate(data, schema)
        if errors:
            last_error = "; ".join(errors)
            logger.warning(
                "Schema validation failed on attempt %d (provider=%s, run_id=%s): %s",
                attempt + 1,
                provider,
                run_id,
                last_error,
            )
            if attempt == 0:
                current_prompt = (
                    f"{prompt}\n\n"
                    f"[CORRECTION REQUIRED] Your previous response did not match "
                    f"the required JSON schema.\n"
                    f"Validation errors: {last_error}\n"
                    f"Required schema:\n"
                    f"{json.dumps(schema, indent=2)}\n"
                    f"Please return a corrected, valid JSON object."
                )
                continue
            return None

        # ---- Success ----
        logger.debug(
            "LLM call succeeded (provider=%s, model=%s, attempt=%d, run_id=%s).",
            provider,
            model,
            attempt + 1,
            run_id,
        )
        return data  # type: ignore[return-value]

    return None
