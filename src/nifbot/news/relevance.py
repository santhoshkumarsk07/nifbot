"""Keyword relevance filter with topic tags. Keywords live in config/news.yaml."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


class RelevanceFilter:
    """Tags a headline with every topic whose keywords appear in it."""

    def __init__(self, topics: Mapping[str, Sequence[str]]) -> None:
        self._patterns = {
            tag: re.compile(r"\b(" + "|".join(re.escape(k) for k in kws) + r")\b", re.IGNORECASE)
            for tag, kws in topics.items()
            if kws
        }

    def tags(self, text: str) -> tuple[str, ...]:
        return tuple(sorted(tag for tag, pat in self._patterns.items() if pat.search(text)))
