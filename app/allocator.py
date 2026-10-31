"""Capital Allocator — calculates check sizes across Back verdicts under fund constraints.

Pure-code allocation module.
Capital is weighted by (score.total × confidence) and subject to hard check-size caps.
Human-review, watch, and pass verdicts receive zero allocation.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import Verdict

logger = logging.getLogger(__name__)


def allocate(
    verdicts: list[Verdict],
    total_capital_usd: float = 5_000_000.0,
    fund_rules: dict[str, Any] | None = None,
) -> list[Verdict]:
    """Distribute fund capital across companies with a 'back' decision.

    Parameters
    ----------
    verdicts:
        Final IC verdicts.
    total_capital_usd:
        Total investable fund capital for this cohort.
    fund_rules:
        Optional constraints:
          - max_check_pct: max check as fraction of fund (default 0.25)
          - reserve_pct: reserve fraction withheld (default 0.20)
          - min_check_usd: minimum check size (default 250_000)
    """
    rules = fund_rules or {}
    reserve_pct = float(rules.get("reserve_pct", 0.20))
    max_check_pct = float(rules.get("max_check_pct", 0.30))
    min_check = float(rules.get("min_check_usd", 250_000.0))

    deployable_capital = total_capital_usd * (1.0 - reserve_pct)
    max_check = deployable_capital * max_check_pct

    # Filter to Back decisions
    back_verdicts = [v for v in verdicts if v.decision == "back"]

    if not back_verdicts:
        # No back verdicts — return unchanged with check_size = None
        return verdicts

    # Compute raw weights: score.total * confidence
    raw_weights = [
        max(0.1, v.score.total * max(0.1, v.score.confidence))
        for v in back_verdicts
    ]
    total_weight = sum(raw_weights)

    # Initial pro-rata split
    allocations: dict[str, float] = {}
    for v, w in zip(back_verdicts, raw_weights):
        raw_check = (w / total_weight) * deployable_capital
        bounded_check = max(min_check, min(max_check, raw_check))
        # Round to nearest $10,000 for realistic check sizing
        allocations[v.company_id] = round(bounded_check, -4)

    # Re-normalize if sum exceeds deployable capital
    sum_allocated = sum(allocations.values())
    if sum_allocated > deployable_capital and sum_allocated > 0:
        scale = deployable_capital / sum_allocated
        for cid in allocations:
            allocations[cid] = round(allocations[cid] * scale, -4)

    # Populate check_size on verdicts
    result_verdicts: list[Verdict] = []
    for v in verdicts:
        check = allocations.get(v.company_id) if v.decision == "back" else None
        updated_v = v.model_copy(update={"check_size": check})
        result_verdicts.append(updated_v)

    return result_verdicts
