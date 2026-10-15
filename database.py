"""SQLite persistence for account snapshots, deal history and small key/value metadata.

Deals are keyed by (account_key, ticket) so repeated history syncs never create
duplicates. account_key is a one-way hash of login+server; the full login number
is not stored in the database.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    ts_utc INTEGER NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    margin REAL,
    free_margin REAL,
    floating REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_account_ts ON snapshots(account_key, ts_utc);

CREATE TABLE IF NOT EXISTS deals (
    account_key TEXT NOT NULL,
    ticket INTEGER NOT NULL,
    order_ticket INTEGER,
    position_id INTEGER,
    time_server INTEGER NOT NULL,
    time_msc INTEGER,
    type INTEGER NOT NULL,
    entry INTEGER,
    symbol TEXT,
    volume REAL,
    price REAL,
    profit REAL,
    commission REAL,
    swap REAL,
    fee REAL,
    magic INTEGER,
    reason INTEGER,
    PRIMARY KEY (account_key, ticket)
);
CREATE INDEX IF NOT EXISTS idx_deals_account_time ON deals(account_key, time_server);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEAL_COLUMNS = (
    "ticket", "order_ticket", "position_id", "time_server", "time_msc", "type", "entry",
    "symbol", "volume", "price", "profit", "commission", "swap", "fee", "magic", "reason",
)


def account_key(login: Any, server: Any) -> str:
    raw = f"{login}@{server}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta -----------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # -- snapshots ------------------------------------------------------------------
    def add_snapshot(self, acct: str, ts_utc: int, balance: float, equity: float,
                     margin: float | None, free_margin: float | None, floating: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots(account_key, ts_utc, balance, equity, margin, free_margin, floating) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (acct, ts_utc, balance, equity, margin, free_margin, floating),
            )
            self._conn.commit()

    def get_snapshots(self, acct: str, since_utc: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts_utc, balance, equity, margin, free_margin, floating FROM snapshots "
                "WHERE account_key = ? AND ts_utc >= ? ORDER BY ts_utc",
                (acct, since_utc),
            ).fetchall()
        return [dict(r) for r in rows]

    def first_snapshot_time(self, acct: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts_utc) AS t FROM snapshots WHERE account_key = ?", (acct,)
            ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    # -- deals ----------------------------------------------------------------------
    def upsert_deals(self, acct: str, deals: Iterable[dict[str, Any]]) -> int:
        """Insert or update deals; returns the number of previously unknown tickets."""
        rows = []
        for d in deals:
            rows.append((
                acct, int(d["ticket"]), d.get("order"), d.get("position_id"), int(d["time"]),
                d.get("time_msc"), int(d["type"]), d.get("entry"), d.get("symbol") or "",
                float(d.get("volume") or 0), float(d.get("price") or 0), float(d.get("profit") or 0),
                float(d.get("commission") or 0), float(d.get("swap") or 0), float(d.get("fee") or 0),
                d.get("magic"), d.get("reason"),
            ))
        if not rows:
            return 0
        with self._lock:
            before = self._conn.execute(
                "SELECT COUNT(*) FROM deals WHERE account_key = ?", (acct,)
            ).fetchone()[0]
            self._conn.executemany(
                "INSERT INTO deals(account_key, " + ", ".join(DEAL_COLUMNS) + ") "
                "VALUES(" + ", ".join("?" * (len(DEAL_COLUMNS) + 1)) + ") "
                "ON CONFLICT(account_key, ticket) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in DEAL_COLUMNS if c != "ticket"),
                rows,
            )
            self._conn.commit()
            after = self._conn.execute(
                "SELECT COUNT(*) FROM deals WHERE account_key = ?", (acct,)
            ).fetchone()[0]
        return after - before

    def get_deals(self, acct: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT " + ", ".join(DEAL_COLUMNS) + " FROM deals WHERE account_key = ? "
                "ORDER BY time_msc, ticket",
                (acct,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_deal_time(self, acct: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(time_server) AS t FROM deals WHERE account_key = ?", (acct,)
            ).fetchone()
        return row["t"] if row and row["t"] is not None else None
