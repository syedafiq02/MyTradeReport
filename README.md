# MT5 Live Personal Dashboard

A local, **read-only** web dashboard for your own MetaTrader 5 account: live balance/equity,
open positions, closed-trade history, performance metrics and equity/balance charts.

```
MetaTrader 5 desktop  →  Python connector (MetaTrader5 pkg)  →  Flask API + SQLite  →  HTML/CSS/JS dashboard
```

Everything runs on your Windows PC at **http://127.0.0.1:5000**. No cloud, no Node.js, no Docker.

- Strictly read-only: there is no order placement, modification, closing or funding code.
- Never asks for or stores your MT5 trading password. It attaches to the terminal you are already logged into.
- Binds to `127.0.0.1` only, so other devices cannot reach it.

## Quick start

1. Open **MetaTrader 5** and log in to your account (File → Login to Trade Account).
2. Double-click **`run.bat`**.
   It checks for Python, creates `.venv`, installs requirements (only when they change),
   creates `.env` from `.env.example` on first run, starts the server and opens your browser.
3. To try the UI with synthetic data instead: `run.bat demo`. Everything is labelled **DEMO** and stored in a
   separate database (`data/demo.sqlite3`).

Stop the dashboard with `Ctrl+C` in its window. If it is already running, `run.bat` just opens the browser.

## Connecting your MT5 terminal

The connector calls `MetaTrader5.initialize()` and reuses the logged-in terminal session.

**If you have more than one MT5 terminal installed or running, set `MT5_PATH`.** Without it, Python attaches to
whichever terminal it finds first, and that can change between runs. In `.env`:

```
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
```

The server log prints which terminal it attached to, for example
`Connected to MT5 terminal (build 6257): <Broker> [<path>]`.

Requirements and tips:

- The terminal must be running and logged in. If it is not logged in, the dashboard shows
  "no trading account is logged in" and keeps retrying with backoff.
- History: in MT5's *History* tab, right-click → *All History* so that the terminal has downloaded old deals.
- If the terminal closes, the dashboard switches to **Offline**, keeps showing the last known values greyed
  out, and reconnects automatically. It never switches to demo data on its own.

### Server time

MT5 reports deal times in the broker's *server time*. The offset to UTC is auto-detected from live ticks while
the market is open, then stored. Until it is detected (for example on weekends at first start), times are treated
as UTC and the *Synchronization* panel says so. To fix it permanently, set `MT5_SERVER_UTC_OFFSET=3` (or 2, etc.)
in `.env`. All times in the UI are shown in Malaysia time (Asia/Kuala_Lumpur, UTC+8).

## Optional password gate

By default, anyone using your PC's browser can open the dashboard. To require a password (this is the dashboard
password, **not** your MT5 password):

```
.venv\Scripts\python app.py hash-password
```

Paste the printed `DASHBOARD_PASSWORD_HASH=...` line into `.env` and restart. Sessions use HttpOnly,
SameSite=Strict cookies. The login form has CSRF protection and locks for 60 seconds after 5 wrong attempts.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Bind address. Keep `127.0.0.1`. |
| `MT5_PATH` | empty (auto) | Path to `terminal64.exe` |
| `MT5_SERVER_UTC_OFFSET` | `auto` | Broker server offset in hours, or `auto` |
| `POLL_INTERVAL` | `5` | Seconds between account/position reads |
| `HISTORY_INTERVAL` | `60` | Seconds between deal-history syncs |
| `SNAPSHOT_INTERVAL` | `30` | Seconds between stored equity snapshots |
| `STALE_AFTER` | `30` | Data older than this is shown as **Stale** |
| `HISTORY_START_YEAR` | `2010` | Earliest year read on a full history sync |
| `DASHBOARD_PASSWORD_HASH` / `DASHBOARD_PASSWORD` | empty | Optional access gate |
| `DEMO_MODE` | `0` | `1` = synthetic DEMO data |

## Manual setup (without run.bat)

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python app.py --open          (or: app.py --demo --open)
```

Requires Python 3.10+ on 64-bit Windows. The `MetaTrader5` package is Windows-only.

## How the numbers are calculated

- **Closed trades**: one per *exit* deal (`DEAL_ENTRY_OUT`, `OUT_BY`, or the closing part of an `INOUT` reversal),
  as in the MT5 report. Entry price (volume-weighted) and holding time come from the position's entry deals,
  matched by `position_id`. Partial closes and reversals are handled. If an exit has no entry deal in the synced
  history, it is labelled **unpaired** and excluded from average holding time.
- **Net per trade** = profit + commission + swap + fee, with entry-side commission allocated to exits by volume.
- **Net trading profit**: all buy/sell deal results, including entry commissions of still-open positions.
- **Balance chart**: rebuilt from every deal. If the sum of deals does not equal your current balance (older history
  unavailable), the curve is anchored to the current balance and dependent metrics are tagged **approx**.
- **Max drawdown / growth**: computed on the balance history with deposits and withdrawals neutralised.
  Growth is time-weighted.
- **Equity chart / equity drawdown**: only from snapshots taken while this app runs. No earlier equity history is
  shown or invented.
- Metrics that cannot be calculated show **Not available** with a reason.

## Project layout

```
app.py             Flask app, auth, API routes, CLI (run / hash-password)
config.py          .env loading and settings
mt5_connector.py   Read-only MT5 access + DemoConnector
sync_service.py    Background worker (single thread → no overlapping jobs)
database.py        SQLite: snapshots, de-duplicated deals, metadata
performance.py     Trade pairing and metrics (pure functions)
templates/         index.html, login.html
static/            css/style.css, js/dashboard.js (Canvas charts, no libraries)
tests/             unittest suite
data/              runtime SQLite databases (git-ignored)
```

API (GET only): `/api/status`, `/api/live`, `/api/performance`, `/api/trades?limit=&offset=`,
`/api/charts?range=24h|7d|30d|90d|all`.

## Tests

```
.venv\Scripts\python -m unittest discover -s tests -v
```

The tests cover trade pairing (partials, reversals, unpaired exits), metrics, de-duplication, the API, input
validation, the password gate, Host-header checks, MT5-unavailable and disconnect handling (offline, never mock),
and a check that the source contains no trading calls.

## Security notes

- Keep `HOST=127.0.0.1`. Do not port-forward or expose this server. It uses Flask's built-in server, intended for
  local use only.
- Requests with a foreign `Host` header are rejected (DNS-rebinding protection). No CORS is enabled. A strict CSP
  is sent.
- Your full login number is never sent to the browser (it is shown masked, like `•••••678`). The database stores
  only a hash of login@server.
- `.env`, `.venv/` and `data/` (databases, generated secret key) are git-ignored.
