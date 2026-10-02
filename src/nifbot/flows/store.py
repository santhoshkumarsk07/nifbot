"""SQLite tables for daily flows (shares the news database file)."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date

from nifbot.flows.fii_dii import CashFlow
from nifbot.flows.participant_oi import ParticipantOI

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_flows (
    day TEXT PRIMARY KEY, fii_net_cr REAL NOT NULL, dii_net_cr REAL NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS participant_oi (
    day TEXT NOT NULL, participant TEXT NOT NULL,
    fut_idx_long INTEGER, fut_idx_short INTEGER,
    opt_idx_call_long INTEGER, opt_idx_put_long INTEGER,
    opt_idx_call_short INTEGER, opt_idx_put_short INTEGER,
    total_long INTEGER, total_short INTEGER,
    PRIMARY KEY (day, participant)
);
"""


class FlowStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn
        self._c.executescript(_SCHEMA)

    def add_cash(self, flow: CashFlow, source: str) -> None:
        self._c.execute(
            "INSERT OR REPLACE INTO cash_flows VALUES (?,?,?,?)",
            (flow.day.isoformat(), flow.fii_net_cr, flow.dii_net_cr, source),
        )
        self._c.commit()

    def cash_until(self, day: date, limit: int = 30) -> list[CashFlow]:
        """Flows for days strictly before ``day`` (what was known pre-market)."""
        rows = self._c.execute(
            "SELECT day, fii_net_cr, dii_net_cr FROM cash_flows WHERE day < ? "
            "ORDER BY day DESC LIMIT ?",
            (day.isoformat(), limit),
        )
        return [CashFlow(date.fromisoformat(r[0]), r[1], r[2]) for r in rows][::-1]

    def add_participant(self, rows: dict[str, ParticipantOI]) -> None:
        for p in rows.values():
            d = asdict(p)
            d["day"] = p.day.isoformat()
            self._c.execute(
                "INSERT OR REPLACE INTO participant_oi VALUES "
                "(:day,:participant,:fut_idx_long,:fut_idx_short,:opt_idx_call_long,"
                ":opt_idx_put_long,:opt_idx_call_short,:opt_idx_put_short,:total_long,:total_short)",
                d,
            )
        self._c.commit()

    def participant_history(self, who: str, before: date, limit: int = 2) -> list[ParticipantOI]:
        """Latest ``limit`` days strictly before ``before``, oldest first."""
        rows = self._c.execute(
            "SELECT * FROM participant_oi WHERE participant = ? AND day < ? "
            "ORDER BY day DESC LIMIT ?",
            (who, before.isoformat(), limit),
        )
        out = []
        for r in rows:
            vals = list(r)
            out.append(ParticipantOI(date.fromisoformat(vals[0]), vals[1], *vals[2:]))
        return out[::-1]
