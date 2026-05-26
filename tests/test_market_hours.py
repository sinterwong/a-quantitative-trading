"""
test_market_hours.py — 港股/A股交易时段判断单元测试

覆盖:
- is_hk_market_open(): 周末/节假日闭市, 交易时段(09:30-11:59/13:00-15:59)
- is_market_open(): A股交易日 + 时段判断
- next_market_seconds(): 距离下次开市秒数
"""

from __future__ import annotations

import unittest
from datetime import datetime, date
from unittest.mock import patch


class TestIsHkMarketOpen(unittest.TestCase):
    """港股闭市判断: 周末/节假日/午休/集合竞价"""

    def test_weekend_closed(self):
        """周末恒定闭市"""
        from backend.services.intraday.market_hours import is_hk_market_open
        # 2026-05-30 是周六
        saturday = datetime(2026, 5, 30, 10, 0)
        self.assertFalse(is_hk_market_open(saturday))
        sunday = datetime(2026, 5, 31, 14, 0)
        self.assertFalse(is_hk_market_open(sunday))

    def test_trading_hours_morning(self):
        """港股早盘: 09:30-11:59 开市, 09:29 闭市"""
        from backend.services.intraday.market_hours import is_hk_market_open
        # 2026-05-28 是周四(交易日)
        # 09:30 开市
        open_0930 = datetime(2026, 5, 28, 9, 30)
        self.assertTrue(is_hk_market_open(open_0930))
        # 09:29 集合竞价,尚未开市
        before_0930 = datetime(2026, 5, 28, 9, 29)
        self.assertFalse(is_hk_market_open(before_0930))
        # 11:59 仍在交易
        near_noon = datetime(2026, 5, 28, 11, 59)
        self.assertTrue(is_hk_market_open(near_noon))

    def test_trading_hours_afternoon(self):
        """港股午盘: 13:00-15:59 开市, 12:59 午休闭市"""
        from backend.services.intraday.market_hours import is_hk_market_open
        # 2026-05-28 周四
        open_1300 = datetime(2026, 5, 28, 13, 0)
        self.assertTrue(is_hk_market_open(open_1300))
        # 15:59 仍在交易
        near_close = datetime(2026, 5, 28, 15, 59)
        self.assertTrue(is_hk_market_open(near_close))
        # 12:59 午休闭市
        lunch_break = datetime(2026, 5, 28, 12, 59)
        self.assertFalse(is_hk_market_open(lunch_break))

    def test_lunch_break_noon(self):
        """午休 12:00-13:00 闭市"""
        from backend.services.intraday.market_hours import is_hk_market_open
        noon = datetime(2026, 5, 28, 12, 0)
        self.assertFalse(is_hk_market_open(noon))
        half_past = datetime(2026, 5, 28, 12, 30)
        self.assertFalse(is_hk_market_open(half_past))

    def test_calendar_failure_fallback_false(self):
        """exchange_calendars 查询失败时保守返回 False"""
        from backend.services.intraday.market_hours import is_hk_market_open
        with patch(
            'backend.services.intraday.market_hours._get_hk_calendar',
            side_effect=RuntimeError('calendar unavailable'),
        ):
            # 周中工作日,但日历查询失败 → 保守闭市
            weekday = datetime(2026, 5, 28, 10, 0)  # 周四
            self.assertFalse(is_hk_market_open(weekday))


class TestIsMarketOpen(unittest.TestCase):
    """A 股交易时段判断"""

    def test_weekend_closed(self):
        """周末闭市"""
        from backend.services.intraday.market_hours import is_market_open
        saturday = datetime(2026, 5, 30, 10, 0)
        self.assertFalse(is_market_open(saturday))

    def test_trading_hours_morning(self):
        """A 股早盘 09:35-11:30（9:35 才开市，9:30 尚未开市）"""
        from backend.services.intraday.market_hours import is_market_open
        with patch('backend.services.intraday.market_hours._is_trading_day', return_value=True):
            # 9:35 起A股开市
            at_0935 = datetime(2026, 5, 28, 9, 35)
            self.assertTrue(is_market_open(at_0935))
            # 11:30 仍在交易
            near_noon = datetime(2026, 5, 28, 11, 30)
            self.assertTrue(is_market_open(near_noon))
            # 9:34 尚未开市
            before_open = datetime(2026, 5, 28, 9, 34)
            self.assertFalse(is_market_open(before_open))

    def test_trading_hours_afternoon(self):
        """A 股午盘 13:00-14:55（14:55 收盘，14:56 已闭市）"""
        from backend.services.intraday.market_hours import is_market_open
        with patch('backend.services.intraday.market_hours._is_trading_day', return_value=True):
            open_1300 = datetime(2026, 5, 28, 13, 0)
            self.assertTrue(is_market_open(open_1300))
            # 14:54 仍在交易
            near_close = datetime(2026, 5, 28, 14, 54)
            self.assertTrue(is_market_open(near_close))
            # 14:56 已收盘
            after_close = datetime(2026, 5, 28, 14, 56)
            self.assertFalse(is_market_open(after_close))

    def test_outside_trading_hours(self):
        """交易时段外闭市"""
        from backend.services.intraday.market_hours import is_market_open
        with patch('backend.services.intraday.market_hours._is_trading_day', return_value=True):
            before_open = datetime(2026, 5, 28, 9, 29)
            self.assertFalse(is_market_open(before_open))
            after_close = datetime(2026, 5, 28, 15, 1)
            self.assertFalse(is_market_open(after_close))


class TestNextMarketSeconds(unittest.TestCase):
    """距离下次开市秒数"""

    def test_returns_positive_int(self):
        """返回值应为正整数"""
        from backend.services.intraday.market_hours import next_market_seconds
        result = next_market_seconds(datetime(2026, 5, 28, 7, 0))
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)