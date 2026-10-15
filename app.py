"""MT5 Live Personal Dashboard - local Flask backend.

Strictly read-only. Binds to 127.0.0.1 by default. Run:  python app.py  [--demo] [--open]
Generate a password hash for .env:  python app.py hash-password
"""
from __future__ import annotations

import argparse
import atexit
import getpass
import hmac
import logging
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from functools import wraps
from typing import Any, Callable

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config, load_config
from database import Database
from mt5_connector import DemoConnector, MT5Connector
from performance import downsample
from sync_service import SyncService

log = logging.getLogger("app")

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400, "all": None}
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 60


def mask_login(login: Any) -> str:
    text = str(login or "")
    if len(text) <= 3:
        return "•" * len(text)
    return "•" * min(len(text) - 3, 5) + text[-3:]


def mask_ticket(ticket: Any) -> str:
    text = str(ticket or "")
    return "…" + text[-4:] if len(text) > 4 else text


def create_app(config: Config | None = None, start_sync: bool = True) -> Flask:
    config = config or load_config()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="mt5dash_session",
        PERMANENT_SESSION_LIFETIME=12 * 3600,
        JSON_SORT_KEYS=False,
    )

    db = Database(config.db_path)
    connector = DemoConnector() if config.demo_mode else MT5Connector(config.mt5_path, config.server_utc_offset)
    sync = SyncService(config, connector, db)
    app.extensions["mt5_sync"] = sync
    app.extensions["mt5_db"] = db
    if start_sync:
        sync.start()
        atexit.register(sync.stop)

    allowed_hosts = {f"127.0.0.1:{config.port}", f"localhost:{config.port}", f"{config.host}:{config.port}"}
    if config.port == 80:
        allowed_hosts |= {"127.0.0.1", "localhost", config.host}
    login_failures = {"count": 0, "locked_until": 0.0}
    failures_lock = threading.Lock()

    # -- security hooks -------------------------------------------------------------
    @app.before_request
    def check_host() -> None:
        # Blocks DNS-rebinding style access from other sites via a foreign Host header.
        if not app.testing and request.host not in allowed_hosts:
            abort(400)

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def is_authenticated() -> bool:
        return not config.auth_enabled or session.get("auth") is True

    def login_required(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args, **kwargs):
            if is_authenticated():
                return view(*args, **kwargs)
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login"))
        return wrapper

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def check_csrf() -> None:
        sent = request.form.get("csrf_token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400)

    def password_ok(candidate: str) -> bool:
        if config.password_hash:
            try:
                return check_password_hash(config.password_hash, candidate)
            except ValueError:
                log.error("DASHBOARD_PASSWORD_HASH in .env is not a valid hash")
                return False
        return hmac.compare_digest(candidate.encode("utf-8"), config.password.encode("utf-8"))

    # -- pages ----------------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.auth_enabled:
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            check_csrf()
            with failures_lock:
                locked = time.time() < login_failures["locked_until"]
            if locked:
                error = "Too many attempts. Please wait a minute and try again."
            elif password_ok(request.form.get("password", "")[:256]):
                session.clear()
                session["auth"] = True
                session.permanent = True
                with failures_lock:
                    login_failures["count"] = 0
                return redirect(url_for("index"))
            else:
                time.sleep(0.5)
                with failures_lock:
                    login_failures["count"] += 1
                    if login_failures["count"] >= MAX_LOGIN_FAILURES:
                        login_failures["locked_until"] = time.time() + LOCKOUT_SECONDS
                        login_failures["count"] = 0
                error = "Incorrect password."
        return render_template("login.html", csrf_token=csrf_token(), error=error,
                               demo=config.demo_mode), (401 if error else 200)

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login" if config.auth_enabled else "index"))

    @app.get("/")
    @login_required
    def index():
        return render_template("index.html", csrf_token=csrf_token(), demo=config.demo_mode,
                               auth_enabled=config.auth_enabled,
                               poll_ms=int(config.poll_interval * 1000),
                               history_ms=int(config.history_interval * 1000))

    # -- API (GET only, read-only) --------------------------------------------------
    def query_int(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = request.args.get(name)
        if raw is None or raw == "":
            return default
        if not raw.isdigit() or not (minimum <= int(raw) <= maximum):
            abort(400, description=f"Invalid '{name}' parameter")
        return int(raw)

    @app.errorhandler(400)
    def bad_request(err):
        if request.path.startswith("/api/"):
            return jsonify({"error": getattr(err, "description", "bad request")}), 400
        return "Bad request", 400

    @app.get("/api/status")
    @login_required
    def api_status():
        return jsonify(sync.status())

    @app.get("/api/live")
    @login_required
    def api_live():
        state = sync.snapshot_state()
        account = state["account"]
        offset = state["offset"]
        out_account = None
        if account:
            out_account = {
                "login_masked": mask_login(account.get("login")),
                "server": account.get("server"),
                "company": account.get("company"),
                "currency": account.get("currency"),
                "currency_digits": account.get("currency_digits"),
                "balance": account.get("balance"),
                "equity": account.get("equity"),
                "floating": account.get("profit"),
                "margin": account.get("margin"),
                "free_margin": account.get("margin_free"),
                "margin_level": account.get("margin_level"),
                "leverage": account.get("leverage"),
                "credit": account.get("credit"),
                "account_type": account.get("trade_mode_name"),
            }
        positions = [{
            "ticket": mask_ticket(p.get("ticket")),
            "symbol": p.get("symbol"),
            "direction": "buy" if p.get("type") == 0 else "sell",
            "volume": p.get("volume"),
            "entry_price": p.get("price_open"),
            "current_price": p.get("price_current"),
            "sl": p.get("sl") or None,
            "tp": p.get("tp") or None,
            "swap": p.get("swap"),
            "profit": p.get("profit"),
            "open_time_utc": ((p.get("time_msc") or (p.get("time") or 0) * 1000) / 1000.0) - offset,
        } for p in state["positions"]]
        return jsonify({"status": sync.status(), "account": out_account, "positions": positions})

    @app.get("/api/performance")
    @login_required
    def api_performance():
        perf = sync.snapshot_state()["performance"]
        if not perf:
            return jsonify({"available": False, "metrics": None, "history_complete": None})
        return jsonify({"available": True, "metrics": perf["metrics"],
                        "history_complete": perf["history_complete"],
                        "balance_shift": perf["balance_curve"]["shift"]})

    @app.get("/api/trades")
    @login_required
    def api_trades():
        limit = query_int("limit", 50, 1, 500)
        offset = query_int("offset", 0, 0, 10_000_000)
        perf = sync.snapshot_state()["performance"]
        trades = list(reversed(perf["trades"])) if perf else []
        page = trades[offset:offset + limit]
        return jsonify({
            "total": len(trades),
            "limit": limit,
            "offset": offset,
            "trades": [{**t, "ticket": mask_ticket(t["ticket"]), "position_id": None} for t in page],
        })

    @app.get("/api/charts")
    @login_required
    def api_charts():
        rng = request.args.get("range", "7d")
        if rng not in RANGES:
            abort(400, description="Invalid 'range' parameter")
        state = sync.snapshot_state()
        span = RANGES[rng]
        since = int(time.time() - span) if span else 0
        equity: list[dict[str, Any]] = []
        first_snapshot = None
        if state["account_key"]:
            snaps = db.get_snapshots(state["account_key"], since)
            equity = [{"t": s["ts_utc"], "equity": s["equity"], "balance": s["balance"]} for s in snaps]
            first_snapshot = db.first_snapshot_time(state["account_key"])
        curve = state["performance"]["balance_curve"] if state["performance"] else {"points": [], "complete": None}
        balance = [{"t": p["t"], "balance": p["balance"]} for p in curve["points"] if p["t"] >= since]
        earlier = [p for p in curve["points"] if p["t"] < since]
        if since and earlier:
            # Start the step line with the balance that was in effect when the range begins.
            balance.insert(0, {"t": since, "balance": earlier[-1]["balance"]})
        return jsonify({
            "range": rng,
            "equity": downsample(equity),
            "first_snapshot_utc": first_snapshot,
            "balance": downsample(balance),
            "balance_complete": curve["complete"],
        })

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MT5 Live Personal Dashboard (read-only, local)")
    parser.add_argument("command", nargs="?", choices=["run", "hash-password"], default="run")
    parser.add_argument("--demo", action="store_true", help="use clearly-labelled synthetic DEMO data")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        pw = getpass.getpass("Dashboard password (not your MT5 password): ")
        if len(pw) < 8:
            print("Please use at least 8 characters.")
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match.")
            return 1
        print("\nAdd this line to your .env file:\n")
        print(f"DASHBOARD_PASSWORD_HASH={generate_password_hash(pw)}")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.demo:
        os.environ["DEMO_MODE"] = "1"
    config = load_config()
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        log.warning("HOST is set to %s - the dashboard may be reachable from other machines!", config.host)
    if not config.auth_enabled:
        log.info("No DASHBOARD_PASSWORD set - access gate disabled (OK for a single-user PC).")
    url = f"http://127.0.0.1:{config.port}" if config.host in {"127.0.0.1", "0.0.0.0"} else \
        f"http://{config.host}:{config.port}"
    # On Windows a second server could silently bind the same port, so refuse to start twice.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1" if config.host == "0.0.0.0" else config.host, config.port)) == 0:
            log.error("Port %s is already in use - the dashboard (or another program) is already running. "
                      "Open %s or stop the other instance first.", config.port, url)
            if args.open or config.open_browser:
                webbrowser.open(url)
            return 1
    app = create_app(config)
    log.info("Dashboard: %s  (mode: %s)", url, "DEMO" if config.demo_mode else "LIVE MT5")
    if args.open or config.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    # load_dotenv=False: .env is already handled by config.py (no python-dotenv needed).
    app.run(host=config.host, port=config.port, debug=False, use_reloader=False, threaded=True,
            load_dotenv=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
