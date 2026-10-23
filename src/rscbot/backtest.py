"""M3-T3 事件回测：虚拟时钟 + bar 事件 + 撮合 + 权益曲线。

设计取舍：
- 时间轴取所有标的 bar 时间戳的并集；某标的没有 bar 时沿用上一根收盘价做标记
  （和交易所"最新价"的语义一致），所以多标的可以混在同一根时钟里。
- 撮合直接复用 `PaperBroker`（同一套费用/保证金/强平规则），回测和实盘不会
  出现两套口径。
- 策略每根 bar 拿到该标的最近 N 根收盘价，返回意图；引擎负责下单/平仓。
  没有随机性，同一份数据两次回测指标逐位相同。

用法：
    python -m rscbot.backtest --db data/market.db --symbol INDEX:SPX --rng 1y --fast 10 --slow 30
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass

from rscbot import metrics
from rscbot.broker import PaperBroker, Reject


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_bars(db: str, symbol: str, rng: str = "1d", limit: int | None = None) -> list[Bar]:
    conn = connect(db)
    try:
        rows = conn.execute(
            """SELECT b.ts, b.open, b.high, b.low, b.close, b.volume
                 FROM bars b JOIN instruments i ON i.id = b.instrument_id
                WHERE i.symbol = ? AND b.rng = ? ORDER BY b.ts""",
            (symbol, rng),
        ).fetchall()
    finally:
        conn.close()
    bars = [Bar(r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows if r["close"] is not None]
    return bars[-limit:] if limit else bars


def instrument_meta(db: str, symbol: str) -> dict:
    conn = connect(db)
    try:
        r = conn.execute(
            "SELECT fee_bps, max_leverage, step, minimum, category FROM instruments WHERE symbol=?",
            (symbol,),
        ).fetchone()
    finally:
        conn.close()
    return dict(r) if r else {"fee_bps": 0.0, "max_leverage": 1.0, "step": 1.0,
                              "minimum": 0.0, "category": None}


class Strategy:
    """策略基类：每根 bar 返回意图列表。"""

    def on_bar(self, ts: int, symbol: str, closes: list[float]) -> list[dict]:
        return []


class SmaCross(Strategy):
    """双均线演示策略：快线上穿慢线开多，下穿平仓。只为验证引擎，不追求收益。"""

    def __init__(self, fast: int = 10, slow: int = 30):
        if fast >= slow:
            raise ValueError("fast 必须小于 slow")
        self.fast, self.slow = fast, slow

    def on_bar(self, ts: int, symbol: str, closes: list[float]) -> list[dict]:
        if len(closes) < self.slow + 1:
            return []
        fast_now = sum(closes[-self.fast:]) / self.fast
        slow_now = sum(closes[-self.slow:]) / self.slow
        fast_prev = sum(closes[-self.fast - 1:-1]) / self.fast
        slow_prev = sum(closes[-self.slow - 1:-1]) / self.slow
        if fast_prev <= slow_prev and fast_now > slow_now:
            return [{"symbol": symbol, "action": "long"}]
        if fast_prev >= slow_prev and fast_now < slow_now:
            return [{"symbol": symbol, "action": "close"}]
        return []


def run(bars_by_symbol: dict[str, list[Bar]], strategy: Strategy, cash: float = 1000.0,
        leverage: float = 1.0, fee_bps: dict[str, float] | None = None,
        quantity: float = 1.0, max_drift: float | None = None) -> dict:
    """把所有标的的 bar 按时间戳归并成一条事件流，逐根撮合。"""
    fee_bps = fee_bps or {}
    broker = PaperBroker(cash) if max_drift is None else PaperBroker(cash, max_drift)
    timeline = sorted({b.ts for bars in bars_by_symbol.values() for b in bars})
    closes: dict[str, list[float]] = {s: [] for s in bars_by_symbol}
    prices: dict[str, float] = {}
    cursor = {s: 0 for s in bars_by_symbol}
    equity_curve: list[float] = []
    snapshots: list[dict] = []
    rejected = 0
    t0 = time.perf_counter()

    for ts in timeline:
        for sym, bars in bars_by_symbol.items():
            i = cursor[sym]
            while i < len(bars) and bars[i].ts <= ts:
                closes[sym].append(bars[i].close)
                prices[sym] = bars[i].close
                i += 1
            cursor[sym] = i

        broker.check_exits(ts, prices)
        for sym in bars_by_symbol:
            if not closes[sym] or (cursor[sym] == 0):
                continue
            for intent in strategy.on_bar(ts, sym, closes[sym]):
                act = intent.get("action")
                try:
                    if act == "long" and sym not in broker.positions:
                        broker.open(ts, sym, "long", quantity, prices[sym], leverage,
                                    fee_bps.get(sym, 0.0))
                    elif act == "short" and sym not in broker.positions:
                        broker.open(ts, sym, "short", quantity, prices[sym], leverage,
                                    fee_bps.get(sym, 0.0))
                    elif act == "close" and sym in broker.positions:
                        broker.close(ts, sym, prices[sym], reason="signal")
                except Reject as e:
                    rejected += 1
                    if rejected <= 5:
                        print(f"[reject] {ts} {sym} {act}: {e}", file=sys.stderr)

        snap = broker.snapshot(ts, prices)
        snapshots.append(snap)
        equity_curve.append(snap["equity"])

    elapsed = time.perf_counter() - t0
    trades = broker.trade_log()
    return {
        "symbols": list(bars_by_symbol),
        "bars": len(timeline),
        "trades": trades,
        "rejected": rejected,
        "equity": equity_curve,
        "snapshots": snapshots,
        "metrics": metrics.summary(equity_curve, trades,
                                   periods_per_year=_periods_per_year(timeline)),
        "elapsed_s": round(elapsed, 3),
        "broker": {"balance": broker.balance, "fees_paid": broker.fees_paid,
                   "realized_pnl": broker.realized_pnl},
    }


def _periods_per_year(timeline: list[int]) -> float:
    """按 bar 中位间隔折算一年有多少根，而不是写死 252。"""
    if len(timeline) < 3:
        return 252.0
    diffs = sorted(b - a for a, b in zip(timeline, timeline[1:]) if b > a)
    step = diffs[len(diffs) // 2]
    return 365 * 24 * 3600 / step if step else 252.0


def _fmt(res: dict) -> str:
    m = res["metrics"]
    t = m.get("trade", {})
    sym = ",".join(res["symbols"])
    return "\n".join([
        f"标的 {sym}   bars={res['bars']}  用时 {res['elapsed_s']}s  拒单 {res['rejected']}",
        f"期末权益 {m['final_equity']:.4f}  总收益 {m['total_return']*100:.2f}%  "
        f"CAGR {m['cagr']*100:.2f}%  波动 {m['volatility']*100:.2f}%",
        f"Sharpe {m['sharpe']:.3f}  Sortino {m['sortino']:.3f}  Calmar {m['calmar']:.3f}  "
        f"MaxDD {m['max_drawdown']*100:.2f}%",
        f"交易 {t.get('trades', 0)} 笔  胜率 {t.get('win_rate', 0)*100:.1f}%  "
        f"盈亏比 {t.get('profit_factor', 0):.2f}  费用 {t.get('fees', 0):.4f}",
        f"账户 balance {res['broker']['balance']:.4f}  realized {res['broker']['realized_pnl']:.4f}  "
        f"fees {res['broker']['fees_paid']:.4f}",
    ])


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文表头会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="M3 事件回测（SMA 双均线演示）")
    ap.add_argument("--db", default=os.environ.get("RSC_DB", "data/market.db"))
    ap.add_argument("--symbol", default="INDEX:SPX", help="标的 symbol，多个用逗号分隔")
    ap.add_argument("--rng", default="1y", choices=["1d", "5d", "1y"])
    ap.add_argument("--fast", type=int, default=10)
    ap.add_argument("--slow", type=int, default=30)
    ap.add_argument("--cash", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--quantity", type=float, default=1.0, help="每次开仓的数量（指数/ETF 用 1 份）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbol.split(",") if s.strip()]
    bars = {}
    fees = {}
    for s in syms:
        b = load_bars(args.db, s, args.rng)
        if not b:
            print(f"{s}: 没有 {args.rng} K 线", file=sys.stderr)
            continue
        bars[s] = b
        meta = instrument_meta(args.db, s)
        fees[s] = meta.get("fee_bps") or 0.0
        if args.leverage > (meta.get("max_leverage") or 1.0):
            print(f"{s}: 杠杆 {args.leverage} 超过上限 {meta.get('max_leverage')}",
                  file=sys.stderr)
    if not bars:
        return 2
    res = run(bars, SmaCross(args.fast, args.slow), cash=args.cash,
              leverage=args.leverage, fee_bps=fees, quantity=args.quantity)
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if k != "snapshots"},
                         ensure_ascii=False, indent=1))
    else:
        print(_fmt(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
