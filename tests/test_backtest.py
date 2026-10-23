"""M3-T3 引擎：合成 K 线跑通事件流，并验证复现性。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rscbot.backtest import Bar, SmaCross, run  # noqa: E402


def _trend_bars():
    """先横盘 30 根，再涨 20 根、跌 30 根：确保快慢线真的发生上穿和下穿。"""
    return ([100.0] * 30 + [100.0 + i for i in range(20)]
            + [120.0 - 1.5 * i for i in range(30)])


def _bars(closes):
    return [Bar(ts=i * 300, open=c, high=c, low=c, close=c, volume=1.0)
            for i, c in enumerate(closes)]


class EngineTest(unittest.TestCase):
    def test_equity_length_matches_timeline(self):
        res = run({"X": _bars([100.0 + i for i in range(60)])}, SmaCross(5, 20), cash=1000.0)
        self.assertEqual(res["bars"], 60)
        self.assertEqual(len(res["equity"]), 60)
        self.assertEqual(res["rejected"], 0)

    def test_cross_produces_round_trip(self):
        closes = _trend_bars()
        res = run({"X": _bars(closes)}, SmaCross(5, 20), cash=1000.0, fee_bps={"X": 0.0})
        self.assertGreaterEqual(len(res["trades"]), 1)
        self.assertEqual(res["trades"][0]["symbol"], "X")
        self.assertEqual(res["trades"][0]["side"], "long")

    def test_deterministic(self):
        closes = _trend_bars()
        a = run({"X": _bars(closes)}, SmaCross(5, 20))
        b = run({"X": _bars(closes)}, SmaCross(5, 20))
        self.assertEqual(a["equity"], b["equity"])
        self.assertEqual(a["metrics"], b["metrics"])

    def test_fees_reduce_equity(self):
        closes = _trend_bars()
        free = run({"X": _bars(closes)}, SmaCross(5, 20), fee_bps={"X": 0.0})
        paid = run({"X": _bars(closes)}, SmaCross(5, 20), fee_bps={"X": 20.0})
        self.assertGreater(paid["broker"]["fees_paid"], 0)
        self.assertLess(paid["metrics"]["final_equity"], free["metrics"]["final_equity"])

    def test_multi_symbol_timeline(self):
        res = run({"A": _bars([100.0 + i for i in range(40)]),
                   "B": _bars([50.0 + 0.5 * i for i in range(40)])},
                  SmaCross(5, 20), cash=1000.0)
        self.assertEqual(res["bars"], 40)
        self.assertEqual(set(res["symbols"]), {"A", "B"})


if __name__ == "__main__":
    unittest.main()
