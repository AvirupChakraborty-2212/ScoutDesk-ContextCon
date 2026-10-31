"""Tests for crust.py — budget enforcement, NoData exception, and credit tracking."""

try:
    import pytest
except ImportError:
    import contextlib

    class _MockPytest:
        @staticmethod
        @contextlib.contextmanager
        def raises(expected_exception):
            try:
                yield
            except expected_exception:
                pass
            else:
                raise AssertionError(f"Expected {expected_exception} but nothing was raised")

    pytest = _MockPytest()

from app.crust import BudgetExceeded, CreditCounter, NoData, _parse_retry_after
import httpx


def test_credit_counter_budget_enforcement():
    counter = CreditCounter(budget=10)
    assert counter.budget == 10
    assert counter.used == 0
    assert counter.remaining == 10

    # Charge 6 credits
    counter.charge(6)
    assert counter.used == 6
    assert counter.remaining == 4

    # Charge 4 credits (hits exactly 10)
    counter.charge(4)
    assert counter.used == 10
    assert counter.remaining == 0

    # Next charge exceeds budget -> must raise BudgetExceeded
    with pytest.raises(BudgetExceeded):
        counter.charge(1)


def test_nodata_exception():
    with pytest.raises(NoData):
        raise NoData("no match found in Crustdata graph")


def test_parse_retry_after():
    req = httpx.Request("POST", "https://api.crustdata.com/company/enrich")
    
    # 1. Header with explicit seconds
    resp1 = httpx.Response(429, request=req, headers={"Retry-After": "5"})
    assert _parse_retry_after(resp1, 2.0) == 5.0

    # 2. Header missing -> returns fallback
    resp2 = httpx.Response(429, request=req, headers={})
    assert _parse_retry_after(resp2, 3.5) == 3.5

    # 3. Invalid header -> returns fallback
    resp3 = httpx.Response(429, request=req, headers={"Retry-After": "invalid"})
    assert _parse_retry_after(resp3, 4.0) == 4.0
