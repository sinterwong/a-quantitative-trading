"""tests/test_level2_quality.py — Level2QualityCollector / Level2QualityReporter 单元测试"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from core.level2_quality import (
    FieldQuality,
    Level2QualityCollector,
    Level2QualityReport,
    Level2QualityReporter,
    SymbolQuality,
    _REQUIRED_FIELDS,
)


# ---------------------------------------------------------------------------
# 辅助：直接向测试 DB 插入假快照
# ---------------------------------------------------------------------------

def _insert_snapshot(db_path: str, symbol: str, ts: str, complete: bool = True):
    """向 l2_snapshots 插入一行测试数据。"""
    val = 1800.0 if complete else None
    vol = 1000 if complete else None
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('''
            INSERT INTO l2_snapshots
            (symbol, ts, last_price, volume, amount,
             bid_price_1, bid_vol_1, ask_price_1, ask_vol_1,
             bid_price_2, bid_vol_2, ask_price_2, ask_vol_2,
             bid_price_3, bid_vol_3, ask_price_3, ask_vol_3,
             bid_price_4, bid_vol_4, ask_price_4, ask_vol_4,
             bid_price_5, bid_vol_5, ask_price_5, ask_vol_5,
             raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            symbol, ts,
            val, vol, val,
            val, vol, val, vol,
            val, vol, val, vol,
            val, vol, val, vol,
            val, vol, val, vol,
            val, vol, val, vol,
            '{}',
        ))
        conn.commit()
    finally:
        conn.close()


def _make_db_with_snapshots(n: int = 10, symbol: str = '600519.SH',
                             complete: bool = True) -> str:
    """创建临时 DB，插入 n 条完整（或不完整）快照，返回 db_path。"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    collector = Level2QualityCollector([symbol], db_path=db_path)
    now = datetime.now()
    for i in range(n):
        ts = (now - timedelta(seconds=i * 30)).isoformat(timespec='seconds')
        _insert_snapshot(db_path, symbol, ts, complete=complete)
    return db_path


# ---------------------------------------------------------------------------
# Level2QualityCollector 测试
# ---------------------------------------------------------------------------

class TestLevel2QualityCollectorInit(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.collector = Level2QualityCollector(['600519.SH', '000858.SZ'],
                                                db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_db_created(self):
        self.assertTrue(os.path.exists(self.db_path))

    def test_table_exists(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='l2_snapshots'")
        self.assertIsNotNone(cur.fetchone())
        conn.close()

    def test_n_snapshots_initially_zero(self):
        counts = self.collector.n_snapshots
        self.assertEqual(counts, {})

    def test_collect_once_does_not_crash(self):
        """collect_once() 网络不可用时应静默返回 0。"""
        result = self.collector.collect_once()
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    def test_manual_insert_shows_in_n_snapshots(self):
        ts = datetime.now().isoformat(timespec='seconds')
        _insert_snapshot(self.db_path, '600519.SH', ts, complete=True)
        counts = self.collector.n_snapshots
        self.assertEqual(counts.get('600519.SH', 0), 1)


class TestLevel2QualityCollectorThread(unittest.TestCase):

    def test_start_stop_does_not_crash(self):
        fd, db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            collector = Level2QualityCollector(['600519.SH'], db_path=db_path)
            collector.start(interval=999)   # 超长间隔，不会实际触发
            collector.stop()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# ---------------------------------------------------------------------------
# Level2QualityReporter 测试
# ---------------------------------------------------------------------------

class TestLevel2QualityReporterNoDb(unittest.TestCase):

    def test_missing_db_returns_failed_report(self):
        reporter = Level2QualityReporter(db_path='/tmp/nonexistent_l2_test.db')
        report = reporter.generate(days=5)
        self.assertIsInstance(report, Level2QualityReport)
        self.assertFalse(report.passed)
        self.assertGreater(len(report.notes), 0)

    def test_empty_db_returns_failed_report(self):
        fd, db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            Level2QualityCollector(['600519.SH'], db_path=db_path)   # init schema
            reporter = Level2QualityReporter(db_path)
            report = reporter.generate(days=5)
            self.assertFalse(report.passed)
        finally:
            os.unlink(db_path)


class TestLevel2QualityReporterWithData(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db_with_snapshots(n=20, symbol='600519.SH', complete=True)
        self.reporter = Level2QualityReporter(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_generate_returns_report(self):
        report = self.reporter.generate(days=1)
        self.assertIsInstance(report, Level2QualityReport)

    def test_symbols_populated(self):
        report = self.reporter.generate(days=1)
        self.assertEqual(len(report.symbols), 1)
        self.assertEqual(report.symbols[0].symbol, '600519.SH')

    def test_n_snapshots_correct(self):
        report = self.reporter.generate(days=1)
        self.assertEqual(report.symbols[0].n_snapshots, 20)

    def test_complete_data_passes(self):
        report = self.reporter.generate(days=1, threshold=0.95)
        self.assertTrue(report.passed)

    def test_overall_completeness_near_one(self):
        report = self.reporter.generate(days=1)
        self.assertGreater(report.overall_completeness, 0.95)

    def test_field_stats_has_all_required_fields(self):
        report = self.reporter.generate(days=1)
        sq = report.symbols[0]
        field_names = {fq.field_name for fq in sq.field_stats}
        for fname in _REQUIRED_FIELDS:
            self.assertIn(fname, field_names)

    def test_filter_by_symbol(self):
        report = self.reporter.generate(days=1, symbols=['600519.SH'])
        self.assertEqual(len(report.symbols), 1)


class TestLevel2QualityReporterIncompleteData(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db_with_snapshots(n=10, symbol='600519.SH', complete=False)
        self.reporter = Level2QualityReporter(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_incomplete_data_fails(self):
        report = self.reporter.generate(days=1, threshold=0.95)
        self.assertFalse(report.passed)

    def test_notes_populated(self):
        report = self.reporter.generate(days=1)
        self.assertGreater(len(report.notes), 0)


class TestLevel2QualityReportSave(unittest.TestCase):

    def test_save_creates_markdown(self):
        db_path = _make_db_with_snapshots(n=5, complete=True)
        fd, md_path = tempfile.mkstemp(suffix='.md')
        os.close(fd)
        try:
            reporter = Level2QualityReporter(db_path)
            report = reporter.generate(days=1)
            saved = report.save(md_path=md_path)
            self.assertTrue(os.path.exists(saved))
            with open(saved, encoding='utf-8') as f:
                content = f.read()
            self.assertIn('Level 2', content)
            self.assertIn('600519.SH', content)
        finally:
            os.unlink(db_path)
            if os.path.exists(md_path):
                os.unlink(md_path)

    def test_save_with_json_path(self):
        import json
        db_path = _make_db_with_snapshots(n=5, complete=True)
        fd_md, md_path = tempfile.mkstemp(suffix='.md')
        fd_js, json_path = tempfile.mkstemp(suffix='.json')
        os.close(fd_md)
        os.close(fd_js)
        try:
            reporter = Level2QualityReporter(db_path)
            report = reporter.generate(days=1)
            report.save(md_path=md_path, json_path=json_path)
            with open(json_path, encoding='utf-8') as f:
                data = json.load(f)
            self.assertIn('overall_completeness', data)
            self.assertIn('symbols', data)
        finally:
            os.unlink(db_path)
            for p in (md_path, json_path):
                if os.path.exists(p):
                    os.unlink(p)


class TestFieldQualityDataclass(unittest.TestCase):

    def test_field_quality_passed_true(self):
        fq = FieldQuality(field_name='bid_price_1', total=100, valid=98,
                          completeness=0.98, passed=True)
        self.assertTrue(fq.passed)

    def test_field_quality_passed_false(self):
        fq = FieldQuality(field_name='ask_vol_5', total=100, valid=60,
                          completeness=0.60, passed=False)
        self.assertFalse(fq.passed)


if __name__ == '__main__':
    unittest.main()
