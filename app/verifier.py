"""Verifier — block uncited numbers, unknown fact ids, value mismatches."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.ledger import LedgerView
from app.models import Fact, Memo, MemoSentence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex to find {<fact_id>} placeholders inside a template (supports F12, F1, UUIDs, etc.)
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_-]+)\}")

# Regex to find standalone number-like tokens NOT preceded by { (placeholder guard handled separately).
# Matches integers, decimals, commas-as-thousands, and optional unit suffixes.
_NUMBER_TOKEN_RE = re.compile(
    r"\b"
    r"(\d[\d,]*(?:\.\d+)?)"        # digits with optional commas/decimal
    r"(?:\s*(?:K|M|B|lakh|crore|%))?"  # optional suffix
    r"\b",
    re.IGNORECASE,
)

# Regex to match a placeholder like {F12} so we can exclude matches within them
_PLACEHOLDER_SPAN_RE = re.compile(r"\{[A-Za-z0-9_-]+\}")

# Suffixes and their multipliers for parse_number
_SUFFIX_MULTIPLIERS: dict[str, float] = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "lakh": 100_000,
    "crore": 10_000_000,
    "%": 0.01,  # convert to fraction for comparison (100% → 1.0)
}

# Relative and absolute tolerances for value comparison
_REL_TOL = 0.005    # 0.5 % relative
_ABS_TOL_PCT = 0.5  # ±0.5 percentage points (when unit is %)

# Direction words that imply positive/negative delta
_POSITIVE_WORDS = frozenset(["grew", "increased", "up", "rose", "surged", "jumped"])
_NEGATIVE_WORDS = frozenset({"fell", "declined", "down", "dropped", "decreased", "slipped"})


# ---------------------------------------------------------------------------
# extract_number
# ---------------------------------------------------------------------------

def extract_number(text: str) -> float | None:
    """
    Parse a human-readable number with optional K/M/B/lakh/crore/% suffixes.

    Returns the numeric value as a float.
    - "1.2M" → 1_200_000.0
    - "50%" → 0.50 (converted to fraction)
    - "2,500" → 2500.0
    - "3 lakh" → 300_000.0

    Returns None if no parseable number is found.
    """
    if not text:
        return None

    text = text.strip()

    # Pattern: optional sign, digits, optional decimal, optional suffix
    pattern = re.compile(
        r"^([+-]?)"
        r"([\d,]+(?:\.\d+)?)"
        r"(?:\s*"
        r"(K|M|B|lakh|crore|%)"
        r")?$",
        re.IGNORECASE,
    )

    m = pattern.match(text)
    if not m:
        # Try extracting the first number-like sequence from free text
        m2 = re.search(
            r"([+-]?[\d,]+(?:\.\d+)?)"
            r"(?:\s*(K|M|B|lakh|crore|%))?",
            text,
            re.IGNORECASE,
        )
        if not m2:
            return None
        sign_str = ""
        num_str = m2.group(1)
        suffix = m2.group(2) or ""
    else:
        sign_str = m.group(1) or ""
        num_str = m.group(2)
        suffix = m.group(3) or ""

    # Remove thousands commas
    num_str = num_str.replace(",", "")
    try:
        value = float(num_str)
    except ValueError:
        return None

    if sign_str == "-":
        value = -value

    multiplier = _SUFFIX_MULTIPLIERS.get(suffix.lower(), 1.0)
    return value * multiplier


# ---------------------------------------------------------------------------
# Value comparison helpers
# ---------------------------------------------------------------------------

def _values_close(rendered_val: float, fact_val: float, unit: str | None) -> bool:
    """Return True if rendered_val is within tolerance of fact_val."""
    if unit == "%":
        # absolute tolerance in percentage points
        rendered_pct = rendered_val * 100 if abs(rendered_val) <= 1.0 else rendered_val
        fact_pct = fact_val * 100 if abs(fact_val) <= 1.0 else fact_val
        return abs(rendered_pct - fact_pct) <= _ABS_TOL_PCT
    # relative tolerance
    if fact_val == 0.0:
        return abs(rendered_val) < 1.0  # treat near-zero as equal
    return abs(rendered_val - fact_val) / abs(fact_val) <= _REL_TOL


def _fact_numeric_value(fact: Fact) -> float | None:
    """Coerce a Fact's value to float."""
    v = fact.value
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_fact_delta(fact: Fact) -> float | None:
    """
    Return the numeric delta implied by a fact for direction-word checking.

    For growth/yoy/mom keys the value IS the delta.
    """
    growth_keys = {
        "company.headcount_yoy",
        "company.headcount_mom",
        "company.revenue_yoy",
        "company.revenue_mom",
    }
    if fact.key in growth_keys:
        return _fact_numeric_value(fact)
    return None


# ---------------------------------------------------------------------------
# Sentence-level checks
# ---------------------------------------------------------------------------

def _extract_placeholders(template: str) -> list[str]:
    """Return list of fact IDs referenced in a template string."""
    return _PLACEHOLDER_RE.findall(template)


def _render_template(template: str, ledger: LedgerView) -> str:
    """Replace {F<id>} placeholders with their fact values."""
    def replacer(m: re.Match) -> str:
        fid = m.group(1)
        fact = ledger.get(fid)
        if fact is None:
            return f"[{fid}: MISSING]"
        val = fact.value
        if val is None:
            return "[N/A]"
        if fact.unit == "%":
            try:
                pct = float(val) * 100 if abs(float(val)) <= 1.0 else float(val)
                return f"{pct:.1f}%"
            except (TypeError, ValueError):
                return str(val)
        return str(val)
    return _PLACEHOLDER_RE.sub(replacer, template)


def _check_citation_exists(
    fact_id: str,
    company_id: str,
    ledger: LedgerView,
) -> str | None:
    """
    Rule 1: Citation must exist in the ledger.

    Returns a violation string or None.
    """
    fact = ledger.get(fact_id)
    if fact is None:
        return f"Unknown fact {fact_id} not found in ledger"

    # Must belong to the same company OR its founders/competitors
    if fact.company_id != company_id:
        allowed_subjects = {"company", "founder", "competitor"}
        subject_root = fact.subject.split(":")[0] if ":" in fact.subject else fact.subject
        if subject_root not in allowed_subjects:
            return (
                f"Fact {fact_id} belongs to company '{fact.company_id}', "
                f"expected '{company_id}'"
            )

    return None


def _check_value_match(
    rendered_text: str,
    cited_facts: list[Fact],
) -> str | None:
    """
    Rule 2: Numbers in the rendered sentence must match their fact values.

    We try to parse the first number-like token in the rendered sentence and
    compare it against each cited fact's numeric value.

    Returns a violation string or None.
    """
    if not cited_facts:
        return None

    # Find the first number in the rendered text
    m = re.search(
        r"([+-]?[\d,]+(?:\.\d+)?)\s*(K|M|B|lakh|crore|%)?",
        rendered_text,
        re.IGNORECASE,
    )
    if not m:
        return None  # no number to verify

    raw_number = m.group(1) + (m.group(2) or "")
    parsed = extract_number(raw_number)
    if parsed is None:
        return None

    # Check against any cited fact
    for fact in cited_facts:
        fval = _fact_numeric_value(fact)
        if fval is None:
            continue
        if _values_close(parsed, fval, fact.unit):
            return None  # at least one fact matches — pass

    # None matched
    fact_vals = [str(f.value) for f in cited_facts if f.value is not None]
    return (
        f"Value '{raw_number}' in memo does not match cited fact(s) "
        f"{[f.id for f in cited_facts]}: values are {fact_vals}"
    )


def _check_no_bare_numbers(template: str, cited_facts: list[Fact]) -> str | None:
    """
    Rule 3: Any numeric token in the template that is NOT part of a {F<id>}
    placeholder value is a violation.

    Strategy: strip out all {F<id>} blocks from the template, then scan for
    remaining numeric tokens.
    """
    # Remove all {F<id>} placeholders
    cleaned = _PLACEHOLDER_RE.sub(" PLACEHOLDER ", template)

    # Look for number-like tokens in what's left
    # Exclude years written out plainly (handled as contextual narrative)
    number_tokens = re.findall(
        r"\b(\d[\d,]*(?:\.\d+)?)(?:\s*(?:K|M|B|lakh|crore|%))?\b",
        cleaned,
        re.IGNORECASE,
    )
    # Filter: allow ordinals, small integers that are obviously counts (0-10), year refs
    violations: list[str] = []
    for tok in number_tokens:
        tok_clean = tok.replace(",", "")
        try:
            val = float(tok_clean)
        except ValueError:
            continue
        # Heuristic: years (2020-2030) used as narrative are allowed
        if 2000 <= val <= 2030:
            continue
        # Small ordinal-like integers 0-10 in text context — flag anything ≥ 11
        if val >= 11:
            violations.append(tok)

    if violations:
        return (
            f"Bare numbers {violations} present in template outside {{}}-placeholders"
        )
    return None


def _check_direction_words(template: str, cited_facts: list[Fact]) -> str | None:
    """
    Rule 4: Direction words must be consistent with cited fact deltas.

    Returns a violation string or None.
    """
    template_lower = template.lower()

    has_positive = any(w in template_lower for w in _POSITIVE_WORDS)
    has_negative = any(w in template_lower for w in _NEGATIVE_WORDS)

    if not has_positive and not has_negative:
        return None  # no direction claim to verify

    deltas: list[float] = []
    for f in cited_facts:
        d = _get_fact_delta(f)
        if d is not None:
            deltas.append(d)

    if not deltas:
        return None  # no delta facts to validate against — allow the sentence

    dominant_delta = deltas[0]  # use first cited delta fact

    if has_positive and dominant_delta <= 0:
        word = next(w for w in _POSITIVE_WORDS if w in template_lower)
        return (
            f"Direction word '{word}' implies positive delta but cited fact has "
            f"delta={dominant_delta:+.3f}"
        )

    if has_negative and dominant_delta >= 0:
        word = next(w for w in _NEGATIVE_WORDS if w in template_lower)
        return (
            f"Direction word '{word}' implies negative delta but cited fact has "
            f"delta={dominant_delta:+.3f}"
        )

    return None


def _check_time_leakage(
    cited_facts: list[Fact],
    as_of: date,
) -> str | None:
    """
    Rule 5 (as_of mode only): no cited fact may have known_since > as_of.

    Returns a violation string or None.
    """
    for f in cited_facts:
        if f.known_since is not None and f.known_since > as_of:
            return (
                f"Fact {f.id} has known_since={f.known_since} > as_of={as_of} "
                f"(time firewall violation)"
            )
    return None


# ---------------------------------------------------------------------------
# Sentence verifier
# ---------------------------------------------------------------------------

def _verify_sentence(
    sentence: MemoSentence,
    company_id: str,
    ledger: LedgerView,
    as_of: date | None,
) -> tuple[bool, list[str], str]:
    """
    Verify a single MemoSentence.

    Returns (passed: bool, violations: list[str], rendered: str).
    """
    violations: list[str] = []
    template = sentence.template

    # Collect referenced fact IDs
    ref_ids = _extract_placeholders(template)

    # Rule 1 — citation existence
    cited_facts: list[Fact] = []
    for fid in ref_ids:
        v = _check_citation_exists(fid, company_id, ledger)
        if v:
            violations.append(v)
        else:
            fact = ledger.get(fid)
            if fact is not None:
                cited_facts.append(fact)

    # Rule 5 — time leakage (check before rendering to fail fast)
    if as_of is not None:
        v5 = _check_time_leakage(cited_facts, as_of)
        if v5:
            violations.append(v5)

    # Render the template (substituting placeholders)
    rendered = _render_template(template, ledger)

    # Rule 2 — value match (uses rendered text)
    if cited_facts:
        v2 = _check_value_match(rendered, cited_facts)
        if v2:
            violations.append(v2)

    # Rule 3 — no bare numbers
    v3 = _check_no_bare_numbers(template, cited_facts)
    if v3:
        violations.append(v3)

    # Rule 4 — direction words
    v4 = _check_direction_words(template, cited_facts)
    if v4:
        violations.append(v4)

    passed = len(violations) == 0
    return passed, violations, rendered


# ---------------------------------------------------------------------------
# Public API — verify
# ---------------------------------------------------------------------------

def verify(
    memo: Memo,
    ledger_view: LedgerView,
    as_of: date | None = None,
) -> Memo:
    """
    Check memo sentences; remove those that fail verification.

    Rules enforced per sentence:
    1. Citation exists — every {F<id>} must be in ledger_view for the same company.
    2. Value match — rendered numbers must match cited fact values within tolerance.
    3. No bare numbers — numeric tokens outside placeholders are violations.
    4. Direction words — "grew"/"fell" must match the sign of cited fact deltas.
    5. Time leakage (as_of mode) — no fact with known_since > as_of.

    Parameters
    ----------
    memo:
        The Memo produced by the LLM Writer.
    ledger_view:
        The time-filtered view of all known facts.
    as_of:
        If provided, enforce the time firewall (Rule 5).

    Returns
    -------
    Memo
        A new Memo with failing sentences removed and rendered text filled in.
        ``memo.removed_by_verifier`` is incremented for each removed sentence.
    """
    company_id = memo.company_id
    removed_count = memo.removed_by_verifier
    cleaned_sections: dict[str, list[MemoSentence]] = {}

    for section_name, sentences in memo.sections.items():
        kept: list[MemoSentence] = []
        for sentence in sentences:
            passed, violations, rendered = _verify_sentence(
                sentence, company_id, ledger_view, as_of
            )
            if passed:
                kept.append(MemoSentence(
                    template=sentence.template,
                    fact_ids=sentence.fact_ids,
                    rendered=rendered,
                ))
            else:
                removed_count += 1
                # Record violations in a special internal field (best-effort — 
                # MemoSentence has no violations field, so we drop the sentence silently)
        cleaned_sections[section_name] = kept

    return Memo(
        company_id=company_id,
        sections=cleaned_sections,
        removed_by_verifier=removed_count,
        revisions=memo.revisions,
    )


# ---------------------------------------------------------------------------
# Public API — verify_ic_rationale
# ---------------------------------------------------------------------------

def verify_ic_rationale(
    rationale: str,
    fact_ids: list[str],
    ledger_view: LedgerView,
    as_of: date | None,
) -> tuple[bool, list[str]]:
    """
    Verify the IC (Investment Committee) rationale free-text string.

    Checks:
    1. All provided fact_ids exist in ledger_view.
    2. No fact has known_since > as_of (time leakage).
    3. No bare large numbers outside of {F<id>} placeholders.
    4. Direction words are consistent with cited fact deltas.

    Parameters
    ----------
    rationale:
        The free-text rationale string (may contain {F<id>} placeholders).
    fact_ids:
        List of fact IDs the LLM claims to cite in the rationale.
    ledger_view:
        The time-filtered ledger view.
    as_of:
        If provided, enforces the time firewall.

    Returns
    -------
    tuple[bool, list[str]]
        (passed, list_of_violations) — passed is True iff no violations found.
    """
    violations: list[str] = []

    # Collect cited facts, checking existence
    cited_facts: list[Fact] = []
    for fid in fact_ids:
        fact = ledger_view.get(fid)
        if fact is None:
            violations.append(f"Cited fact {fid} not found in ledger")
        else:
            cited_facts.append(fact)

    # Also find inline {F<id>} references in the rationale text
    inline_ids = _extract_placeholders(rationale)
    for fid in inline_ids:
        if fid not in fact_ids:
            fact = ledger_view.get(fid)
            if fact is None:
                violations.append(f"Inline fact reference {fid} not found in ledger")
            else:
                if fact not in cited_facts:
                    cited_facts.append(fact)

    # Time leakage
    if as_of is not None:
        v5 = _check_time_leakage(cited_facts, as_of)
        if v5:
            violations.append(v5)

    # No bare numbers
    v3 = _check_no_bare_numbers(rationale, cited_facts)
    if v3:
        violations.append(v3)

    # Direction words
    v4 = _check_direction_words(rationale, cited_facts)
    if v4:
        violations.append(v4)

    passed = len(violations) == 0
    return passed, violations
