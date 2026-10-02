"""Rolling, time-decayed news score that is safe against look-ahead.

Only items with ``fetched_at <= as_of`` count: an item contributes from the
moment we actually had it, not from its (possibly earlier) publish time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from nifbot.news.models import ScoredNews

PRIOR = 1.0
IMPACT_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.15}


def news_score(
    items: Iterable[ScoredNews], as_of: datetime, half_life_minutes: float = 120
) -> float:
    """Shrunk weighted mean sentiment in [-1, 1].

    Weight = impact weight x 0.5^(age / half_life). The denominator adds a
    neutral prior of weight ``PRIOR`` so the score fades toward 0 as news ages
    or when there is little of it, and many similar headlines cannot push it to
    +/-1 on their own.
    """
    num = den = 0.0
    for s in items:
        age = (as_of - s.item.fetched_at).total_seconds() / 60
        if age < 0:
            continue  # not known yet at as_of
        w = IMPACT_WEIGHT[s.impact] * 0.5 ** (age / half_life_minutes)
        num += s.sentiment * w
        den += w
    return num / (den + PRIOR)
