"""Fact Ledger with time-firewall support.

Every piece of data in ScoutDesk lives here as a Fact.  The time firewall
ensures that in backtest / as_of mode only facts that were publicly known
on or before that date are visible, preventing look-ahead bias.

known_since assignment rules (documented as module-level constants):
  KNOWN_SINCE_FUNDING   → use the round's close/announcement date
  KNOWN_SINCE_ROLE      → person's role start date
  KNOWN_SINCE_POSTING   → job posting's posted_at date
  KNOWN_SINCE_NEWS      → article published date
  KNOWN_SINCE_REALTIME  → today's date  (EXCLUDED in backtest mode)
  KNOWN_SINCE_DERIVED   → max(known_since of all input facts)
  KNOWN_SINCE_UNKNOWN   → None           (EXCLUDED in as_of mode)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Iterator

from app.models import Fact

# ---------------------------------------------------------------------------
# known_since assignment rule labels
# ---------------------------------------------------------------------------

KNOWN_SINCE_FUNDING: str = "funding_round"   # set to round close / announce date
KNOWN_SINCE_ROLE: str = "person_role"        # set to role start date
KNOWN_SINCE_POSTING: str = "job_posting"     # set to job posting posted_at date
KNOWN_SINCE_NEWS: str = "news_article"       # set to article published date
KNOWN_SINCE_REALTIME: str = "realtime"       # headcount growth %, current totals → today
KNOWN_SINCE_DERIVED: str = "derived"         # max(known_since of all input facts)
KNOWN_SINCE_UNKNOWN: str = "unknown"         # → None, excluded in as_of mode


def derive_known_since(input_facts: list[Fact]) -> date | None:
    """Return max(known_since) across *input_facts*.

    Returns ``None`` if any input fact has ``known_since = None``, because the
    provenance chain is broken and the derived fact's timestamp is unknowable.
    """
    dates: list[date] = []
    for f in input_facts:
        if f.known_since is None:
            return None
        dates.append(f.known_since)
    return max(dates) if dates else None


# ---------------------------------------------------------------------------
# LedgerView — filtered, immutable snapshot
# ---------------------------------------------------------------------------


@dataclass
class LedgerView:
    """A read-only, optionally time-firewalled view over the ledger.

    If ``as_of`` is ``None`` the view is live — all facts are visible.
    If ``as_of`` is a date, the time firewall is active:
      • facts with ``known_since <= as_of``  → visible
      • facts with ``known_since = None``    → EXCLUDED (unknown provenance)
      • facts with ``known_since > as_of``   → EXCLUDED (future knowledge)

    The ``blocked_count`` property and ``blocked_fact_ids()`` method let the
    UI report "312 facts hidden by time firewall" with a drilldown list.
    """

    _facts: dict[str, Fact]               # full snapshot, keyed by fact_id
    _company_index: dict[str, list[str]]  # company_id → [fact_id, …]
    as_of: date | None

    _visible_ids: set[str] = field(default_factory=set, init=False)
    _blocked_ids: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        for fact_id, fact in self._facts.items():
            if self._passes_firewall(fact):
                self._visible_ids.add(fact_id)
            else:
                self._blocked_ids.add(fact_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _passes_firewall(self, fact: Fact) -> bool:
        """Return True iff *fact* is visible under the current as_of setting."""
        if self.as_of is None:
            # Live mode: everything is visible.
            return True
        if fact.known_since is None:
            # Unknown provenance → excluded in as_of mode.
            return False
        return fact.known_since <= self.as_of

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def facts(self, company_id: str) -> list[Fact]:
        """Return all visible facts for *company_id*, respecting the firewall."""
        fact_ids = self._company_index.get(company_id, [])
        return [
            self._facts[fid]
            for fid in fact_ids
            if fid in self._visible_ids
        ]

    def get(self, fact_id: str) -> Fact | None:
        """Return the fact if it passes the firewall, otherwise ``None``."""
        if fact_id not in self._visible_ids:
            return None
        return self._facts.get(fact_id)

    @property
    def blocked_count(self) -> int:
        """Number of facts excluded by the time firewall."""
        return len(self._blocked_ids)

    def blocked_fact_ids(self) -> list[str]:
        """Sorted list of fact ids excluded by the time firewall (for UI drilldown)."""
        return sorted(self._blocked_ids)

    def visible_count(self) -> int:
        """Number of facts visible through this view."""
        return len(self._visible_ids)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"LedgerView(as_of={self.as_of!r}, "
            f"visible={self.visible_count()}, blocked={self.blocked_count})"
        )


# ---------------------------------------------------------------------------
# FactLedger
# ---------------------------------------------------------------------------


class FactLedger:
    """Central, append-only store for all Facts in a ScoutDesk run.

    • ``add`` is thread-safe (guarded by ``threading.Lock``) so coroutines
      running in a thread pool can call it concurrently.
    • All other methods are read-only and therefore inherently safe.
    • ``dump`` / ``load`` enable full run replay from a JSON file.
    """

    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self._company_index: dict[str, list[str]] = {}
        self._lock: Lock = Lock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, fact: Fact) -> str:
        """Store *fact* in the ledger and return its ``id``.

        If ``fact.id`` is an empty string a UUID4 is generated and attached
        to the fact before storing.  The operation is idempotent: adding a
        fact whose id is already present is a no-op.
        """
        with self._lock:
            fact_id: str = fact.id if fact.id else str(uuid.uuid4())
            # Idempotent — already stored.
            if fact_id in self._facts:
                return fact_id
            # Attach generated id when the caller left it blank.
            if not fact.id:
                fact = fact.model_copy(update={"id": fact_id})
            self._facts[fact_id] = fact
            self._company_index.setdefault(fact.company_id, []).append(fact_id)
            return fact_id

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, fact_id: str) -> Fact:
        """Return the Fact with *fact_id*.  Raises ``KeyError`` if absent."""
        try:
            return self._facts[fact_id]
        except KeyError:
            raise KeyError(f"Fact '{fact_id}' not found in ledger") from None

    def by_company(self, company_id: str) -> list[Fact]:
        """Return all facts (unfiltered) for *company_id*."""
        ids = self._company_index.get(company_id, [])
        return [self._facts[fid] for fid in ids]

    def all_company_ids(self) -> list[str]:
        """Return a sorted list of every company_id that has at least one fact."""
        return sorted(self._company_index.keys())

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self) -> Iterator[Fact]:
        return iter(self._facts.values())

    # ------------------------------------------------------------------
    # View (time firewall)
    # ------------------------------------------------------------------

    def view(self, as_of: date | None = None) -> LedgerView:
        """Return an immutable ``LedgerView`` filtered to *as_of*.

        ``as_of=None``   → live view, no filtering (all facts visible).
        ``as_of=<date>`` → time-firewalled view; see :class:`LedgerView`.
        """
        with self._lock:
            # Take snapshots under the lock for thread-safety.
            facts_snapshot: dict[str, Fact] = dict(self._facts)
            index_snapshot: dict[str, list[str]] = {
                k: list(v) for k, v in self._company_index.items()
            }

        return LedgerView(
            _facts=facts_snapshot,
            _company_index=index_snapshot,
            as_of=as_of,
        )

    # ------------------------------------------------------------------
    # Serialisation — for run replay
    # ------------------------------------------------------------------

    def dump(self, path: Path) -> None:
        """Serialise all facts to *path* as a JSON array.

        Parent directories are created automatically.  Existing files are
        overwritten so that re-running a step produces the same artefact.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            facts_list = [f.model_dump(mode="json") for f in self._facts.values()]
        with path.open("w", encoding="utf-8") as fh:
            json.dump(facts_list, fh, indent=2, default=str)

    @classmethod
    def load(cls, path: Path) -> "FactLedger":
        """Deserialise a ledger previously saved by :meth:`dump` and return it."""
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            raw_list: list[dict] = json.load(fh)
        ledger = cls()
        for raw in raw_list:
            fact = Fact.model_validate(raw)
            ledger.add(fact)
        return ledger

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, int]:
        """Return a lightweight summary suitable for logging or UI display."""
        return {
            "total_facts": len(self._facts),
            "companies": len(self._company_index),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FactLedger(facts={len(self._facts)}, "
            f"companies={len(self._company_index)})"
        )
