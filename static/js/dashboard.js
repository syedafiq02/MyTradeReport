/* MT5 Live Personal Dashboard - vanilla JS, no external libraries.
 * All server data is inserted with textContent (never innerHTML) to avoid injection.
 */
(function () {
  "use strict";

  const TZ = "Asia/Kuala_Lumpur";
  const TZ_OFFSET_S = 8 * 3600; // Malaysia has no DST
  const body = document.body;
  const POLL_MS = Number(body.dataset.pollMs) || 5000;
  const HISTORY_MS = Number(body.dataset.historyMs) || 60000;
  const IS_DEMO = body.dataset.demo === "1";
  const TRADES_PAGE = 25;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const state = {
    currency: "USD",
    digits: 2,
    range: "7d",
    series: "equity",
    tradesOffset: 0,
    tradesTotal: 0,
    status: null,
    backendDown: false,
    inFlight: { live: false, history: false },
  };

  // ---------- helpers ----------
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  };
  const isNum = (v) => typeof v === "number" && isFinite(v);

  const fmtCache = {};
  function moneyFormatter(currency, digits) {
    const key = currency + digits;
    if (!fmtCache[key]) {
      try {
        fmtCache[key] = new Intl.NumberFormat("en-US", {
          style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits,
        });
      } catch (e) { // non-ISO currency codes (e.g. USC cent accounts)
        const nf = new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
        fmtCache[key] = { format: (v) => nf.format(v) + " " + currency };
      }
    }
    return fmtCache[key];
  }
  function money(v, signed) {
    if (!isNum(v)) return "—";
    const s = moneyFormatter(state.currency, state.digits).format(v);
    return signed && v > 0 ? "+" + s : s;
  }
  function compactMoney(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 1 : 2).replace(/\.?0+$/, "") + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return v.toLocaleString("en-US", { maximumFractionDigits: a < 10 ? 2 : 0 });
  }
  const num = (v, d) => (isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "—");
  const price = (v) => (isNum(v) && v !== 0 ? String(+v.toFixed(6)) : "—");
  const lots = (v) => (isNum(v) ? String(+v.toFixed(2)) : "—");
  const signClass = (v) => (isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "");
  function setSign(node, cls) {
    node.classList.remove("pos", "neg");
    if (cls) node.classList.add(cls);
  }

  const dtFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const timeFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const hourFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "numeric", hour12: false });
  const fmtDateTime = (sec) => (isNum(sec) ? dtFmt.format(new Date(sec * 1000)) : "—");
  const fmtTime = (sec) => (isNum(sec) ? timeFmt.format(new Date(sec * 1000)) : "—");
  function duration(sec) {
    if (!isNum(sec)) return "—";
    sec = Math.max(0, Math.round(sec));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m";
    return sec + "s";
  }

  async function getJSON(url) {
    const res = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("Not signed in");
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // Greeting based on the current Malaysia time (no data involved).
  (function greet() {
    const h = Number(hourFmt.format(new Date())) % 24;
    $("#greeting").textContent = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  })();

  // ---------- status ----------
  const STATE_LABEL = { live: "Live", degraded: "Live (with errors)", stale: "Stale", offline: "Offline" };

  function renderStatus(status, backendDown) {
    const pill = $("#status");
    const label = $("#status-label");
    const alert = $("#alert");
    let st = backendDown ? "offline" : status.state;
    // Client-side safety net: never show "live" for old data.
    if (!backendDown && st === "live" && isNum(status.last_success_utc) &&
        Date.now() / 1000 - status.last_success_utc > status.stale_after) st = "stale";
    pill.dataset.state = st;
    label.textContent = (IS_DEMO ? "DEMO · " : "") + (backendDown ? "Backend unreachable" : STATE_LABEL[st] || st);
    const updated = status && status.last_success_utc ? fmtTime(status.last_success_utc) + " MYT" : "";
    $("#status-time").textContent = updated ? "Updated " + updated : "";
    $("#sub-equity").textContent = updated ? "As of " + updated : "";

    const cards = $("#account-cards");
    const live = st === "live" || st === "degraded";
    cards.classList.toggle("is-stale", !live);
    for (const n of document.querySelectorAll(".growth [data-field]")) n.classList.toggle("muted", !live);
    $("#stale-note").hidden = live || !(status && status.last_success_utc);

    let msg = null, warn = false;
    if (backendDown) {
      msg = ["Cannot reach the local dashboard server.", "Check that run.bat / app.py is still running."];
    } else if (status.error && st !== "live") {
      msg = [st === "offline" ? "MT5 connection unavailable" : "MT5 sync problem", status.error];
      warn = st !== "offline";
    } else if (status.note) {
      msg = ["Data may be outdated", status.note];
      warn = true;
    } else if (st === "stale") {
      msg = ["Data is stale", "No successful update for " + Math.round(status.age_seconds || 0) + " seconds."];
      warn = true;
    }
    alert.hidden = !msg;
    alert.classList.toggle("alert-warn", warn);
    alert.replaceChildren();
    if (msg) {
      alert.append(el("strong", null, msg[0]), el("span", null, msg[1]));
    }

    if (status) {
      const sync = (k) => document.querySelector('[data-sync="' + k + '"]');
      sync("mode").textContent = status.mode === "demo" ? "DEMO (synthetic data)" : "Live MT5 terminal";
      sync("last").textContent = status.last_success_utc ? fmtDateTime(status.last_success_utc) + " MYT" : "never";
      sync("history").textContent = status.last_history_sync_utc ? fmtDateTime(status.last_history_sync_utc) + " MYT" : "never";
      sync("intervals").textContent = "Account every " + status.poll_interval + "s · history every " + status.history_interval + "s";
      sync("offset").textContent = isNum(status.server_utc_offset_hours)
        ? "UTC" + (status.server_utc_offset_hours >= 0 ? "+" : "") + status.server_utc_offset_hours + "h (" + status.server_offset_source + ")"
        : "Unknown — deal times assume UTC until detected";
    }
  }

  // ---------- account ----------
  function renderAccount(a) {
    if (!a) return;
    state.currency = a.currency || "USD";
    state.digits = isNum(a.currency_digits) ? a.currency_digits : 2;
    $("#account-login").textContent = a.login_masked || "—";
    $("#account-ident").textContent = [a.server, a.account_type].filter(Boolean).join(" · ") || "Account";
    $("#account-type").textContent = a.account_type || "";
    $("#account-meta").textContent = [a.currency, a.leverage ? "1:" + a.leverage : null, a.company].filter(Boolean).join(" · ");
    const set = (field, text, cls) => {
      const node = document.querySelector('[data-field="' + field + '"]');
      node.textContent = text;
      setSign(node, cls);
    };
    set("balance", money(a.balance));
    set("equity", money(a.equity));
    set("floating", money(a.floating, true), signClass(a.floating));
    set("margin", money(a.margin));
    set("free_margin", money(a.free_margin));
    set("margin_level", a.margin ? num(a.margin_level, 1) + "%" : "—");
    $("#sub-balance").textContent = isNum(a.credit) && a.credit !== 0 ? "Credit " + money(a.credit) : (a.currency ? a.currency + " account" : "");

    // Donut: share of (margin + free margin) currently used as margin. Both are MT5 values.
    const used = $("#margin-donut .donut-used"), free = $("#margin-donut .donut-free");
    const total = (isNum(a.margin) ? a.margin : 0) + (isNum(a.free_margin) ? a.free_margin : 0);
    if (isNum(a.margin) && isNum(a.free_margin) && total > 0) {
      const pct = Math.max(0, Math.min(100, (a.margin / total) * 100));
      used.setAttribute("stroke-dasharray", pct + " " + (100 - pct));
      free.setAttribute("stroke-dasharray", "100 0");
      $("#margin-donut").setAttribute("aria-label", "Margin " + money(a.margin) + ", free margin " + money(a.free_margin));
    } else {
      used.setAttribute("stroke-dasharray", "0 100");
      free.setAttribute("stroke-dasharray", "0 100");
    }
  }

  // ---------- positions (list) ----------
  function typeTag(dir) {
    return el("span", "tag " + (dir === "buy" ? "tag-buy" : "tag-sell"), dir || "?");
  }
  function cell(text, cls) { return el("td", cls, text); }

  function renderPositions(positions, hasAccount) {
    const list = $("#positions-body");
    list.replaceChildren();
    $("#positions-count").textContent = hasAccount ? String(positions.length) : "";
    $("#sub-floating").textContent = hasAccount
      ? positions.length + " open position" + (positions.length === 1 ? "" : "s") : "";
    if (!hasAccount) { list.append(el("li", "empty", "No account data yet.")); return; }
    if (!positions.length) { list.append(el("li", "empty", "No open positions.")); return; }
    for (const p of positions) {
      const li = el("li", "position");
      li.append(el("span", "pos-badge", (p.symbol || "?").slice(0, 3).toUpperCase()));
      const main = el("div", "pos-main");
      const title = el("strong", null, p.symbol || "—");
      title.append(typeTag(p.direction));
      const meta = el("span", "pos-meta");
      meta.append(el("code", null, p.ticket), " · opened " + fmtDateTime(p.open_time_utc) + " MYT");
      main.append(
        title,
        el("span", null, lots(p.volume) + " lot · " + price(p.entry_price) + " → " + price(p.current_price)),
        meta,
      );
      const right = el("div", "pos-right");
      right.append(
        el("span", "pnl-pill " + signClass(p.profit), money(p.profit, true)),
        el("small", null, "Swap " + money(p.swap)),
      );
      li.title = "Floating P/L " + money(p.profit, true);
      li.append(main, right);
      list.append(li);
    }
  }

  // ---------- performance ----------
  // Every backend metric, grouped. Nothing here is calculated in the browser.
  const METRIC_GROUPS = [
    ["Profitability", [
      ["net_profit", "Net trading profit", "signed"],
      ["gross_profit", "Gross profit", "signed"],
      ["gross_loss", "Gross loss", "signed"],
      ["profit_factor", "Profit factor", "ratio"],
    ]],
    ["Risk", [
      ["max_drawdown", "Max balance drawdown", "money"],
      ["max_drawdown_pct", "Max drawdown %", "pct"],
      ["equity_drawdown", "Max equity drawdown", "money"],
      ["recovery_factor", "Recovery factor", "ratio"],
    ]],
    ["Activity", [
      ["total_trades", "Closed trades", "int"],
      ["winning_trades", "Winning trades", "int"],
      ["losing_trades", "Losing trades", "int"],
      ["win_rate", "Win rate", "pct"],
      ["avg_holding_seconds", "Avg holding time", "duration"],
    ]],
    ["Growth & cash flow", [
      ["growth_pct", "Account growth", "signedpct"],
      ["deposits", "Deposits", "money"],
      ["withdrawals", "Withdrawals", "money"],
    ]],
  ];

  function formatMetric(kind, v) {
    switch (kind) {
      case "signed": return [money(v, true), signClass(v)];
      case "money": return [money(v), ""];
      case "ratio": return [num(v, 2), ""];
      case "pct": return [num(v, 2) + "%", ""];
      case "signedpct": return [(v > 0 ? "+" : "") + num(v, 2) + "%", signClass(v)];
      case "int": return [String(v), ""];
      case "duration": return [duration(v), ""];
      default: return [String(v), ""];
    }
  }
  const missing = (m) => !m || m.status === "unavailable" || m.value === null || m.value === undefined;
  // [text, class] for a metric, "Not available" when the backend says so.
  function metricText(m, kind) {
    if (!m) return ["—", ""]; // not received yet
    if (missing(m)) return ["Not available", "na"];
    return formatMetric(kind, m.value);
  }
  const approxTag = (m) => (m && m.status === "approx" ? el("span", "tag tag-warn", "approx") : null);
  function setMetric(node, m, kind) {
    const [text, cls] = metricText(m, kind);
    node.textContent = text;
    node.classList.remove("pos", "neg", "na");
    if (cls) node.classList.add(cls);
    node.title = (m && m.note) || "";
  }
  function bold(text) { return el("b", null, text); }

  function renderStats(metrics) {
    const wrap = $("#metrics");
    wrap.replaceChildren();
    for (const [title, items] of METRIC_GROUPS) {
      const group = el("div", "stat-group");
      group.append(el("h3", null, title));
      for (const [key, label, kind] of items) {
        const m = metrics[key];
        if (!m) continue;
        const row = el("div", "stat-row");
        const lab = el("div", "stat-label", label);
        const tag = approxTag(m);
        if (tag) lab.append(tag);
        if (m.note) lab.append(el("span", "stat-note", m.note));
        const [text, cls] = metricText(m, kind);
        row.append(lab, el("div", "stat-value" + (cls ? " " + cls : ""), text));
        group.append(row);
      }
      if (group.childElementCount > 1) wrap.append(group);
    }
  }

  function renderActivity(metrics) {
    const total = metrics.total_trades, wins = metrics.winning_trades, losses = metrics.losing_trades;
    setMetric($("#act-total"), total, "int");
    const side = $("#act-side");
    side.replaceChildren();
    const [wr] = metricText(metrics.win_rate, "pct");
    const [hold] = metricText(metrics.avg_holding_seconds, "duration");
    side.append("Win rate: ", bold(wr), el("br"), "Avg holding time: ", bold(hold));

    const bar = $("#act-bar"), legend = $("#act-legend");
    bar.replaceChildren();
    legend.replaceChildren();
    if (missing(total) || missing(wins) || missing(losses) || total.value <= 0) {
      bar.append(el("span", "seg-none"));
      legend.append(el("li", null, "No closed trades yet"));
      return;
    }
    // Trades that are neither winners nor losers (net exactly zero), if any.
    const even = Math.max(0, total.value - wins.value - losses.value);
    const segs = [["seg-win", wins.value], ["seg-loss", losses.value], ["seg-even", even]];
    for (const [cls, n] of segs) {
      if (n <= 0) continue;
      const s = el("span", cls);
      s.style.flex = String(n);
      bar.append(s);
    }
    bar.setAttribute("aria-label", wins.value + " winning, " + losses.value + " losing trades");
    const item = (cls, n, label) => {
      const li = el("li", cls);
      li.append(bold("Total " + n), " : " + label);
      legend.append(li);
    };
    item("win", wins.value, "Won");
    item("loss", losses.value, "Lost");
    if (even > 0) item("", even, "Break-even");
  }

  function renderPnl(metrics) {
    setMetric($("#pnl-net"), metrics.net_profit, "signed");
    const tagWrap = $("#pnl-net-tag");
    tagWrap.replaceChildren();
    const tag = approxTag(metrics.net_profit);
    if (tag) tagWrap.append(tag);
    const side = $("#pnl-side");
    side.replaceChildren();
    side.append("Profit factor: ", bold(metricText(metrics.profit_factor, "ratio")[0]), el("br"),
      "Recovery factor: ", bold(metricText(metrics.recovery_factor, "ratio")[0]));

    // Horizontal magnitude bars on one shared money scale.
    const rows = [
      ["gross_profit", "Gross profit", "signed", "pos"],
      ["gross_loss", "Gross loss", "signed", "neg"],
      ["max_drawdown", "Max balance drawdown", "money", "dd"],
      ["equity_drawdown", "Max equity drawdown", "money", "dd"],
    ];
    const max = Math.max(0, ...rows.map(([k]) => (missing(metrics[k]) ? 0 : Math.abs(metrics[k].value))));
    const wrap = $("#pnl-bars");
    wrap.replaceChildren();
    for (const [key, label, kind, cls] of rows) {
      const m = metrics[key];
      if (!m) continue;
      const row = el("div", "bar-row");
      const lab = el("div", "bar-label", label);
      const tag = approxTag(m);
      if (tag) lab.append(tag);
      const line = el("div", "bar-line");
      const track = el("div", "bar-track");
      const fill = el("div", "bar-fill " + (missing(m) ? "na-fill" : cls));
      fill.style.width = (missing(m) || max <= 0 ? 0 : (Math.abs(m.value) / max) * 100) + "%";
      track.append(fill);
      const [text, vcls] = metricText(m, kind);
      line.append(track, el("span", "bar-val" + (vcls ? " " + vcls : ""), text));
      row.append(lab, line);
      row.title = m.note || "";
      wrap.append(row);
    }
    if (!wrap.childElementCount) wrap.append(el("p", "empty", "Waiting for deal history…"));
    $("#pnl-axis-max").textContent = max > 0 ? compactMoney(max) : "";
  }

  function renderGrowth(metrics) {
    setMetric($("#growth-value"), metrics.growth_pct, "signedpct");
    const tag = approxTag(metrics.growth_pct);
    const sub = $("#growth-sub");
    sub.replaceChildren("Account growth (time-weighted)");
    if (tag) sub.append(" ", tag);
    const info = $("#growth-info-text");
    info.replaceChildren();
    info.append("Deposits ", bold(metricText(metrics.deposits, "money")[0]),
      " · Withdrawals ", bold(metricText(metrics.withdrawals, "money")[0]), el("br"),
      "Max drawdown ", bold(metricText(metrics.max_drawdown_pct, "pct")[0]), " from peak balance");
  }

  function renderPerformance(data) {
    if (!data.available) {
      const wrap = $("#metrics");
      wrap.replaceChildren(el("p", "empty", "No deal history has been synced yet. Metrics appear after the first successful history sync."));
      $("#perf-note").textContent = "";
      const blank = {};
      renderActivity(blank);
      renderPnl(blank);
      renderGrowth(blank);
      $("#growth-info-text").textContent = "Waiting for the first deal-history sync.";
      return;
    }
    $("#perf-note").textContent = data.history_complete
      ? "Calculated from full synced deal history"
      : "History incomplete — some values are approximate";
    renderStats(data.metrics);
    renderActivity(data.metrics);
    renderPnl(data.metrics);
    renderGrowth(data.metrics);
  }

  // ---------- trades ----------
  function emptyRow(colspan, text) {
    const tr = el("tr");
    const td = el("td", "empty", text);
    td.colSpan = colspan;
    tr.append(td);
    return tr;
  }

  function renderTrades(data) {
    state.tradesTotal = data.total;
    const tbody = $("#trades-body");
    tbody.replaceChildren();
    $("#trades-count").textContent = String(data.total);
    if (!data.trades.length) {
      tbody.append(emptyRow(11, data.total ? "No more trades." : "No closed trades in synced history yet."));
    }
    for (const t of data.trades) {
      const tr = el("tr");
      const typeTd = el("td");
      typeTd.append(typeTag(t.direction));
      const entryTd = cell(t.entry_price === null ? "" : price(t.entry_price), "num");
      if (!t.paired) entryTd.append(el("span", "tag tag-warn", "unpaired"));
      tr.append(
        cell(fmtDateTime(t.close_time)), cell(t.symbol || "—"), typeTd, cell(lots(t.volume), "num"),
        entryTd, cell(price(t.exit_price), "num"),
        cell(money(t.profit, true), "num " + signClass(t.profit)),
        cell(money(t.commission + (t.fee || 0)), "num " + signClass(t.commission + (t.fee || 0))),
        cell(money(t.swap), "num " + signClass(t.swap)),
        cell(money(t.net, true), "num net " + signClass(t.net)),
        cell(t.holding_seconds === null ? "—" : duration(t.holding_seconds)),
      );
      tbody.append(tr);
    }
    const start = data.total ? data.offset + 1 : 0;
    const end = Math.min(data.offset + data.limit, data.total);
    $("#trades-page").textContent = data.total ? start + "–" + end + " of " + data.total : "";
    $("#trades-prev").disabled = data.offset <= 0;
    $("#trades-next").disabled = end >= data.total;
  }

  async function loadTrades() {
    try {
      renderTrades(await getJSON("/api/trades?limit=" + TRADES_PAGE + "&offset=" + state.tradesOffset));
    } catch (e) { /* status polling reports connectivity */ }
  }

  // ---------- canvas line chart ----------
  const TIME_STEPS = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400,
    7 * 86400, 14 * 86400, 30 * 86400, 91 * 86400, 182 * 86400, 365 * 86400];
  const tickDayFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "numeric", month: "short" });
  const tickMonthFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, month: "short", year: "numeric" });
  const tickHourFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });

  function niceStep(span, count) {
    const raw = span / Math.max(count, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }
  function rgba(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return "rgba(212, 175, 55, " + a + ")";
    const n = parseInt(m[1], 16);
    return "rgba(" + (n >> 16) + ", " + ((n >> 8) & 255) + ", " + (n & 255) + ", " + a + ")";
  }

  class LineChart {
    constructor(canvas, opts) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.tip = canvas.parentElement.querySelector(".chart-tip");
      this.opts = Object.assign({ step: false, label: "Value", empty: "No data yet." }, opts);
      this.points = [];
      this.hover = null;
      new ResizeObserver(() => this.draw()).observe(canvas);
      canvas.addEventListener("pointermove", (e) => this.onMove(e));
      canvas.addEventListener("pointerleave", () => { this.hover = null; this.tip.hidden = true; this.draw(); });
    }

    setData(points, emptyMessage) {
      this.points = points.filter((p) => isNum(p.t) && isNum(p.v));
      if (emptyMessage) this.opts.empty = emptyMessage;
      this.hover = null;
      this.tip.hidden = true;
      this.draw();
    }

    css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

    layout() {
      const rect = this.canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(rect.width, 50), h = Math.max(rect.height, 50);
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
        this.canvas.width = Math.round(w * dpr);
        this.canvas.height = Math.round(h * dpr);
      }
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return { w, h };
    }

    draw() {
      const ctx = this.ctx;
      const { w, h } = this.layout();
      ctx.clearRect(0, 0, w, h);
      const family = getComputedStyle(document.body).fontFamily || "system-ui, sans-serif";
      ctx.font = "11.5px " + family;
      const pts = this.points;
      if (!pts.length) {
        ctx.fillStyle = this.css("--text-2");
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        this.wrapText(this.opts.empty, w / 2, h / 2, Math.min(w - 40, 420));
        this.geom = null;
        return;
      }

      // Domains
      let tMin = pts[0].t, tMax = pts[pts.length - 1].t;
      if (tMax - tMin < 60) { tMin -= 1800; tMax += 1800; }
      let vMin = Infinity, vMax = -Infinity;
      for (const p of pts) { if (p.v < vMin) vMin = p.v; if (p.v > vMax) vMax = p.v; }
      if (vMax - vMin < 1e-9) { const pad = Math.max(Math.abs(vMax) * 0.01, 1); vMin -= pad; vMax += pad; }
      const yStep = niceStep(vMax - vMin, 4);
      vMin = Math.floor(vMin / yStep) * yStep;
      vMax = Math.ceil(vMax / yStep) * yStep;

      const yLabels = [];
      for (let v = vMin; v <= vMax + yStep / 2; v += yStep) yLabels.push(v);
      const labelW = Math.max(...yLabels.map((v) => ctx.measureText(compactMoney(v)).width));
      const pad = { l: Math.ceil(labelW) + 30, r: 24, t: 34, b: 34 };
      const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
      const x = (t) => pad.l + ((t - tMin) / (tMax - tMin)) * pw;
      const y = (v) => pad.t + (1 - (v - vMin) / (vMax - vMin)) * ph;
      this.geom = { x, y, pad, pw, ph, w, h };

      const grid = this.css("--chart-grid"), label = this.css("--chart-label");
      const line = this.css("--chart-line"), bright = this.css("--gold-bright");

      // Horizontal grid + y labels
      ctx.lineWidth = 1;
      ctx.strokeStyle = grid;
      ctx.fillStyle = label;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      yLabels.forEach((v, i) => {
        const yy = Math.round(y(v)) + 0.5;
        ctx.setLineDash(i === 0 ? [] : [2, 5]);
        ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
        ctx.fillText(compactMoney(v), pad.l - 12, yy);
      });
      ctx.setLineDash([]);

      // X ticks (aligned to Malaysia-time boundaries) with faint vertical guides
      const span = tMax - tMin;
      const maxTicks = Math.max(2, Math.floor(pw / 100));
      const step = TIME_STEPS.find((s) => span / s <= maxTicks) || TIME_STEPS[TIME_STEPS.length - 1];
      const fmt = step >= 30 * 86400 ? tickMonthFmt : step >= 86400 ? tickDayFmt : null;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.font = "12px " + family;
      let tick = Math.ceil((tMin + TZ_OFFSET_S) / step) * step - TZ_OFFSET_S;
      for (; tick <= tMax; tick += step) {
        const xx = Math.round(x(tick)) + 0.5;
        ctx.strokeStyle = grid;
        ctx.beginPath(); ctx.moveTo(xx, pad.t); ctx.lineTo(xx, pad.t + ph + 5); ctx.stroke();
        let lbl;
        if (fmt) lbl = fmt.format(new Date(tick * 1000));
        else if (((tick + TZ_OFFSET_S) % 86400) === 0 && span > 86400) lbl = tickDayFmt.format(new Date(tick * 1000));
        else lbl = tickHourFmt.format(new Date(tick * 1000));
        ctx.fillText(lbl, xx, h - pad.b + 11);
      }

      // Series path
      const path = () => {
        ctx.beginPath();
        pts.forEach((p, i) => {
          const px = x(p.t), py = y(p.v);
          if (i === 0) ctx.moveTo(px, py);
          else if (this.opts.step) { ctx.lineTo(px, y(pts[i - 1].v)); ctx.lineTo(px, py); }
          else ctx.lineTo(px, py);
        });
      };
      // Area fill (gold fading to transparent)
      path();
      ctx.lineTo(x(pts[pts.length - 1].t), pad.t + ph);
      ctx.lineTo(x(pts[0].t), pad.t + ph);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ph);
      grad.addColorStop(0, rgba(line, 0.22));
      grad.addColorStop(1, rgba(line, 0));
      ctx.fillStyle = grad;
      ctx.fill();
      // Line
      path();
      ctx.strokeStyle = line;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();

      // Last point: marker + value bubble (hidden while hovering)
      const last = pts[pts.length - 1];
      const lx = x(last.t), ly = y(last.v);
      if (this.hover === null) {
        ctx.strokeStyle = rgba(line, 0.35);
        ctx.setLineDash([2, 4]);
        ctx.beginPath(); ctx.moveTo(lx, ly); ctx.lineTo(lx, pad.t + ph); ctx.stroke();
        ctx.setLineDash([]);
        this.bubble(lx, ly, money(last.v), family, bright);
      }
      this.dot(lx, ly, line);

      // Hover crosshair
      if (this.hover !== null && pts[this.hover]) {
        const p = pts[this.hover];
        const hx = x(p.t);
        ctx.strokeStyle = rgba(bright, 0.5);
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(hx, pad.t); ctx.lineTo(hx, pad.t + ph); ctx.stroke();
        ctx.setLineDash([]);
        this.dot(hx, y(p.v), bright);
      }
    }

    bubble(px, py, text, family, color) {
      const ctx = this.ctx, { w, pad } = this.geom;
      ctx.font = "600 12px " + family;
      const tw = ctx.measureText(text).width, bw = tw + 18, bh = 24;
      let bx = px - bw / 2;
      bx = Math.max(pad.l, Math.min(bx, w - bw - 4));
      let by = py - bh - 12;
      if (by < 2) by = py + 12;
      ctx.fillStyle = this.css("--surface-2");
      ctx.strokeStyle = rgba(color, 0.6);
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, 12); else ctx.rect(bx, by, bw, bh);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = color;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, bx + bw / 2, by + bh / 2 + 0.5);
    }

    dot(px, py, color) {
      const ctx = this.ctx;
      ctx.beginPath(); ctx.arc(px, py, 9, 0, Math.PI * 2);
      ctx.fillStyle = rgba(color, 0.16); ctx.fill();
      ctx.beginPath(); ctx.arc(px, py, 5.5, 0, Math.PI * 2);
      ctx.fillStyle = this.css("--surface"); ctx.fill();
      ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
    }

    wrapText(text, cx, cy, maxW) {
      const words = text.split(" ");
      const lines = [];
      let line = "";
      for (const w of words) {
        const test = line ? line + " " + w : w;
        if (this.ctx.measureText(test).width > maxW && line) { lines.push(line); line = w; } else line = test;
      }
      lines.push(line);
      const lh = 17;
      lines.forEach((l, i) => this.ctx.fillText(l, cx, cy + (i - (lines.length - 1) / 2) * lh));
    }

    onMove(e) {
      if (!this.geom || !this.points.length) return;
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const { x } = this.geom;
      // nearest point by x (binary search)
      let lo = 0, hi = this.points.length - 1;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (x(this.points[mid].t) < mx) lo = mid; else hi = mid;
      }
      const idx = Math.abs(x(this.points[lo].t) - mx) <= Math.abs(x(this.points[hi].t) - mx) ? lo : hi;
      if (idx !== this.hover) { this.hover = idx; this.draw(); }
      const p = this.points[idx];
      this.tip.replaceChildren(el("b", null, money(p.v)), el("span", null, fmtDateTime(p.t) + " MYT"));
      this.tip.hidden = false;
      const tw = this.tip.offsetWidth;
      const px = x(p.t);
      let left = px + 14;
      if (left + tw > rect.width) left = px - tw - 14;
      this.tip.style.left = Math.max(0, left) + "px";
      this.tip.style.top = Math.max(0, this.geom.y(p.v) - 50) + "px";
    }
  }

  const equityChart = new LineChart($("#chart-equity"), { label: "Equity" });
  const balanceChart = new LineChart($("#chart-balance"), { label: "Balance", step: true });

  async function loadCharts() {
    let data;
    try { data = await getJSON("/api/charts?range=" + encodeURIComponent(state.range)); } catch (e) { return; }
    equityChart.setData(
      data.equity.map((s) => ({ t: s.t, v: s.equity })),
      "No equity snapshots in this range yet. Snapshots start accumulating while this app is running.",
    );
    const lastEq = data.equity[data.equity.length - 1];
    $("#equity-latest").textContent = lastEq ? money(lastEq.equity) : "—";
    const since = data.first_snapshot_utc;
    $("#equity-note").textContent = since
      ? "Equity snapshots recorded locally since " + fmtDateTime(since) + " MYT. No earlier equity history is available or invented."
      : "Equity snapshots only start accumulating once this app is running. No earlier equity history is shown or invented.";

    const bal = data.balance.map((p) => ({ t: p.t, v: p.balance }));
    // Balance is unchanged since the last deal, so extend the step line to "now".
    if (bal.length) bal.push({ t: Date.now() / 1000, v: bal[bal.length - 1].v });
    balanceChart.setData(bal, "No balance changes from deal history in this range.");
    $("#balance-latest").textContent = bal.length ? money(bal[bal.length - 1].v) : "—";
    $("#balance-note").textContent = data.balance_complete === null || data.balance_complete === undefined
      ? "Waiting for the first deal-history sync."
      : data.balance_complete
        ? "Rebuilt from every synced deal; it reconciles with the current account balance."
        : "Synced history does not fully reconcile with the current balance (older deals may be unavailable), so the curve is anchored to the current balance and earlier values are approximate.";
  }

  // ---------- polling ----------
  async function pollLive() {
    if (state.inFlight.live) return; // never overlap requests
    state.inFlight.live = true;
    try {
      const data = await getJSON("/api/live");
      state.status = data.status;
      state.backendDown = false;
      renderAccount(data.account);
      renderPositions(data.positions, !!data.account);
      renderStatus(data.status, false);
    } catch (e) {
      state.backendDown = true;
      renderStatus(state.status, true);
    } finally {
      state.inFlight.live = false;
      body.classList.remove("is-loading");
    }
  }

  async function pollHistory() {
    if (state.inFlight.history) return;
    state.inFlight.history = true;
    try {
      const perf = await getJSON("/api/performance");
      renderPerformance(perf);
      await Promise.all([loadTrades(), loadCharts()]);
      return perf.available;
    } catch (e) { /* reported by live polling */ } finally {
      state.inFlight.history = false;
    }
    return false;
  }

  // ---------- interactions ----------
  function pressGroup(group, active) {
    for (const b of group.querySelectorAll("button")) {
      const on = b === active;
      b.classList.toggle("active", on);
      if (b.hasAttribute("aria-pressed")) b.setAttribute("aria-pressed", String(on));
    }
  }

  // Range buttons
  $("#range").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-range]");
    if (!btn) return;
    state.range = btn.dataset.range;
    pressGroup($("#range"), btn);
    loadCharts();
  });

  // Equity / balance series switch
  $("#series").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-series]");
    if (!btn) return;
    state.series = btn.dataset.series;
    pressGroup($("#series"), btn);
    for (const panel of document.querySelectorAll(".chart-panel")) panel.hidden = panel.dataset.panel !== state.series;
  });

  // Report tabs: show a subset of sections
  function setView(view) {
    body.dataset.view = view;
    const btn = document.querySelector('#views button[data-view="' + view + '"]');
    if (btn) pressGroup($("#views"), btn);
  }
  $("#views").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) setView(btn.dataset.view);
  });

  // Rail links / "more" links: if the target is hidden by the current tab, switch back to the full report.
  document.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#"]');
    if (!a) return;
    const target = document.getElementById(a.hash.slice(1));
    if (target && !target.offsetParent && body.dataset.view !== "general") {
      setView(a.dataset.goto && document.querySelector('#views button[data-view="' + a.dataset.goto + '"]') ? a.dataset.goto : "general");
    }
  });

  $("#trades-prev").addEventListener("click", () => {
    state.tradesOffset = Math.max(0, state.tradesOffset - TRADES_PAGE);
    loadTrades();
  });
  $("#trades-next").addEventListener("click", () => {
    if (state.tradesOffset + TRADES_PAGE < state.tradesTotal) state.tradesOffset += TRADES_PAGE;
    loadTrades();
  });

  // Rail scroll-spy: highlight the section currently in view.
  const railLinks = Array.from(document.querySelectorAll(".rail-link"));
  if ("IntersectionObserver" in window && railLinks.length) {
    const visible = new Map();
    const atBottom = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    const highlight = () => {
      // The last sections can never reach the detection band, so the page bottom selects the last visible one.
      const shown = railLinks.filter((a) => { const s = document.getElementById(a.hash.slice(1)); return s && s.offsetParent; });
      const current = atBottom() && shown.length ? shown[shown.length - 1]
        : railLinks.find((a) => visible.get(a.hash.slice(1)));
      if (!current) return;
      for (const a of railLinks) {
        const on = a === current;
        a.classList.toggle("active", on);
        if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
      }
    };
    const spy = new IntersectionObserver((entries) => {
      for (const en of entries) visible.set(en.target.id, en.isIntersecting);
      highlight();
    }, { rootMargin: "-90px 0px -55% 0px" });
    window.addEventListener("scroll", () => { if (atBottom()) highlight(); }, { passive: true });
    for (const a of railLinks) {
      const sec = document.getElementById(a.hash.slice(1));
      if (sec) spy.observe(sec);
    }
  }

  // The first history sync usually finishes a few seconds after start-up, so poll the
  // history endpoints quickly until performance data appears, then at the normal rate.
  async function start() {
    await pollLive();
    let ready = await pollHistory();
    setInterval(pollLive, POLL_MS);
    for (let i = 0; !ready && i < 12; i++) {
      await new Promise((r) => setTimeout(r, 5000));
      ready = await pollHistory();
    }
    setInterval(pollHistory, HISTORY_MS);
  }
  // Re-evaluate stale detection between polls (e.g. if the backend stops responding).
  setInterval(() => { if (state.status) renderStatus(state.status, state.backendDown); }, 5000);
  start();
})();
