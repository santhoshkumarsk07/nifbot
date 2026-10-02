"""SYNTHETIC web content for tests (feeds, NSE/FRED files). Not real data."""

from __future__ import annotations

from datetime import datetime

from nifbot.net import Fetcher, FetchError


class FakeFetcher(Fetcher):
    """Serves canned bodies by URL prefix; unknown URLs fail."""

    def __init__(self, pages: dict[str, bytes | Exception]) -> None:
        super().__init__()
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body
        raise FetchError(f"no fake page for {url}")


def rss(items: list[tuple[str, str, datetime | None]], desc: str = "") -> bytes:
    parts = []
    for title, link, pub in items:
        pd = f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>" if pub else ""
        parts.append(
            f"<item><title><![CDATA[{title}]]></title><link>{link}</link>"
            f"<description>{desc}</description>{pd}</item>"
        )
    return f"<?xml version='1.0'?><rss><channel>{''.join(parts)}</channel></rss>".encode()


PARTICIPANT_CSV = """Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 01, 2026
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts,Total Short Contracts
Client,"300,000",150000,1,1,1000,2000,1500,2500,1,1,1,1,600000,500000
DII,50000,100000,1,1,0,0,0,0,1,1,1,1,60000,110000
FII,100000,300000,1,1,500,800,400,900,1,1,1,1,200000,400000
Pro,80000,80000,1,1,700,700,700,700,1,1,1,1,160000,160000
TOTAL,530000,630000,4,4,2200,3500,2600,4100,4,4,4,4,1020000,1170000
"""

FII_DII_JSON = (
    b'[{"category":"DII **","date":"01-Oct-2026","buyValue":"15,000.10",'
    b'"sellValue":"12,000.00","netValue":"3,000.10"},'
    b'{"category":"FII/FPI **","date":"01-Oct-2026","buyValue":"10,000",'
    b'"sellValue":"12,500.5","netValue":"-2,500.50"}]'
)


def fred_csv(*rows: tuple[str, str]) -> bytes:
    return ("observation_date,X\n" + "\n".join(f"{d},{v}" for d, v in rows)).encode()
