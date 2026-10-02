"""Rolling, time-decayed news score that is safe against look-ahead.

Only items with ``fetched_at <= as_of`` count: an item contributes from the
moment we actually had it, not from its (possibly earlier) publish time.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime

from nifbot.news.models import ScoredNews

IMPACT_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.15}


def news_score(
    items: Iterable[ScoredNews], as_of: datetime, half_life_minutes: float = 120
) -> float:
    """Sum of sentiment x impact weight x 0.5^(age/half_life), squashed to [-1, 1]."""
    total = 0.0
    for s in items:
        age = (as_of - s.item.fetched_at).total_seconds() / 60
        if age < 0:
            continue  # not known yet at as_of
        total += s.sentiment * IMPACT_WEIGHT[s.impact] * 0.5 ** (age / half_life_minutes)
    return math.tanh(total / 2)
