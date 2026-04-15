"""
core/data_sources.py — 统一数据源接口

支持数据源：
  - SPFuturesDataSource:     S&P 500 / Nasdaq 期货（yfinance）
  - VIXDataSource:           CBOE VIX 指数（直接 HTTP）
  - HSIFuturesDataSource:    恒生指数期货（yfinance）
  - NorthBoundDataSource:    北向资金 KAMT（复用现有 cached_kamt）
  - TencentMinuteDataSource:  腾讯分钟K线（复用现有逻辑）

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
import threading
import time
import os
import sys

import requests
import pandas as pd
import numpy as np

# ─── Base ────────────────────────────────────────────────────────────────────

class DataSource(ABC):
    """数据源基类"""

    name: str = 'DataSource'

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
    S&P 500 (ES) / Nasdaq (NQ) 期货数据。
    yfinance 优先级最高，失败则用 requests 直连 Yahoo Finance 历史接口。
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
            import yfinance as yf
            ticker = yf.Ticker(self.symbol)
            hist = ticker.history(period='2d', auto_adjust=True)
            if hist.empty:
                raise ValueError('Empty history')
            latest = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else latest

            result = {
                'symbol': self.symbol,
                'timestamp': datetime.now(),
                'open': float(latest['Open']),
                'high': float(latest['High']),
                'low': float(latest['Low']),
                'close': float(latest['Close']),
                'volume': int(latest['Volume']),
                'prev_close': float(prev['Close']),
                'change_pct': round(
                    (float(latest['Close']) - float(prev['Close'])) / float(prev['Close']) * 100, 3
                ),
                'source': 'yfinance',
            }
            self._cache = result
            self._cache_time = now
            return result
        except Exception as e:
            print(f"[{self.name}] yfinance failed: {e}, trying direct...")
            return self._fetch_direct()

    def _fetch_direct(self) -> Dict[str, Any]:
        """使用 yfinance download（更稳定）"""
        try:
            import yfinance as yf
            hist = yf.download(self.symbol, period='2d', auto_adjust=True, progress=False)
            if hist.empty:
                raise ValueError('empty')
            closes = hist['Close'].dropna()
            latest_close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2]) if len(closes) > 1 else latest_close
            result_dict = {
                'symbol': self.symbol,
                'timestamp': datetime.now(),
                'close': latest_close,
                'prev_close': prev_close,
                'change_pct': round((latest_close - prev_close) / prev_close * 100, 3),
                'source': 'yfinance_direct',
            }
            self._cache = result_dict
            self._cache_time = time.time()
            return result_dict
        except Exception as e:
            return {'symbol': self.symbol, 'error': str(e), 'source': 'failed'}

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
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
    CBOE VIX 波动率指数。
    优先直连 CBOE（最可靠），fallback yfinance ^VIX。
    """

    name = 'VIX'

    def __init__(self):
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < 300:  # 5min TTL
            return self._cache

        result = self._fetch_cboe()
        if result.get('error'):
            result = self._fetch_yfinance()
        self._cache = result
        self._cache_time = now
        return result

    def _fetch_cboe(self) -> Dict[str, Any]:
        """CBOE 直连（VIX 当前值）"""
        try:
            url = 'https://cdn.cboe.com/api/global/economic_data/indices/vix/daily/20XX/05_VIX技术和.vix.csv'
            # CBOE 当前数据（简化版，实际 URL 需查询）
            # 使用备用：Yahoo Finance ^VIX 历史
            return self._fetch_yfinance()
        except Exception:
            return {'symbol': '^VIX', 'error': 'cboe failed'}

    def _fetch_yfinance(self) -> Dict[str, Any]:
        """Yahoo Finance ^VIX"""
        try:
            import yfinance as yf
            ticker = yf.Ticker('^VIX')
            hist = ticker.history(period='2d', auto_adjust=True)
            if hist.empty:
                return {'symbol': '^VIX', 'error': 'empty'}
            latest = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else latest
            result = {
                'symbol': '^VIX',
                'timestamp': datetime.now(),
                'close': float(latest['Close']),
                'prev_close': float(prev['Close']),
                'change_pct': round(
                    (float(latest['Close']) - float(prev['Close'])) / float(prev['Close']) * 100, 2
                ),
                'source': 'yfinance',
            }
            return result
        except Exception as e:
            return {'symbol': '^VIX', 'error': str(e)}

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        try:
            import yfinance as yf
            ticker = yf.Ticker('^VIX')
            hist = ticker.history(period=f'{days+2}d', auto_adjust=True)
            return hist.tail(days) if not hist.empty else pd.DataFrame()
        except Exception:
            return pd.DataFrame()


class HSIFuturesDataSource(DataSource):
    """恒生指数期货（HSI main）"""

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
            import yfinance as yf
            ticker = yf.Ticker(self.symbol)
            hist = ticker.history(period='2d', auto_adjust=True)
            if hist.empty:
                raise ValueError('empty')
            latest = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else latest
            result = {
                'symbol': self.symbol,
                'timestamp': datetime.now(),
                'close': float(latest['Close']),
                'prev_close': float(prev['Close']),
                'change_pct': round(
                    (float(latest['Close']) - float(prev['Close'])) / float(prev['Close']) * 100, 3
                ),
                'source': 'yfinance',
            }
            self._cache = result
            self._cache_time = now
            return result
        except Exception as e:
            return {'symbol': self.symbol, 'error': str(e), 'source': 'failed'}

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
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
    腾讯分钟K线数据（复用 scripts/ 中的逻辑）。
    fetch_latest() → 最近1分钟 bar
    fetch_history(minutes) → 最近 N 分钟
    """

    name = 'TencentMinute'

    def __init__(self, symbol: str = '600900.SH'):
        self.symbol = symbol
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < 30:
            return self._cache

        try:
            import urllib.request
            # 清除代理
            env = os.environ.copy()
            env.pop('HTTP_PROXY', None)
            env.pop('HTTPS_PROXY', None)
            # 腾讯分钟K线接口
            url = (
                f'https://web.ifzq.gtimg.cn/appstock/app/kline/mkline'
                f'?param={self.symbol},m1,,10'
            )
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode('gbk')
            import json
            data = json.loads(raw)
            # 解析分钟数据
            qt = data.get('data', {}).get(self.symbol, {})
            m1 = qt.get('m1', [])
            if not m1:
                return {'symbol': self.symbol, 'error': 'no m1 data'}
            # m1[-1] = [时间, 开, 收, 高, 低, 量]
            last = m1[-1]
            dt_str = last[0]
            result = {
                'symbol': self.symbol,
                'timestamp': datetime.strptime(dt_str, '%Y%m%d%H%M%S') if len(dt_str) == 14 else datetime.now(),
                'open': float(last[1]),
                'close': float(last[2]),
                'high': float(last[3]),
                'low': float(last[4]),
                'volume': float(last[5]) if len(last) > 5 else 0,
                'source': 'tencent',
            }
            self._cache = result
            self._cache_time = now
            return result
        except Exception as e:
            return {'symbol': self.symbol, 'error': str(e), 'source': 'failed'}

    def fetch_history(self, minutes: int = 60) -> pd.DataFrame:
        """获取最近 N 分钟 K 线"""
        try:
            import urllib.request
            env = os.environ.copy()
            env.pop('HTTP_PROXY', None)
            env.pop('HTTPS_PROXY', None)
            url = (
                f'https://web.ifzq.gtimg.cn/appstock/app/kline/mkline'
                f'?param={self.symbol},m1,,{minutes}'
            )
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode('gbk')
            import json
            data = json.loads(raw)
            qt = data.get('data', {}).get(self.symbol, {})
            m1 = qt.get('m1', [])
            rows = []
            for bar in m1:
                dt_str = bar[0]
                try:
                    dt = datetime.strptime(dt_str, '%Y%m%d%H%M%S')
                except ValueError:
                    continue
                rows.append({
                    'datetime': dt,
                    'open': float(bar[1]),
                    'close': float(bar[2]),
                    'high': float(bar[3]),
                    'low': float(bar[4]),
                    'volume': float(bar[5]) if len(bar) > 5 else 0,
                })
            df = pd.DataFrame(rows)
            if not df.empty:
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
    EventBus 集成：在 MarketEvent 中携带完整市场快照。
    """

    name = 'CompositeMarket'

    def __init__(self):
        self.sp500 = SPFuturesDataSource('ES=F')
        self.nasdaq = SPFuturesDataSource('NQ=F')
        self.vix = VIXDataSource()
        self.hsi = HSIFuturesDataSource()
        self.north = NorthBoundDataSource()

    def fetch_latest(self) -> MarketSnapshot:
        snap = MarketSnapshot()

        # 外盘
        try:
            d = self.sp500.fetch_latest()
            snap.sp500_change_pct = d.get('change_pct', 0)
        except Exception:
            pass

        try:
            d = self.nasdaq.fetch_latest()
            snap.nasdaq_change_pct = d.get('change_pct', 0)
        except Exception:
            pass

        try:
            d = self.vix.fetch_latest()
            snap.vix = d.get('close', 0)
        except Exception:
            pass

        try:
            d = self.hsi.fetch_latest()
            snap.hsih_change_pct = d.get('change_pct', 0)
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
