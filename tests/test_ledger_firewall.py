"""Tests for ledger.py — FactLedger and time-firewall LedgerView."""

from datetime import date, datetime, timezone

from app.ledger import (
    FactLedger,
    LedgerView,
    derive_known_since,
    KNOWN_SINCE_REALTIME,
    KNOWN_SINCE_FUNDING,
)
from app.models import Fact


def _make_fact(id: str, company: str, key: str, value: any, known_since: date | None) -> Fact:
    return Fact(
        id=id,
        company_id=company,
        subject="company",
        key=key,
        value=value,
        unit=None,
        endpoint="test",
        field_path=key,
        derived_from=[],
        known_since=known_since,
        fetched_at=datetime.now(timezone.utc),
        raw_excerpt="test",
    )


def test_time_firewall_filtering():
    ledger = FactLedger()
    c_id = "test.ai"

    f1 = _make_fact("F1", c_id, "funding.round", 1_000_000, date(2023, 6, 1))
    f2 = _make_fact("F2", c_id, "funding.round", 5_000_000, date(2024, 6, 1))
    f3 = _make_fact("F3", c_id, "company.headcount", 25, date(2025, 1, 1))
    f_undated = _make_fact("F4", c_id, "unknown.metric", "foo", None)

    ledger.add(f1)
    ledger.add(f2)
    ledger.add(f3)
    ledger.add(f_undated)

    # 1. Test as_of = 2024-01-01 (Only F1 should be visible)
    view_2024 = ledger.view(as_of=date(2024, 1, 1))
    visible_2024 = view_2024.facts(c_id)

    assert len(visible_2024) == 1
    assert visible_2024[0].id == "F1"
    assert view_2024.get("F1") is not None
    assert view_2024.get("F2") is None
    assert view_2024.get("F3") is None
    assert view_2024.get("F4") is None
    assert view_2024.blocked_count == 3

    # 2. Test as_of = 2024-06-01 (F1 and F2 visible)
    view_mid2024 = ledger.view(as_of=date(2024, 6, 1))
    visible_mid = view_mid2024.facts(c_id)
    assert len(visible_mid) == 2
    assert {f.id for f in visible_mid} == {"F1", "F2"}
    assert view_mid2024.blocked_count == 2

    # 3. Test Live mode (as_of = None -> ALL facts visible)
    live_view = ledger.view(as_of=None)
    live_facts = live_view.facts(c_id)
    assert len(live_facts) == 4
    assert live_view.blocked_count == 0
    assert live_view.get("F4") is not None


def test_derive_known_since():
    f1 = _make_fact("F1", "c", "k1", 1, date(2023, 1, 1))
    f2 = _make_fact("F2", "c", "k2", 2, date(2023, 8, 15))
    f3 = _make_fact("F3", "c", "k3", 3, None)

    # Max of valid dates
    assert derive_known_since([f1, f2]) == date(2023, 8, 15)

    # If any input fact is undated, derived known_since is None
    assert derive_known_since([f1, f2, f3]) is None
