"""
core/level2.py — Level2 数据源 + 订单簿因子

Phase 4 核心组件：
  1. Level2DataSource: 新浪 10档买卖盘口（免费）
  2. TickBarAggregator: tick → 规则 K 线（Volume/Tick/Time bars）
  3. OrderImbalanceFactor: 订单不平衡度因子
  4. VWAPDeviationFactor: 成交价偏离 VWAP 因子
  5. AmihudIlliquidityFactor: Amihud 非流动性因子

数据来源：
  - 新浪实时行情（含10档盘口）：hq.sinajs.cn
  - 东方财富 Level2（扩展字段）：push2.eastmoney.com
  - 腾讯分钟K线（已实现）：TencentMinuteDataSource
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Callable, Tuple
import time
import threading
import os

import requests
import pandas as pd
import numpy as np


# ─── Level2 Data Source ───────────────────────────────────────────────────────

@dataclass
class OrderBook:
    """订单簿快照"""
    timestamp: datetime
    symbol: str
    bids: List[Tuple[float, int]]   # [(price, volume), ...] 共10档
    asks: List[Tuple[float, int]]   # [(price, volume), ...] 共10档
    last_price: float = 0
    volume: int = 0                 # 成交量（股）
    amount: float = 0              # 成交额（元）
    change_pct: float = 0          # 涨跌幅%

    def bid_ask_spread(self) -> float:
        """买卖价差（绝对值）"""
        if self.bids and self.asks:
            return self.asks[0][0] - self.bids[0][0]
        return 0

    def mid_price(self) -> float:
        """中间价"""
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2
        return self.last_price

    def order_imbalance(self) -> float:
        """
        订单不平衡度（Order Imbalance, OI）：
        OI = (BidVol - AskVol) / (BidVol + AskVol)
        范围 [-1, 1]
        正值 → 买方压力（价格上涨动力）
        负值 → 卖方压力（价格下跌动力）
        """
        bid_vol = sum(v for _, v in self.bids)
        ask_vol = sum(v for _, v in self.asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0
        return (bid_vol - ask_vol) / total

    def bid_ask_imbalance(self) -> float:
        """仅用第一档的 OI（最激进指标）"""
        if self.bids and self.asks:
            b0, a0 = self.bids[0][1], self.asks[0][1]
            total = b0 + a0
            if total == 0:
                return 0
            return (b0 - a0) / total
        return 0


class Level2DataSource:
    """
    新浪 Level2 实时盘口数据源（10档买卖盘）。
    polling 模式：每 interval 秒抓取一次。
    """

    name = 'Level2'

    def __init__(self, symbol: str = 'sh600900', interval: int = 3):
        self.symbol = symbol
        self.interval = interval
        self._last_ob: Optional[OrderBook] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._handler: Optional[Callable] = None
        self._cache: Optional[OrderBook] = None
        self._cache_time: float = 0
        self._cache_ttl: int = 2  # 2秒

    def fetch_latest(self) -> Optional[OrderBook]:
        """抓取最新订单簿"""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        ob = self._fetch_sina()
        if ob:
            self._cache = ob
            self._cache_time = now
            self._last_ob = ob
        return ob

    def _fetch_sina(self) -> Optional[OrderBook]:
        """
        新浪实时行情（含10档盘口）。
        数据格式：
        sh600900="名称,开盘,昨收,当前,最高,最低,买1价,买1量,..."
        """
        try:
            sym = self.symbol.replace('.SH', 'sh').replace('.SZ', 'sz')
            url = f'https://hq.sinajs.cn/rn={int(time.time())}&list={sym}'
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn',
            }
            resp = requests.get(url, headers=headers, timeout=8)
            # GBK decode
            text = resp.content.decode('gbk', errors='replace')
            return self._parse_sina(text)
        except Exception as e:
            print(f"[Level2] Sina fetch error: {e}")
            return None

    def _parse_sina(self, text: str) -> Optional[OrderBook]:
        """
        解析新浪行情文本（5档买卖盘口）。
        实际字段结构（34字段，验证于 2026-04-15）：
          [1]  开盘=26.410  [2] 昨收=26.420  [3] 当前=26.550
          [4]  最高=26.670  [5] 最低=26.360
          [8]  成交量=97056586  [9] 成交额=2576891492
          [10] ???=90246
          bid:  [11]=26.550/86900  [13]=26.540/132000  [15]=26.530/211200
                [17]=26.520/490500  [19]=26.510/89079
          ask:  [21]=26.560/136600  [23]=26.570/173600  [25]=26.580/339200
                [27]=26.590/328800  [29]=26.600/(无单独量字段)
          [30] 日期=[2026-04-15]  [31] 时间=[15:00:03]
        """
        try:
            content = text.split('"')[1] if '"' in text else ''
            if not content:
                return None
            fields = content.split(',')

            last_price = float(fields[3]) if len(fields) > 3 and fields[3] else 0
            prev_close = float(fields[2]) if len(fields) > 2 and fields[2] else last_price
            change_pct = (last_price - prev_close) / prev_close * 100 if prev_close else 0

            # 5档 bid: 价格在 [11,13,15,17,19]，量在 [12,14,16,18,20]
            bids = []
            for price_i, vol_i in zip([11,13,15,17,19], [12,14,16,18,20]):
                if len(fields) > vol_i and fields[price_i] and fields[vol_i]:
                    bids.append((float(fields[price_i]), int(float(fields[vol_i]))))

            # 5档 ask: 价格在 [21,23,25,27,29]，量在 [22,24,26,28]（ask5无量）
            asks = []
            for price_i, vol_i in zip([21,23,25,27,29], [22,24,26,28,None]):
                if len(fields) > price_i and fields[price_i]:
                    vol = int(float(fields[vol_i])) if vol_i and len(fields) > vol_i and fields[vol_i] else 0
                    asks.append((float(fields[price_i]), vol))

            volume = int(float(fields[8])) if len(fields) > 8 and fields[8] else 0
            amount = float(fields[9]) if len(fields) > 9 and fields[9] else 0

            dt = (fields[30] + ' ' + fields[31]).strip() if len(fields) > 31 else ''
            try:
                ts = datetime.strptime(dt, '%Y-%m-%d %H:%M:%S')
            except Exception:
                ts = datetime.now()

            return OrderBook(
                timestamp=ts, symbol=self.symbol,
                bids=bids, asks=asks,
                last_price=last_price, volume=volume,
                amount=amount, change_pct=change_pct,
            )
        except Exception as e:
            print(f"[Level2] Parse error: {e}: {text[:200]}")
            return None

    def subscribe(self, handler: Callable[['Level2DataSource', OrderBook], None]):
        """订阅实时盘口"""
        self._handler = handler

    def start_polling(self, interval: int = None):
        """启动轮询"""
        interval = interval or self.interval
        self._running = True

        def loop():
            while self._running:
                ob = self.fetch_latest()
                if ob and self._handler:
                    try:
                        self._handler(self, ob)
                    except Exception as e:
                        print(f"[Level2] handler error: {e}")
                time.sleep(interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self._thread

    def stop_polling(self):
        self._running = False

    def get_latest(self) -> Optional[OrderBook]:
        return self._last_ob


# ─── Tick Bar Aggregator ──────────────────────────────────────────────────────

@dataclass
class TickBar:
    """Tick Bar（规则K线）"""
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    n_ticks: int          # 累计 tick 数
    vwap: float           # 成交量加权平均价
    timestamp: datetime

    @property
    def change_pct(self) -> float:
        return (self.close - self.open) / self.open * 100 if self.open else 0


class TickBarAggregator:
    """
    Tick → 规则 Bar 聚合器。
    支持三种规则：
      - time:   固定时间窗口（固定间隔 bar）
      - volume: 固定成交量（Volume Bar）
      - tick:   固定 tick 数（Tick Bar）
    """

    def __init__(self, symbol: str, rule: str = 'time', threshold: int = 60):
        """
        rule: 'time' (秒) | 'volume' (股) | 'tick' (笔)
        threshold: 阈值
        """
        self.symbol = symbol
        self.rule = rule
        self.threshold = threshold
        self._current_bar: Optional[TickBar] = None
        self._bars: List[TickBar] = []
        self._bar_start_time: Optional[datetime] = None
        self._bar_start_vol: int = 0
        self._bar_start_amount: float = 0
        self._bar_start_n_ticks: int = 0

    def on_tick(self, price: float, volume: int, amount: float, timestamp: datetime):
        """
        接收 tick 数据，触发 bar 闭合检查。
        """
        if self._current_bar is None:
            self._start_bar(price, volume, amount, timestamp)
            return

        # 更新 bar
        self._current_bar.high = max(self._current_bar.high, price)
        self._current_bar.low = min(self._current_bar.low, price)
        self._current_bar.close = price
        self._current_bar.volume = volume - self._bar_start_vol
        self._current_bar.amount = amount - self._bar_start_amount
        self._current_bar.n_ticks = self._current_bar.n_ticks + 1 - self._bar_start_n_ticks

        # VWAP
        if self._current_bar.volume > 0:
            self._current_bar.vwap = self._current_bar.amount / self._current_bar.volume

        # 检查闭合条件
        if self._should_close(price, volume, timestamp):
            self._bars.append(self._current_bar)
            self._start_bar(price, volume, amount, timestamp)

    def _start_bar(self, price: float, volume: int, amount: float, ts: datetime):
        self._bar_start_time = ts
        self._bar_start_vol = volume
        self._bar_start_amount = amount
        self._bar_start_n_ticks = 0
        self._current_bar = TickBar(
            symbol=self.symbol,
            open=price, high=price, low=price, close=price,
            volume=0, amount=0,
            n_ticks=0, vwap=price,
            timestamp=ts,
        )

    def _should_close(self, price: float, volume: int, ts: datetime) -> bool:
        if self._current_bar is None:
            return False
        if self.rule == 'time':
            elapsed = (ts - self._bar_start_time).total_seconds()
            return elapsed >= self.threshold
        elif self.rule == 'volume':
            delta_vol = volume - self._bar_start_vol
            return delta_vol >= self.threshold
        elif self.rule == 'tick':
            return self._current_bar.n_ticks >= self.threshold
        return False

    def get_latest_bar(self) -> Optional[TickBar]:
        return self._current_bar

    def get_closed_bars(self) -> List[TickBar]:
        return list(self._bars)


# ─── Level2 Factor Base ───────────────────────────────────────────────────────

@dataclass
class Level2Factor:
    """
    Level2 因子基类。
    基于订单簿快照计算因子值。
    """
    name: str = 'Level2Factor'
    lookback: int = 20   # 多少个快照做平滑

    def evaluate(self, obs: List[OrderBook]) -> pd.Series:
        """
        计算因子时间序列。
        obs: 按时间排序的 OrderBook 快照列表。
        返回: z-score 归一化的因子值（pd.Series）
        """
        if len(obs) < 2:
            return pd.Series(dtype=float)

        raw = self._raw(obs)
        return self._normalize(raw, obs)

    def _raw(self, obs: List[OrderBook]) -> pd.Series:
        """子类实现：计算原始因子值"""
        raise NotImplementedError

    def _normalize(self, raw: pd.Series, obs: List[OrderBook]) -> pd.Series:
        """z-score 归一化"""
        mean = raw.mean()
        std = raw.std()
        if std == 0 or pd.isna(std):
            return pd.Series(0, index=raw.index)
        return (raw - mean) / std

    def signals(self, factor_values: pd.Series, threshold: float = 1.0) -> List:
        """从因子值生成信号"""
        latest = factor_values.iloc[-1] if len(factor_values) > 0 else 0
        if abs(latest) < threshold:
            return []
        direction = 'SELL' if latest > 0 else 'BUY'
        from core.factors.base import Signal
        return [Signal(
            timestamp=datetime.now(),
            symbol='',
            direction=direction,
            strength=min(abs(latest) / threshold, 1.0),
            factor_name=self.name,
            price=0,
        )]


class OrderImbalanceFactor(Level2Factor):
    """
    订单不平衡度因子（OI）。
    OI > 0 → 买方压力 → 价格上涨动力
    OI < 0 → 卖方压力 → 价格下跌动力
    """

    name = 'OrderImbalance'

    def _raw(self, obs: List[OrderBook]) -> pd.Series:
        timestamps = [ob.timestamp for ob in obs]
        oi_values = [ob.order_imbalance() for ob in obs]
        return pd.Series(oi_values, index=timestamps)


class BidAskSpreadFactor(Level2Factor):
    """
    买卖价差因子（Spread）。
    价差扩大 → 流动性紧张 → 波动率上升
    """

    name = 'BidAskSpread'

    def _raw(self, obs: List[OrderBook]) -> pd.Series:
        timestamps = [ob.timestamp for ob in obs]
        spreads = [ob.bid_ask_spread() for ob in obs]
        return pd.Series(spreads, index=timestamps)


class MidPriceDriftFactor(Level2Factor):
    """
    中间价漂移因子。
    mid_price(t) - mid_price(t-1) > 0 → 价格向上
    """

    name = 'MidPriceDrift'

    def _raw(self, obs: List[OrderBook]) -> pd.Series:
        mids = [ob.mid_price() for ob in obs]
        timestamps = [ob.timestamp for ob in obs]
        s = pd.Series(mids, index=timestamps)
        return s.diff()


class VolumeRateFactor(Level2Factor):
    """
    量比因子。
    当前成交量 / 前 N 个快照平均成交量
    """

    name = 'VolumeRate'

    def __init__(self, lookback: int = 20, **kwargs):
        super().__init__(lookback=lookback, **kwargs)

    def _raw(self, obs: List[OrderBook]) -> pd.Series:
        timestamps = [ob.timestamp for ob in obs]
        volumes = [ob.volume for ob in obs]
        s = pd.Series(volumes, index=timestamps)
        # 量比 = 当前 / 移动平均
        ma = s.rolling(self.lookback, min_periods=1).mean()
        rate = s / ma.replace(0, 1)
        return rate


class AmihudIlliquidityFactor(Level2Factor):
    """
    Amihud 非流动性因子（ILIQ）。
    ILIQ = |return| / volume
    衡量单位成交量对价格的冲击。
    值越大 → 流动性越差（冲击成本高）。
    """

    name = 'AmihudILIQ'

    def __init__(self, lookback: int = 20, **kwargs):
        super().__init__(lookback=lookback, **kwargs)

    def _raw(self, obs: List[OrderBook]) -> pd.Series:
        timestamps = [ob.timestamp for ob in obs]
        n = len(obs)
        values = []
        for i in range(n):
            if i == 0:
                values.append(0)
                continue
            ret = abs((obs[i].last_price - obs[i-1].last_price) / obs[i-1].last_price) if obs[i-1].last_price else 0
            vol = obs[i].volume - obs[i-1].volume if i > 0 else obs[i].volume
            if vol > 0:
                values.append(ret / (vol / 1e6))  # 成交量转万元单位
            else:
                values.append(0)
        s = pd.Series(values, index=timestamps)
        return s.rolling(self.lookback, min_periods=1).mean()
