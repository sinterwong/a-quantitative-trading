"""
CooldownTracker + market_hours 纯函数测试。
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import patch

import pytest

from backend.services.intraday.cooldown import CooldownTracker, COOLDOWN
from backend.services.intraday import market_hours
from backend.services.intraday.market_hours import (
    is_market_open, next_market_seconds, _is_trading_day,
)


# ── CooldownTracker ──────────────────────────────────────

def test_cooldown_first_fire_returns_true():
    ct = CooldownTracker(cooldown=10)
    assert ct.can_fire('A') is True


def test_cooldown_blocks_within_window():
    ct = CooldownTracker(cooldown=100)
    assert ct.can_fire('A') is True
    assert ct.can_fire('A') is False   # 100s 内不能重复


def test_cooldown_allows_after_window():
    ct = CooldownTracker(cooldown=1)
    assert ct.can_fire('A') is True
    time.sleep(1.05)
    assert ct.can_fire('A') is True


def test_cooldown_isolated_per_symbol():
    ct = CooldownTracker(cooldown=100)
    assert ct.can_fire('A') is True
    assert ct.can_fire('B') is True


def test_cooldown_purge_old_removes_expired():
    ct = CooldownTracker(cooldown=1)
    ct.can_fire('A')
    ct.can_fire('B')
    time.sleep(1.1)
    ct.purge_old()
    assert ct._last == {}


def test_cooldown_default_constant_sensible():
    assert COOLDOWN >= 60  # 至少 1 分钟


# ── market_hours ─────────────────────────────────────────

def test_is_market_open_weekend_returns_false():
    sat = datetime(2026, 5, 16, 10, 0)  # 周六
    assert sat.weekday() == 5
    assert is_market_open(sat) is False


def test_is_market_open_morning_session():
    """周一 10:30,日历降级到 weekday 也应认定为开盘。"""
    # reset calendar to force AKShare branch失败
    market_hours._trade_calendar = set()
    market_hours._trade_calendar_date = ''
    with patch('akshare.tool_trade_date_hist_sina', side_effect=RuntimeError('offline')):
        mon = datetime(2026, 5, 11, 10, 30)
        assert mon.weekday() == 0
        assert is_market_open(mon) is True


def test_is_market_open_lunch_break():
    """周一 12:00 应该 closed (午休)。"""
    market_hours._trade_calendar = set()
    market_hours._trade_calendar_date = ''
    with patch('akshare.tool_trade_date_hist_sina', side_effect=RuntimeError('offline')):
        noon = datetime(2026, 5, 11, 12, 0)
        assert is_market_open(noon) is False


def test_is_market_open_calendar_hit_blocks_holiday():
    """日历可用且当日不在集合 → False。"""
    holiday = datetime(2026, 5, 11, 10, 0)
    market_hours._trade_calendar = {'2026-05-12'}  # 仅次日是交易日
    market_hours._trade_calendar_date = '2026-05-11'
    assert is_market_open(holiday) is False


def test_next_market_seconds_before_afternoon():
    """11:35 后,距下午开盘 1:30(13:00) ≈ 85 分钟 = 5100s。"""
    market_hours._trade_calendar = set()
    market_hours._trade_calendar_date = ''
    now = datetime(2026, 5, 11, 11, 35)
    secs = next_market_seconds(now)
    # 13:00 - 11:35 = 85 分钟 = 5100s
    assert 5000 < secs < 5200


def test_is_trading_day_uses_calendar_when_available():
    """通过 mock akshare.tool_trade_date_hist_sina 注入交易日历。"""
    import pandas as pd
    market_hours._trade_calendar = set()
    market_hours._trade_calendar_date = ''
    fake_df = pd.DataFrame({'trade_date': ['2026-05-11']})
    with patch('akshare.tool_trade_date_hist_sina', return_value=fake_df):
        assert _is_trading_day(datetime(2026, 5, 11)) is True
        assert _is_trading_day(datetime(2026, 5, 12)) is False
