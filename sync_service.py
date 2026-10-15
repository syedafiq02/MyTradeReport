"""Background synchronisation between the MT5 connector, SQLite and the in-memory cache.

A single worker thread owns the connector (the MetaTrader5 package is not thread-safe)
and runs every job sequentially, so sync jobs can never overlap. Flask request
handlers only read the cached state under a lock.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from config import Config
from database import Database, account_key
from performance import compute_performance

log = logging.getLogger("sync")

HISTORY_OVERLAP_SECONDS = 7 * 86400   # re-read recent days each incremental sync
FULL_RESYNC_SECONDS = 6 * 3600        # periodically re-read the whole history


class SyncService:
    def __init__(self, config: Config, connector: Any, db: Database):
        self.config = config
        self.connector = connector
        self.db = db
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._account: dict[str, Any] | None = None
        self._positions: list[dict[str, Any]] = []
        self._acct_key: str | None = None
        self._last_success: float | None = None
        self._last_history_sync: float | None = None
        self._last_error: str | None = None
        self._last_attempt: float | None = None
        self._offset: int | None = None
        self._offset_source = "unknown"
        self._performance: dict[str, Any] | None = None
        self._terminal: dict[str, Any] = {}

        self._next_history = 0.0
        self._next_full_resync = 0.0
        self._last_snapshot = 0.0
        self._retry_delay = config.poll_interval
        self._next_connect = 0.0

        stored = self.db.get_meta("server_offset_seconds")
        if stored is not None:
            try:
                self.connector.set_known_offset(int(stored))
            except ValueError:
                pass

    # -- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mt5-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.connector.shutdown()

    def _run(self) -> None:
        log.info("Sync worker started (mode=%s, poll=%ss, history=%ss)",
                 self.connector.mode, self.config.poll_interval, self.config.history_interval)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:  # never let the worker die
                log.exception("Unexpected error in sync worker")
            elapsed = time.monotonic() - started
            self._stop.wait(max(self.config.poll_interval - elapsed, 0.5))
        log.info("Sync worker stopped")

    # -- one cycle ------------------------------------------------------------------
    def run_once(self) -> None:
        now = time.time()
        with self._lock:
            self._last_attempt = now
        if not self.connector.connected:
            if now < self._next_connect:
                return
            if not self.connector.connect():
                self._fail(self.connector.last_error or "Unable to connect to MT5")
                self._next_connect = now + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, 60.0)
                return
            log.info("%s connected", "DEMO data source" if self.connector.mode == "demo" else "MT5 terminal")
            self._retry_delay = self.config.poll_interval
            self._next_history = 0.0  # sync history right after (re)connecting

        try:
            account = self.connector.fetch_account()
            positions = self.connector.fetch_positions()
            terminal = self.connector.terminal_status()
        except Exception as exc:
            self._fail(str(exc))
            return

        acct = account_key(account.get("login"), account.get("server"))
        if acct != self._acct_key:
            # Different account logged in: reset per-account schedule.
            self._next_history = 0.0
            self._next_full_resync = 0.0

        offset, source = self.connector.server_offset([p["symbol"] for p in positions])
        if offset is not None and offset != self._offset and source != "configured":
            self.db.set_meta("server_offset_seconds", str(offset))

        with self._lock:
            self._account = account
            self._positions = positions
            self._acct_key = acct
            self._terminal = terminal
            self._offset = offset
            self._offset_source = source
            self._last_success = now
            self._last_error = None

        if now - self._last_snapshot >= self.config.snapshot_interval:
            self.db.add_snapshot(acct, int(now), float(account["balance"]), float(account["equity"]),
                                 account.get("margin"), account.get("margin_free"), account.get("profit"))
            self._last_snapshot = now

        if now >= self._next_history:
            self._sync_history(acct, account, now)
            self._next_history = now + self.config.history_interval

    def _sync_history(self, acct: str, account: dict[str, Any], now: float) -> None:
        latest = self.db.latest_deal_time(acct)
        if latest is None or now >= self._next_full_resync:
            start = int(datetime(self.config.history_start_year, 1, 1, tzinfo=timezone.utc).timestamp())
            self._next_full_resync = now + FULL_RESYNC_SECONDS
        else:
            start = latest - HISTORY_OVERLAP_SECONDS
        try:
            deals = self.connector.fetch_deals(start)
        except Exception as exc:
            self._fail(str(exc))
            return
        new = self.db.upsert_deals(acct, deals)
        if new:
            log.info("History sync: %d new deal(s)", new)
        self._recompute(acct, account)
        with self._lock:
            self._last_history_sync = now

    def _recompute(self, acct: str, account: dict[str, Any]) -> None:
        deals = self.db.get_deals(acct)
        snapshots = self.db.get_snapshots(acct)
        perf = compute_performance(deals, account, snapshots, self._offset or 0)
        with self._lock:
            self._performance = perf

    def _fail(self, message: str) -> None:
        log.warning("Sync failed: %s", message)
        with self._lock:
            self._last_error = message

    # -- read API (thread-safe) -----------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            age = now - self._last_success if self._last_success else None
            if self._last_success is None:
                state = "offline"
            elif self._last_error and not self.connector.connected:
                state = "offline"
            elif age is not None and age > self.config.stale_after:
                state = "stale"
            elif self._last_error:
                state = "degraded"
            else:
                state = "live"
            if state == "live" and not self._terminal.get("terminal_connected", True):
                state = "stale"
                note = "MT5 terminal is running but not connected to the broker server."
            else:
                note = None
            return {
                "mode": self.connector.mode,
                "state": state,
                "note": note,
                "last_success_utc": self._last_success,
                "last_history_sync_utc": self._last_history_sync,
                "last_attempt_utc": self._last_attempt,
                "age_seconds": round(age, 1) if age is not None else None,
                "error": self._last_error,
                "server_utc_offset_hours": (self._offset / 3600) if self._offset is not None else None,
                "server_offset_source": self._offset_source,
                "poll_interval": self.config.poll_interval,
                "history_interval": self.config.history_interval,
                "stale_after": self.config.stale_after,
            }

    def snapshot_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "account": dict(self._account) if self._account else None,
                "positions": [dict(p) for p in self._positions],
                "account_key": self._acct_key,
                "offset": self._offset or 0,
                "performance": self._performance,
            }
