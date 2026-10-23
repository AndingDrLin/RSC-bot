/* RSC Bot 监控台：每 5 秒轮询 /api/summary。原生 JS，无构建步骤。 */

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5000;

/* ── 格式化 ─────────────────────────────── */
const nf = (n, d = 2) =>
  (n === null || n === undefined || Number.isNaN(n))
    ? "—"
    : Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });

const signed = (n, d = 2) => (n > 0 ? "+" : "") + nf(n, d);
const signCls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "flat");
// 小幅波动保留 3 位，避免显示成 ±0.00%
const pctD = (x) => (Math.abs(x) < 0.1 ? 3 : 2);
const shortSym = (s) => (s || "").split(":").pop();
const clockOf = (iso) => (iso ? iso.slice(11, 19) : "—");

// 数量按品种精度自适应：crypto 最小 0.000001，股票常见 0.01
function qty(n) {
  const a = Math.abs(n || 0);
  if (!a) return "0";
  if (a < 0.001) return Number(n).toFixed(6);
  if (a < 1) return Number(n).toFixed(4);
  if (a < 100) return Number(n).toFixed(2);
  return nf(n, 0);
}

function ageText(s) {
  if (s === null || s === undefined) return "无数据";
  if (s < 90) return `${s} 秒前`;
  if (s < 5400) return `${Math.round(s / 60)} 分钟前`;
  return `${(s / 3600).toFixed(1)} 小时前`;
}

const SIDE_LABEL = { long: "开多", short: "开空", close: "平仓" };
const POS_SIDE = { long: "多", short: "空" };
const STATUS_LABEL = { filled: "已成交", pending: "待成交", canceled: "已撤销" };

/* ── 状态摘要 ───────────────────────────── */
function headline(d) {
  const a = d.account, s = d.system;
  if (!s.db_exists || s.quotes_rows === 0) {
    return "行情采集尚未写入数据。首次启动需拉取全量快照（约 3.4 MB），请稍候。";
  }

  const parts = [];
  if (a.day_pnl !== null && a.day_pnl !== undefined) {
    const pct = a.total ? (a.day_pnl / (a.total - a.day_pnl)) * 100 : 0;
    parts.push(`当日盈亏 <b>${signed(a.day_pnl)} RSC（${signed(pct, pctD(pct))}%）</b>`);
  } else {
    parts.push(`账户权益 <b>${nf(a.total)} RSC</b>`);
  }

  const pos = d.positions || [];
  if (pos.length) {
    const top = pos[0];
    const verb = top.pnl >= 0 ? "最大贡献" : "最大拖累";
    parts.push(`${verb} ${shortSym(top.symbol)} ${POS_SIDE[top.side]}头 ${signed(top.pnl)} RSC`);
  } else {
    parts.push("当前无持仓");
  }

  parts.push(`仓位使用率 ${nf(a.exposure_pct, 1)}%`);

  const risky = pos.filter((p) => p.liq_distance_pct !== null && p.liq_distance_pct < 15);
  if (risky.length) {
    parts.push(`<span class="danger">⚠ ${shortSym(risky[0].symbol)} 距强平价 ${nf(risky[0].liq_distance_pct, 1)}%</span>`);
  }

  return parts.join(" ｜ ");
}

/* ── 指标卡 ─────────────────────────────── */
function renderKpis(d) {
  const a = d.account;

  $("kpi-total").textContent = nf(a.total);
  $("kpi-total-sub").textContent = `现金 ${nf(a.cash)} + 浮动盈亏 ${signed(a.unrealized)}`;

  const day = $("kpi-day");
  if (a.day_pnl === null || a.day_pnl === undefined) {
    day.textContent = "—";
    day.className = "kpi-value";
    $("kpi-day-sub").textContent = "当日基准值尚未建立";
  } else {
    day.textContent = signed(a.day_pnl);
    day.className = "kpi-value " + signCls(a.day_pnl);
    const pct = a.total ? (a.day_pnl / (a.total - a.day_pnl)) * 100 : 0;
    $("kpi-day-sub").textContent = `收益率 ${signed(pct, pctD(pct))}% · 北京时间 00:00 起算`;
  }

  const u = $("kpi-unreal");
  u.textContent = signed(a.unrealized);
  u.className = "kpi-value " + signCls(a.unrealized);

  $("kpi-exp").textContent = nf(a.exposure_pct, 1) + "%";
  $("kpi-exp-sub").textContent = `名义敞口 ${nf(a.notional)} RSC / 权益 ${nf(a.total)}`;

  for (const id of ["kpi-total", "kpi-day", "kpi-unreal", "kpi-exp"]) {
    const card = $(id).closest(".kpi");
    card.classList.remove("up", "down");
    if (id === "kpi-day" || id === "kpi-unreal") {
      const v = id === "kpi-day" ? a.day_pnl : a.unrealized;
      if (v > 0) card.classList.add("up");
      else if (v < 0) card.classList.add("down");
    }
  }
}

/* ── 持仓 ───────────────────────────────── */
function renderPositions(d) {
  const body = $("pos-body");
  const pos = d.positions || [];
  $("pos-count").textContent = pos.length;
  if (!pos.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">当前无持仓</td></tr>`;
    return;
  }
  const total = d.account.total || 1;
  body.innerHTML = pos.map((p) => {
    const w = (p.value / total) * 100;
    const liq = p.liq_distance_pct;
    const warn = liq !== null && liq < 15;
    const barW = Math.min(Math.max(liq === null ? 0 : liq, 0), 100);
    return `<tr>
      <td><div class="sym"><b>${shortSym(p.symbol)}</b><span>${p.name || ""}</span></div></td>
      <td><span class="side ${p.side}">${POS_SIDE[p.side]} ${p.leverage}x</span></td>
      <td class="num ${signCls(p.pnl)}">${signed(p.pnl)}</td>
      <td class="num ${signCls(p.pnl_pct)}">${signed(p.pnl_pct, 1)}%</td>
      <td class="num"><div class="bar"><span>${nf(w, 2)}%</span>
        <span class="track"><i class="fill" style="width:${Math.min(w * 8, 100)}%"></i></span></div></td>
      <td class="num"><div class="bar"><span class="${warn ? "warn" : ""}">${
        liq === null ? "—" : nf(liq, 1) + "%"}</span>
        <span class="track"><i class="fill ${warn ? "warn" : ""}" style="width:${barW}%"></i></span></div></td>
    </tr>`;
  }).join("");
}

/* ── 行情 ───────────────────────────────── */
function renderMovers(d) {
  const el = $("movers");
  const rows = [...(d.movers || []), ...(d.losers || [])];
  if (!rows.length) {
    el.innerHTML = `<div class="empty">暂无行情数据</div>`;
    return;
  }
  el.innerHTML = rows.map((m) => {
    const up = m.change_pct >= 0;
    const w = Math.min(Math.abs(m.change_pct) * 6, 100);
    return `<div class="mrow">
      <div class="m-sym">${shortSym(m.symbol)}${m.delayed ? ' <span class="badge-delay">延时</span>' : ""}</div>
      <div class="m-chg ${up ? "up" : "down"}">${signed(m.change_pct, 2)}%</div>
      <div class="m-bar"><i style="width:${w}%;background:${up ? "var(--up)" : "var(--down)"}"></i></div>
    </div>`;
  }).join("");
}

/* ── 委托 ───────────────────────────────── */
function renderOrders(d) {
  const body = $("ord-body");
  const rows = d.orders || [];
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">当前无委托记录</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((o) => `
    <tr>
      <td>${clockOf(o.created_at)}</td>
      <td>${shortSym(o.symbol)}</td>
      <td><span class="side ${o.side}">${SIDE_LABEL[o.side] || o.side}</span></td>
      <td class="num">${qty(o.quantity)}</td>
      <td><span class="tag ${o.status}">${STATUS_LABEL[o.status] || o.status}</span></td>
      <td class="num">${nf(o.fee, 4)}</td>
    </tr>`).join("");
}

/* ── 运行状态 ───────────────────────────── */
function renderSystem(d) {
  const s = d.system;
  const age = s.collector_age_s;
  const alive = age !== null && age < 180;
  const bars = s.bars || {};
  const b5 = bars["1d"], b1h = bars["5d"], bd = bars["1y"];
  const barText = (b) => (b ? `${nf(b.rows, 0)} 根 · ${b.symbols} 标的` : "—");

  $("sys").innerHTML = [
    ["行情采集", alive ? `<span class="ok">运行中 · ${ageText(age)}</span>`
                       : `<span class="danger">${ageText(age)}</span>`],
    ["今日快照", nf(s.ticks_today, 0)],
    ["报价记录", nf(s.quotes_rows, 0)],
    ["自选池", s.watchlist_n
      ? `${nf(s.quote_symbols_15m, 0)}/${s.watchlist_n} · 15 分钟内`
      : "未配置"],
    ["活标的", s.watchlist_n
      ? `<span class="ok">${nf(s.bars_live_1d, 0)}</span>/${s.watchlist_n + (s.candidates_n || 0)} · 24h 内有 K 线`
      : "—"],
    ["5 分钟 K", barText(b5)],
    ["1 小时 K", barText(b1h)],
    ["日线 K", barText(bd)],
    ["数据库", `${nf(s.db_mb, 1)} MB`],
    ["运行时长", `${Math.floor(s.uptime_s / 3600)}h ${Math.floor((s.uptime_s % 3600) / 60)}m`],
    ["刷新间隔", "5 秒"],
  ].map(([k, v]) => `<div class="sys-item"><span>${k}</span><b>${v}</b></div>`).join("");

  $("events").innerHTML = (d.events || []).length
    ? d.events.map((e) => {
        const t = new Date(e.ts * 1000).toTimeString().slice(0, 8);
        let p = e.payload || "";
        if (p.length > 60) p = p.slice(0, 60) + "…";
        return `<div class="ev"><time>${t}</time><span class="evk">${e.kind}</span><span>${p}</span></div>`;
      }).join("")
    : `<div class="empty">暂无系统事件</div>`;
}

function renderAll(d) {
  $("headline").innerHTML = headline(d);
  renderKpis(d);
  renderPositions(d);
  renderMovers(d);
  renderOrders(d);
  renderSystem(d);
  $("foot-ts").textContent = "快照时间 " + new Date(d.ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

/* ── 轮询 ───────────────────────────────── */
function pulse(d) {
  const el = $("pulse");
  const age = d?.system?.collector_age_s;
  el.className = "pulse";
  if (age === null || age === undefined) el.classList.add("dead");
  else if (age > 180) el.classList.add("stale");
}

async function tick() {
  try {
    const r = await fetch("/api/summary", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    renderAll(d);
    pulse(d);
  } catch (e) {
    $("headline").innerHTML = `<span class="danger">数据接口不可用（${e.message}），5 秒后重试</span>`;
    $("pulse").className = "pulse dead";
  }
}

setInterval(tick, REFRESH_MS);
setInterval(() => {
  $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);
tick();
