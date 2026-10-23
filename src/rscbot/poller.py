"""分层轮询调度：Tier B 自选池（5 分钟）+ 历史回补（1d/5d/1y）。

为什么不"一次拉 60 个标的"：全站 30 req/min、客户端自限 25 req/min，60 个请求
集中打出去会占满整个窗口，把 60 秒一次的热门池 tick 挤掉（rate limiter 会 sleep
到下一个窗口）。这里把自选池摊平到整个 5 分钟窗口（约每 5 秒 1 个），回补队列
只吃剩余额度；collector 侧再用 --bar-gap 控制两次 K 线请求的最小间隔。

调度优先级：热门池 tick（collector 主循环）> Tier B > 回补。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("rscbot.poller")

DEFAULT_INTERVAL = 300.0
# 多久算"该回补"：5d/1h 半天补一次足够，1y/日线两天补一次
DEFAULT_STALE = {"5d": 6 * 3600, "1y": 2 * 86400}
MAX_TRIES = 3


def load_watchlist(path: str | os.PathLike | None = None) -> dict:
    """读 config/watchlist.json。文件缺失时返回空池（只跑热门池）。"""
    if path is None:
        path = os.environ.get("RSC_WATCHLIST") or (
            Path(__file__).resolve().parents[2] / "config" / "watchlist.json"
        )
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("watchlist %s unreadable (%s), tier B disabled", path, e)
        return {"symbols": [], "interval_s": DEFAULT_INTERVAL, "backfill": dict(DEFAULT_STALE)}
    syms = [str(s).strip() for s in cfg.get("symbols") or [] if str(s).strip()]
    if len(syms) < 60:
        log.warning("watchlist has only %d symbols (acceptance needs >=60)", len(syms))
    return {
        "symbols": syms,
        "interval_s": float(cfg.get("interval_s") or DEFAULT_INTERVAL),
        "backfill": {k: int(v) for k, v in (cfg.get("backfill") or DEFAULT_STALE).items()},
        "path": str(path),
    }


class BarPoller:
    def __init__(self, store, watchlist: dict):
        self.store = store
        self.symbols = list(watchlist.get("symbols") or [])
        self.interval = float(watchlist.get("interval_s") or DEFAULT_INTERVAL)
        self.stale = dict(watchlist.get("backfill") or DEFAULT_STALE)
        self.ids: dict[str, int] = {}
        self.next_at: dict[str, float] = {}
        self.queue: deque[tuple[str, str]] = deque()
        self.tries: dict[tuple[str, str], int] = {}
        self.seeded = False
        self.stats = {"bars": 0, "ok": 0, "errors": 0, "backfill": 0}

    # ── 标的映射 ────────────────────────────────────────
    def refresh_ids(self) -> int:
        """从 instruments 表解出 symbol -> id，并把首轮时间摊平。"""
        want = set(self.symbols)
        rows = self.store.conn.execute("SELECT symbol, id FROM instruments").fetchall()
        self.ids = {sym: int(i) for sym, i in rows if sym in want}
        missing = [s for s in self.symbols if s not in self.ids]
        if missing:
            log.warning("watchlist symbols not in catalog (%d): %s", len(missing),
                        ", ".join(missing[:8]))
        if not self.ids:
            return 0
        step = self.interval / len(self.ids)
        now = time.time()
        for i, sym in enumerate(sorted(self.ids)):
            self.next_at.setdefault(sym, now + i * step)
        return len(self.ids)

    # ── 回补队列 ────────────────────────────────────────
    def seed_backfill(self) -> int:
        """把缺失/过期的 (symbol, 5d|1y) 放进队列。空库时 60 标的 = 120 个请求。"""
        if self.seeded or not self.ids:
            return 0
        now = int(time.time())
        added = 0
        for sym in sorted(self.ids):
            iid = self.ids[sym]
            for rng, keep in self.stale.items():
                last = self.store.bar_last_ts(iid, rng)
                if last is None or now - last > keep:
                    self.queue.append((sym, rng))
                    added += 1
        self.seeded = True
        log.info("backfill queue: %d requests (%d symbols)", added, len(self.ids))
        return added

    # ── 调度 ────────────────────────────────────────────
    def take(self, now: float | None = None) -> tuple[str, str] | None:
        """取下一个该做的请求：先 Tier B（最久没刷的），再回补。"""
        now = time.time() if now is None else now
        due = [(t, sym) for sym, t in self.next_at.items() if t <= now]
        if due:
            due.sort()
            return due[0][1], "1d"
        if self.queue:
            return self.queue.popleft()
        return None

    def next_due(self, now: float | None = None) -> float | None:
        """下一个 Tier B 到期时间，用来算 sleep（回补队列随时可做）。"""
        if not self.next_at:
            return None
        return min(self.next_at.values())

    def done(self, symbol: str, rng: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.stats["ok"] += 1
        if rng == "1d":
            # 固定间隔而不是 now+interval 累加，避免请求慢时相位漂移；
            # 落后超过一个周期就重起相位，不补跑。
            nxt = self.next_at.get(symbol, now) + self.interval
            if nxt <= now:
                nxt = now + self.interval
            self.next_at[symbol] = nxt
        else:
            self.stats["backfill"] += 1
        self.tries.pop((symbol, rng), None)

    def fail(self, symbol: str, rng: str) -> None:
        self.stats["errors"] += 1
        key = (symbol, rng)
        self.tries[key] = self.tries.get(key, 0) + 1
        if self.tries[key] >= MAX_TRIES:
            log.error("giving up %s %s after %d tries", symbol, rng, self.tries[key])
            self.tries.pop(key, None)
            return
        if rng == "1d":
            self.next_at[symbol] = time.time() + 30 * self.tries[key]
        else:
            self.queue.append((symbol, rng))

    def summary(self) -> dict:
        return {
            "symbols": len(self.ids),
            "interval_s": self.interval,
            "backfill_left": len(self.queue),
            **self.stats,
        }
