"""分层轮询调度：Tier B 自选池（5 分钟）+ 候选池 + 历史回补（5d/1y）。

为什么不"一次拉 60 个标的"：全站 30 req/min、客户端自限 25 req/min，60 个请求
集中打出去会占满整个窗口，把 60 秒一次的热门池 tick 挤掉（rate limiter 会 sleep
到下一个窗口）。这里把自选池摊平到整个 5 分钟窗口（约每 5 秒 1 个），回补队列
只吃剩余额度；collector 侧再用 --bar-gap 控制两次 K 线请求的最小间隔。

调度优先级：热门池 tick（collector 主循环）> Tier B > 回补。

关于"归档"（archived）标的：交易所的 history.json 对一部分标的返回
`archived: true` + 几个月前的旧 K 线（2026-07 的数据），同时 state.json 里的
sparkline 却还在更新。实测这类标的是可以"自愈"的（AMEX:PPLT 从 9 月的归档数据
在半小时后变成最新），所以不要放弃，而是把复查间隔从 5 分钟拉长到 30 分钟，
把额度留给活标的。候选池（candidates）= 核心池之外的高流动性标的，用同样的
5 分钟节奏试跑，一旦不再是 archived，它的 K 线就直接进库可用。
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
# 归档/陈旧标的的复查间隔：不给额度压力，又能等到它自愈
DEFAULT_SLOW = 1800.0
# 多久算"该回补"：5d/1h 半天补一次足够，1y/日线两天补一次
DEFAULT_STALE = {"5d": 6 * 3600, "1y": 2 * 86400}
# 1d 的最后一根比这还旧，就当归档处理（72h 容忍周末）
FRESH_MAX_AGE = 72 * 3600
MAX_TRIES = 5


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
        return {"symbols": [], "candidates": [], "interval_s": DEFAULT_INTERVAL,
                "slow_s": DEFAULT_SLOW, "backfill": dict(DEFAULT_STALE)}
    syms = [str(s).strip() for s in cfg.get("symbols") or [] if str(s).strip()]
    cands = [str(s).strip() for s in cfg.get("candidates") or [] if str(s).strip()]
    if len(syms) < 60:
        log.warning("watchlist has only %d symbols (acceptance needs >=60)", len(syms))
    return {
        "symbols": syms,
        "candidates": [c for c in cands if c not in set(syms)],
        "interval_s": float(cfg.get("interval_s") or DEFAULT_INTERVAL),
        "slow_s": float(cfg.get("slow_interval_s") or DEFAULT_SLOW),
        "backfill": {k: int(v) for k, v in (cfg.get("backfill") or DEFAULT_STALE).items()},
        "path": str(path),
    }


class BarPoller:
    def __init__(self, store, watchlist: dict):
        self.store = store
        self.symbols = list(watchlist.get("symbols") or [])
        self.candidates = list(watchlist.get("candidates") or [])
        self.interval = float(watchlist.get("interval_s") or DEFAULT_INTERVAL)
        self.slow_s = float(watchlist.get("slow_s") or DEFAULT_SLOW)
        self.stale = dict(watchlist.get("backfill") or DEFAULT_STALE)
        self.ids: dict[str, int] = {}
        self.next_at: dict[str, float] = {}
        self.slow: dict[str, float] = {}      # symbol -> 归档后的复查间隔
        self.queue: deque[tuple[str, str]] = deque()
        self.tries: dict[tuple[str, str], int] = {}
        self.cool_until = 0.0                  # 被 429 打回后的全局冷却
        self.calls = 0                         # take() 计数，用于 4:1 穿插回补
        self.seeded = False
        self.stats = {"bars": 0, "ok": 0, "errors": 0, "backfill": 0,
                      "archived": 0, "fresh": 0, "recovered": 0}

    @property
    def pool(self) -> list[str]:
        return self.symbols + self.candidates

    def eff_interval(self, symbol: str) -> float:
        return self.slow.get(symbol, self.interval)

    # ── 标的映射 ────────────────────────────────────────
    def refresh_ids(self) -> int:
        """从 instruments 表解出 symbol -> id，并把首轮时间摊平。"""
        want = set(self.pool)
        rows = self.store.conn.execute("SELECT symbol, id FROM instruments").fetchall()
        self.ids = {sym: int(i) for sym, i in rows if sym in want}
        missing = [s for s in self.pool if s not in self.ids]
        if missing:
            log.warning("pool symbols not in catalog (%d): %s", len(missing),
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
        """把核心池里缺失/过期的 (symbol, 5d|1y) 放进队列。空库时 60 标的 = 120 个请求。"""
        if self.seeded or not self.ids:
            return 0
        now = int(time.time())
        added = 0
        for sym in sorted(self.symbols):
            iid = self.ids.get(sym)
            if iid is None:
                continue
            for rng, keep in self.stale.items():
                last = self.store.bar_last_ts(iid, rng)
                if last is None or now - last > keep:
                    self.queue.append((sym, rng))
                    added += 1
        self.seeded = True
        log.info("backfill queue: %d requests (%d symbols)", added, len(self.symbols))
        return added

    # ── 调度 ────────────────────────────────────────────
    def take(self, now: float | None = None) -> tuple[str, str] | None:
        """取下一个该做的请求：Tier B 为主，每 4 个请求让 1 个给回补队列。

        池子涨到 98 个标的后，5 分钟一轮就要 19.6 req/min，不预留额度回补队列
        永远轮不到（97 个回补请求会一直挂着），所以按 4:1 强制穿插。
        """
        now = time.time() if now is None else now
        if now < self.cool_until:
            return None
        due = [(t, sym) for sym, t in self.next_at.items() if t <= now]
        if due:
            due.sort()
        self.calls += 1
        if self.queue and (self.calls % 4 == 0 or not due):
            return self.queue.popleft()
        if due:
            return due[0][1], "1d"
        return None

    def next_due(self, now: float | None = None) -> float | None:
        """下一个可做时间，用来算 sleep（回补队列随时可做）。"""
        cands = list(self.next_at.values())
        if self.queue:
            cands.append(time.time() if now is None else now)
        if self.cool_until:
            cands.append(self.cool_until)
        return min(cands) if cands else None

    def done(self, symbol: str, rng: str, fresh: bool = True,
             now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.stats["ok"] += 1
        if rng == "1d":
            if fresh:
                if symbol in self.slow:
                    self.slow.pop(symbol)
                    self.stats["recovered"] += 1
                    log.info("bar %s back to live (interval %.0fs)", symbol, self.interval)
                self.stats["fresh"] += 1
            # 固定间隔而不是 now+interval 累加，避免请求慢时相位漂移；
            # 落后超过一个周期就重起相位，不补跑。
            step = self.eff_interval(symbol)
            nxt = self.next_at.get(symbol, now) + step
            if nxt <= now:
                nxt = now + step
            self.next_at[symbol] = nxt
        else:
            self.stats["backfill"] += 1
        self.tries.pop((symbol, rng), None)

    def archived(self, symbol: str, rng: str = "1d", now: float | None = None) -> None:
        """返回的是归档数据：降频复查，别占额度。"""
        now = time.time() if now is None else now
        self.stats["ok"] += 1
        self.stats["archived"] += 1
        if symbol not in self.slow:
            log.info("bar %s archived (retry every %.0fs)", symbol, self.slow_s)
        self.slow[symbol] = self.slow_s
        self.next_at[symbol] = now + self.slow_s
        self.tries.pop((symbol, rng), None)

    def fail(self, symbol: str, rng: str, cool: float = 0.0) -> None:
        self.stats["errors"] += 1
        key = (symbol, rng)
        self.tries[key] = self.tries.get(key, 0) + 1
        if cool:
            self.cool_until = max(self.cool_until, time.time() + cool)
            log.warning("rate limited, cooling %.0fs", cool)
        if self.tries[key] >= MAX_TRIES:
            log.error("giving up %s %s after %d tries", symbol, rng, self.tries[key])
            self.tries.pop(key, None)
            if rng == "1d":
                # 不丢标的：只是拉长到归档级复查（下次全量刷新后再试）
                self.next_at[symbol] = time.time() + self.slow_s
            return
        if rng == "1d":
            self.next_at[symbol] = time.time() + 30 * self.tries[key]
        else:
            self.queue.append((symbol, rng))

    def summary(self) -> dict:
        n_slow = len([s for s in self.slow if s in self.ids])
        return {
            "symbols": len(self.symbols),
            "candidates": len(self.candidates),
            "interval_s": self.interval,
            "slow": n_slow,
            "backfill_left": len(self.queue),
            "cooling": round(max(0.0, self.cool_until - time.time()), 1),
            **self.stats,
        }
