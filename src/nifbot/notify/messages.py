"""Plain-text Telegram message builders (no HTML/Markdown: headlines are untrusted)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from nifbot import DISCLAIMER
from nifbot.data.models import Quote
from nifbot.flows.fii_dii import CashFlow, trend
from nifbot.flows.global_cues import Cue
from nifbot.flows.participant_oi import ParticipantOI
from nifbot.news.models import ScoredNews
from nifbot.trading_calendar import MarketEvent

US_SERIES = {"SP500", "DJIA", "NASDAQCOM"}


def news_alert(s: ScoredNews) -> str:
    """One high-impact news alert."""
    it = s.item
    when = (it.published_at or it.fetched_at).strftime("%H:%M")
    return (
        f"[{s.sign}] NEWS ({s.impact}) {when} IST\n"
        f"{it.title}\n"
        f"Source: {it.source_name}\n"
        f"Topics: {', '.join(s.tags)} | sentiment {s.sentiment:+.2f}\n"
        f"{it.url}"
    )


@dataclass
class BriefInputs:
    day: date
    cues: list[Cue] = field(default_factory=list)
    missing_cues: list[str] = field(default_factory=list)
    gift_nifty: Quote | None = None
    cash_flows: list[CashFlow] = field(default_factory=list)
    fii_oi: list[ParticipantOI] = field(default_factory=list)  # oldest first, before `day`
    vix: Quote | None = None
    news: Sequence[ScoredNews] = ()
    news_score: float = 0.0
    events: Sequence[MarketEvent] = ()
    holiday_list_verified: bool = True


def premarket_tilt(inp: BriefInputs) -> tuple[float, list[str]]:
    """Transparent rules-only tilt in [-1, 1] with the reasons that moved it.

    Not a forecast and not backtested: it summarises overnight inputs. Each part
    is capped so no single input dominates.
    """
    parts: list[tuple[str, float]] = []
    fresh = [c for c in inp.cues if not c.is_stale(inp.day)]  # stale cues never count
    us = [c.change for c in fresh if c.series in US_SERIES]
    if us:
        parts.append(("US indices", max(-1.0, min(1.0, sum(us) / len(us) / 1.0))))
    for c in fresh:
        if c.series == "NIKKEI225":
            parts.append(("Nikkei", max(-1.0, min(1.0, c.change / 1.5)) * 0.5))
        if c.series == "DCOILBRENTEU":
            parts.append(("Brent", -max(-1.0, min(1.0, c.change / 3.0)) * 0.5))
    if inp.cash_flows:
        fii = inp.cash_flows[-1].fii_net_cr
        parts.append(("FII cash", max(-1.0, min(1.0, fii / 3000)) * 0.5))
    if inp.fii_oi:
        ratio = inp.fii_oi[-1].fut_long_ratio
        parts.append(("FII index futures", max(-1.0, min(1.0, (ratio - 0.5) * 4)) * 0.5))
    if inp.news:
        parts.append(("News", inp.news_score * 0.75))
    if not parts:
        return 0.0, []
    score = max(-1.0, min(1.0, sum(v for _, v in parts) / len(parts) * 1.5))
    reasons = [f"{name} {'+' if v > 0.05 else '-' if v < -0.05 else '0'}" for name, v in parts]
    return score, reasons


def premarket_brief(inp: BriefInputs, max_news: int = 6) -> str:
    """08:45 brief. Missing inputs are shown as not available, never estimated."""
    lines = [f"PRE-MARKET BRIEF {inp.day:%a %d %b %Y}", ""]
    if not inp.holiday_list_verified:
        lines.append("Note: holiday list not yet verified against the NSE circular.")
    lines.append("Global cues (last close, source FRED):")
    lines += [f"  {c.render(inp.day)}" for c in inp.cues] or ["  not available"]
    if any(c.is_stale(inp.day) for c in inp.cues):
        lines.append("  (STALE values are shown for context but not used in the tilt)")
    if inp.missing_cues:
        lines.append(f"  not available: {', '.join(inp.missing_cues)}")
    if inp.gift_nifty:
        lines.append(f"GIFT Nifty: {inp.gift_nifty.ltp:,.1f}")
    else:
        lines.append("GIFT Nifty: not available (no free official feed configured)")
    lines.append("")
    if inp.cash_flows:
        last = inp.cash_flows[-1]
        lines.append(
            f"FII/DII cash {last.day:%d %b}: FII {last.fii_net_cr:+,.0f} cr, "
            f"DII {last.dii_net_cr:+,.0f} cr"
        )
        for n in (5, 20):
            t = trend(inp.cash_flows, n)
            if t:
                lines.append(f"  {n}-day: FII {t[0]:+,.0f} cr, DII {t[1]:+,.0f} cr")
    else:
        lines.append("FII/DII cash: not available")
    if inp.fii_oi:
        cur = inp.fii_oi[-1]
        msg = (
            f"FII index futures {cur.day:%d %b}: long {cur.fut_long_ratio:.0%} "
            f"(net {cur.fut_net:+,} contracts)"
        )
        if len(inp.fii_oi) > 1:
            prev = inp.fii_oi[-2]
            msg += f", change {cur.fut_net - prev.fut_net:+,}"
        lines.append(msg)
    else:
        lines.append("FII participant OI: not available")
    lines.append(f"India VIX: {inp.vix.ltp:.2f}" if inp.vix else "India VIX: not available yet")
    lines.append("")
    if inp.events:
        lines.append("Events today:")
        lines += [f"  {e.time or '--:--'} {e.name} [{e.impact}]" for e in inp.events]
    else:
        lines.append("Events today: none in calendar")
    lines.append("")
    top = sorted(
        inp.news, key=lambda s: ({"high": 0, "medium": 1, "low": 2}[s.impact], -abs(s.sentiment))
    )[:max_news]
    lines.append(f"Key news (score {inp.news_score:+.2f}):")
    lines += [f"  [{s.sign}] {s.item.title} ({s.item.source_name})" for s in top] or ["  none"]
    lines.append("")
    tilt, reasons = premarket_tilt(inp)
    label = "positive" if tilt >= 0.2 else "negative" if tilt <= -0.2 else "neutral"
    lines.append(f"Pre-market tilt: {label} ({tilt:+.2f}) from {', '.join(reasons) or 'no inputs'}")
    lines.append("(rules-based summary of overnight inputs, not a backtested forecast)")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
