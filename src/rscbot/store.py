"""SQLite 行情库。单文件、无服务、可被 pandas/duckdb 直接读。"""

from __future__ import annotations

import json
import os
import sqlite3
import time

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


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
