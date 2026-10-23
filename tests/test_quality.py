"""M2-T6 质量报告：缺口率、新鲜度、对账口径。"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rscbot import quality  # noqa: E402
from rscbot.store import SCHEMA  # noqa: E402


def _db(bars):
    """bars: [(iid, symbol, category, ts, close)]"""
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    seen = set()
    for iid, sym, cat, ts, close in bars:
        if iid not in seen:
            conn.execute(
                "INSERT INTO instruments (id, symbol, category, fee_bps, max_leverage) "
                "VALUES (?,?,?,?,?)", (iid, sym, cat, 5.0, 20.0))
            seen.add(iid)
        conn.execute("INSERT INTO bars (instrument_id, rng, ts, open, high, low, close, volume) "
                     "VALUES (?,?,?,?,?,?,?,?)", (iid, "1d", ts, close, close, close, close, 1.0))
    conn.commit()
    conn.close()
    return path


class CoverageTest(unittest.TestCase):
    def test_gap_rate_counts_missing_slots(self):
        path = _db([(1, "X", "us", ts, 100.0) for ts in (0, 300, 900, 1200)])
        try:
            conn = quality.connect(path)
            c = quality.coverage(conn, "1d", now=1200, days=1)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(c["symbols"], 1)
        self.assertEqual(c["rows"], 4)
        self.assertEqual(c["expected"], 5)      # 0..1200 每 300s 一根 = 5 格
        self.assertAlmostEqual(c["gap_rate"], 0.2, places=12)

    def test_contiguous_bars_have_zero_gap(self):
        path = _db([(1, "X", "us", i * 300, 100.0) for i in range(10)])
        try:
            conn = quality.connect(path)
            c = quality.coverage(conn, "1d", now=1200, days=1)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(c["gap_rate"], 0.0)


class FreshnessTest(unittest.TestCase):
    def test_live_and_archived(self):
        path = _db([(1, "LIVE", "us", 1200, 1.0), (2, "OLD", "jp", 1200 - 10 * 86400, 1.0)])
        try:
            conn = quality.connect(path)
            f = quality.freshness(conn, now=1200)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(f["rng"]["1d"]["symbols"], 2)
        self.assertEqual(f["rng"]["1d"]["live"], 1)
        self.assertEqual(f["rng"]["1d"]["archived"], 1)
        self.assertEqual(f["live_1d_by_category"], {"us": 1})


class AnomalyTest(unittest.TestCase):
    def test_jump_is_flagged(self):
        path = _db([(1, "X", "us", 0, 100.0), (1, "X", "us", 300, 150.0)])
        try:
            conn = quality.connect(path)
            a = quality.anomalies(conn, now=600)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(a["spikes"]["total"], 1)
        self.assertAlmostEqual(a["spikes"]["worst"][0]["ret"], 0.5, places=6)

    def test_normal_move_and_future_ts(self):
        path = _db([(1, "X", "us", 0, 100.0), (1, "X", "us", 300, 101.0),
                    (1, "X", "us", 99999, 101.5)])
        try:
            conn = quality.connect(path)
            a = quality.anomalies(conn, now=600)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(a["spikes"]["total"], 0)
        self.assertEqual(a["future_bars"], 1)
        self.assertEqual(a["future_sample"][0]["symbol"], "X")

    def test_flat_run_is_counted(self):
        path = _db([(1, "X", "us", ts, 100.0) for ts in range(0, 300 * 10, 300)])
        try:
            conn = quality.connect(path)
            a = quality.anomalies(conn, now=3000, flat_n=6)
            conn.close()
        finally:
            os.unlink(path)
        self.assertEqual(a["flat"]["runs"], 1)
        self.assertEqual(a["flat"]["worst"][0]["bars"], 10)


class ReportTest(unittest.TestCase):
    def test_missing_db_returns_error(self):
        rep = quality.report("/nonexistent/market.db")
        self.assertIn("error", rep)

    def test_report_shape(self):
        path = _db([(1, "X", "us", i * 300, 100.0) for i in range(10)])
        try:
            rep = quality.report(path, now=3000)
        finally:
            os.unlink(path)
        self.assertIn("coverage", rep)
        self.assertIn("freshness", rep)
        self.assertEqual(rep["coverage"]["1d"]["rows"], 10)


if __name__ == "__main__":
    unittest.main()
