"""Application configuration loaded from environment variables and an optional .env file.

No third-party dependency is used for .env parsing to keep requirements minimal.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE parser. Existing environment variables take precedence."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _get_int(name: str, default: int, minimum: int, maximum: int) -> int:
    return int(_get_float(name, default, minimum, maximum))


def _secret_key() -> str:
    """Use SECRET_KEY from env, else a random key persisted in data/ (never in source)."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / ".secret_key"
    if key_file.is_file():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    return key


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    demo_mode: bool
    mt5_path: str | None
    server_utc_offset: str  # "auto" or a number of hours, e.g. "3" or "2.5"
    poll_interval: float
    history_interval: float
    snapshot_interval: float
    stale_after: float
    history_start_year: int
    password: str
    password_hash: str
    secret_key: str
    db_path: Path
    open_browser: bool

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password or self.password_hash)


def load_config() -> Config:
    _load_dotenv(BASE_DIR / ".env")
    demo = _get_bool("DEMO_MODE", False)
    # DEMO data goes into a separate database file so it can never mix with real records.
    db_name = "demo.sqlite3" if demo else "mt5_dashboard.sqlite3"
    host = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    mt5_path = os.environ.get("MT5_PATH", "").strip() or None
    poll = _get_float("POLL_INTERVAL", 5, 2, 120)
    return Config(
        host=host,
        port=_get_int("PORT", 5000, 1, 65535),
        demo_mode=demo,
        mt5_path=mt5_path,
        server_utc_offset=os.environ.get("MT5_SERVER_UTC_OFFSET", "auto").strip() or "auto",
        poll_interval=poll,
        history_interval=_get_float("HISTORY_INTERVAL", 60, 10, 3600),
        snapshot_interval=_get_float("SNAPSHOT_INTERVAL", 30, 5, 3600),
        stale_after=_get_float("STALE_AFTER", max(30, poll * 4), poll * 2, 3600),
        history_start_year=_get_int("HISTORY_START_YEAR", 2010, 1990, 2100),
        password=os.environ.get("DASHBOARD_PASSWORD", ""),
        password_hash=os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip(),
        secret_key=_secret_key(),
        db_path=DATA_DIR / db_name,
        open_browser=_get_bool("OPEN_BROWSER", False),
    )
