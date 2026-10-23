"""行情采集器：热门池 60s + 自选池分层轮询 + K 线回补。

预算（客户端自限 25 req/min，服务端 30）：
  热门池  state.json?section=market      1 req/min
  自选池  history.json?range=1d (5m 线)  ~12 req/min（60 标的 / 300s）
  回补    history.json?range=5d|1y        剩余额度，--bar-gap 控制最小间隔
  全量    state.json（1638 标的元数据）    每 --full-every 秒 1 次（默认 30 min）

用法：
    python -m rscbot.collector --minutes 0
    python -m rscbot.collector --no-bars          # 只跑热门池（旧行为）
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from rscbot.client import Client  # noqa: E402
from rscbot.poller import BarPoller, load_watchlist  # noqa: E402
from rscbot.store import Store  # noqa: E402

log = logging.getLogger("rscbot.collector")
_stop = False


def _sigterm(*_):
    global _stop
    _stop = True


def _close_of(candles: list[dict]) -> float | None:
    """最后一根 K 线的收盘价，用作看板报价。"""
    if not candles:
        return None
    try:
        return float(candles[-1].get("close"))
    except (TypeError, ValueError):
        return None


def market_tick(c: Client, store: Store, args, last_full: float) -> tuple[float, int, dict | None]:
    """全量刷新（按需）+ 热门池 tick。返回 (last_full, quotes, full_js)。"""
    t0 = time.time()
    full_js = None
    if t0 - last_full >= args.full_every:
        full = c.state(priority=4)
        store.put_instruments(full["instruments"])
        store.put_positions(int(t0), full.get("positions", []))
        store.put_orders(int(t0), full.get("orders", []))
        store.put_portfolio(int(t0), full["wallet"], full["portfolio"])
        last_full = t0
        full_js = full
        log.info("full state: %d instruments", len(full["instruments"]))

    s = c.state("market", priority=2)
    ts = int(t0)
    for i in s.get("instruments", []):
        store.put_tick(ts, i)
    store.put_positions(ts, s.get("positions", []))
    store.put_portfolio(ts, s["wallet"], s["portfolio"])
    store.commit()
    log.info("tick %s: %d quotes, wallet=%s", ts, len(s.get("instruments", [])),
             s["wallet"]["balance"])
    return last_full, len(s.get("instruments", [])), full_js


def bar_request(c: Client, store: Store, poller: BarPoller, symbol: str, rng: str) -> bool:
    """拉一个标的的一个 range，写 bars（1d 额外补一条报价给看板）。"""
    iid = poller.ids[symbol]
    t0 = time.time()
    js = c.history(iid, rng)
    candles = js.get("candles") or []
    n = store.put_bars(iid, rng, candles)
    if rng == "1d":
        px = _close_of(candles)
        if px is not None:
            store.put_history_quote(int(t0), iid, symbol, px)
    store.commit()
    poller.stats["bars"] += n
    poller.done(symbol, rng)
    log.debug("bars %s %s: %d rows (%.1fs)", symbol, rng, n, time.time() - t0)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="0 = 无限")
    ap.add_argument("--db", default=os.environ.get("RSC_DB", "data/market.db"))
    ap.add_argument("--interval", type=float, default=60.0, help="热门池 tick 间隔秒")
    ap.add_argument("--full-every", type=float, default=1800.0,
                    help="全量 state（含 1638 标的元数据）刷新间隔秒")
    ap.add_argument("--watchlist", default=None, help="自选池 JSON，默认 config/watchlist.json")
    ap.add_argument("--bar-gap", type=float, default=3.0,
                    help="两次 K 线请求的最小间隔秒（3s ≈ 20 req/min 上限）")
    ap.add_argument("--no-bars", action="store_true", help="只跑热门池，不拉 K 线")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    signal.signal(signal.SIGINT, _sigterm)
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (AttributeError, ValueError):
        pass

    c = Client(os.environ["RSC_EMAIL"], os.environ["RSC_PASSWORD"])
    c.login()
    store = Store(args.db)
    store.checkpoint()
    log.info("db=%s quotes=%d instruments=%d", args.db, store.count("quotes"),
             store.count("instruments"))

    poller: BarPoller | None = None
    if not args.no_bars:
        wl = load_watchlist(args.watchlist)
        poller = BarPoller(store, wl)
        n = poller.refresh_ids()
        if n:
            poller.seed_backfill()
        log.info("tier B: %d/%d symbols mapped, interval %.0fs, bars=%d",
                 n, len(poller.symbols), poller.interval, store.count("bars"))

    t_end = time.time() + args.minutes * 60 if args.minutes else float("inf")
    last_full = 0.0
    ticks = 0
    next_tick = time.time()
    next_bar = 0.0
    report_at = time.time() + 300

    while not _stop and time.time() < t_end:
        now = time.time()
        try:
            if now >= next_tick:
                last_full, _, full_js = market_tick(c, store, args, last_full)
                ticks += 1
                if full_js is not None:
                    # 元数据刷新后重新映射标的，顺便把主库 WAL 落盘
                    store.checkpoint()
                    if poller is not None:
                        poller.refresh_ids()
                        if not poller.seeded:
                            poller.seed_backfill()
                next_tick = now + args.interval
                continue

            if poller is not None and now >= next_bar:
                act = poller.take(now)
                if act:
                    try:
                        bar_request(c, store, poller, act[0], act[1])
                    except Exception as e:  # noqa: BLE001
                        poller.fail(act[0], act[1])
                        log.warning("bar %s %s failed: %s", act[0], act[1], e)
                    next_bar = time.time() + args.bar_gap
                elif poller.queue:
                    next_bar = now + 0.5
                else:
                    nd = poller.next_due(now)
                    next_bar = nd if nd is not None else now + 5
                continue

            if poller is not None and time.time() >= report_at:
                log.info("pipeline: %s", poller.summary())
                report_at = time.time() + 300

            wake = next_tick
            if poller is not None:
                wake = min(wake, next_bar) if next_bar else wake
            time.sleep(max(0.05, min(1.0, wake - time.time())))
        except Exception as e:  # noqa: BLE001
            log.exception("collect failed: %s", e)
            store.log_event("collect_error", {"err": str(e)})
            time.sleep(1.0)

    log.info("stopped: %d ticks, requests=%s errors=%s", ticks,
             c.stats["requests"], c.stats["errors"])
    store.checkpoint()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
