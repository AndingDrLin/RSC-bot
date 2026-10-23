"""SQLite 行情库。单文件、无服务、可被 pandas/duckdb 直接读。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS quotes (
  ts            INTEGER NOT NULL,
  instrument_id INTEGER NOT NULL,
  symbol        TEXT,
  price         REAL,
  bid           REAL,
  ask           REAL,
  low           REAL,
  high          REAL,
  prev_close    REAL,
  delay_seconds INTEGER,
  market_closed INTEGER,
  source        TEXT,
  PRIMARY KEY (ts, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_quotes_sym ON quotes(symbol, ts);

CREATE TABLE IF NOT EXISTS instruments (
  id            INTEGER PRIMARY KEY,
  symbol        TEXT,
  name          TEXT,
  exchange      TEXT,
  category      TEXT,
  currency      TEXT,
  fee_bps       REAL,
  max_leverage  REAL,
  minimum       REAL,
  step          REAL,
  close_only    INTEGER,
  execution_mode TEXT,
  updated_at    INTEGER
);

CREATE TABLE IF NOT EXISTS positions (
  ts            INTEGER NOT NULL,
  position_id   INTEGER NOT NULL,
  instrument_id INTEGER,
  symbol        TEXT,
  side          TEXT,
  leverage      REAL,
  quantity      REAL,
  average       REAL,
  margin        REAL,
  pnl           REAL,
  equity        REAL,
  liquidation   REAL,
  take_profit   REAL,
  stop_loss     REAL,
  PRIMARY KEY (ts, position_id)
);

CREATE TABLE IF NOT EXISTS orders (
  ts          INTEGER NOT NULL,
  order_id    INTEGER NOT NULL,
  instrument_id INTEGER,
  symbol      TEXT,
  side        TEXT,
  status      TEXT,
  quantity    REAL,
  leverage    REAL,
  price       REAL,
  fee         REAL,
  pnl         REAL,
  created_at  TEXT,
  PRIMARY KEY (ts, order_id)
);

CREATE TABLE IF NOT EXISTS portfolio (
  ts        INTEGER PRIMARY KEY,
  balance   REAL,
  reserved  REAL,
  notional  REAL,
  margin    REAL,
  pnl       REAL,
  equity    REAL
);

CREATE TABLE IF NOT EXISTS events (
  ts      INTEGER,
  kind    TEXT,
  payload TEXT
);

-- K 线：来自 history.json。rng 是请求的 range（1d->5m、5d->1h、1y->1d），
-- ts 为 UTC epoch 秒（at 字段是 UTC ISO8601，如 2026-10-23T15:20:00Z）。
CREATE TABLE IF NOT EXISTS bars (
  instrument_id INTEGER NOT NULL,
  rng           TEXT    NOT NULL,
  ts            INTEGER NOT NULL,
  open          REAL,
  high          REAL,
  low           REAL,
  close         REAL,
  volume        REAL,
  PRIMARY KEY (instrument_id, rng, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(rng, ts);

-- 采集器自己的状态（回补游标、自选池指纹等）
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ── 写入 ────────────────────────────────────────────
    def put_instruments(self, insts: list[dict]):
        now = int(time.time())
        rows = [
            (i["id"], i["symbol"], i.get("name"), i.get("exchange"), i.get("category"),
             i.get("currency"), i.get("fee_bps"), i.get("max_leverage"),
             i.get("minimum"), i.get("step"), 1 if i.get("close_only") else 0,
             i.get("execution_mode"), now)
            for i in insts
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        self.conn.commit()

    def put_tick(self, ts: int, inst: dict):
        q = inst.get("quote") or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, inst["id"], inst["symbol"], _f(q.get("price")), _f(q.get("bid")),
             _f(q.get("ask")), _f(q.get("low")), _f(q.get("high")),
             _f(q.get("previous_close")), q.get("delay_seconds"),
             1 if inst.get("market_closed") else 0, q.get("source")),
        )

    def commit(self):
        self.conn.commit()

    def put_positions(self, ts: int, positions: list[dict]):
        rows = [
            (ts, p["id"], p.get("instrument_id"), p.get("symbol"), p.get("side"),
             _f(p.get("leverage")), _f(p.get("quantity")), _f(p.get("average")),
             _f(p.get("margin")), _f(p.get("pnl")), _f(p.get("equity")),
             _f(p.get("liquidation")), _f(p.get("take_profit")), _f(p.get("stop_loss")))
            for p in positions
        ]
        self.conn.executemany("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def put_orders(self, ts: int, orders: list[dict]):
        rows = []
        for o in orders:
            d = o.get("details") or {}
            rows.append((ts, o["id"], o.get("instrument_id"), o.get("symbol"),
                         o.get("side"), o.get("status"), _f(o.get("quantity")),
                         _f(o.get("leverage")), _f(d.get("price")), _f(d.get("fee")),
                         _f(d.get("pnl")), o.get("created_at")))
        self.conn.executemany("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def put_bars(self, instrument_id: int, rng: str, candles: list[dict]) -> int:
        """写入 history.json 的 K 线；at 是 UTC ISO8601，转 epoch 秒。"""
        rows = []
        for c in candles or ():
            ts = _iso_to_epoch(c.get("at"))
            if ts is None:
                continue
            rows.append((instrument_id, rng, ts, _f(c.get("open")), _f(c.get("high")),
                         _f(c.get("low")), _f(c.get("close")), _f(c.get("volume"))))
        if rows:
            self.conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def put_history_quote(self, ts: int, instrument_id: int, symbol: str, price) -> None:
        """用 K 线收盘价补一条报价，让看板能看到自选池行情。
        prev_close 故意留空：history 没有可靠的前收，乱填会让涨跌幅失真。"""
        if price is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO quotes (ts,instrument_id,symbol,price,source) "
            "VALUES (?,?,?,?,'history')",
            (ts, instrument_id, symbol, price),
        )

    def put_portfolio(self, ts: int, wallet: dict, pf: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO portfolio VALUES (?,?,?,?,?,?,?)",
            (ts, _f(wallet.get("balance")), _f(wallet.get("reserved")),
             _f(pf.get("notional")), _f(pf.get("margin")), _f(pf.get("pnl")),
             _f(pf.get("equity"))),
        )

    def log_event(self, kind: str, payload: dict):
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?)",
            (int(time.time()), kind, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()

    # ── 查询 ────────────────────────────────────────────
    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def last_tick_ts(self) -> int | None:
        r = self.conn.execute("SELECT MAX(ts) FROM quotes").fetchone()
        return r[0] if r else None

    def bar_last_ts(self, instrument_id: int, rng: str) -> int | None:
        r = self.conn.execute(
            "SELECT MAX(ts) FROM bars WHERE instrument_id=? AND rng=?", (instrument_id, rng)
        ).fetchone()
        return r[0] if r else None

    def bar_stats(self) -> dict:
        try:
            rows = self.conn.execute(
                "SELECT rng, COUNT(*), COUNT(DISTINCT instrument_id), MAX(ts) "
                "FROM bars GROUP BY rng"
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {r[0]: {"rows": r[1], "symbols": r[2], "last_ts": r[3]} for r in rows}

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """把 WAL 写回主库。不做的话 market.db 只有几 KB，单独 cp 出来是空库。"""
        self.conn.commit()
        try:
            self.conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except sqlite3.Error:
            pass


def db_size_mb(path: str) -> float:
    """数据库总占用：主库 + WAL + SHM（看板一直显示 0.0 就是只算了主库）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            total += os.path.getsize(p)
    return total / 1e6


def _iso_to_epoch(s) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
