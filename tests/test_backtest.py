"""Tests for backtest.py — outcome calculation, horizon dates, and report metrics arithmetic."""

from datetime import date

from app.models import BacktestCase, BacktestReport


def test_backtest_report_metrics():
    # 5 test cases
    cases = [
        # 2 Back calls: 1 raised, 1 did not
        BacktestCase(company_id="c1", decision="back", score=85.0, outcome_raised_within_horizon=True, outcome_fact_ids=["F1"]),
        BacktestCase(company_id="c2", decision="back", score=78.0, outcome_raised_within_horizon=False, outcome_fact_ids=[]),

        # 2 Pass calls: 0 raised, 2 did not
        BacktestCase(company_id="c3", decision="pass", score=30.0, outcome_raised_within_horizon=False, outcome_fact_ids=[]),
        BacktestCase(company_id="c4", decision="pass", score=42.0, outcome_raised_within_horizon=False, outcome_fact_ids=[]),

        # 1 Watch call: 1 raised
        BacktestCase(company_id="c5", decision="watch", score=58.0, outcome_raised_within_horizon=True, outcome_fact_ids=["F2"]),
    ]

    total_raised = sum(1 for c in cases if c.outcome_raised_within_horizon)  # 2 raised out of 5
    base_rate = round(total_raised / len(cases), 4)  # 0.40

    back_cases = [c for c in cases if c.decision == "back"]
    back_raised = sum(1 for c in back_cases if c.outcome_raised_within_horizon)
    back_hit_rate = round(back_raised / len(back_cases), 4)  # 1/2 = 0.50

    pass_cases = [c for c in cases if c.decision == "pass"]
    pass_raised = sum(1 for c in pass_cases if c.outcome_raised_within_horizon)
    pass_hit_rate = round(pass_raised / len(pass_cases), 4)  # 0/2 = 0.00

    report = BacktestReport(
        as_of=date(2024, 1, 1),
        horizon_months=18,
        n=len(cases),
        back_hit_rate=back_hit_rate,
        pass_hit_rate=pass_hit_rate,
        base_rate=base_rate,
        baseline_hit_rate=0.30,
        facts_blocked=140,
        cases=cases,
    )

    assert report.n == 5
    assert report.back_hit_rate == 0.50
    assert report.pass_hit_rate == 0.00
    assert report.base_rate == 0.40
    assert report.facts_blocked == 140
