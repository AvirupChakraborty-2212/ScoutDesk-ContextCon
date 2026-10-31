"""Tests for verifier.py — citation checks, number matching, direction words, and as_of leakage."""

from datetime import date, datetime, timezone

from app.ledger import FactLedger
from app.models import Fact, Memo, MemoSentence
from app.verifier import extract_number, verify, verify_ic_rationale


def _make_fact(id: str, company: str, key: str, value: any, unit: str | None = None, known_since: date | None = None) -> Fact:
    return Fact(
        id=id,
        company_id=company,
        subject="company",
        key=key,
        value=value,
        unit=unit,
        endpoint="test",
        field_path=key,
        derived_from=[],
        known_since=known_since or date(2024, 1, 1),
        fetched_at=datetime.now(timezone.utc),
        raw_excerpt="test",
    )


def test_extract_number():
    assert extract_number("1.2M") == 1_200_000.0
    assert extract_number("500K") == 500_000.0
    assert extract_number("1.5B") == 1_500_000_000.0
    assert extract_number("50%") == 0.50
    assert extract_number("25.5%") == 0.255
    assert extract_number("2,500") == 2500.0
    assert extract_number("5 lakh") == 500_000.0
    assert extract_number("2 crore") == 20_000_000.0


def test_verifier_accepts_valid_citations():
    ledger = FactLedger()
    c_id = "target.ai"

    f1 = _make_fact("F1", c_id, "company.headcount_total", 25, "employees")
    f2 = _make_fact("F2", c_id, "company.headcount_yoy", 0.40, "%")
    ledger.add(f1)
    ledger.add(f2)

    view = ledger.view(as_of=None)

    memo = Memo(
        company_id=c_id,
        sections={
            "summary": [
                MemoSentence(template="The company employs {F1} team members.", fact_ids=["F1"], rendered="The company employs 25 team members."),
                MemoSentence(template="Headcount grew {F2} over the past year.", fact_ids=["F2"], rendered="Headcount grew 40.0% over the past year."),
            ]
        },
    )

    cleaned = verify(memo, view)
    assert len(cleaned.sections["summary"]) == 2
    assert cleaned.removed_by_verifier == 0


def test_verifier_blocks_uncited_facts():
    ledger = FactLedger()
    c_id = "target.ai"

    view = ledger.view(as_of=None)

    memo = Memo(
        company_id=c_id,
        sections={
            "summary": [
                MemoSentence(template="The company employs {F999}.", fact_ids=["F999"], rendered="The company employs 25."),
            ]
        },
    )

    cleaned = verify(memo, view)
    # F999 does not exist in ledger -> sentence must be removed!
    assert len(cleaned.sections["summary"]) == 0
    assert cleaned.removed_by_verifier == 1


def test_verifier_blocks_direction_mismatch():
    ledger = FactLedger()
    c_id = "target.ai"

    # Negative YoY delta (-20%)
    f_neg = _make_fact("F1", c_id, "company.headcount_yoy", -0.20, "%")
    ledger.add(f_neg)
    view = ledger.view(as_of=None)

    # Sentence claims "grew"
    memo = Memo(
        company_id=c_id,
        sections={
            "summary": [
                MemoSentence(template="Team size grew {F1} YoY.", fact_ids=["F1"], rendered="Team size grew -20.0% YoY."),
            ]
        },
    )

    cleaned = verify(memo, view)
    assert len(cleaned.sections["summary"]) == 0
    assert cleaned.removed_by_verifier == 1
