"""行情采集器：每分钟拉一次热门池，落 SQLite。

预算：1 req/min（省下 24 次给策略与执行）。
用法：
    python -m rscbot.collector --minutes 60
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
from rscbot.store import Store  # noqa: E402

log = logging.getLogger("rscbot.collector")
_stop = False


def _sigterm(*_):
    global _stop
    _stop = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="0 = 无限")
    ap.add_argument("--db", default=os.environ.get("RSC_DB", "data/market.db"))
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--full-every", type=float, default=1800.0,
                    help="全量 state（含 1638 标的元数据）刷新间隔秒")
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
    log.info("db=%s quotes=%d", args.db, store.count("quotes"))

    t_end = time.time() + args.minutes * 60 if args.minutes else float("inf")
    last_full = 0.0
    ticks = 0

    while not _stop and time.time() < t_end:
        t0 = time.time()
        try:
            if t0 - last_full >= args.full_every:
                full = c.state(priority=4)
                store.put_instruments(full["instruments"])
                store.put_positions(int(t0), full.get("positions", []))
                store.put_orders(int(t0), full.get("orders", []))
                store.put_portfolio(int(t0), full["wallet"], full["portfolio"])
                last_full = t0
                log.info("full state: %d instruments", len(full["instruments"]))

            s = c.state("market", priority=2)
            ts = int(t0)
            for i in s.get("instruments", []):
                store.put_tick(ts, i)
            store.put_positions(ts, s.get("positions", []))
            store.put_portfolio(ts, s["wallet"], s["portfolio"])
            store.commit()
            ticks += 1
            log.info("tick %d: %d quotes, wallet=%s", ticks,
                     len(s.get("instruments", [])), s["wallet"]["balance"])
        except Exception as e:  # noqa: BLE001
            log.exception("collect failed: %s", e)
            store.log_event("collect_error", {"err": str(e)})

        # 对齐到下一分钟
        sleep = args.interval - (time.time() - t0)
        while sleep > 0 and not _stop and time.time() < t_end:
            time.sleep(min(1.0, sleep))
            sleep -= 1.0

    log.info("stopped: %d ticks, requests=%s errors=%s",
             ticks, c.stats["requests"], c.stats["errors"])
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
