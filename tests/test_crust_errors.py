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

from app.crust import BudgetExceeded, CreditCounter, NoData


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
