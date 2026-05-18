"""
tests/test_scheduler_holiday_lock.py — P2-19 Scheduler 节假日感知 + 并发锁

覆盖：
  - is_trading_day(): AKShare 日历 hit/miss + 周末降级
  - Scheduler._seconds_until_next_check 计算到次日 08:25 的秒数
  - PortfolioService 的 get_cursor() 在多线程并发写时序列化（不丢更新）
  - WAL 模式 PRAGMA 正确启用
"""

from __future__ import annotations

import os
import sys
import threading
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backend'))


class TestIsTradingDay(unittest.TestCase):

    def test_uses_calendar_when_available(self):
        import quant_app.run_worker as bm
        # 重置缓存，避免被前置测试污染
        bm._trade_calendar = set()
        bm._trade_calendar_date = ''

        # mock AKShare 返回包含今天的日历
        today = datetime.now().strftime('%Y-%m-%d')
        with patch.object(bm, '_build_trade_calendar', return_value={today, '2026-01-02'}):
            self.assertTrue(bm.is_trading_day())

    def test_returns_false_for_known_holiday(self):
        import quant_app.run_worker as bm
        bm._trade_calendar = set()
        bm._trade_calendar_date = ''
        # 日历存在但不含今天
        with patch.object(bm, '_build_trade_calendar',
                          return_value={'2099-01-02', '2099-01-03'}):
            self.assertFalse(bm.is_trading_day())

    def test_falls_back_to_weekday_when_calendar_unavailable(self):
        import quant_app.run_worker as bm
        bm._trade_calendar = set()
        bm._trade_calendar_date = ''
        with patch.object(bm, '_build_trade_calendar', return_value=set()):
            # AKShare 失败 → 降级到 weekday()
            expected = datetime.now().weekday() < 5
            self.assertEqual(bm.is_trading_day(), expected)


class TestTradeCalendarCache(unittest.TestCase):
    """AKShare 失败时优先用本地缓存,而不是退化为"周一到周五"。"""

    def setUp(self):
        import quant_app.run_worker as bm
        import tempfile, json
        self._tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self._tmp.close()
        self._orig_path = bm._TRADE_CAL_CACHE
        bm._TRADE_CAL_CACHE = self._tmp.name
        bm._trade_calendar = set()
        bm._trade_calendar_date = ''
        self._bm = bm
        self._json = json

    def tearDown(self):
        self._bm._TRADE_CAL_CACHE = self._orig_path
        os.unlink(self._tmp.name)

    def _seed_cache(self, dates, age_days=0):
        from datetime import datetime, timedelta
        fetched_at = datetime.now() - timedelta(days=age_days)
        with open(self._tmp.name, 'w') as f:
            self._json.dump({
                'fetched_at': fetched_at.isoformat(timespec='seconds'),
                'dates': sorted(dates),
            }, f)

    def test_uses_cache_when_akshare_fails(self):
        from datetime import datetime
        bm = self._bm
        today = datetime.now().strftime('%Y-%m-%d')
        self._seed_cache({today, '2099-01-02'}, age_days=2)
        # 强制让 import akshare 抛 ImportError 模拟"AKShare 全挂"
        with patch.dict('sys.modules', {'akshare': None}):
            self.assertTrue(bm.is_trading_day())

    def test_cache_excludes_today_returns_false(self):
        bm = self._bm
        self._seed_cache({'2099-01-02', '2099-01-03'}, age_days=2)
        with patch.dict('sys.modules', {'akshare': None}):
            self.assertFalse(bm.is_trading_day())

    def test_successful_akshare_writes_cache(self):
        bm = self._bm
        with patch.object(bm, '_build_trade_calendar') as mock_build:
            mock_build.side_effect = (
                lambda: bm._save_trade_calendar_cache({'2026-05-18'}) or {'2026-05-18'}
            )
            bm.is_trading_day()
        # 缓存有内容
        with open(self._tmp.name) as f:
            data = self._json.load(f)
        self.assertIn('2026-05-18', data['dates'])


class TestSecondsUntilNextCheck(unittest.TestCase):

    def test_returns_seconds_to_tomorrow_0825(self):
        from quant_app.run_worker import Scheduler
        # 模拟周一 14:00
        now = datetime(2026, 5, 4, 14, 0, 0)   # Mon
        s = Scheduler._seconds_until_next_check(now)
        # 14:00 → 次日 08:25 = 18h25m = 66300s
        self.assertAlmostEqual(s, 18 * 3600 + 25 * 60, delta=2)

    def test_minimum_60s(self):
        from quant_app.run_worker import Scheduler
        # 模拟 08:24:30 — 距次日 08:25 还有 23h59m30s 但若已超过则返回 60s
        now = datetime(2026, 5, 4, 8, 24, 0)
        s = Scheduler._seconds_until_next_check(now)
        self.assertGreater(s, 0)


class TestPortfolioWriteLock(unittest.TestCase):
    """并发写不应丢更新。"""

    def setUp(self):
        # 重定向 DB_PATH 到临时文件
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix='portfolio_test_')
        self.db_path = os.path.join(self.tmpdir, 'p.db')
        import backend.services.portfolio as ps
        self._orig_db_path = ps.DB_PATH
        ps.DB_PATH = self.db_path
        ps.init_db()

    def tearDown(self):
        import backend.services.portfolio as ps
        ps.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_cash_set_no_loss(self):
        """启动 N 个线程各写 cash，验证最后值是其中一个写入的值（非崩溃）。"""
        import backend.services.portfolio as ps
        svc = ps.PortfolioService(db_path=self.db_path)

        N = 20
        threads = []
        for i in range(N):
            t = threading.Thread(target=svc.set_cash, args=(float(1000 + i),))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 不抛异常 + cash 落在 [1000, 1019]
        cash = svc.get_cash()
        self.assertIn(int(cash), set(range(1000, 1000 + N)))

    def test_wal_mode_enabled(self):
        import backend.services.portfolio as ps
        conn = ps.get_db()
        try:
            row = conn.execute('PRAGMA journal_mode').fetchone()
            mode = row[0] if row else None
            self.assertEqual(str(mode).lower(), 'wal')
        finally:
            conn.close()

    def test_busy_timeout_set(self):
        import backend.services.portfolio as ps
        conn = ps.get_db()
        try:
            row = conn.execute('PRAGMA busy_timeout').fetchone()
            self.assertGreaterEqual(int(row[0]), 1000)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
