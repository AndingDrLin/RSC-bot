"""RSC Bot 看板后端：只读 SQLite，零外部依赖（除 FastAPI）。

设计目标：信息密度高，但外行也能一眼看懂当前发生了什么。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rscbot.store import db_size_mb

BASE = Path(__file__).parent
DB = os.environ.get("RSC_DB") or (
    "/app/data/market.db" if os.path.isdir("/app/data") else "data/market.db"
)
WATCHLIST = Path(os.environ.get("RSC_WATCHLIST") or (BASE.parents[2] / "config" / "watchlist.json"))
STARTED = time.time()

app = FastAPI(title="RSC Bot", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=str(BASE / "templates"))


# ── 数据访问 ────────────────────────────────────────────
def rows(sql: str, args=()) -> list[dict]:
    if not os.path.exists(DB):
        return []
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _today_start() -> int:
    now = datetime.now().astimezone()
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


# ── 汇总 ────────────────────────────────────────────────
def build_summary() -> dict:
    pf = rows("SELECT * FROM portfolio ORDER BY ts DESC LIMIT 1")
    pos = rows("SELECT * FROM positions WHERE ts=(SELECT MAX(ts) FROM positions)")
    quotes = rows(
        """
        WITH l AS (SELECT instrument_id, MAX(ts) ts FROM quotes GROUP BY instrument_id)
        SELECT q.instrument_id, q.symbol, q.price, q.prev_close, q.delay_seconds,
               q.market_closed, q.ts, i.name, i.category, i.fee_bps, i.leverage AS _x
        FROM quotes q
        JOIN l ON q.instrument_id = l.instrument_id AND q.ts = l.ts
        LEFT JOIN instruments i ON i.id = q.instrument_id
        """
    ) if False else rows(
        """
        WITH l AS (SELECT instrument_id, MAX(ts) ts FROM quotes GROUP BY instrument_id)
        SELECT q.instrument_id, q.symbol, q.price, q.prev_close, q.delay_seconds,
               q.market_closed, q.ts, i.name, i.category, i.fee_bps
        FROM quotes q
        JOIN l ON q.instrument_id = l.instrument_id AND q.ts = l.ts
        LEFT JOIN instruments i ON i.id = q.instrument_id
        """
    )

    price = {q["instrument_id"]: _f(q["price"]) for q in quotes}
    meta = {q["instrument_id"]: q for q in quotes}

    balance = _f(pf[0]["balance"]) if pf else 0.0
    unreal = _f(pf[0]["pnl"]) if pf else 0.0
    margin = _f(pf[0]["margin"]) if pf else 0.0
    notional = _f(pf[0]["notional"]) if pf else 0.0

    positions = []
    for p in pos:
        px = price.get(p["instrument_id"]) or _f(p["average"])
        val = _f(p["quantity"]) * px
        liq = _f(p["liquidation"]) if p.get("liquidation") else None
        entry = _f(p["average"])
        dist = None
        if liq and px:
            dist = (px - liq) / px * 100 if p["side"] == "long" else (liq - px) / px * 100
        pnl = _f(p["pnl"])
        base = _f(p["margin"]) or 1e-9
        positions.append({
            "symbol": p["symbol"],
            "name": (meta.get(p["instrument_id"]) or {}).get("name") or p["symbol"],
            "side": p["side"],
            "leverage": _f(p["leverage"], 1),
            "quantity": _f(p["quantity"]),
            "entry": entry,
            "price": px,
            "value": val,
            "pnl": pnl,
            "pnl_pct": pnl / base * 100,
            "liquidation": liq,
            "liq_distance_pct": dist,
            "category": (meta.get(p["instrument_id"]) or {}).get("category"),
        })
    positions.sort(key=lambda x: -abs(x["pnl"]))

    movers = []
    for q in quotes:
        prev = _f(q["prev_close"])
        if prev > 0 and q["price"]:
            movers.append({
                "symbol": q["symbol"],
                "name": q.get("name") or q["symbol"],
                "price": _f(q["price"]),
                "change_pct": (_f(q["price"]) - prev) / prev * 100,
                "delayed": (q.get("delay_seconds") or 0) > 60,
            })
    movers.sort(key=lambda x: -x["change_pct"])

    total = balance + unreal

    # 今日盈亏：今日第一条 portfolio 的 (balance + pnl) 与当前比
    first = rows(
        "SELECT * FROM portfolio WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (_today_start(),)
    )
    day_pnl = None
    if first:
        base_today = _f(first[0]["balance"]) + _f(first[0]["pnl"])
        if base_today > 0:
            day_pnl = total - base_today

    last_ts = rows("SELECT MAX(ts) AS m FROM quotes")
    last = last_ts[0]["m"] if last_ts else None
    ticks_today = rows(
        "SELECT COUNT(DISTINCT ts) AS c FROM quotes WHERE ts >= ?", (_today_start(),)
    )
    n_quotes = rows("SELECT COUNT(*) AS c FROM quotes")

    # K 线覆盖（bars 表可能还不存在，rows() 会吞掉 no such table）
    bars = {
        r["rng"]: {"rows": r["c"], "symbols": r["s"], "last_ts": r["m"]}
        for r in rows(
            "SELECT rng, COUNT(*) AS c, COUNT(DISTINCT instrument_id) AS s, "
            "MAX(ts) AS m FROM bars GROUP BY rng"
        )
    }
    fresh = rows("SELECT COUNT(DISTINCT instrument_id) AS c FROM quotes WHERE ts >= ?",
                 (int(time.time()) - 900,))
    watchlist_n = 0
    try:
        watchlist_n = len(json.loads(WATCHLIST.read_text(encoding="utf-8")).get("symbols") or [])
    except (OSError, ValueError):
        pass

    db_mb = db_size_mb(DB) if os.path.exists(DB) else 0.0

    return {
        "ts": int(time.time()),
        "account": {
            "total": total,
            "cash": balance,
            "unrealized": unreal,
            "margin": margin,
            "notional": notional,
            "exposure_pct": (abs(notional) / total * 100) if total else 0.0,
            "day_pnl": day_pnl,
        },
        "positions": positions,
        "movers": movers[:6],
        "losers": list(reversed(movers[-6:])) if len(movers) > 6 else [],
        "orders": rows(
            "SELECT order_id, symbol, side, status, quantity, price, fee, pnl, created_at "
            "FROM orders ORDER BY ts DESC LIMIT 8"
        ),
        "system": {
            "collector_ts": last,
            "collector_age_s": int(time.time() - last) if last else None,
            "ticks_today": (ticks_today[0]["c"] if ticks_today else 0),
            "quotes_rows": (n_quotes[0]["c"] if n_quotes else 0),
            "bars": bars,
            "watchlist_n": watchlist_n,
            "quote_symbols_15m": (fresh[0]["c"] if fresh else 0),
            "db_mb": round(db_mb, 1),
            "uptime_s": int(time.time() - STARTED),
            "db_exists": os.path.exists(DB),
        },
        "events": rows("SELECT ts, kind, payload FROM events ORDER BY ts DESC LIMIT 6"),
    }


# ── 路由 ────────────────────────────────────────────────
@app.get("/")
def index(request: Request):
    return tpl.TemplateResponse(request=request, name="index.html", context={})


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": os.path.exists(DB), "uptime_s": int(time.time() - STARTED)}


@app.get("/api/summary")
def api_summary():
    try:
        return JSONResponse(build_summary())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
