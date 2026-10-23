"""M2-T6 数据质量：覆盖、缺口率、新鲜度，以及与交易所 history 的对账。

为什么单独一个模块：验收口径（docs/03 §1.3）要"缺口率 < 2%"和"5m bar 与
history 对账一致"，靠手敲 SQL 没法每天复现。这里把三者做成同一份报告：

  coverage   每个标的的 bar 数 vs 时间跨度内应有的槽位数 → 缺口率
  freshness  最后一根 K 线距今多久 → 活标的 / 归档标的（交易所只发旧数据）
  reconcile  抽样重新拉 history.json，逐根比对 close（可选，要占额度）

口径说明：没有交易日历（M2-T5 未做），缺口率用"首末根之间的应有槽位数"做分母，
所以它衡量的是"已经采到的窗口内有没有洞"，不惩罚"整天没采到"。休市时段本来
就没有 5m bar，连续缺口会被算成缺失——这是当前口径的已知偏差，等日历落地再收窄。

用法：
    python -m rscbot.quality --db data/market.db
    python -m rscbot.quality --db data/market.db --json
    python -m rscbot.quality --reconcile 3          # 抽 3 个标的与交易所对账
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

# rng -> (bar 间隔秒, 多久算"新鲜")
BAR_STEP = {"1d": 300, "5d": 3600, "1y": 86400}
FRESH_WINDOW = {"1d": 24 * 3600, "5d": 48 * 3600, "1y": 7 * 86400}
# 交易所对一部分标的只发归档数据：1d 最后一根比这还旧，就当归档处理
ARCHIVE_AFTER = 72 * 3600


def connect(db: str) -> sqlite3.Connection:
    """只读连接：collector 在写（WAL），不要抢写锁。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def coverage(conn: sqlite3.Connection, rng: str = "1d") -> dict:
    """每个标的的缺口率：缺口 = 首末根之间应有的槽位数 - 实际根数。"""
    if not _has_table(conn, "bars"):
        return {"rng": rng, "symbols": 0, "rows": 0, "expected": 0, "gaps": 0,
                "gap_rate": 0.0, "worst": []}
    step = BAR_STEP.get(rng, 300)
    rows = conn.execute(
        """SELECT b.instrument_id, i.symbol, i.category, COUNT(*) AS n,
                  MIN(b.ts) AS first_ts, MAX(b.ts) AS last_ts
             FROM bars b LEFT JOIN instruments i ON i.id = b.instrument_id
            WHERE b.rng = ?
            GROUP BY b.instrument_id""",
        (rng,),
    ).fetchall()
    total_slots = total_rows = 0
    worst = []
    for r in rows:
        span = (r["last_ts"] - r["first_ts"]) // step + 1 if r["n"] > 1 else 1
        gaps = max(0, span - r["n"])
        total_slots += span
        total_rows += r["n"]
        if span > 10:
            worst.append({
                "symbol": r["symbol"] or str(r["instrument_id"]),
                "category": r["category"],
                "bars": r["n"],
                "expected": span,
                "gap_rate": round(gaps / span, 4),
            })
    worst.sort(key=lambda x: x["gap_rate"], reverse=True)
    gaps = max(0, total_slots - total_rows)
    return {
        "rng": rng,
        "symbols": len(rows),
        "rows": total_rows,
        "expected": total_slots,
        "gaps": gaps,
        "gap_rate": round(gaps / total_slots, 4) if total_slots else 0.0,
        "worst": worst[:8],
    }


def freshness(conn: sqlite3.Connection, now: int | None = None) -> dict:
    """每个 rng 有多少标的"还活着"，以及活标的的市场分布。"""
    now = int(time.time()) if now is None else now
    out: dict = {"now": now, "rng": {}, "live_1d_by_category": {}}
    if not _has_table(conn, "bars"):
        return out
    for rng, window in FRESH_WINDOW.items():
        rows = conn.execute(
            """SELECT b.instrument_id, i.category, i.symbol, MAX(b.ts) AS last_ts
                 FROM bars b LEFT JOIN instruments i ON i.id = b.instrument_id
                WHERE b.rng = ? GROUP BY b.instrument_id""",
            (rng,),
        ).fetchall()
        live = [r for r in rows if r["last_ts"] and now - r["last_ts"] <= window]
        archived = [r for r in rows if r["last_ts"] and now - r["last_ts"] > ARCHIVE_AFTER]
        out["rng"][rng] = {
            "symbols": len(rows),
            "live": len(live),
            "archived": len(archived),
            "live_pct": round(len(live) / len(rows), 3) if rows else 0.0,
        }
        if rng == "1d":
            by_cat: dict[str, int] = {}
            for r in live:
                key = r["category"] or "?"
                by_cat[key] = by_cat.get(key, 0) + 1
            out["live_1d_by_category"] = dict(sorted(by_cat.items(), key=lambda kv: -kv[1]))
    return out


def reconcile(conn: sqlite3.Connection, client, rng: str = "1d", sample: int = 3,
              now: int | None = None) -> dict:
    """抽样重拉交易所 history，逐根比对 close；同源写入应当完全一致。"""
    now = int(time.time()) if now is None else now
    window = FRESH_WINDOW.get(rng, 86400)
    rows = conn.execute(
        """SELECT b.instrument_id, i.symbol, MAX(b.ts) AS last_ts
             FROM bars b LEFT JOIN instruments i ON i.id = b.instrument_id
            WHERE b.rng = ? GROUP BY b.instrument_id
            HAVING MAX(b.ts) >= ? LIMIT ?""",
        (rng, now - window, sample),
    ).fetchall()
    results = []
    for r in rows:
        iid, sym = r["instrument_id"], r["symbol"] or str(r["instrument_id"])
        local = {
            row["ts"]: row["close"]
            for row in conn.execute(
                "SELECT ts, close FROM bars WHERE instrument_id=? AND rng=?", (iid, rng)
            )
        }
        try:
            js = client.history(iid, rng)
        except Exception as e:  # noqa: BLE001
            results.append({"symbol": sym, "error": str(e)[:120]})
            continue
        checked = diffs = missing = 0
        max_diff = 0.0
        for c in js.get("candles") or []:
            ts = _iso_to_epoch(c.get("at"))
            if ts is None or ts not in local or local[ts] is None:
                missing += 1
                continue
            try:
                d = abs(float(c["close"]) - float(local[ts]))
            except (TypeError, ValueError, KeyError):
                continue
            checked += 1
            max_diff = max(max_diff, d)
            if d > 1e-9:
                diffs += 1
        results.append({
            "symbol": sym, "candles": len(js.get("candles") or []),
            "checked": checked, "mismatch": diffs, "absent_locally": missing,
            "max_abs_diff": max_diff, "archived": js.get("archived"),
        })
    return {"rng": rng, "sample": len(results), "results": results}


def report(db: str, now: int | None = None) -> dict:
    """不联网的质量报告：覆盖 + 新鲜度 + 库体积。"""
    now = int(time.time()) if now is None else now
    if not os.path.exists(db):
        return {"error": f"db not found: {db}"}
    conn = connect(db)
    try:
        out = {
            "generated_at": now,
            "db": db,
            "db_mb": _db_mb(db),
            "coverage": {rng: coverage(conn, rng) for rng in BAR_STEP},
            "freshness": freshness(conn, now),
        }
        if _has_table(conn, "quotes"):
            last = conn.execute("SELECT MAX(ts) AS m, COUNT(*) AS n FROM quotes").fetchone()
            out["quotes"] = {
                "rows": last["n"],
                "last_ts": last["m"],
                "age_s": (now - last["m"]) if last["m"] else None,
            }
        return out
    finally:
        conn.close()


def _db_mb(path: str) -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            total += os.path.getsize(p)
    return round(total / 1e6, 2)


def _iso_to_epoch(s) -> int | None:
    from datetime import datetime
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _fmt(rep: dict) -> str:
    if "error" in rep:
        return rep["error"]
    lines = [
        f"db={rep['db']}  {rep['db_mb']} MB  generated={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rep['generated_at']))}",
        "",
        f"{'range':5s} {'标的':>4s} {'行数':>8s} {'应有':>8s} {'缺口率':>7s}",
    ]
    for rng, c in rep["coverage"].items():
        lines.append(f"{rng:5s} {c.get('symbols', 0):4d} {c.get('rows', 0):8d} "
                     f"{c.get('expected', 0):8d} {c.get('gap_rate', 0)*100:6.2f}%")
    lines.append("")
    for rng, f in rep["freshness"]["rng"].items():
        lines.append(f"{rng:5s} 活 {f['live']:3d}/{f['symbols']:3d}  归档 {f['archived']:3d}  ({f['live_pct']*100:.0f}%)")
    live = rep["freshness"]["live_1d_by_category"]
    if live:
        lines.append("活标的分布: " + "  ".join(f"{k}={v}" for k, v in live.items()))
    worst = rep["coverage"]["1d"]["worst"]
    if worst:
        lines.append("")
        lines.append("缺口最大的标的（1d）:")
        for w in worst[:5]:
            lines.append(f"  {w['symbol']:16s} {w['bars']:5d}/{w['expected']:5d}  {w['gap_rate']*100:5.1f}%")
    if rep.get("quotes"):
        q = rep["quotes"]
        lines.append("")
        lines.append(f"quotes: {q['rows']} 行，最新 {(q['age_s'] or 0)/60:.1f} 分钟前")
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
    ap = argparse.ArgumentParser(description="M2 数据质量报告")
    ap.add_argument("--db", default=os.environ.get("RSC_DB", "data/market.db"))
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--reconcile", type=int, default=0, metavar="N",
                    help="抽 N 个标的重拉 history.json 对账（占额度）")
    ap.add_argument("--rng", default="1d", choices=sorted(BAR_STEP))
    args = ap.parse_args()

    rep = report(args.db)
    if "error" in rep:
        print(rep["error"], file=sys.stderr)
        return 2
    if args.reconcile:
        from rscbot.client import Client  # 延迟导入：不联网时零依赖
        c = Client(os.environ["RSC_EMAIL"], os.environ["RSC_PASSWORD"])
        c.login()
        conn = connect(args.db)
        try:
            rep["reconcile"] = reconcile(conn, c, args.rng, args.reconcile)
        finally:
            conn.close()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(_fmt(rep))
        if "reconcile" in rep:
            print("\n对账:")
            for r in rep["reconcile"]["results"]:
                if "error" in r:
                    print(f"  {r['symbol']:16s} 失败: {r['error']}")
                else:
                    print(f"  {r['symbol']:16s} {r['checked']:4d} 根比对  "
                          f"不一致 {r['mismatch']}  最大差 {r['max_abs_diff']:.3g}  "
                          f"本地缺 {r['absent_locally']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
