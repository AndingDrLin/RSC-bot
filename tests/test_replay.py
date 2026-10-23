"""M3-T7 对拍：费用公式与成交价偏差的计算口径。"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rscbot import replay  # noqa: E402
from rscbot.store import SCHEMA  # noqa: E402


def _db(order_price=100.02, quote_price=100.0, qty=0.01, fee_bps=10.0,
        created_at="1970-01-01T00:16:40Z", fee_mult=1.0):
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO instruments (id, symbol, category, fee_bps, max_leverage) "
                 "VALUES (1,'X','crypto',?,20.0)", (fee_bps,))
    conn.execute("INSERT INTO quotes (ts, instrument_id, symbol, price) VALUES (?,?,?,?)",
                 (1000, 1, "X", quote_price))
    notional = qty * order_price
    conn.execute(
        "INSERT INTO orders (ts, order_id, instrument_id, symbol, side, status, quantity, "
        "leverage, price, fee, pnl, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (1000, 7, 1, "X", "long", "filled", qty, 1.0, order_price,
         notional * fee_bps / 10000.0 * fee_mult, None, created_at))
    conn.commit()
    conn.close()
    return path


class ReplayTest(unittest.TestCase):
    def test_fee_matches_exactly(self):
        path = _db()
        try:
            conn = replay.connect(path)
            rep = replay.compare(conn)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(rep["orders"], 1)
        self.assertEqual(rep["fee_checked"], 1)
        self.assertLess(rep["max_fee_err_rel"], 1e-12)

    def test_price_deviation_in_bps(self):
        path = _db(order_price=100.02, quote_price=100.0)
        try:
            conn = replay.connect(path)
            rep = replay.compare(conn)
            conn.close()
        finally:
            os.unlink(path)
        self.assertAlmostEqual(rep["rows"][0]["price_bps"], 2.0, places=9)

    def test_order_without_nearby_quote_is_not_priced(self):
        """6 月的老成交：created_at 附近没有报价，就不该给出 bp 偏差。"""
        path = _db(created_at="2026-06-25T07:00:49.238Z")
        try:
            conn = replay.connect(path)
            rep = replay.compare(conn)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(rep["price_checked"], 0)
        self.assertEqual(rep["fee_checked"], 1)

    def test_fee_double_charge_is_reported_as_outlier(self):
        """2026-06 的 ETH 老成交费用是 2×，不能被中位数掩盖。"""
        path = _db(fee_mult=2.0)
        conn = sqlite3.connect(path)
        for oid in (8, 9):
            conn.execute(
                "INSERT INTO orders (ts, order_id, instrument_id, symbol, side, status, quantity, "
                "leverage, price, fee, pnl, created_at) VALUES (?,?,1,'X','long','filled',"
                "0.02,1.0,100.0,0.002,NULL,'1970-01-01T00:16:41Z')", (1000 + oid, oid))
        conn.commit()
        conn.close()
        try:
            conn = replay.connect(path)
            rep = replay.compare(conn)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(len(rep["fee_outliers"]), 1)
        self.assertAlmostEqual(rep["fee_outliers"][0]["ratio"], 2.0, places=9)
        self.assertAlmostEqual(rep["max_fee_err_rel"], 0.5, places=9)
        self.assertLess(rep["fee_err_rel_median"], 1e-12)  # 3 笔里 1 笔 2×，中位数仍为 0

    def test_duplicate_order_id_keeps_latest(self):
        path = _db()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO orders (ts, order_id, instrument_id, symbol, side, status, "
                     "quantity, leverage, price, fee) VALUES (2000, 7, 1, 'X', 'long', "
                     "'filled', 0.01, 1.0, 101.0, 0.000101)")
        conn.commit()
        conn.close()
        try:
            conn = replay.connect(path)
            rep = replay.compare(conn)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(rep["orders"], 1)
        self.assertEqual(rep["rows"][0]["price"], 101.0)


if __name__ == "__main__":
    unittest.main()
