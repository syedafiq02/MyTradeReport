"""Read-only access to a MetaTrader 5 terminal through the official MetaTrader5 package.

STRICTLY READ-ONLY: this module only reads account info, positions, ticks and deal
history. It never places, modifies or closes orders and never handles passwords -
it attaches to a terminal session that the user has logged into manually.

Timestamps: the MT5 API returns deal/position times as "trade server time" encoded
as Unix seconds. Connectors return those raw values plus a best-effort server->UTC
offset; conversion to real UTC happens in the sync service.

The MetaTrader5 package is not thread-safe, so every method here must be called
from a single thread (the sync worker in sync_service.py).
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("mt5_connector")

try:  # Imported lazily so the web app still runs (and explains the problem) without it.
    import MetaTrader5 as mt5  # type: ignore
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - depends on the machine
    mt5 = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

ACCOUNT_FIELDS = (
    "login", "server", "company", "currency", "balance", "equity", "profit", "margin",
    "margin_free", "margin_level", "leverage", "trade_mode", "credit", "currency_digits",
)
POSITION_FIELDS = (
    "ticket", "symbol", "type", "volume", "price_open", "price_current", "profit",
    "swap", "sl", "tp", "time", "time_msc", "magic", "identifier",
)
DEAL_FIELDS = (
    "ticket", "order", "position_id", "time", "time_msc", "type", "entry", "symbol",
    "volume", "price", "profit", "commission", "swap", "fee", "magic", "reason",
)

TRADE_MODES = {0: "Demo", 1: "Contest", 2: "Real"}


class ConnectorError(Exception):
    """Raised when the terminal cannot provide requested data."""


def _pick(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    data = obj._asdict() if hasattr(obj, "_asdict") else dict(obj)
    return {f: data.get(f) for f in fields}


class MT5Connector:
    """Connector for a real MetaTrader 5 terminal."""

    mode = "live"

    def __init__(self, terminal_path: str | None = None, server_utc_offset: str = "auto"):
        self.terminal_path = terminal_path
        self._offset_setting = server_utc_offset
        self.connected = False
        self.last_error: str | None = None
        self._offset_seconds: int | None = None
        self._offset_source = "unknown"
        self._last_max_tick: int | None = None
        self._watch_symbols: list[str] = []
        self._watch_refreshed = 0.0
        self._fixed_offset = self._parse_fixed_offset(server_utc_offset)
        if self._fixed_offset is not None:
            self._offset_seconds = self._fixed_offset
            self._offset_source = "configured"

    @staticmethod
    def _parse_fixed_offset(setting: str) -> int | None:
        if setting.lower() == "auto":
            return None
        try:
            hours = float(setting)
        except ValueError:
            log.warning("Invalid MT5_SERVER_UTC_OFFSET %r, using auto-detection", setting)
            return None
        return int(round(hours * 3600))

    # -- connection -----------------------------------------------------------------
    def _mt5_error(self, action: str) -> str:
        code, message = mt5.last_error() if mt5 else (None, _IMPORT_ERROR)
        return f"{action} failed (MT5 error {code}: {message})"

    def connect(self) -> bool:
        if mt5 is None:
            self.last_error = (
                "The MetaTrader5 Python package is not installed or failed to load "
                f"({_IMPORT_ERROR}). Run: pip install -r requirements.txt"
            )
            return False
        kwargs = {"path": self.terminal_path} if self.terminal_path else {}
        try:
            ok = mt5.initialize(**kwargs)
        except Exception as exc:  # defensive: package raised instead of returning False
            ok = False
            self.last_error = f"MT5 initialize raised {type(exc).__name__}: {exc}"
        if not ok:
            if not self.last_error or "raised" not in self.last_error:
                self.last_error = self._mt5_error("Connecting to the MT5 terminal") + (
                    ". Make sure MetaTrader 5 desktop is installed and running"
                    + ("" if self.terminal_path else ", or set MT5_PATH in .env")
                    + "."
                )
            self.connected = False
            return False
        if mt5.account_info() is None:
            self.last_error = (
                "MT5 terminal found, but no trading account is logged in. "
                "Log in to your account inside MetaTrader 5 (File > Login to Trade Account)."
            )
            mt5.shutdown()
            self.connected = False
            return False
        self.connected = True
        self.last_error = None
        info = mt5.terminal_info()
        version = mt5.version()
        log.info("Connected to MT5 terminal (build %s): %s [%s]", version[1] if version else "?",
                 getattr(info, "company", "?"), getattr(info, "path", "?"))
        if not self.terminal_path:
            log.info("Tip: if several MT5 terminals are installed, set MT5_PATH in .env to pick one reliably.")
        if info is not None and not info.connected:
            log.warning("MT5 terminal is running but not connected to the broker server")
        return True

    def shutdown(self) -> None:
        if mt5 is not None and self.connected:
            try:
                mt5.shutdown()
            except Exception:  # pragma: no cover
                pass
        self.connected = False

    def _lost(self, action: str) -> ConnectorError:
        self.last_error = self._mt5_error(action)
        self.shutdown()
        return ConnectorError(self.last_error)

    def terminal_status(self) -> dict[str, Any]:
        info = mt5.terminal_info() if self.connected else None
        if info is None:
            return {"terminal_connected": False}
        return {
            "terminal_connected": bool(info.connected),
            "terminal_name": info.name,
            "terminal_company": info.company,
        }

    # -- data -----------------------------------------------------------------------
    def fetch_account(self) -> dict[str, Any]:
        info = mt5.account_info()
        if info is None:
            raise self._lost("Reading account info")
        account = _pick(info, ACCOUNT_FIELDS)
        account["trade_mode_name"] = TRADE_MODES.get(account.get("trade_mode"), "Unknown")
        return account

    def fetch_positions(self) -> list[dict[str, Any]]:
        positions = mt5.positions_get()
        if positions is None:
            code, _ = mt5.last_error()
            if code == 1:  # RES_S_OK with no data
                return []
            raise self._lost("Reading open positions")
        return [_pick(p, POSITION_FIELDS) for p in positions]

    def fetch_deals(self, from_server_ts: int) -> list[dict[str, Any]]:
        date_from = datetime.fromtimestamp(max(from_server_ts, 0), tz=timezone.utc)
        # Server time can be ahead of UTC, so query a little into the "future".
        date_to = datetime.now(timezone.utc) + timedelta(days=2)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            code, _ = mt5.last_error()
            if code == 1:
                return []
            raise self._lost("Reading deal history")
        return [_pick(d, DEAL_FIELDS) for d in deals]

    # -- server time offset ----------------------------------------------------------
    def server_offset(self, extra_symbols: list[str] | None = None) -> tuple[int | None, str]:
        """Return (offset_seconds, source). offset = server_time - utc_time."""
        if self._fixed_offset is not None:
            return self._fixed_offset, "configured"
        try:
            self._detect_offset(extra_symbols or [])
        except Exception as exc:  # detection must never break the sync
            log.debug("Server offset detection failed: %s", exc)
        return self._offset_seconds, self._offset_source

    def _detect_offset(self, extra_symbols: list[str]) -> None:
        now = time.time()
        if now - self._watch_refreshed > 600:
            symbols = mt5.symbols_get() or ()
            self._watch_symbols = [s.name for s in symbols if getattr(s, "visible", False)][:60]
            self._watch_refreshed = now
        max_tick = 0
        for name in dict.fromkeys(extra_symbols + self._watch_symbols):
            tick = mt5.symbol_info_tick(name)
            if tick is not None and tick.time > max_tick:
                max_tick = int(tick.time)
        if not max_tick:
            return
        previous, self._last_max_tick = self._last_max_tick, max_tick
        # Only trust the estimate while ticks are actually arriving (market open);
        # a stale weekend tick would produce a wrong offset.
        if previous is None or max_tick <= previous:
            return
        diff = max_tick - time.time()
        rounded = int(round(diff / 900.0) * 900)
        if abs(diff - rounded) <= 90 and abs(rounded) <= 14 * 3600:
            if rounded != self._offset_seconds:
                log.info("Detected MT5 server time offset: UTC%+.2fh", rounded / 3600)
            self._offset_seconds = rounded
            self._offset_source = "auto-detected"

    def set_known_offset(self, seconds: int | None) -> None:
        """Restore a previously detected offset (persisted in the database)."""
        if self._fixed_offset is None and seconds is not None and self._offset_seconds is None:
            self._offset_seconds = seconds
            self._offset_source = "auto-detected (stored)"


class DemoConnector:
    """Synthetic data for UI development. Always labelled DEMO; never used as a fallback."""

    mode = "demo"

    SYMBOLS = {
        # symbol: (start price, contract size, pip-ish volatility, digits)
        "EURUSD": (1.0850, 100_000, 0.00015, 5),
        "XAUUSD": (2350.0, 100, 0.6, 2),
        "GBPUSD": (1.2700, 100_000, 0.00018, 5),
        "US30": (39000.0, 1, 8.0, 1),
    }

    def __init__(self, seed: int = 42):
        self.connected = False
        self.last_error: str | None = None
        self._rng = random.Random(seed)
        self._prices = {s: v[0] for s, v in self.SYMBOLS.items()}
        self._deals: list[dict[str, Any]] = []
        self._positions: list[dict[str, Any]] = []
        self._balance = 0.0
        self._build_history()

    def connect(self) -> bool:
        self.connected = True
        return True

    def shutdown(self) -> None:
        self.connected = False

    def terminal_status(self) -> dict[str, Any]:
        return {"terminal_connected": True, "terminal_name": "DEMO terminal", "terminal_company": "DEMO"}

    def server_offset(self, extra_symbols: list[str] | None = None) -> tuple[int | None, str]:
        return 0, "demo"

    def set_known_offset(self, seconds: int | None) -> None:
        pass

    def _profit(self, symbol: str, direction: int, volume: float, open_p: float, close_p: float) -> float:
        contract = self.SYMBOLS[symbol][1]
        sign = 1 if direction == 0 else -1
        return round((close_p - open_p) * sign * volume * contract, 2)

    def _build_history(self) -> None:
        rng = self._rng
        now = int(time.time())
        t = now - 90 * 86400
        ticket = 5_000_000
        self._deals.append(dict(ticket=ticket, order=0, position_id=0, time=t, time_msc=t * 1000,
                                type=2, entry=0, symbol="", volume=0.0, price=0.0, profit=10_000.0,
                                commission=0.0, swap=0.0, fee=0.0, magic=0, reason=0))
        self._balance = 10_000.0
        position = 900_000
        while t < now - 3 * 3600:
            t += rng.randint(2 * 3600, 30 * 3600)
            if t > now - 4 * 3600:
                break
            symbol = rng.choice(list(self.SYMBOLS))
            base, _, vol, digits = self.SYMBOLS[symbol]
            direction = rng.randint(0, 1)
            volume = rng.choice([0.05, 0.1, 0.2, 0.3])
            open_p = round(base * (1 + rng.uniform(-0.03, 0.03)), digits)
            move = rng.gauss(0.08, 1.0) * vol * 40
            close_p = round(open_p + (move if direction == 0 else -move), digits)
            hold = rng.randint(15 * 60, 20 * 3600)
            commission = -round(volume * 3.5, 2)
            position += 1
            ticket += 1
            self._deals.append(dict(ticket=ticket, order=ticket, position_id=position, time=t,
                                    time_msc=t * 1000, type=direction, entry=0, symbol=symbol,
                                    volume=volume, price=open_p, profit=0.0, commission=commission,
                                    swap=0.0, fee=0.0, magic=0, reason=0))
            close_t = min(t + hold, now - 2 * 3600)
            profit = self._profit(symbol, direction, volume, open_p, close_p)
            swap = -round(volume * 2, 2) if hold > 12 * 3600 else 0.0
            ticket += 1
            self._deals.append(dict(ticket=ticket, order=ticket, position_id=position, time=close_t,
                                    time_msc=close_t * 1000, type=1 - direction, entry=1, symbol=symbol,
                                    volume=volume, price=close_p, profit=profit, commission=commission,
                                    swap=swap, fee=0.0, magic=0, reason=0))
            self._balance += profit + 2 * commission + swap
            t = close_t
        # Two open positions; their entry commissions are already in the balance.
        for i, symbol in enumerate(["EURUSD", "XAUUSD"]):
            base, _, _, digits = self.SYMBOLS[symbol]
            open_t = now - (i + 1) * 5400
            ticket += 1
            position += 1
            commission = -round(0.1 * 3.5, 2)
            self._deals.append(dict(ticket=ticket, order=ticket, position_id=position, time=open_t,
                                    time_msc=open_t * 1000, type=i % 2, entry=0, symbol=symbol,
                                    volume=0.1, price=base, profit=0.0, commission=commission,
                                    swap=0.0, fee=0.0, magic=0, reason=0))
            self._balance += commission
            self._positions.append(dict(ticket=position, symbol=symbol, type=i % 2, volume=0.1,
                                        price_open=base, price_current=base, profit=0.0, swap=0.0,
                                        sl=0.0, tp=0.0, time=open_t, time_msc=open_t * 1000, magic=0,
                                        identifier=position))
        self._balance = round(self._balance, 2)

    def _tick(self) -> None:
        for symbol, (_, _, vol, digits) in self.SYMBOLS.items():
            self._prices[symbol] = round(self._prices[symbol] + self._rng.gauss(0, vol), digits)
        for p in self._positions:
            p["price_current"] = self._prices[p["symbol"]]
            p["profit"] = self._profit(p["symbol"], p["type"], p["volume"], p["price_open"], p["price_current"])

    def fetch_account(self) -> dict[str, Any]:
        self._tick()
        floating = round(sum(p["profit"] + p["swap"] for p in self._positions), 2)
        margin = round(sum(p["volume"] * self.SYMBOLS[p["symbol"]][1] * p["price_current"] / 100
                           for p in self._positions), 2)
        equity = round(self._balance + floating, 2)
        return {
            "login": 12345678, "server": "DEMO-Server", "company": "DEMO data (not a real broker)",
            "currency": "USD", "balance": self._balance, "equity": equity, "profit": floating,
            "margin": margin, "margin_free": round(equity - margin, 2),
            "margin_level": round(equity / margin * 100, 2) if margin else 0.0,
            "leverage": 100, "trade_mode": 0, "trade_mode_name": "DEMO", "credit": 0.0,
            "currency_digits": 2,
        }

    def fetch_positions(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._positions]

    def fetch_deals(self, from_server_ts: int) -> list[dict[str, Any]]:
        return [dict(d) for d in self._deals if d["time"] >= from_server_ts]
