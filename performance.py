"""Pure functions that turn raw MT5 deals into closed trades and performance metrics.

MT5 deal model (not one-deal-per-trade):
  * Each position has entry deals (DEAL_ENTRY_IN) and exit deals (DEAL_ENTRY_OUT,
    DEAL_ENTRY_OUT_BY), linked by position_id. Partial closes produce several exits.
  * DEAL_ENTRY_INOUT is a reversal: it closes the existing position and opens the
    opposite direction in one deal.
  * Balance operations (deposits / withdrawals), credit, bonuses, charges and
    commissions can appear as non-trade deals.

A "closed trade" here = one exit deal (same convention as the MT5 report). Its entry
price and holding time are recovered from the position's entry deals when those are
present in the synced history; otherwise the trade is labelled as not paired.
Entry-side commission/fee is allocated to exits pro-rata by volume.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

# ENUM_DEAL_TYPE
DEAL_BUY, DEAL_SELL, DEAL_BALANCE, DEAL_CREDIT, DEAL_CHARGE, DEAL_CORRECTION, DEAL_BONUS = range(7)
EXTERNAL_FLOW_TYPES = {DEAL_BALANCE, DEAL_BONUS}       # deposits, withdrawals, bonuses
NO_BALANCE_EFFECT_TYPES = {DEAL_CREDIT}                # credit is tracked separately by MT5
TRADE_TYPES = {DEAL_BUY, DEAL_SELL}
# ENUM_DEAL_ENTRY
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
EXIT_ENTRIES = {ENTRY_OUT, ENTRY_OUT_BY}

VOLUME_EPS = 1e-6


def metric(value: Any, status: str = "ok", note: str | None = None) -> dict[str, Any]:
    """status: 'ok' (calculated), 'approx' (calculated from incomplete data), 'unavailable'."""
    if value is None and status != "unavailable":
        status = "unavailable"
    return {"value": value, "status": status, "note": note}


def deal_amount(d: dict[str, Any]) -> float:
    return float(d.get("profit") or 0) + float(d.get("commission") or 0) + \
        float(d.get("swap") or 0) + float(d.get("fee") or 0)


def deal_time_utc(d: dict[str, Any], offset_seconds: int) -> float:
    msc = d.get("time_msc")
    base = msc / 1000.0 if msc else float(d.get("time_server", d.get("time", 0)))
    return base - offset_seconds


def _sorted(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(deals, key=lambda d: (d.get("time_msc") or d.get("time_server", 0) * 1000, d["ticket"]))


class _PositionState:
    __slots__ = ("in_volume", "in_value", "in_time_value", "in_costs", "open_volume", "direction",
                 "first_in")

    def __init__(self) -> None:
        self.in_volume = 0.0       # total volume ever opened
        self.in_value = 0.0        # sum(price * volume) for entries
        self.in_time_value = 0.0   # sum(time * volume) for entries
        self.in_costs = 0.0        # entry commission + fee (+ any entry profit/swap)
        self.open_volume = 0.0     # currently open volume according to synced history
        self.direction: str | None = None
        self.first_in: float | None = None

    def add_entry(self, volume: float, price: float, t: float, costs: float, direction: str) -> None:
        if self.open_volume <= VOLUME_EPS:
            # Fresh (or reversed) position: restart averages.
            self.in_volume = self.in_value = self.in_time_value = self.in_costs = 0.0
            self.first_in = t
        self.direction = direction
        self.in_volume += volume
        self.in_value += price * volume
        self.in_time_value += t * volume
        self.in_costs += costs
        self.open_volume += volume


def build_trades(deals: list[dict[str, Any]], offset_seconds: int = 0) -> list[dict[str, Any]]:
    """Return closed trades (one per exit deal), oldest first."""
    positions: dict[int, _PositionState] = defaultdict(_PositionState)
    trades: list[dict[str, Any]] = []

    for d in _sorted(deals):
        if d["type"] not in TRADE_TYPES:
            continue
        entry = d.get("entry")
        volume = float(d.get("volume") or 0)
        price = float(d.get("price") or 0)
        t = deal_time_utc(d, offset_seconds)
        deal_dir = "buy" if d["type"] == DEAL_BUY else "sell"
        pos = positions[int(d.get("position_id") or 0)]

        if entry == ENTRY_IN:
            pos.add_entry(volume, price, t, deal_amount(d), deal_dir)
            continue

        if entry not in EXIT_ENTRIES and entry != ENTRY_INOUT:
            continue

        close_volume = volume
        reopen_volume = 0.0
        if entry == ENTRY_INOUT:
            if pos.open_volume > VOLUME_EPS:
                close_volume = min(volume, pos.open_volume)
                reopen_volume = max(volume - close_volume, 0.0)
            # else: entry side unknown; the whole deal is treated as an exit.

        paired = pos.open_volume + VOLUME_EPS >= close_volume and pos.in_volume > VOLUME_EPS
        position_dir = pos.direction if paired else ("sell" if deal_dir == "buy" else "buy")
        share = close_volume / volume if volume > VOLUME_EPS else 1.0
        exit_amount = deal_amount(d) * share
        allocated_entry_costs = pos.in_costs * (close_volume / pos.in_volume) if paired else 0.0
        entry_price = pos.in_value / pos.in_volume if paired else None
        entry_time = pos.in_time_value / pos.in_volume if paired else None

        commission = float(d.get("commission") or 0) * share
        fee = float(d.get("fee") or 0) * share
        swap = float(d.get("swap") or 0) * share
        profit = float(d.get("profit") or 0) * share
        trades.append({
            "ticket": d["ticket"],
            "position_id": d.get("position_id"),
            "symbol": d.get("symbol") or "",
            "direction": position_dir,
            "volume": round(close_volume, 8),
            "entry_price": entry_price,
            "exit_price": price,
            "open_time": entry_time,
            "close_time": t,
            "holding_seconds": (t - entry_time) if paired and entry_time is not None else None,
            "profit": round(profit, 2),
            "commission": round(commission + (allocated_entry_costs if paired else 0.0), 2),
            "swap": round(swap, 2),
            "fee": round(fee, 2),
            "net": round(exit_amount + allocated_entry_costs, 2),
            "paired": paired,
            "entry_kind": {ENTRY_OUT: "out", ENTRY_OUT_BY: "out_by", ENTRY_INOUT: "reversal"}[entry],
        })

        if paired:
            pos.in_costs -= allocated_entry_costs
            pos.in_value -= (pos.in_value / pos.in_volume) * close_volume
            pos.in_time_value -= (pos.in_time_value / pos.in_volume) * close_volume
            pos.in_volume -= close_volume
            pos.open_volume -= close_volume
        else:
            pos.open_volume = max(pos.open_volume - close_volume, 0.0)
        if reopen_volume > VOLUME_EPS:
            pos.open_volume = 0.0
            pos.add_entry(reopen_volume, price, t, 0.0, deal_dir)
    return trades


def balance_curve(deals: list[dict[str, Any]], current_balance: float | None,
                  offset_seconds: int = 0) -> dict[str, Any]:
    """Reconstruct the balance history from deals.

    If the sum of all deals does not match the current balance, synced history is
    incomplete (e.g. older deals not available). The curve is then shifted so that
    it ends at the real current balance and flagged as anchored/approximate.
    """
    points: list[dict[str, Any]] = []
    running = 0.0
    for d in _sorted(deals):
        if d["type"] in NO_BALANCE_EFFECT_TYPES:
            continue
        amount = deal_amount(d)
        if abs(amount) < 1e-12 and d["type"] in TRADE_TYPES and d.get("entry") == ENTRY_IN:
            continue  # entry deal without costs: balance unchanged
        running += amount
        points.append({
            "t": deal_time_utc(d, offset_seconds),
            "balance": running,
            "change": amount,
            "flow": d["type"] in EXTERNAL_FLOW_TYPES,
        })
    complete = True
    shift = 0.0
    if current_balance is not None and points:
        diff = float(current_balance) - running
        if abs(diff) > max(0.01, abs(current_balance) * 1e-9):
            complete = False
            shift = diff
            for p in points:
                p["balance"] += shift
    for p in points:
        p["balance"] = round(p["balance"], 2)
    return {"points": points, "complete": complete, "shift": round(shift, 2)}


def drawdown(points: list[dict[str, Any]], value_key: str = "balance") -> dict[str, float] | None:
    """Max drawdown with deposits/withdrawals neutralised (flows move the peak too)."""
    peak = None
    max_abs = 0.0
    max_pct = 0.0
    prev = None
    for p in points:
        value = p[value_key]
        flow = p.get("flow_amount", p["change"] if p.get("flow") else 0.0)
        if peak is None:
            peak = value
        elif flow:
            peak += flow
        peak = max(peak, value)
        dd = peak - value
        if dd > max_abs:
            max_abs = dd
        if peak > 0 and dd / peak > max_pct:
            max_pct = dd / peak
        prev = value
    if prev is None:
        return None
    return {"absolute": round(max_abs, 2), "relative_pct": round(max_pct * 100, 2)}


def time_weighted_growth(points: list[dict[str, Any]]) -> float | None:
    """Compound return of trading results only; deposits/withdrawals are neutralised."""
    factor = 1.0
    used = False
    prev_balance = None
    for p in points:
        if prev_balance is not None and not p["flow"]:
            if prev_balance <= 0:
                return None
            factor *= (prev_balance + p["change"]) / prev_balance
            used = True
        prev_balance = p["balance"]
    return round((factor - 1) * 100, 2) if used else None


def equity_drawdown(snapshots: list[dict[str, Any]], flows: list[tuple[float, float]]) -> dict[str, float] | None:
    """Max equity drawdown observed in local snapshots, adjusted for deposit/withdrawal flows."""
    if len(snapshots) < 2:
        return None
    flows = sorted(flows)
    fi = 0
    pts = []
    for s in snapshots:
        flow = 0.0
        while fi < len(flows) and flows[fi][0] <= s["ts_utc"]:
            if pts:  # flows before the first snapshot are already in its equity
                flow += flows[fi][1]
            fi += 1
        pts.append({"equity": s["equity"], "change": 0.0, "flow_amount": flow})
    return drawdown(pts, "equity")


def compute_performance(deals: list[dict[str, Any]], account: dict[str, Any] | None,
                        snapshots: list[dict[str, Any]], offset_seconds: int = 0) -> dict[str, Any]:
    trades = build_trades(deals, offset_seconds)
    current_balance = account.get("balance") if account else None
    curve = balance_curve(deals, current_balance, offset_seconds)
    approx_status = "ok" if curve["complete"] else "approx"
    approx_note = None if curve["complete"] else (
        "Synced deal history does not fully explain the current balance; "
        "older history may be missing, so this value is approximate."
    )

    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_profit = round(sum(wins), 2)
    gross_loss = round(sum(losses), 2)

    trade_deal_total = sum(deal_amount(d) for d in deals if d["type"] in TRADE_TYPES)
    other_total = sum(deal_amount(d) for d in deals
                      if d["type"] not in TRADE_TYPES | EXTERNAL_FLOW_TYPES | NO_BALANCE_EFFECT_TYPES)
    deposits = sum(deal_amount(d) for d in deals if d["type"] in EXTERNAL_FLOW_TYPES and deal_amount(d) > 0)
    withdrawals = sum(deal_amount(d) for d in deals if d["type"] in EXTERNAL_FLOW_TYPES and deal_amount(d) < 0)

    has_trades = bool(trades)
    no_trades_note = "No closed trades in synced history yet."
    dd = drawdown(curve["points"]) if curve["points"] else None
    net_profit = round(trade_deal_total, 2) if deals else None

    if has_trades and gross_loss < 0:
        pf = metric(round(gross_profit / abs(gross_loss), 2))
    elif has_trades:
        pf = metric(None, "unavailable", "No losing trades yet - profit factor is undefined.")
    else:
        pf = metric(None, "unavailable", no_trades_note)

    if dd and dd["absolute"] > 0 and net_profit is not None:
        recovery = metric(round(net_profit / dd["absolute"], 2), approx_status, approx_note)
    else:
        recovery = metric(None, "unavailable", "Requires a non-zero balance drawdown in deal history.")

    holding = [t["holding_seconds"] for t in trades if t["holding_seconds"] is not None]
    unpaired = sum(1 for t in trades if not t["paired"])

    growth = time_weighted_growth(curve["points"])
    flows = [(p["t"], p["change"]) for p in curve["points"] if p["flow"]]
    eq_dd = equity_drawdown(snapshots, flows)

    metrics = {
        "gross_profit": metric(gross_profit if has_trades else None, note=None if has_trades else no_trades_note),
        "gross_loss": metric(gross_loss if has_trades else None, note=None if has_trades else no_trades_note),
        "net_profit": metric(net_profit, note=(
            "Sum of profit, commission, swap and fee across all buy/sell deals (incl. entry costs of open positions)."
            if net_profit is not None else "No deal history synced yet.")),
        "other_adjustments": metric(round(other_total, 2) if deals else None,
                                    note="Non-trade charges, corrections, interest, dividends or taxes."),
        "deposits": metric(round(deposits, 2) if deals else None),
        "withdrawals": metric(round(withdrawals, 2) if deals else None),
        "profit_factor": pf,
        "recovery_factor": recovery,
        "max_drawdown": metric(dd["absolute"] if dd else None, approx_status if dd else "unavailable",
                               approx_note if dd else "No balance history available."),
        "max_drawdown_pct": metric(dd["relative_pct"] if dd else None, approx_status if dd else "unavailable",
                                   "Balance drawdown from peak; deposits/withdrawals neutralised."
                                   if dd else "No balance history available."),
        "equity_drawdown": metric(eq_dd["absolute"] if eq_dd else None, "ok" if eq_dd else "unavailable",
                                  "Observed only in snapshots recorded since this app started running."),
        "growth_pct": metric(growth, approx_status if growth is not None else "unavailable",
                             "Time-weighted (deposits/withdrawals neutralised)." if growth is not None
                             else "Requires a positive starting balance in deal history."),
        "total_trades": metric(len(trades), note="Each exit deal counts as one closed trade (MT5 convention)."),
        "winning_trades": metric(len(wins) if has_trades else None),
        "losing_trades": metric(len(losses) if has_trades else None),
        "win_rate": metric(round(len(wins) / len(trades) * 100, 2) if has_trades else None,
                           note=None if has_trades else no_trades_note),
        "avg_holding_seconds": metric(
            round(sum(holding) / len(holding)) if holding else None,
            "ok" if holding and not unpaired else ("approx" if holding else "unavailable"),
            (f"{unpaired} trade(s) excluded: entry deal not found in synced history." if unpaired and holding
             else None if holding else "No trades with matched entry and exit deals."),
        ),
        "unpaired_trades": metric(unpaired),
    }
    return {
        "metrics": metrics,
        "trades": trades,
        "balance_curve": curve,
        "history_complete": curve["complete"],
    }


def downsample(points: list[dict[str, Any]], max_points: int = 600) -> list[dict[str, Any]]:
    """Keep the shape of a long series while bounding payload size (always keeps last point)."""
    n = len(points)
    if n <= max_points:
        return points
    step = n / max_points
    out = [points[int(i * step)] for i in range(max_points - 1)]
    out.append(points[-1])
    return out
