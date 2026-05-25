"""
market_hours.py — 交易时段 / 交易日历判断（模块级纯函数）。

被 IntradayMonitor 与 backend/api.py 共享使用。
"""

from datetime import datetime, timedelta
from typing import Optional

from ..signals import (
    MARKET_MORNING_START, MARKET_MORNING_END,
    MARKET_AFTERNOON_START, MARKET_AFTERNOON_END,
)


_trade_calendar: set = set()
_trade_calendar_date: str = ''


def _is_trading_day(now: datetime) -> bool:
    """判断是否为 A 股交易日（复用 Scheduler 的 AKShare 日历逻辑）。"""
    global _trade_calendar, _trade_calendar_date
    today_str = now.strftime('%Y-%m-%d')

    if _trade_calendar_date != today_str:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            dates = df.iloc[:, 0]
            _trade_calendar = {str(d)[:10] for d in dates}
            _trade_calendar_date = today_str
        except Exception:
            _trade_calendar = set()

    if _trade_calendar:
        return today_str in _trade_calendar

    return now.weekday() < 5


def is_market_open(now: Optional[datetime] = None) -> bool:
    """判断当前是否为 A 股交易时段（节假日 + 交易时间双重判断）。"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5 or not _is_trading_day(now):
        return False
    h, m = now.hour, now.minute

    def t(h_, m_):
        return h_ * 60 + m_

    cur = h * 60 + m
    morning   = t(*MARKET_MORNING_START) <= cur <= t(*MARKET_MORNING_END)
    afternoon = t(*MARKET_AFTERNOON_START) <= cur <= t(*MARKET_AFTERNOON_END)
    return morning or afternoon


def _get_hk_calendar():
    """获取港交所 calendar（lazy init，缓存全局单例）。"""
    import exchange_calendars as ec
    return ec.get_calendar('XHKG')


def is_hk_market_open(now: Optional[datetime] = None) -> bool:
    """判断当前是否为港股交易时段（exchange_calendars XHKG 日历 + 港股交易时间双重判断）。

    - 节假日（端午/清明/国庆等）：is_session 返回 False
    - 午休 12:00-13:00：不在交易时段内
    - 09:29 集合竞价：尚未开市
    """
    if now is None:
        now = datetime.now()
    try:
        cal = _get_hk_calendar()
        import pandas as pd
        ts = pd.Timestamp(now.date())
        if not cal.is_session(ts):
            return False
    except Exception:
        # 日历查询失败时保守：港股休市（比假信号安全）
        return False
    # 港股交易时段：09:30-11:59 / 13:00-15:59（HKT），午休 12:00-13:00 闭市
    h, m = now.hour, now.minute
    cur = h * 60 + m
    morning = 9 * 60 + 30 <= cur <= 11 * 60 + 59
    afternoon = 13 * 60 <= cur <= 16 * 60 - 1
    return morning or afternoon


def next_market_seconds(now: Optional[datetime] = None) -> int:
    """距离下次开市还有多少秒(用于启动前 sleep)。"""
    if now is None:
        now = datetime.now()
    h, m = now.hour, now.minute
    cur = h * 60 + m

    afternoon_start = MARKET_AFTERNOON_START[0] * 60 + MARKET_AFTERNOON_START[1]
    if cur < afternoon_start:
        return (afternoon_start - cur) * 60

    tomorrow = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    morning_start = MARKET_MORNING_START[0] * 60 + MARKET_MORNING_START[1]
    return int((tomorrow.timestamp() - now.timestamp())) + (morning_start * 60)
