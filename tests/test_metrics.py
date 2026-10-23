"""M3-T4 指标：用手算得出的小样本验证公式。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rscbot import metrics  # noqa: E402


class ReturnTest(unittest.TestCase):
    def test_total_return(self):
        self.assertAlmostEqual(metrics.total_return([100.0, 110.0, 99.0, 108.9]), 0.089, places=12)

    def test_returns_series(self):
        r = metrics.returns([100.0, 110.0, 99.0])
        self.assertEqual(len(r), 2)
        self.assertAlmostEqual(r[0], 0.10, places=12)
        self.assertAlmostEqual(r[1], -0.10, places=12)


class DrawdownTest(unittest.TestCase):
    def test_max_drawdown(self):
        dd, peak, trough = metrics.max_drawdown([100.0, 110.0, 99.0, 108.9])
        self.assertAlmostEqual(dd, 0.10, places=12)
        self.assertEqual((peak, trough), (1, 2))

    def test_monotonic_has_no_drawdown(self):
        self.assertEqual(metrics.max_drawdown([100.0, 101.0, 102.0])[0], 0.0)


class SharpeTest(unittest.TestCase):
    def test_positive_series_positive_sharpe(self):
        eq = [100.0, 101.0, 102.0, 101.5, 103.0, 104.0]
        self.assertGreater(metrics.sharpe(eq, 252), 0)

    def test_flat_series_is_zero(self):
        self.assertEqual(metrics.sharpe([100.0] * 10, 252), 0.0)

    def test_cagr_of_doubling(self):
        eq = [100.0 * (2 ** (i / 252)) for i in range(253)]
        self.assertAlmostEqual(metrics.cagr(eq, 252), 1.0, places=6)


class TradeStatsTest(unittest.TestCase):
    def test_win_rate_and_profit_factor(self):
        trades = [{"pnl": 10.0}, {"pnl": -5.0}, {"pnl": 20.0}]
        s = metrics.trade_stats(trades)
        self.assertEqual(s["trades"], 3)
        self.assertAlmostEqual(s["win_rate"], 2 / 3, places=12)
        self.assertAlmostEqual(s["profit_factor"], 6.0, places=12)

    def test_no_losses_does_not_divide_by_zero(self):
        s = metrics.trade_stats([{"pnl": 1.0}])
        self.assertEqual(s["profit_factor"], 0.0)


class SummaryTest(unittest.TestCase):
    def test_summary_keys(self):
        s = metrics.summary([100.0, 110.0, 99.0, 108.9], [{"pnl": 9.0}], 252)
        for k in ("total_return", "cagr", "sharpe", "sortino", "calmar",
                  "max_drawdown", "volatility", "trade"):
            self.assertIn(k, s)


if __name__ == "__main__":
    unittest.main()
