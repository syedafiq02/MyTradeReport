"""Tests for calculations, persistence, API behaviour and failure handling.

Run:  .venv\\Scripts\\python -m unittest discover -s tests -v
No real MT5 account is needed (DEMO and fake connectors are used).
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, mask_login  # noqa: E402
from config import load_config  # noqa: E402
from database import Database  # noqa: E402
from mt5_connector import ConnectorError, DemoConnector, MT5Connector  # noqa: E402
from performance import balance_curve, build_trades, compute_performance  # noqa: E402
from sync_service import SyncService  # noqa: E402

T0 = 1_700_000_000


def deal(ticket, t, type_, entry=0, position=0, volume=0.0, price=0.0, profit=0.0,
         commission=0.0, swap=0.0, fee=0.0, symbol="EURUSD"):
    return dict(ticket=ticket, order=ticket, position_id=position, time=t, time_server=t,
                time_msc=t * 1000, type=type_, entry=entry, symbol=symbol, volume=volume,
                price=price, profit=profit, commission=commission, swap=swap, fee=fee,
                magic=0, reason=0)


def sample_deals():
    return [
        deal(1, T0, 2, profit=1000.0, symbol=""),                                   # deposit
        # Position 10: buy 1.0, closed in two partial exits (win)
        deal(2, T0 + 100, 0, 0, 10, 1.0, 1.1000, commission=-7.0),
        deal(3, T0 + 700, 1, 1, 10, 0.4, 1.1050, profit=200.0, commission=-2.8),
        deal(4, T0 + 1300, 1, 1, 10, 0.6, 1.1020, profit=120.0, commission=-4.2),
        # Position 11: sell 0.5 (loss)
        deal(5, T0 + 2000, 1, 0, 11, 0.5, 1.1000, commission=-3.5),
        deal(6, T0 + 5600, 0, 1, 11, 0.5, 1.1100, profit=-500.0, commission=-3.5, swap=-1.0),
        # Position 12: exit with no entry in history (unpaired)
        deal(7, T0 + 6000, 1, 1, 12, 0.1, 1.2000, profit=50.0),
        # Withdrawal
        deal(8, T0 + 7000, 2, profit=-100.0, symbol=""),
    ]


class PerformanceTests(unittest.TestCase):
    def test_trades_pairing_and_partials(self):
        trades = build_trades(sample_deals())
        self.assertEqual(len(trades), 4)
        first, second, loss, unpaired = trades
        self.assertEqual(first["direction"], "buy")
        self.assertAlmostEqual(first["entry_price"], 1.1)
        self.assertEqual(first["holding_seconds"], 600)
        # entry commission -7.0 allocated 40% / 60%
        self.assertAlmostEqual(first["net"], 200.0 - 2.8 - 2.8)
        self.assertAlmostEqual(second["net"], 120.0 - 4.2 - 4.2)
        self.assertEqual(loss["direction"], "sell")
        self.assertAlmostEqual(loss["net"], -500.0 - 3.5 - 1.0 - 3.5)
        self.assertEqual(loss["holding_seconds"], 3600)
        self.assertFalse(unpaired["paired"])
        self.assertIsNone(unpaired["entry_price"])
        self.assertEqual(unpaired["direction"], "buy")  # sell exit closes a buy

    def test_reversal_deal(self):
        deals = [
            deal(1, T0, 0, 0, 20, 1.0, 1.0),
            deal(2, T0 + 60, 1, 2, 20, 1.5, 1.01, profit=100.0),   # INOUT: close 1.0, open sell 0.5
            deal(3, T0 + 120, 0, 1, 20, 0.5, 1.00, profit=50.0),
        ]
        trades = build_trades(deals)
        self.assertEqual([t["direction"] for t in trades], ["buy", "sell"])
        self.assertEqual([t["volume"] for t in trades], [1.0, 0.5])
        self.assertTrue(all(t["paired"] for t in trades))
        self.assertAlmostEqual(trades[1]["entry_price"], 1.01)

    def test_metrics(self):
        deals = sample_deals()
        net_all = sum(d["profit"] + d["commission"] + d["swap"] for d in deals)
        account = {"balance": round(net_all, 2)}
        perf = compute_performance(deals, account, [])
        m = perf["metrics"]
        self.assertTrue(perf["history_complete"])
        self.assertEqual(m["total_trades"]["value"], 4)
        self.assertEqual(m["winning_trades"]["value"], 3)
        self.assertEqual(m["losing_trades"]["value"], 1)
        self.assertEqual(m["win_rate"]["value"], 75.0)
        self.assertAlmostEqual(m["gross_profit"]["value"], 194.4 + 111.6 + 50.0)
        self.assertAlmostEqual(m["gross_loss"]["value"], -508.0)
        self.assertAlmostEqual(m["profit_factor"]["value"], round(356.0 / 508.0, 2))
        self.assertAlmostEqual(m["net_profit"]["value"], -152.0)
        self.assertAlmostEqual(m["deposits"]["value"], 1000.0)
        self.assertAlmostEqual(m["withdrawals"]["value"], -100.0)
        self.assertEqual(m["avg_holding_seconds"]["status"], "approx")  # one unpaired trade
        # Peak 1000 -> after entry commissions/wins peak 1302.6 ... drawdown from peak > 0
        self.assertGreater(m["max_drawdown"]["value"], 0)
        self.assertIsNotNone(m["recovery_factor"]["value"])
        self.assertLess(m["growth_pct"]["value"], 0)

    def test_withdrawal_is_not_drawdown(self):
        deals = [deal(1, T0, 2, profit=1000.0), deal(2, T0 + 10, 2, profit=-500.0)]
        perf = compute_performance(deals, {"balance": 500.0}, [])
        self.assertEqual(perf["metrics"]["max_drawdown"]["value"], 0)
        self.assertEqual(perf["metrics"]["total_trades"]["value"], 0)
        self.assertEqual(perf["metrics"]["profit_factor"]["status"], "unavailable")

    def test_incomplete_history_is_anchored_and_flagged(self):
        deals = [deal(1, T0, 0, 0, 5, 1.0, 1.0), deal(2, T0 + 60, 1, 1, 5, 1.0, 1.1, profit=100.0)]
        curve = balance_curve(deals, current_balance=5100.0)
        self.assertFalse(curve["complete"])
        self.assertEqual(curve["points"][-1]["balance"], 5100.0)
        perf = compute_performance(deals, {"balance": 5100.0}, [])
        self.assertEqual(perf["metrics"]["max_drawdown"]["status"], "approx")

    def test_empty(self):
        perf = compute_performance([], None, [])
        self.assertEqual(perf["metrics"]["total_trades"]["value"], 0)
        self.assertEqual(perf["metrics"]["win_rate"]["status"], "unavailable")
        self.assertEqual(perf["metrics"]["net_profit"]["status"], "unavailable")

    def test_server_offset_applied(self):
        trades = build_trades(sample_deals(), offset_seconds=3 * 3600)
        self.assertEqual(trades[0]["close_time"], T0 + 700 - 3 * 3600)


class DatabaseTests(unittest.TestCase):
    def test_upsert_deduplicates(self):
        db = Database(":memory:")
        self.assertEqual(db.upsert_deals("acct", sample_deals()), 8)
        self.assertEqual(db.upsert_deals("acct", sample_deals()), 0)
        self.assertEqual(len(db.get_deals("acct")), 8)
        self.assertEqual(db.upsert_deals("other", sample_deals()[:2]), 2)
        self.assertEqual(len(db.get_deals("acct")), 8)
        db.add_snapshot("acct", T0, 1000, 1010, 5, 1005, 10)
        self.assertEqual(db.get_snapshots("acct")[0]["equity"], 1010)
        self.assertEqual(db.first_snapshot_time("acct"), T0)
        db.set_meta("k", "v")
        self.assertEqual(db.get_meta("k"), "v")


class FailingConnector:
    """Simulates an unavailable MT5 terminal."""
    mode = "live"

    def __init__(self):
        self.connected = False
        self.last_error = None

    def connect(self):
        self.last_error = "MT5 error -10003: IPC initialize failed"
        return False

    def shutdown(self):
        pass

    def set_known_offset(self, s):
        pass


class FlakyConnector(DemoConnector):
    """Connects, then loses the terminal on the next account read."""
    mode = "live"

    def __init__(self):
        super().__init__()
        self.fail = False

    def fetch_account(self):
        if self.fail:
            self.connected = False
            self.last_error = "terminal closed"
            raise ConnectorError("Reading account info failed (MT5 error -10004: No IPC connection)")
        return super().fetch_account()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = load_config()
        self.config = dataclasses.replace(base, db_path=Path(self.tmp.name) / "t.sqlite3",
                                          demo_mode=True, password="", password_hash="")

    def tearDown(self):
        for app in getattr(self, "_apps", []):
            app.extensions["mt5_db"].close()
        self.tmp.cleanup()

    def make_app(self, **overrides):
        app = create_app(dataclasses.replace(self.config, **overrides), start_sync=False)
        app.testing = True
        self._apps = getattr(self, "_apps", []) + [app]
        return app

    def test_demo_flow(self):
        app = self.make_app()
        app.extensions["mt5_sync"].run_once()
        c = app.test_client()
        self.assertEqual(c.get("/").status_code, 200)
        live = c.get("/api/live").get_json()
        self.assertEqual(live["status"]["mode"], "demo")
        self.assertEqual(live["status"]["state"], "live")
        self.assertEqual(live["account"]["login_masked"], mask_login(12345678))
        self.assertEqual(len(live["positions"]), 2)
        perf = c.get("/api/performance").get_json()
        self.assertTrue(perf["available"])
        self.assertTrue(perf["history_complete"], "demo history should reconcile with balance")
        self.assertGreater(perf["metrics"]["total_trades"]["value"], 10)
        trades = c.get("/api/trades?limit=5").get_json()
        self.assertEqual(len(trades["trades"]), 5)
        self.assertGreater(trades["total"], 10)
        charts = c.get("/api/charts?range=all").get_json()
        self.assertEqual(len(charts["equity"]), 1)
        self.assertGreater(len(charts["balance"]), 10)
        # Full login must never appear in any API response.
        for url in ("/api/live", "/api/performance", "/api/trades", "/api/charts?range=all", "/api/status"):
            self.assertNotIn("12345678", c.get(url).get_data(as_text=True), url)
        # Repeated sync must not duplicate deals.
        sync = app.extensions["mt5_sync"]
        sync._next_history = 0
        sync.run_once()
        self.assertEqual(c.get("/api/trades").get_json()["total"], trades["total"])

    def test_validation(self):
        c = self.make_app().test_client()
        self.assertEqual(c.get("/api/trades?limit=0").status_code, 400)
        self.assertEqual(c.get("/api/trades?limit=abc").status_code, 400)
        self.assertEqual(c.get("/api/trades?offset=-1").status_code, 400)
        self.assertEqual(c.get("/api/charts?range=1y").status_code, 400)
        self.assertEqual(c.post("/api/live").status_code, 405)

    def test_empty_state_before_first_sync(self):
        c = self.make_app().test_client()
        live = c.get("/api/live").get_json()
        self.assertEqual(live["status"]["state"], "offline")
        self.assertIsNone(live["account"])
        self.assertFalse(c.get("/api/performance").get_json()["available"])
        charts = c.get("/api/charts").get_json()
        self.assertEqual(charts["equity"], [])
        self.assertEqual(c.get("/api/trades").get_json()["total"], 0)

    def test_mt5_unavailable_is_offline_without_mock_fallback(self):
        cfg = dataclasses.replace(self.config, demo_mode=False)
        db = Database(Path(self.tmp.name) / "live.sqlite3")
        sync = SyncService(cfg, FailingConnector(), db)
        sync.run_once()
        status = sync.status()
        self.assertEqual(status["state"], "offline")
        self.assertEqual(status["mode"], "live")
        self.assertIn("IPC", status["error"])
        self.assertIsNone(sync.snapshot_state()["account"])
        db.close()

    def test_disconnect_marks_offline(self):
        cfg = dataclasses.replace(self.config, demo_mode=False)
        db = Database(Path(self.tmp.name) / "flaky.sqlite3")
        conn = FlakyConnector()
        sync = SyncService(cfg, conn, db)
        sync.run_once()
        self.assertEqual(sync.status()["state"], "live")
        conn.fail = True
        sync.run_once()
        self.assertEqual(sync.status()["state"], "offline")
        self.assertIsNotNone(sync.snapshot_state()["account"])  # last known values kept, flagged offline
        db.close()

    def test_real_connector_bad_path_fails_gracefully(self):
        conn = MT5Connector(terminal_path=str(Path(self.tmp.name) / "missing" / "terminal64.exe"))
        self.assertFalse(conn.connect())
        self.assertTrue(conn.last_error)

    def test_password_gate(self):
        app = self.make_app(password="correct horse")
        c = app.test_client()
        self.assertEqual(c.get("/api/live").status_code, 401)
        self.assertEqual(c.get("/").status_code, 302)
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.assertEqual(c.post("/login", data={"password": "x", "csrf_token": "bad"}).status_code, 400)
        self.assertEqual(c.post("/login", data={"password": "wrong", "csrf_token": token}).status_code, 401)
        r = c.post("/login", data={"password": "correct horse", "csrf_token": token})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/api/live").status_code, 200)

    def test_host_header_check_and_headers(self):
        app = self.make_app()
        app.testing = False
        c = app.test_client()
        self.assertEqual(c.get("/api/status", headers={"Host": "evil.example:5000"}).status_code, 400)
        r = c.get("/api/status", base_url=f"http://127.0.0.1:{self.config.port}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)


class ReadOnlyTests(unittest.TestCase):
    def test_no_trading_calls_in_source(self):
        forbidden = re.compile(r"order_send|order_check|order_calc|positions_close|Close\(")
        for path in ROOT.glob("*.py"):
            self.assertIsNone(forbidden.search(path.read_text(encoding="utf-8")), path.name)

    def test_default_bind_is_localhost(self):
        self.assertEqual(load_config().host, "127.0.0.1")
        self.assertIn('"HOST", "127.0.0.1"', (ROOT / "config.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
