"""M3-T1/T2 黄金用例：手算过的撮合、保证金、强平、费用。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rscbot.broker import PaperBroker, Reject  # noqa: E402


class FeeTest(unittest.TestCase):
    def test_btc_fee_matches_exchange(self):
        """真实成交 BTC：1e-6 × 81644.72，fee_bps=10 → 8.164472e-05。"""
        b = PaperBroker(1000.0)
        f = b.open(1, "CRYPTO:BTC/USD", "long", 1e-6, 81644.72, leverage=1.0, fee_bps=10.0)
        self.assertAlmostEqual(f.fee, 8.164472e-05, places=15)
        self.assertAlmostEqual(b.balance, 1000.0 - 8.164472e-05, places=12)

    def test_fee_is_per_side(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 1.0, 100.0, fee_bps=10.0)
        f = b.close(2, "X", 100.0)
        self.assertAlmostEqual(f.fee, 0.1, places=12)
        self.assertAlmostEqual(b.fees_paid, 0.2, places=12)


class MarginTest(unittest.TestCase):
    def test_margin_is_notional_over_leverage(self):
        b = PaperBroker(1000.0)
        f = b.open(1, "FX:JPY", "long", 156.73, 0.0063826, leverage=20.0, fee_bps=0.0)
        self.assertAlmostEqual(f.margin, 156.73 * 0.0063826 / 20.0, places=15)

    def test_available_drops_by_margin(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 5.0, 100.0, leverage=1.0, fee_bps=0.0)
        self.assertAlmostEqual(b.margin_used, 500.0, places=12)
        self.assertAlmostEqual(b.available, 500.0, places=12)

    def test_insufficient_margin_rejected(self):
        b = PaperBroker(10.0)
        with self.assertRaises(Reject) as ctx:
            b.open(1, "X", "long", 1.0, 100.0, leverage=1.0)
        self.assertEqual(ctx.exception.code, "insufficient_margin")


class LiquidationTest(unittest.TestCase):
    def test_long_liq_price_matches_exchange(self):
        """FX:JPY 20x：交易所 liquidation=0.006143，公式 0.0063826×(1-0.75/20)。"""
        b = PaperBroker(1000.0)
        b.open(1, "FX:JPY", "long", 1.0, 0.0063826, leverage=20.0, fee_bps=0.0)
        liq = b.positions["FX:JPY"].liquidation_price()
        self.assertAlmostEqual(liq, 0.0063826 * (1 - 0.75 / 20), places=15)
        self.assertLess(abs(liq - 0.006143), 1e-6)

    def test_short_liq_price(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "short", 1.0, 100.0, leverage=10.0)
        self.assertAlmostEqual(b.positions["X"].liquidation_price(), 107.5, places=12)

    def test_liquidation_triggers_on_tick(self):
        b = PaperBroker(100.0)
        b.open(1, "X", "long", 1.0, 100.0, leverage=10.0, fee_bps=0.0)
        fills = b.check_exits(2, {"X": 92.4})
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].reason, "liquidation")
        self.assertAlmostEqual(fills[0].pnl, -7.6, places=12)
        self.assertNotIn("X", b.positions)


class PnlTest(unittest.TestCase):
    def test_long_pnl(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 10.0, 100.0, fee_bps=0.0)
        f = b.close(2, "X", 110.0)
        self.assertAlmostEqual(f.pnl, 100.0, places=12)
        self.assertAlmostEqual(b.balance, 1100.0, places=12)

    def test_short_pnl(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "short", 10.0, 100.0, fee_bps=0.0)
        f = b.close(2, "X", 90.0)
        self.assertAlmostEqual(f.pnl, 100.0, places=12)

    def test_equity_includes_unrealized(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 5.0, 100.0, fee_bps=0.0)
        self.assertAlmostEqual(b.equity({"X": 110.0}), 1050.0, places=12)

    def test_partial_close(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 5.0, 100.0, fee_bps=5.0)
        f = b.close(2, "X", 110.0, quantity=2.0)
        self.assertAlmostEqual(f.pnl, 20.0, places=12)
        self.assertAlmostEqual(f.margin, 200.0, places=12)
        self.assertAlmostEqual(b.positions["X"].quantity, 3.0, places=12)
        self.assertAlmostEqual(b.balance, 1000.0 - 0.25 + 20.0 - 0.11, places=12)


class RejectTest(unittest.TestCase):
    def test_price_drift(self):
        b = PaperBroker(1000.0)
        with self.assertRaises(Reject) as ctx:
            b.open(1, "X", "long", 1.0, 106.0, reference=100.0)
        self.assertEqual(ctx.exception.code, "price_drift")

    def test_drift_within_limit_ok(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 1.0, 104.9, reference=100.0)

    def test_double_open_rejected(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 1.0, 100.0)
        with self.assertRaises(Reject) as ctx:
            b.open(2, "X", "long", 1.0, 100.0)
        self.assertEqual(ctx.exception.code, "symbol_busy")

    def test_close_without_position(self):
        b = PaperBroker(1000.0)
        with self.assertRaises(Reject) as ctx:
            b.close(1, "X", 100.0)
        self.assertEqual(ctx.exception.code, "no_position")


class StopTakeProfitTest(unittest.TestCase):
    def test_stop_loss_long(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "long", 1.0, 100.0, leverage=5.0, stop_loss=95.0)
        self.assertEqual(b.positions["X"].hit(94.0), "stop_loss")

    def test_take_profit_short(self):
        b = PaperBroker(1000.0)
        b.open(1, "X", "short", 1.0, 100.0, leverage=5.0, take_profit=90.0)
        self.assertEqual(b.positions["X"].hit(89.0), "take_profit")


if __name__ == "__main__":
    unittest.main()
