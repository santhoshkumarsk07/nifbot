"""Rule-based impact classifier (high / medium / low)."""

from __future__ import annotations

from collections.abc import Collection

from nifbot.news.models import Impact

HIGH_TAGS = frozenset({"rbi_policy", "fed", "budget", "geopolitics", "index"})
MEDIUM_TAGS = frozenset({"heavyweights", "sebi", "macro", "crude", "fii", "global"})


def classify(tags: Collection[str], sentiment: float, official: bool) -> Impact:
    """High: market-wide topics with a clear tone, or official policy news.

    Medium: heavyweight stocks, regulators, macro data. Low: everything else.
    """
    strong = abs(sentiment) >= 0.34
    tagset = set(tags)
    if tagset & HIGH_TAGS and (strong or official):
        return "high"
    if tagset & (HIGH_TAGS | MEDIUM_TAGS):
        return "medium" if (strong or official or tagset & HIGH_TAGS) else "low"
    return "low"
