"""
core/data_sources.py — 统一数据源接口（已全面迁移至 Gateway 唯一出口）

所有数据源 fetch_latest() / fetch_history() 均通过 Gateway API 获取外部数据，
不再直接请求外部 HTTP/yfinance 接口（历史 K 线除外，详见各类 docstring）。

支持数据源：
  - SPFuturesDataSource:     S&P 500 / Nasdaq 期货 → Gateway.market_index()
  - VIXDataSource:           CBOE VIX 指数 → Gateway.market_index('^VIX')
  - HSIFuturesDataSource:    恒生指数期货 → Gateway.market_index('^HSI')
  - NorthBoundDataSource:    北向资金 KAMT（内部服务）
  - TencentMinuteDataSource:  腾讯分钟K线 → Gateway.kline(interval='1m')
  - _TencentMarketSource:    港股/美股/指数 → Gateway.market_index()

所有数据源实现：
  - fetch_latest()  → 最新行情 dict
  - fetch_history(days) → pd.DataFrame
  - subscribe(handler)  → 注册实时回调（未来 WebSocket）
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Callable, Dict, List, Optional, Any
import logging
import threading
import time
import os
import sys

logger = logging.getLogger('data_sources')

import requests
import pandas as pd
import numpy as np

# ─── Base ────────────────────────────────────────────────────────────────────

class DataSource(ABC):
    """数据源基类"""

    name: str = 'DataSource'
    _running: bool = False

    @abstractmethod
    def fetch_latest(self) -> Dict[str, Any]:
        """获取最新行情，返回 dict"""
        ...

    @abstractmethod
    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        """获取历史 K 线（最近 N 天）"""
        ...

    def subscribe(self, handler: Callable[['DataSource', Dict], None]):
        """订阅实时数据（默认：polling 模式）"""
        self._sub_handler = handler

    def _notify(self, data: Dict):
        if hasattr(self, '_sub_handler') and self._sub_handler:
            self._sub_handler(self, data)

    def start_polling(self, interval: int = 60):
        """启动轮询（子线程）"""
        def loop():
            while self._running:
                try:
                    d = self.fetch_latest()
                    self._notify(d)
                except Exception as e:
                    print(f"[{self.name}] poll error: {e}")
                time.sleep(interval)

        self._running = True
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    def stop_polling(self):
        self._running = False


# ─── S&P Futures ─────────────────────────────────────────────────────────────

class SPFuturesDataSource(DataSource):
    """
    S&P 500 (ES) / Nasdaq (NQ) 期货数据（已迁移至 Gateway 统一数据出口）。

    走 Gateway.market_index(symbol)，底层由 yfinance provider 提供数据。
    """

    name = 'SPFutures'

    def __init__(self, symbol: str = 'ES=F'):
        self.symbol = symbol  # 'ES=F' (S&P) or 'NQ=F' (Nasdaq)
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._cache_ttl: int = 60  # 秒

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            from core.data_gateway import get_gateway
            snap = get_gateway().market_index(self.symbol)
            if snap and snap.price > 0:
                result = {
                    'symbol': self.symbol,
                    'timestamp': snap.timestamp or datetime.now(),
                    'close': snap.price,
                    'prev_close': snap.prev_close,
                    'change_pct': snap.change_pct,
                    'source': 'gateway',
                }
            else:
                result = {'symbol': self.symbol, 'error': 'no data from gateway'}
        except Exception as e:
            result = {'symbol': self.symbol, 'error': str(e), 'source': 'failed'}

        self._cache = result
        self._cache_time = now
        return result

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        # Gateway market_index 无历史，fallback 到 yfinance K线（历史场景，延迟可接受）
        try:
            import yfinance as yf
            ticker = yf.Ticker(self.symbol)
            hist = ticker.history(period=f'{days+2}d', auto_adjust=True)
            if 'Dividends' in hist.columns:
                hist = hist.drop(columns=['Dividends'], errors='ignore')
            if 'Stock Splits' in hist.columns:
                hist = hist.drop(columns=['Stock Splits'], errors='ignore')
            return hist.tail(days)
        except Exception as e:
            print(f"[{self.name}] fetch_history error: {e}")
            return pd.DataFrame()


class VIXDataSource(DataSource):
    """
    CBOE VIX 波动率指数（已迁移至 Gateway 统一数据出口）。

    走 Gateway.market_index('^VIX')，底层由 yfinance provider 提供数据。
    """

    name = 'VIX'

    def __init__(self):
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < 300:  # 5min TTL
            return self._cache

        try:
            from core.data_gateway import get_gateway
            snap = get_gateway().market_index('^VIX')
            if snap and snap.price > 0:
                result = {
                    'symbol': '^VIX',
                    'timestamp': snap.timestamp or datetime.now(),
                    'close': snap.price,
                    'prev_close': snap.prev_close,
                    'change_pct': snap.change_pct,
                    'source': 'gateway',
                }
            else:
                result = {'symbol': '^VIX', 'error': 'no data from gateway'}
        except Exception as e:
            result = {'symbol': '^VIX', 'error': str(e)}

        self._cache = result
        self._cache_time = now
        return result

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        # VIX 历史通过 yfinance K线兜底（Gateway market_index 无历史）
        try:
            import yfinance as yf
            ticker = yf.Ticker('^VIX')
            hist = ticker.history(period=f'{days+2}d', auto_adjust=True)
            return hist.tail(days) if not hist.empty else pd.DataFrame()
        except Exception:
            return pd.DataFrame()


class HSIFuturesDataSource(DataSource):
    """
    恒生指数期货（已迁移至 Gateway 统一数据出口）。

    走 Gateway.market_index('^HSI')，底层由 yfinance provider 提供数据。
    """

    name = 'HSIFutures'

    def __init__(self, symbol: str = '^HSI'):
        self.symbol = symbol
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < 60:
            return self._cache

        try:
            from core.data_gateway import get_gateway
            snap = get_gateway().market_index(self.symbol)
            if snap and snap.price > 0:
                result = {
                    'symbol': self.symbol,
                    'timestamp': snap.timestamp or datetime.now(),
                    'close': snap.price,
                    'prev_close': snap.prev_close,
                    'change_pct': snap.change_pct,
                    'source': 'gateway',
                }
            else:
                result = {'symbol': self.symbol, 'error': 'no data from gateway'}
        except Exception as e:
            result = {'symbol': self.symbol, 'error': str(e), 'source': 'failed'}

        self._cache = result
        self._cache_time = now
        return result

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        # Gateway market_index 无历史，fallback 到 yfinance K线（历史场景，延迟可接受）
        try:
            import yfinance as yf
            ticker = yf.Ticker(self.symbol)
            hist = ticker.history(period=f'{days+2}d', auto_adjust=True)
            return hist.tail(days)
        except Exception:
            return pd.DataFrame()


# ─── A 股行情 ─────────────────────────────────────────────────────────────────

class TencentMinuteDataSource(DataSource):
    """
    腾讯分钟K线数据（已迁移至 Gateway 统一数据出口）。

    fetch_latest()  → 最近1分钟 bar（走 Gateway.kline interval=1m）
    fetch_history(minutes) → 最近 N 分钟（走 Gateway.kline interval=1m）
    """

    name = 'TencentMinute'

    def __init__(self, symbol: str = '600900.SH'):
        self.symbol = symbol
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def _gateway_kline(self, limit: int) -> pd.DataFrame:
        """通过 Gateway 获取分钟K线（使用 Sina provider 的 KLINE_MINUTE 能力）。"""
        from core.data_gateway import get_gateway
        return get_gateway().kline(self.symbol, interval="1m", limit=limit)

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < 30:
            return self._cache

        try:
            df = self._gateway_kline(limit=10)
            if df.empty:
                return {'symbol': self.symbol, 'error': 'no data from gateway'}
            last = df.iloc[-1]
            dt_col = 'datetime' if 'datetime' in df.columns else 'date'
            result = {
                'symbol': self.symbol,
                'timestamp': last.get(dt_col, datetime.now()),
                'open': float(last['open']),
                'close': float(last['close']),
                'high': float(last['high']),
                'low': float(last['low']),
                'volume': float(last.get('volume', 0)),
                'source': 'gateway',
            }
            self._cache = result
            self._cache_time = now
            return result
        except Exception as e:
            return {'symbol': self.symbol, 'error': str(e), 'source': 'failed'}

    def fetch_history(self, minutes: int = 60) -> pd.DataFrame:
        """获取最近 N 分钟 K 线（走 Gateway）"""
        try:
            df = self._gateway_kline(limit=minutes)
            if 'datetime' in df.columns:
                df = df.set_index('datetime').sort_index()
            return df
        except Exception as e:
            print(f"[{self.name}] fetch_history error: {e}")
            return pd.DataFrame()


# ─── NorthBound ───────────────────────────────────────────────────────────────

class NorthBoundDataSource(DataSource):
    """
    北向资金数据（复用现有 cached_kamt）。
    fetch_latest() → 当日北向净流入（亿元）
    """

    name = 'NorthBound'

    def __init__(self):
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < 60:
            return self._cache

        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
            from services.data_cache import cached_kamt
            kamt = cached_kamt()
            result = {
                'timestamp': datetime.now(),
                'net_north_yi': kamt.get('net_north_yi', 0),
                'net_south_yi': kamt.get('net_south_yi', 0),
                'north_direction': 'BUY' if kamt.get('net_north_yi', 0) > 0 else 'SELL',
                'source': kamt.get('source', 'unknown'),
                'stale': kamt.get('stale', False),
            }
            self._cache = result
            self._cache_time = now
            return result
        except Exception as e:
            return {'symbol': 'KAMT', 'error': str(e), 'source': 'failed'}

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        # KAMT 历史需从数据库读取，当前返回空
        return pd.DataFrame()


# ─── 腾讯行情适配器（港股/美股/指数）──────────────────────────────────────────

class _TencentMarketSource(DataSource):
    """
    将 TencentQuoteDataSource 适配为 DataSource 接口。
    用于 CompositeMarketDataSource 获取港股/美股实时行情。
    """

    def __init__(self, symbol: str, cache_ttl: int = 30):
        self.symbol = symbol
        self.name = f'Tencent:{symbol}'
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._cache_ttl: int = cache_ttl

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            from core.data_gateway import get_gateway
            q = get_gateway().quote(self.symbol)
            if q and q.is_valid:
                result = {
                    'symbol': self.symbol,
                    'timestamp': datetime.now(),
                    'close': q.price,
                    'prev_close': q.prev_close,
                    'change_pct': q.pct_change,
                    'source': 'gateway',
                }
                self._cache = result
                self._cache_time = now
                return result
        except Exception as e:
            logger.debug("[_TencentMarketSource] fetch_latest failed for %s: %s", self.symbol, e)

        return {'symbol': self.symbol, 'error': 'fetch failed', 'source': 'failed'}

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        try:
            from core.data_gateway import get_gateway
            return get_gateway().kline(self.symbol, interval="daily", days=days)
        except Exception as e:
            logger.debug("[_TencentMarketSource] fetch_history failed for %s: %s", self.symbol, e)
            return pd.DataFrame()


# ─── Composite Market Data ────────────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """
    统一市场快照（合并所有外部数据）。
    用于信号生成前的市场环境判断。
    """
    timestamp: datetime = field(default_factory=datetime.now)

    # 外盘
    sp500_change_pct: float = 0       # S&P 500 期货涨跌幅（%）
    nasdaq_change_pct: float = 0      # Nasdaq 期货涨跌幅（%）
    vix: float = 0                    # VIX 恐慌指数
    hsih_change_pct: float = 0        # 恒生指数涨跌幅（%）

    # A 股
    ashare_change_pct: float = 0       # 上证/深证近期涨跌（%）

    # 北向
    north_net_yi: float = 0            # 北向资金净流入（亿元）

    # 内部
    atr_ratio: float = 0               # 市场 ATR ratio（来自 regime_detector）

    def is_us_bullish(self) -> bool:
        """美股期货上涨 → A 股高开概率"""
        return self.sp500_change_pct > 0.3

    def is_us_bearish(self) -> bool:
        return self.sp500_change_pct < -0.3

    def is_hk_bullish(self) -> bool:
        return self.hsih_change_pct > 0.5

    def is_high_volatility(self) -> bool:
        """VIX > 20 → 高波动"""
        return self.vix > 20

    def is_north_inflow(self) -> bool:
        return self.north_net_yi > 10  # 亿


class CompositeMarketDataSource(DataSource):
    """
    组合所有外部数据源，生成统一的 MarketSnapshot。

    所有数据源已迁移至 Gateway 统一数据出口：
    - _TencentMarketSource → Gateway.quotes()
    - SPFuturesDataSource / HSIFuturesDataSource → Gateway.market_index()
    - VIXDataSource → Gateway.market_index('^VIX')
    - NorthBoundDataSource → cached_kamt（内部服务，无外部直连）
    """

    name = 'CompositeMarket'

    def __init__(self):
        # 腾讯主源（走 Gateway.usHK/usUS market_index）
        self.sp500 = _TencentMarketSource('usSPY')
        self.nasdaq = _TencentMarketSource('usQQQ')
        self.hsi = _TencentMarketSource('hkHSI')

        # yfinance 兜底（走 Gateway.market_index，Gateway 层内部路由）
        self._sp500_fb = SPFuturesDataSource('ES=F')
        self._nasdaq_fb = SPFuturesDataSource('NQ=F')
        self._hsi_fb = HSIFuturesDataSource()

        # VIX / 北向
        self.vix = VIXDataSource()
        self.north = NorthBoundDataSource()

    def fetch_latest(self) -> MarketSnapshot:
        snap = MarketSnapshot()

        # 外盘（腾讯主源，yfinance 兜底——均已走 Gateway）
        for src, fb, attr in [
            (self.sp500, self._sp500_fb, 'sp500_change_pct'),
            (self.nasdaq, self._nasdaq_fb, 'nasdaq_change_pct'),
            (self.hsi, self._hsi_fb, 'hsih_change_pct'),
        ]:
            try:
                d = src.fetch_latest()
                val = d.get('change_pct', 0)
                if val or not d.get('error'):
                    setattr(snap, attr, val)
                else:
                    d2 = fb.fetch_latest()
                    setattr(snap, attr, d2.get('change_pct', 0))
            except Exception:
                try:
                    d2 = fb.fetch_latest()
                    setattr(snap, attr, d2.get('change_pct', 0))
                except Exception:
                    pass

        # VIX（走 Gateway.market_index('^VIX')）
        try:
            d = self.vix.fetch_latest()
            snap.vix = d.get('close', 0)
        except Exception:
            pass

        # 北向
        try:
            d = self.north.fetch_latest()
            snap.north_net_yi = d.get('net_north_yi', 0)
        except Exception:
            pass

        return snap

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        # 返回各源历史（简单拼 concat）
        frames = []
        for src, name in [
            (self.sp500, 'sp500'),
            (self.vix, 'vix'),
            (self.hsi, 'hsi'),
        ]:
            try:
                h = src.fetch_history(days)
                if not h.empty:
                    h['source'] = name
                    frames.append(h)
            except Exception:
                pass
        if frames:
            return pd.concat(frames)
        return pd.DataFrame()
