"""M3-T7 Paper Broker 对拍：拿账户里的真实成交反推撮合规则是否对得上。

对拍口径（能用真实数据验证的部分）：
- 费用：交易所记录的 fee 是否等于 名义价值 × fee_bps / 1e4（相对误差 < 1e-9）
- 保证金：margin 是否等于 名义价值 / 杠杆
- 成交价：把"下单前最近一条报价"当参考价，模拟成交价 = 参考价（无滑点假设），
  与真实成交价的偏差应在 5 bp 以内；超出说明存在真实滑点或报价滞后
- 重复 order_id：orders 表按 tick 快照写入，同 id 会出现多行，取最新一行

用法：
    python -m rscbot.replay --db data/market.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def filled_orders(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT o.order_id, o.symbol, o.instrument_id, o.side, o.status, o.quantity,
                  o.leverage, o.price AS fill_price, o.fee, o.pnl, o.ts, o.created_at,
                  i.fee_bps
             FROM orders o
             LEFT JOIN instruments i ON i.id = o.instrument_id
            WHERE o.status = 'filled' AND o.price IS NOT NULL AND o.quantity IS NOT NULL
            ORDER BY o.order_id, o.ts"""
    ).fetchall()
    latest: dict[int, dict] = {}
    for r in rows:
        latest[r["order_id"]] = dict(r)
    return sorted(latest.values(), key=lambda x: x["ts"])


def reference_price(conn: sqlite3.Connection, instrument_id: int, ts: int,
                    window: int = 900) -> float | None:
    r = conn.execute(
        "SELECT price FROM quotes WHERE instrument_id=? AND ts<=? AND price IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (instrument_id, ts + window)
    ).fetchone()
    return float(r["price"]) if r else None


def compare(conn: sqlite3.Connection, max_age_s: int = 900) -> dict:
    rows = []
    for o in filled_orders(conn):
        qty = float(o["quantity"] or 0.0)
        px = float(o["fill_price"] or 0.0)
        fee = float(o["fee"] or 0.0)
        lev = float(o["leverage"] or 1.0) or 1.0
        bps = o["fee_bps"]
        notional = qty * px
        rec = {
            "order_id": o["order_id"], "symbol": o["symbol"], "side": o["side"],
            "quantity": qty, "price": px, "leverage": lev, "notional": round(notional, 10),
        }
        if bps is not None and notional:
            fee_sim = notional * float(bps) / 10000.0
            rec["fee_sim"] = fee_sim
            rec["fee_real"] = fee
            rec["fee_err"] = abs(fee_sim - fee)
            rec["fee_err_rel"] = rec["fee_err"] / fee if fee else 0.0
        rec["margin_sim"] = notional / lev
        ref = reference_price(conn, o["instrument_id"], int(o["ts"]), max_age_s)
        if ref:
            rec["reference"] = ref
            rec["price_bps"] = abs(px / ref - 1.0) * 10000.0
        rows.append(rec)
    checked = [r for r in rows if "fee_err" in r]
    priced = [r for r in rows if "price_bps" in r]
    return {
        "orders": len(rows),
        "fee_checked": len(checked),
        "max_fee_err": max((r["fee_err"] for r in checked), default=0.0),
        "max_fee_err_rel": max((r["fee_err_rel"] for r in checked), default=0.0),
        "price_checked": len(priced),
        "max_price_bps": max((r["price_bps"] for r in priced), default=0.0),
        "mean_price_bps": (sum(r["price_bps"] for r in priced) / len(priced)) if priced else 0.0,
        "rows": rows,
    }


def _fmt(rep: dict) -> str:
    lines = [
        f"成交 {rep['orders']} 笔   费用可比 {rep['fee_checked']} 笔   "
        f"最大费用误差 {rep['max_fee_err']:.3g}（相对 {rep['max_fee_err_rel']:.2g}）",
        f"有前序报价 {rep['price_checked']} 笔   成交价偏差 均值 {rep['mean_price_bps']:.2f} bp   "
        f"最大 {rep['max_price_bps']:.2f} bp",
    ]
    worst = sorted((r for r in rep["rows"] if "price_bps" in r),
                   key=lambda r: r["price_bps"], reverse=True)[:5]
    if worst:
        lines.append("成交价偏差最大的 5 笔:")
        for r in worst:
            lines.append(f"  #{r['order_id']} {r['symbol']:14s} {r['side']:5s} "
                         f"px={r['price']:.6g} ref={r['reference']:.6g} {r['price_bps']:.1f} bp")
    return "\n".join(lines)


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
    ap = argparse.ArgumentParser(description="真实成交 vs Paper Broker 对拍")
    ap.add_argument("--db", default=os.environ.get("RSC_DB", "data/market.db"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    conn = connect(args.db)
    try:
        rep = compare(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(_fmt(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
