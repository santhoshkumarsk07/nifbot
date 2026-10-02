"""Sentiment scorers returning a value in [-1, 1].

* :class:`FinbertScorer` - ProsusAI/finbert run locally (optional ``nlp`` extra).
* :class:`LexiconScorer` - small finance word list; a transparent fallback that
  works without heavy dependencies. Less accurate than FinBERT.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

log = logging.getLogger(__name__)

_POS = {
    "surge",
    "surges",
    "soar",
    "soars",
    "rally",
    "rallies",
    "gain",
    "gains",
    "jump",
    "jumps",
    "rise",
    "rises",
    "record",
    "high",
    "beat",
    "beats",
    "upgrade",
    "upgrades",
    "strong",
    "growth",
    "boost",
    "boosts",
    "inflows",
    "buying",
    "bullish",
    "recovery",
    "rebound",
    "eases",
    "easing",
    "cut",
    "cuts",
    "profit",
    "outperform",
    "optimism",
    "approval",
    "deal",
    "ceasefire",
    "upbeat",
    "positive",
    "robust",
    "accelerates",
}
_NEG = {
    "fall",
    "falls",
    "drop",
    "drops",
    "plunge",
    "plunges",
    "crash",
    "slump",
    "slumps",
    "decline",
    "declines",
    "tumble",
    "tumbles",
    "loss",
    "losses",
    "weak",
    "miss",
    "misses",
    "downgrade",
    "downgrades",
    "selloff",
    "sell-off",
    "outflows",
    "selling",
    "bearish",
    "fear",
    "fears",
    "war",
    "tariff",
    "tariffs",
    "sanction",
    "sanctions",
    "hike",
    "hikes",
    "inflation",
    "slowdown",
    "recession",
    "default",
    "fraud",
    "probe",
    "ban",
    "penalty",
    "concern",
    "concerns",
    "risk",
    "risks",
    "volatile",
    "tension",
    "tensions",
    "attack",
    "negative",
    "pressure",
    "worst",
    "low",
    "lows",
}
_NEGATORS = {"not", "no", "without", "despite", "halt", "halts"}
_WORD = re.compile(r"[a-z][a-z-]*")


class SentimentScorer(Protocol):
    name: str

    def score(self, texts: Sequence[str]) -> list[float]: ...


class LexiconScorer:
    """Counts positive/negative finance words, flipping after a negator."""

    name = "lexicon"

    def score(self, texts: Sequence[str]) -> list[float]:
        out: list[float] = []
        for text in texts:
            words = _WORD.findall(text.lower())
            total = 0
            for i, w in enumerate(words):
                sign = 1 if w in _POS else -1 if w in _NEG else 0
                if sign and i and words[i - 1] in _NEGATORS:
                    sign = -sign
                total += sign
            out.append(max(-1.0, min(1.0, total / 3)))
        return out


class FinbertScorer:
    """FinBERT via HuggingFace transformers, loaded lazily. Text is data only."""

    name = "finbert"

    def __init__(self, pipeline_factory: Callable[[], Any] | None = None) -> None:
        self._factory = pipeline_factory or self._default_factory
        self._pipe: Any = None

    @staticmethod
    def _default_factory() -> Any:
        transformers = importlib.import_module("transformers")  # optional dependency
        return transformers.pipeline("text-classification", model="ProsusAI/finbert", top_k=None)

    def score(self, texts: Sequence[str]) -> list[float]:
        if self._pipe is None:
            self._pipe = self._factory()
        results = self._pipe(list(texts), truncation=True)
        out: list[float] = []
        for res in results:
            probs = {d["label"].lower(): float(d["score"]) for d in res}
            out.append(max(-1.0, min(1.0, probs.get("positive", 0.0) - probs.get("negative", 0.0))))
        return out


def make_scorer(engine: str) -> SentimentScorer:
    """FinBERT if requested and installed, else the lexicon scorer."""
    if engine == "finbert":
        if importlib.util.find_spec("transformers") is None:
            log.warning("transformers not installed; using lexicon sentiment (uv sync --extra nlp)")
            return LexiconScorer()
        return FinbertScorer()
    return LexiconScorer()
