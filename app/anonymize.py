"""Anonymiser for backtest mode — replaces real company / founder names with aliases.

Design rules enforced here:
- LLM prompts sent in backtest mode MUST pass through ``Anonymizer.anonymize`` before
  being dispatched, so the model never sees real identities.
- Responses MUST be passed through ``Anonymizer.deanonymize`` before being stored as
  facts or shown to users.
- Sort names longest-first before replacing to avoid partial-match clobbering
  (e.g. "Stripe India" is replaced before "Stripe").
"""

from __future__ import annotations

import re
from typing import Sequence


class Anonymizer:
    """Bi-directional name anonymiser for backtest / time-travel mode.

    Parameters
    ----------
    company_names:
        All company names (and/or domains) that should be masked.
    founder_names:
        All founder / executive names that should be masked.

    Attributes
    ----------
    mapping:
        ``{real_name: alias}`` — populated at construction time.
    reverse:
        ``{alias: real_name}`` — populated at construction time.
    """

    def __init__(
        self,
        company_names: Sequence[str],
        founder_names: Sequence[str],
    ) -> None:
        self.mapping: dict[str, str] = {}
        self.reverse: dict[str, str] = {}

        # Assign "Company A", "Company B", … aliases
        for idx, name in enumerate(company_names):
            alias = f"Company {_index_to_label(idx)}"
            self._register(name, alias)

        # Assign "Founder 1", "Founder 2", … aliases
        for idx, name in enumerate(founder_names):
            alias = f"Founder {idx + 1}"
            self._register(name, alias)

        # Pre-build sorted replacement lists (longest real name first) for both
        # directions so that _replace_all avoids spurious partial substitutions.
        self._sorted_real: list[str] = sorted(
            self.mapping.keys(), key=len, reverse=True
        )
        self._sorted_aliases: list[str] = sorted(
            self.reverse.keys(), key=len, reverse=True
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def anonymize(self, text: str) -> str:
        """Replace all real names in *text* with their aliases (case-insensitive)."""
        return _replace_all(text, self._sorted_real, self.mapping)

    def deanonymize(self, text: str) -> str:
        """Restore all aliases in *text* back to their real names."""
        return _replace_all(text, self._sorted_aliases, self.reverse)

    def alias_for(self, name: str) -> str:
        """Return the alias for a given real name (case-insensitive lookup).

        Raises ``KeyError`` if *name* was not registered at construction time.
        """
        normalised = name.strip()
        # Try exact match first
        if normalised in self.mapping:
            return self.mapping[normalised]
        # Fall back to case-insensitive scan
        lower = normalised.lower()
        for real, alias in self.mapping.items():
            if real.lower() == lower:
                return alias
        raise KeyError(f"Name not registered in anonymiser: {name!r}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register(self, name: str, alias: str) -> None:
        """Add a real→alias and alias→real pair (skips empty names)."""
        name = name.strip()
        if not name:
            return
        # Guard against duplicate real names registering different aliases
        if name not in self.mapping:
            self.mapping[name] = alias
            self.reverse[alias] = name


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _index_to_label(idx: int) -> str:
    """Convert a 0-based index to an Excel-style column label (A, B, …, Z, AA, …)."""
    label = ""
    n = idx
    while True:
        label = chr(ord("A") + n % 26) + label
        n = n // 26 - 1
        if n < 0:
            break
    return label


def _replace_all(
    text: str,
    keys_sorted_by_length: list[str],
    lookup: dict[str, str],
) -> str:
    """Replace every occurrence of each key in *text* with ``lookup[key]``.

    Matching is **case-insensitive**; the original casing of the matched
    substring is discarded and the replacement string is inserted verbatim.

    Keys are applied longest-first (caller's responsibility to pre-sort) so
    that "Stripe India" is replaced before "Stripe".
    """
    for key in keys_sorted_by_length:
        replacement = lookup.get(key, "")
        if not replacement:
            continue
        # re.escape makes the literal key safe; re.IGNORECASE handles casing
        text = re.sub(re.escape(key), replacement, text, flags=re.IGNORECASE)
    return text
