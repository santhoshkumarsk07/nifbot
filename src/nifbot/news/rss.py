"""RSS 2.0 / Atom parsing with defusedxml (protects against XML bombs / XXE)."""

from __future__ import annotations

import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element  # nosec B405 - type only; parsing uses defusedxml

from defusedxml import ElementTree as SafeET

from nifbot.timeutil import IST

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
ATOM = "{http://www.w3.org/2005/Atom}"


def clean_text(value: str | None, limit: int) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    return _WS_RE.sub(" ", text).strip()[:limit]


def parse_date(value: str | None) -> datetime | None:
    """Parse RFC 822 or ISO 8601 dates; naive values are assumed IST."""
    if not value:
        return None
    value = value.strip()
    out: datetime | None = None
    try:
        out = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            out = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=IST)
    return out.astimezone(IST)


def _text(el: Element, *names: str) -> str | None:
    for name in names:
        child = el.find(name)
        if child is not None:
            text = "".join(child.itertext())
            if text.strip():
                return text
    return None


def parse_feed(data: bytes) -> list[dict[str, object]]:
    """Return entries as dicts with title, url, summary, published_at."""
    root = SafeET.fromstring(data)
    entries: list[dict[str, object]] = []
    items = root.findall("./channel/item") or root.findall(".//item")
    for it in items:
        entries.append(
            {
                "title": clean_text(_text(it, "title"), 500),
                "url": (_text(it, "link", "guid") or "").strip(),
                "summary": clean_text(_text(it, "description"), 2000),
                "published_at": parse_date(
                    _text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
                ),
            }
        )
    for it in root.findall(f"{ATOM}entry"):
        link = ""
        for l_el in it.findall(f"{ATOM}link"):
            if l_el.get("rel", "alternate") == "alternate" and l_el.get("href"):
                link = l_el.get("href", "")
                break
        entries.append(
            {
                "title": clean_text(_text(it, f"{ATOM}title"), 500),
                "url": link.strip(),
                "summary": clean_text(_text(it, f"{ATOM}summary", f"{ATOM}content"), 2000),
                "published_at": parse_date(_text(it, f"{ATOM}updated", f"{ATOM}published")),
            }
        )
    return [e for e in entries if e["title"] and str(e["url"]).startswith("https://")]
