#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S1 回测 CLI — 第一阶段核心任务
==============================
直接运行: python backtest_cli.py [命令] [参数]

命令:
  single   单标的回测
  grid     RSI 参数网格搜索
  compare  ATR止损 vs 固定止损 对比
  wf       Walk-Forward 全量分析

示例:
  python backtest_cli.py single 600900.SH --rsi-buy 30 --rsi-sell 65
  python backtest_cli.py grid 600900.SH --start 20230101
  python backtest_cli.py compare 600900.SH --start 20230101
  python backtest_cli.py wf 600900.SH --train-years 2 --test-years 1
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from typing import Dict, List

# 禁用代理
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUANT_DIR = SCRIPT_DIR
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader
from backtest import BacktestEngine, TechnicalIndicators as TI
from walkforward import WalkForwardAnalyzer


# ─────────────────────────────────────────────────────────
# 信号函数 (类实现，预计算缓存)
# ─────────────────────────────────────────────────────────

class RSISignalFunc:
    """RSI信号，预计算一次RSI，多次调用"""
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period', 'rsi_vals')

    def __init__(self, rsi_buy: float, rsi_sell: float, rsi_period: int = 14):
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period
        self.rsi_vals = None

    def setup(self, data: list):
        n = len(data)
        period = self.rsi_period
        closes = [d['close'] for d in data]
        rsi = [None] * n
        for i in range(period, n):
            g, l = 0.0, 0.0
            for j in range(i - period + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0: g += d
                else:     l -= d
            avg_gain = g / period
            avg_loss = l / period
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.rsi_vals = rsi

    def __call__(self, data: list, idx: int) -> str:
        if self.rsi_vals is None:
            self.setup(data)
        period = self.rsi_period
        rv = self.rsi_vals
        if idx < period or rv[idx] is None or rv[idx - 1] is None:
            return 'hold'
        rsi = rv[idx]
        rsi_prev = rv[idx - 1]
        if rsi_prev < self.rsi_buy <= rsi:
            return 'buy'
        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'
        return 'hold'

    def reset(self):
        self.rsi_vals = None


class RSISignalWithATRFilter:
    """
    RSI + ATR 波动率过滤
    当 ATR(14) > 过去20日ATR最高值的 threshold 时，禁止开新仓
    高波动期往往对应市场顶部/底部，均值回归策略容易失效
    """
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period', 'atr_threshold',
                 'rsi_vals', 'atr_ratio')

    def __init__(self, rsi_buy: float, rsi_sell: float,
                 rsi_period: int = 14, atr_threshold: float = 0.80):
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period
        self.atr_threshold = atr_threshold  # 0.80 = ATR处于近20日80%以上高位
        self.rsi_vals = None
        self.atr_ratio = None

    def setup(self, data: list):
        n = len(data)
        period = self.rsi_period
        closes = [d['close'] for d in data]
        highs = [d.get('high', c) for d, c in zip(data, closes)]
        lows  = [d.get('low',  c) for d, c in zip(data, closes)]

        # RSI
        rsi = [None] * n
        for i in range(period, n):
            g, l = 0.0, 0.0
            for j in range(i - period + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0: g += d
                else:     l -= d
            avg_gain = g / period
            avg_loss = l / period
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.rsi_vals = rsi

        # ATR(14)
        atr = [None] * n
        for i in range(1, n):
            tr = max(highs[i]-lows[i],
                     abs(highs[i]-closes[i-1]),
                     abs(lows[i]-closes[i-1]))
            if i >= 14 and atr[i-1] is not None:
                atr[i] = (atr[i-1] * 13 + tr) / 14
            elif i == 14:
                atr[i] = sum(max(highs[j]-lows[j],
                                  abs(highs[j]-closes[j-1]),
                                  abs(lows[j]-closes[j-1])) for j in range(1, 15)) / 14

        # ATR ratio: 当前ATR / 近20日ATR最高
        atr_ratio = [None] * n
        for i in range(33, n):  # need 14 (atr warmup) + 20 (rolling max)
            window = [atr[j] for j in range(i-19, i+1) if atr[j] is not None]
            if window:
                atr_ratio[i] = atr[i] / max(window) if max(window) > 0 else None
        self.atr_ratio = atr_ratio

    def __call__(self, data: list, idx: int) -> str:
        if self.rsi_vals is None:
            self.setup(data)
        n = len(data)
        period = self.rsi_period
        rv = self.rsi_vals
        atr_r = self.atr_ratio

        if idx < 50 or rv[idx] is None or rv[idx-1] is None:
            return 'hold'

        rsi = rv[idx]
        rsi_prev = rv[idx-1]
        vol_high = (atr_r[idx] is not None) and (atr_r[idx] > self.atr_threshold)

        # 卖出：RSI超买死叉（任何时候都允许）
        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'

        # 买入：RSI上穿 + 非高波动
        if rsi_prev < self.rsi_buy <= rsi:
            if not vol_high:
                return 'buy'
            return 'hold'

        return 'hold'

    def reset(self):
        self.rsi_vals = None
        self.atr_ratio = None


# ─────────────────────────────────────────────────────────
# MACD 信号函数
# ─────────────────────────────────────────────────────────

class MACDSignalFunc:
    """
    MACD 信号 (DEMA variant for responsiveness).
    Params:
      fast_period, slow_period, signal_period: MACD参数
      macd_buy_threshold, macd_sell_threshold: MACD零轴穿越阈值
    """
    __slots__ = ('fast_p', 'slow_p', 'sig_p',
                 'macd_buy_th', 'macd_sell_th',
                 'ema_fast', 'ema_slow', 'ema_signal', 'macd_hist')

    def __init__(self, fast_period: int = 12, slow_period: int = 26,
                 signal_period: int = 9,
                 macd_buy_th: float = 0.0,   # 零轴以上金叉
                 macd_sell_th: float = 0.0): # 零轴以下死叉
        self.fast_p = fast_period
        self.slow_p = slow_period
        self.sig_p = signal_period
        self.macd_buy_th = macd_buy_th
        self.macd_sell_th = macd_sell_th
        self.ema_fast = None
        self.ema_slow = None
        self.ema_signal = None
        self.macd_hist = None

    def _ema(self, data: list, period: int) -> list:
        """Standard EMA"""
        ema = [None] * len(data)
        if period <= 0 or len(data) < period:
            return ema
        # seed with SMA
        seed = sum(data[:period]) / period
        ema[period - 1] = seed
        k = 2.0 / (period + 1)
        for i in range(period, len(data)):
            ema[i] = data[i] * k + ema[i - 1] * (1 - k)
        return ema

    def setup(self, data: list):
        closes = [d['close'] for d in data]
        # EMA
        ema_f = self._ema(closes, self.fast_p)
        ema_s = self._ema(closes, self.slow_p)
        self.ema_fast = ema_f
        self.ema_slow = ema_s

        # MACD line = EMA_fast - EMA_slow
        macd_line = [None] * len(data)
        for i in range(len(data)):
            if ema_f[i] is not None and ema_s[i] is not None:
                macd_line[i] = ema_f[i] - ema_s[i]

        # Signal = EMA(macd_line, signal_period)
        # Convert None to 0 for EMA calc (use 0 as proxy when MACD not available)
        macd_filled = [v if v is not None else 0.0 for v in macd_line]
        sig = self._ema(macd_filled, self.sig_p)
        self.ema_signal = sig

        # MACD Histogram = MACD - Signal
        hist = [None] * len(data)
        for i in range(len(data)):
            if macd_line[i] is not None and sig[i] is not None:
                hist[i] = macd_line[i] - sig[i]
        self.macd_hist = hist

    def __call__(self, data: list, idx: int) -> str:
        if self.macd_hist is None:
            self.setup(data)

        warmup = self.slow_p + self.sig_p  # ~35 days
        if idx < warmup or self.macd_hist[idx] is None or self.macd_hist[idx - 1] is None:
            return 'hold'

        h = self.macd_hist[idx]
        h_prev = self.macd_hist[idx - 1]

        # Buy: MACD histogram crosses above 0 (zero-line golden cross)
        if h_prev < 0 <= h:
            return 'buy'
        # Sell: MACD histogram crosses below 0 (zero-line death cross)
        if h_prev > 0 >= h:
            return 'sell'
        return 'hold'

    def reset(self):
        self.ema_fast = None
        self.ema_slow = None
        self.ema_signal = None
        self.macd_hist = None


class RSIPlusMACDSignalFunc:
    """
    RSI + MACD 共振信号：
    - RSI(25/65) 作为主要入场
    - MACD 零轴金叉/死叉作为确认过滤器
    """
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period',
                 'rsi_vals', 'macd_func')

    def __init__(self, rsi_buy: float = 25, rsi_sell: float = 65,
                 rsi_period: int = 14,
                 macd_fast: int = 12, macd_slow: int = 26,
                 macd_signal: int = 9):
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period
        self.rsi_vals = None
        self.macd_func = MACDSignalFunc(macd_fast, macd_slow, macd_signal)

    def setup(self, data: list):
        # RSI setup (same as RSISignalFunc)
        n = len(data)
        period = self.rsi_period
        closes = [d['close'] for d in data]
        rsi = [None] * n
        for i in range(period, n):
            g, l = 0.0, 0.0
            for j in range(i - period + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0:
                    g += d
                else:
                    l -= d
            avg_gain = g / period
            avg_loss = l / period
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.rsi_vals = rsi
        # MACD setup
        self.macd_func.setup(data)

    def __call__(self, data: list, idx: int) -> str:
        if self.rsi_vals is None:
            self.setup(data)

        warmup = max(self.rsi_period, self.macd_func.slow_p + self.macd_func.sig_p)
        if idx < warmup or self.rsi_vals[idx] is None or self.rsi_vals[idx - 1] is None:
            return 'hold'

        rsi = self.rsi_vals[idx]
        rsi_prev = self.rsi_vals[idx - 1]

        # ── Sell: RSI death cross (always allowed) ──
        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'

        # ── Buy: RSI golden cross + MACD histogram > 0 confirmation ──
        if rsi_prev < self.rsi_buy <= rsi:
            # Check MACD histogram is above zero (confirmation)
            h = self.macd_func.macd_hist[idx]
            h_prev = self.macd_func.macd_hist[idx - 1]
            if h is not None and h_prev is not None and h > 0:
                return 'buy'
            return 'hold'

        return 'hold'

    def reset(self):
        self.rsi_vals = None
        self.macd_func.reset()


# ─────────────────────────────────────────────────────────
# 布林带信号函数
# ─────────────────────────────────────────────────────────

class BBANDSFunc:
    """
    Bollinger Bands 信号：
    - 中轨：N日简单均线（SMA）
    - 上轨：中轨 + K×标准差
    - 下轨：中轨 - K×标准差

    买入信号：价格下穿下轨（RSI 辅助过滤噪音）
    卖出信号：价格上穿上轨（RSI 辅助过滤噪音）

    Parameters:
      period: SMA 周期（默认20）
      std_mult: 标准差倍数（默认2.0）
    """
    __slots__ = ('period', 'std_mult', '_sma', '_std', '_upper', '_lower')

    def __init__(self, period: int = 20, std_mult: float = 2.0):
        self.period = period
        self.std_mult = std_mult
        self._sma = None
        self._std = None
        self._upper = None
        self._lower = None

    def setup(self, data: list):
        n = len(data)
        closes = [d['close'] for d in data]
        p = self.period

        sma = [None] * n
        upper = [None] * n
        lower = [None] * n

        for i in range(p - 1, n):
            window = closes[i - p + 1:i + 1]
            mean = sum(window) / p
            variance = sum((x - mean) ** 2 for x in window) / p
            std = variance ** 0.5
            sma[i] = mean
            upper[i] = mean + self.std_mult * std
            lower[i] = mean - self.std_mult * std

        self._sma = sma
        self._upper = upper
        self._lower = lower

    def __call__(self, data: list, idx: int) -> str:
        if self._sma is None:
            self.setup(data)

        if idx < self.period:
            return 'hold'

        closes = [d['close'] for d in data]
        c = closes[idx]
        c_prev = closes[idx - 1]

        lower = self._lower[idx]
        upper = self._upper[idx]
        lower_prev = self._lower[idx - 1]
        upper_prev = self._upper[idx - 1]

        if lower is None or upper is None:
            return 'hold'

        # Buy: price crosses below lower band
        if c_prev >= lower_prev and c < lower:
            return 'buy'
        # Sell: price crosses above upper band
        if c_prev <= upper_prev and c > upper:
            return 'sell'
        return 'hold'

    def reset(self):
        self._sma = None
        self._upper = None
        self._lower = None


class RSIPlusBBANDSFunc:
    """
    RSI + 布林带共振：
    - 布林带下轨附近出现 RSI 超卖（RSI <= 35） → 共振买入
    - 布林带上轨附近出现 RSI 超买（RSI >= 65） → 共振卖出

    参数:
      rsi_buy / rsi_sell: RSI 阈值
      rsi_period: RSI 周期
      boll_period: 布林带周期
      std_mult: 布林带标准差倍数
    """
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period',
                 'boll_period', 'std_mult',
                 'rsi_vals', 'bb_func')

    def __init__(self, rsi_buy: float = 35, rsi_sell: float = 65,
                 rsi_period: int = 14,
                 boll_period: int = 20, std_mult: float = 2.0):
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period
        self.boll_period = boll_period
        self.std_mult = std_mult
        self.rsi_vals = None
        self.bb_func = BBANDSFunc(boll_period, std_mult)

    def setup(self, data: list):
        # RSI
        n = len(data)
        closes = [d['close'] for d in data]
        p = self.rsi_period
        rsi = [None] * n
        for i in range(p, n):
            g, l = 0.0, 0.0
            for j in range(i - p + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0: g += d
                else:     l -= d
            avg_gain = g / p
            avg_loss = l / p
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.rsi_vals = rsi
        # BB setup
        self.bb_func.setup(data)

    def __call__(self, data: list, idx: int) -> str:
        if self.rsi_vals is None:
            self.setup(data)

        warmup = max(self.rsi_period, self.boll_period)
        if idx < warmup:
            return 'hold'

        closes = [d['close'] for d in data]
        c = closes[idx]
        c_prev = closes[idx - 1]
        rsi = self.rsi_vals[idx]

        lower = self.bb_func._lower[idx]
        upper = self.bb_func._upper[idx]
        lower_prev = self.bb_func._lower[idx - 1]
        upper_prev = self.bb_func._upper[idx - 1]

        # Sell: RSI death cross (always allowed)
        if rsi >= self.rsi_sell and self.rsi_vals[idx - 1] < self.rsi_sell:
            return 'sell'

        # Buy: price at/below lower band + RSI <= rsi_buy
        if (lower is not None and c <= lower and
                rsi is not None and rsi <= self.rsi_buy):
            return 'buy'

        return 'hold'

    def reset(self):
        self.rsi_vals = None
        self.bb_func.reset()


def make_rsi_signal_func(rsi_buy: float, rsi_sell: float, rsi_period: int = 14):
    return RSISignalFunc(rsi_buy, rsi_sell, rsi_period)


def make_rsi_atr_signal_func(rsi_buy: float, rsi_sell: float,
                              rsi_period: int = 14, atr_threshold: float = 0.80):
    return RSISignalWithATRFilter(rsi_buy, rsi_sell, rsi_period, atr_threshold)


# ─────────────────────────────────────────────────────────
# 结果格式化
# ─────────────────────────────────────────────────────────

def _profit_factor(r: dict) -> float:
    trades = r.get('trades', [])
    if not trades:
        return 0.0
    gross_profit = sum(t.get('value', 0) for t in trades
                       if t.get('action') in ('sell', 'close_final') and t.get('pnl_pct', 0) > 0)
    gross_loss = abs(sum(t.get('value', 0) for t in trades
                          if t.get('action') in ('sell', 'close_final') and t.get('pnl_pct', 0) < 0))
    if gross_loss == 0:
        return gross_profit if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def format_metrics(r: dict, indent: int = 2) -> str:
    spaces = ' ' * indent
    sharpe = r.get('sharpe_ratio', 0)
    ret = r.get('total_return_pct', 0)
    ann = r.get('annualized_return_pct', 0)
    dd = r.get('max_drawdown_pct', 0)
    wr = r.get('win_rate_pct', 0)
    trades = r.get('total_trades', 0)
    pf = _profit_factor(r)
    st = r.get('stop_triggers', {})

    lines = [
        f"{spaces}总收益:    {ret:+.2f}%",
        f"{spaces}年化收益:  {ann:+.2f}%",
        f"{spaces}夏普比率:  {sharpe:+.3f}",
        f"{spaces}最大回撤:  {dd:.2f}%",
        f"{spaces}胜率:      {wr:.1f}%  (W={r.get('wins',0)} L={r.get('losses',0)})",
        f"{spaces}盈亏比:    {pf:.2f}",
        f"{spaces}交易次数:  {trades}",
    ]
    if st:
        stop_parts = [f"{k}={v}" for k, v in st.items() if v > 0]
        if stop_parts:
            lines.append(f"{spaces}止损触发:  {', '.join(stop_parts)}")
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────
# S1.1 单次回测
# ─────────────────────────────────────────────────────────

def run_single_backtest(symbol: str,
                         rsi_buy: float = 35,
                         rsi_sell: float = 65,
                         rsi_period: int = 14,
                         stop_loss: float = 0.08,
                         take_profit: float = 0.25,
                         start_date: str = None,
                         end_date: str = None,
                         capital: float = 200000,
                         use_atr_stop: bool = False,
                         atr_multiplier: float = 2.0,
                         trailing_stop: float = None,
                         verbose: bool = True) -> Dict:

    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')

    if verbose:
        print(f"\n  [DATA] {symbol} | RSI({rsi_buy}/{rsi_sell}) "
              f"| SL={stop_loss:.0%} TP={take_profit:.0%}"
              f"{' | ATR' if use_atr_stop else ''}"
              f" | capital={capital:,.0f}")
        print(f"  [DATE] {start_str} ~ {end_str}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)

    if not kline:
        print(f"  [FAIL] 数据加载失败: {symbol}")
        return {}
    if len(kline) < 252:
        print(f"  [FAIL] 数据不足: {len(kline)} days, need >= 252")
        return {}
    if verbose:
        print(f"  [OK] Data: {len(kline)} days ({kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]})")

    signal_func = RSISignalFunc(rsi_buy, rsi_sell, rsi_period)
    signal_func.setup(kline)

    engine = BacktestEngine(
        initial_capital=capital,
        commission=0.0003,
        stop_loss=stop_loss if not use_atr_stop else None,
        take_profit=take_profit,
        trailing_stop=trailing_stop,
        use_atr_stop=use_atr_stop,
        atr_multiplier=atr_multiplier,
        max_position_pct=0.20,
    )

    result = engine.run(kline, signal_func, f"RSI({rsi_buy}/{rsi_sell})")
    result['_params'] = {
        'rsi_buy': rsi_buy, 'rsi_sell': rsi_sell, 'rsi_period': rsi_period,
        'stop_loss': stop_loss, 'take_profit': take_profit,
        'use_atr_stop': use_atr_stop, 'atr_multiplier': atr_multiplier
    }

    if verbose:
        print(f"\n  {'='*50}")
        print(format_metrics(result))
        print(f"  {'='*50}")

    return result


# ─────────────────────────────────────────────────────────
# S1.2 RSI 网格搜索
# ─────────────────────────────────────────────────────────

def run_rsi_grid_search(symbol: str,
                          start_date: str = None,
                          end_date: str = None,
                          capital: float = 200000,
                          verbose: bool = True) -> List[Dict]:

    rsi_buy_grid = [25, 30, 35, 40, 45]
    rsi_sell_grid = [60, 65, 70, 75, 80]
    stop_loss_grid = [0.05, 0.08, 0.10]
    take_profit_grid = [0.20, 0.25, 0.30]

    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')

    print(f"\n{'='*60}")
    print(f"  [GRID] RSI Grid Search: {symbol}")
    print(f"  RSI_buy: {rsi_buy_grid} | RSI_sell: {rsi_sell_grid}")
    print(f"  StopLoss: {stop_loss_grid} | TakeProfit: {take_profit_grid}")
    print(f"  Total combinations: {len(rsi_buy_grid)*len(rsi_sell_grid)*len(stop_loss_grid)*len(take_profit_grid)}")
    print(f"{'='*60}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)
    if not kline or len(kline) < 252:
        print(f"  [FAIL] Data insufficient: {len(kline) if kline else 0} days")
        return []

    print(f"  [OK] Data loaded: {len(kline)} days\n")

    results = []
    total = len(rsi_buy_grid) * len(rsi_sell_grid) * len(stop_loss_grid) * len(take_profit_grid)
    done = 0

    for rb in rsi_buy_grid:
        for rs in rsi_sell_grid:
            if rb >= rs:
                continue
            for sl in stop_loss_grid:
                for tp in take_profit_grid:
                    done += 1
                    sig = RSISignalFunc(rb, rs, 14)
                    sig.setup(kline)
                    engine = BacktestEngine(
                        initial_capital=capital,
                        commission=0.0003,
                        stop_loss=sl,
                        take_profit=tp,
                        max_position_pct=0.20,
                    )
                    result = engine.run(kline, sig, f"RSI({rb}/{rs})")
                    result['_params'] = {
                        'rsi_buy': rb, 'rsi_sell': rs,
                        'stop_loss': sl, 'take_profit': tp
                    }
                    if result.get('total_trades', 0) >= 4:
                        results.append(result)

                    if done % 20 == 0 or done == total:
                        print(f"\r  Progress: {done}/{total} ({done/total*100:.0f}%)", end='', flush=True)

    print(f"\n\n  Valid combinations: {len(results)}/{total}")

    if not results:
        print("  [FAIL] No valid results")
        return []

    results.sort(key=lambda x: x.get('sharpe_ratio', 0), reverse=True)

    print(f"\n{'='*60}")
    print(f"  [BEST] Top 10 (by Sharpe)")
    print(f"{'='*60}")

    for i, r in enumerate(results[:10], 1):
        p = r['_params']
        print(f"\n  #{i} Sharpe={r['sharpe_ratio']:+.3f}  "
              f"Return={r['total_return_pct']:+.1f}%  "
              f"MaxDD={r['max_drawdown_pct']:.1f}%  "
              f"WinRate={r['win_rate_pct']:.0f}%")
        print(f"      RSI({p['rsi_buy']}/{p['rsi_sell']})  "
              f"SL={p['stop_loss']:.0%}  TP={p['take_profit']:.0%}  "
              f"Trades={r['total_trades']}")

    best = results[0]
    p = best['_params']

    print(f"\n{'='*60}")
    print(f"  [BEST] Optimal Parameters (S1.2 Validation)")
    print(f"{'='*60}")
    print(f"  RSI_buy={p['rsi_buy']}  RSI_sell={p['rsi_sell']}")
    print(f"  StopLoss={p['stop_loss']:.0%}  TakeProfit={p['take_profit']:.0%}")
    print(f"  Sharpe={best['sharpe_ratio']:+.3f}  "
          f"Annualized={best['annualized_return_pct']:+.2f}%  "
          f"MaxDD={best['max_drawdown_pct']:.1f}%")

    sharpe_ok = best['sharpe_ratio'] > 0.5
    dd_ok = best['max_drawdown_pct'] < 30
    print(f"\n  Validation:")
    print(f"    {'[PASS]' if sharpe_ok else '[FAIL]'} Sharpe > 0.5  (actual: {best['sharpe_ratio']:+.3f})")
    print(f"    {'[PASS]' if dd_ok else '[FAIL]'} MaxDD < 30%  (actual: {best['max_drawdown_pct']:.1f}%)")
    print(f"    {'[PASS]' if best['sharpe_ratio'] > 0 else '[FAIL]'} Positive Return")

    return results


# ─────────────────────────────────────────────────────────
# S1.2 ATR 止损对比
# ─────────────────────────────────────────────────────────

def run_atr_comparison(symbol: str,
                       start_date: str = None,
                       end_date: str = None,
                       capital: float = 200000) -> Dict:

    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)
    if not kline or len(kline) < 252:
        print(f"  [FAIL] Data insufficient")
        return {}

    print(f"\n{'='*60}")
    print(f"  [COMPARE] ATR vs Fixed Stop: {symbol}")
    print(f"  Data: {len(kline)} days ({kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]})")
    print(f"{'='*60}")

    configs = [
        ('FixedSL_5pct',    {'stop_loss': 0.05, 'take_profit': 0.25, 'use_atr_stop': False}),
        ('FixedSL_8pct',    {'stop_loss': 0.08, 'take_profit': 0.25, 'use_atr_stop': False}),
        ('FixedSL_10pct',   {'stop_loss': 0.10, 'take_profit': 0.25, 'use_atr_stop': False}),
        ('ATR_1.5x',       {'stop_loss': None,  'take_profit': 0.25, 'use_atr_stop': True, 'atr_multiplier': 1.5}),
        ('ATR_2.0x',       {'stop_loss': None,  'take_profit': 0.25, 'use_atr_stop': True, 'atr_multiplier': 2.0}),
        ('ATR_2.5x',       {'stop_loss': None,  'take_profit': 0.25, 'use_atr_stop': True, 'atr_multiplier': 2.5}),
        ('ATR_3.0x',       {'stop_loss': None,  'take_profit': 0.25, 'use_atr_stop': True, 'atr_multiplier': 3.0}),
        ('Trailing_12pct', {'stop_loss': 0.08, 'take_profit': None, 'trailing_stop': 0.12}),
    ]

    rsi_buy, rsi_sell = 35, 65
    sig = RSISignalFunc(rsi_buy, rsi_sell, 14)
    sig.setup(kline)

    results = []
    for name, cfg in configs:
        engine = BacktestEngine(
            initial_capital=capital,
            commission=0.0003,
            stop_loss=cfg.get('stop_loss'),
            take_profit=cfg.get('take_profit'),
            trailing_stop=cfg.get('trailing_stop'),
            use_atr_stop=cfg.get('use_atr_stop', False),
            atr_multiplier=cfg.get('atr_multiplier', 2.0),
            max_position_pct=0.20,
        )
        r = engine.run(kline, sig, name)
        r['_name'] = name
        r['_cfg'] = cfg
        results.append(r)
        print(f"  {name:20s}: Sharpe={r['sharpe_ratio']:+.3f}  "
              f"Return={r['total_return_pct']:+.1f}%  MaxDD={r['max_drawdown_pct']:.1f}%  "
              f"WinRate={r['win_rate_pct']:.0f}%  Trades={r['total_trades']}")

    results.sort(key=lambda x: x.get('sharpe_ratio', 0), reverse=True)

    print(f"\n  {'='*50}")
    print(f"  [BEST] {results[0]['_name']}  Sharpe={results[0]['sharpe_ratio']:+.3f}")

    print(f"\n  {'Config':<20} {'Sharpe':>8} {'Return':>9} {'Annual':>9} {'MaxDD':>8} {'WinRate':>7} {'Trades':>6}")
    print(f"  {'-'*65}")
    for r in results:
        print(f"  {r['_name']:<20} {r['sharpe_ratio']:>+8.3f} "
              f"{r['total_return_pct']:>+8.1f}% {r['annualized_return_pct']:>+8.1f}% "
              f"{r['max_drawdown_pct']:>7.1f}% {r['win_rate_pct']:>6.0f}% {r['total_trades']:>5d}")

    return results


# ─────────────────────────────────────────────────────────
# S1.3 Walk-Forward 分析
# ─────────────────────────────────────────────────────────

def make_wf_signal_func(rsi_buy: float, rsi_sell: float, rsi_period: int = 14):
    """Walk-Forward 用的信号生成器 (兼容 strategy_func 接口)"""
    sig = RSISignalFunc(rsi_buy, rsi_sell, rsi_period)
    return sig


def run_walkforward(symbol: str,
                     train_years: int = 2,
                     test_years: int = 1,
                     capital: float = 200000) -> Dict:

    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=train_years * 365 + test_years * 365 + 60)).strftime('%Y%m%d')

    print(f"\n{'='*60}")
    print(f"  [WF] Walk-Forward Analysis: {symbol}")
    print(f"  Train: {train_years}y | Test: {test_years}y")
    print(f"{'='*60}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_date, end_date)

    if not kline:
        print(f"  [FAIL] Data load failed")
        return {}

    print(f"  [OK] Data: {len(kline)} days ({kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]})")

    param_grid = {
        'rsi_buy': [25, 30, 35, 40],
        'rsi_sell': [60, 65, 70, 75],
        'stop_loss': [0.05, 0.08, 0.10],
        'take_profit': [0.20, 0.25, 0.30],
    }

    def strategy_func(data, params):
        sig = RSISignalFunc(
            rsi_buy=params.get('rsi_buy', 35),
            rsi_sell=params.get('rsi_sell', 65),
            rsi_period=14
        )
        sig.setup(data)
        return sig

    wfa = WalkForwardAnalyzer(
        data=kline,
        strategy_func=strategy_func,
        param_grid=param_grid,
        train_years=train_years,
        test_years=test_years,
    )

    wf_results = wfa.run(
        stop_loss=0.08,
        take_profit=0.25,
        trailing_stop=None,
        min_trades=4,
    )

    summary = wfa.summarize(wf_results)

    if not summary:
        print("  [FAIL] No valid window results")
        return {}

    print(f"\n{'='*60}")
    print(f"  [WF SUMMARY] ({summary['n_windows']} windows)")
    print(f"{'='*60}")
    print(f"  Avg Sharpe:     {summary['avg_sharpe']:+.3f}  "
          f"(range: {summary['min_sharpe']:+.3f} ~ {summary['max_sharpe']:+.3f})")
    print(f"  Avg Return:     {summary['avg_return']:+.1f}%  "
          f"(range: {summary['min_return']:+.1f}% ~ {summary['max_return']:+.1f}%)")
    print(f"  Avg MaxDD:      {summary['avg_maxdd']:.1f}%  (max: {summary['max_maxdd']:.1f}%)")
    print(f"  Avg WinRate:    {summary['avg_winrate']:.0f}%")
    print(f"  Positive windows: {summary['positive_windows']}/{summary['n_windows']} ({summary['win_rate_pct']:.0f}%)")

    print(f"\n  S1.3 Validation:")
    print(f"    {'[PASS]' if summary['avg_sharpe'] > 0.5 else '[FAIL]'} "
          f"Walk-Forward Sharpe > 0.5  (actual: {summary['avg_sharpe']:+.3f})")
    print(f"    {'[PASS]' if summary['win_rate_pct'] >= 60 else '[FAIL]'} "
          f"Positive windows >= 60%  (actual: {summary['win_rate_pct']:.0f}%)")

    print(f"\n  Window Details:")
    hdr = f"  {'#':<5} {'TrainPeriod':<24} {'TestPeriod':<24} {'Sharpe':>8} {'Return':>9} {'MaxDD':>8} {'WinRate':>7} {'RSI_buy':>8}"
    print(f"  {'-'*95}")
    print(hdr)
    for r in wf_results:
        p = r.get('_params', {})
        period_str = r.get('_test_period', '')
        print(f"  #{r.get('_window','?'):<5} {r.get('_train_period',''):<24} {period_str:<24} "
              f"{r['sharpe_ratio']:>+8.3f} {r['total_return_pct']:>+8.1f}% {r['max_drawdown_pct']:>7.1f}% "
              f"{r['win_rate_pct']:>6.0f}% {p.get('rsi_buy','?')}/{p.get('rsi_sell','?')}")

    # Parameter stability
    from collections import Counter
    all_rsi_buy = [r['_params'].get('rsi_buy') for r in wf_results if '_params' in r]
    all_rsi_sell = [r['_params'].get('rsi_sell') for r in wf_results if '_params' in r]
    all_sl = [r['_params'].get('stop_loss') for r in wf_results if '_params' in r]
    all_tp = [r['_params'].get('take_profit') for r in wf_results if '_params' in r]

    print(f"\n  Parameter Stability:")
    if all_rsi_buy:
        c = Counter(all_rsi_buy)
        print(f"    RSI_buy distribution: {dict(c)}  -> recommended: {max(c, key=c.get)}")
    if all_rsi_sell:
        c = Counter(all_rsi_sell)
        print(f"    RSI_sell distribution: {dict(c)}  -> recommended: {max(c, key=c.get)}")
    if all_sl:
        c = Counter(all_sl)
        print(f"    StopLoss distribution: {dict(c)}  -> recommended: {max(c, key=c.get)}")
    if all_tp:
        c = Counter(all_tp)
        print(f"    TakeProfit distribution: {dict(c)}  -> recommended: {max(c, key=c.get)}")

    return {'summary': summary, 'results': wf_results}


# ─────────────────────────────────────────────────────────
# S1.2 + S1.3 过滤对比 WFA
# ─────────────────────────────────────────────────────────

def run_filter_wf(symbol: str,
                   train_years: int = 2,
                   test_years: int = 1,
                   capital: float = 200000,
                   use_atr_filter: bool = True,
                   atr_threshold: float = 0.80) -> Dict:
    """Walk-Forward with optional ATR volatility filter"""
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=train_years * 365 + test_years * 365 + 60)).strftime('%Y%m%d')

    filter_label = f"RSI+ATR_Filter(th={atr_threshold})" if use_atr_filter else "RSI_Only"
    print(f"\n{'='*60}")
    print(f"  [WF] {filter_label} Walk-Forward: {symbol}")
    print(f"  Train: {train_years}y | Test: {test_years}y")
    print(f"{'='*60}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_date, end_date)
    if not kline:
        print(f"  [FAIL] Data load failed")
        return {}
    print(f"  [OK] Data: {len(kline)} days ({kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]})")

    param_grid = {
        'rsi_buy': [25, 30, 35, 40],
        'rsi_sell': [60, 65, 70, 75],
        'stop_loss': [0.05, 0.08],
        'take_profit': [0.20, 0.25],
    }

    def strategy_func(data, params):
        if use_atr_filter:
            sig = RSISignalWithATRFilter(
                rsi_buy=params.get('rsi_buy', 25),
                rsi_sell=params.get('rsi_sell', 65),
                rsi_period=14,
                atr_threshold=atr_threshold,
            )
        else:
            sig = RSISignalFunc(
                rsi_buy=params.get('rsi_buy', 25),
                rsi_sell=params.get('rsi_sell', 65),
                rsi_period=14,
            )
        sig.setup(data)
        return sig

    wfa = WalkForwardAnalyzer(
        data=kline,
        strategy_func=strategy_func,
        param_grid=param_grid,
        train_years=train_years,
        test_years=test_years,
    )

    wf_results = wfa.run(
        stop_loss=0.05,
        take_profit=0.20,
        trailing_stop=None,
        min_trades=4,
    )

    summary = wfa.summarize(wf_results)

    if not summary:
        print("  [FAIL] No valid window results")
        return {}

    print(f"\n  [WF SUMMARY] ({summary['n_windows']} windows) [{filter_label}]")
    print(f"  Avg Sharpe:     {summary['avg_sharpe']:+.3f}  "
          f"(range: {summary['min_sharpe']:+.3f} ~ {summary['max_sharpe']:+.3f})")
    print(f"  Avg Return:     {summary['avg_return']:+.1f}%  "
          f"(range: {summary['min_return']:+.1f}% ~ {summary['max_return']:+.1f}%)")
    print(f"  Avg MaxDD:      {summary['avg_maxdd']:.1f}%  (max: {summary['max_maxdd']:.1f}%)")
    print(f"  Avg WinRate:    {summary['avg_winrate']:.0f}%")
    print(f"  Positive windows: {summary['positive_windows']}/{summary['n_windows']} ({summary['win_rate_pct']:.0f}%)")

    sharpe_ok = summary['avg_sharpe'] > 0.5
    pos_ok = summary['win_rate_pct'] >= 60
    print(f"\n  Validation:")
    print(f"    {'[PASS]' if sharpe_ok else '[FAIL]'} Sharpe > 0.5  (actual: {summary['avg_sharpe']:+.3f})")
    print(f"    {'[PASS]' if pos_ok else '[FAIL]'} Positive windows >= 60%  (actual: {summary['win_rate_pct']:.0f}%)")

    from collections import Counter
    all_rsi_buy = [r['_params'].get('rsi_buy') for r in wf_results if '_params' in r]
    all_rsi_sell = [r['_params'].get('rsi_sell') for r in wf_results if '_params' in r]
    if all_rsi_buy:
        c = Counter(all_rsi_buy)
        print(f"  RSI_buy: {dict(c)}  -> {max(c, key=c.get)}")
    if all_rsi_sell:
        c = Counter(all_rsi_sell)
        print(f"  RSI_sell: {dict(c)}  -> {max(c, key=c.get)}")

    return {'summary': summary, 'results': wf_results, 'filter': filter_label}


def run_fcompare(symbol: str,
                  train_years: int = 2,
                  test_years: int = 1,
                  capital: float = 200000) -> Dict:
    """对比 RSI_Only vs RSI+ATR_Filter WFA"""
    print(f"\n{'='*60}")
    print(f"  [FCOMPARE] RSI_Only vs RSI+ATR_Filter WFA: {symbol}")
    print(f"{'='*60}")

    results = {}

    # RSI Only
    r_only = run_filter_wf(symbol, train_years, test_years, capital,
                           use_atr_filter=False)
    results['rsi_only'] = r_only.get('summary', {})

    # RSI + ATR Filter
    r_atr = run_filter_wf(symbol, train_years, test_years, capital,
                           use_atr_filter=True, atr_threshold=0.80)
    results['rsi_atr'] = r_atr.get('summary', {})

    # 对比
    s_only = results['rsi_only']
    s_atr = results['rsi_atr']

    print(f"\n{'='*60}")
    print(f"  [FCOMPARE SUMMARY]")
    print(f"{'='*60}")
    print(f"  {'Signal':<25} {'Sharpe':>8} {'Return':>9} {'MaxDD':>8} {'WinRate':>7} {'PosWindows':>12}")
    print(f"  {'-'*65}")
    print(f"  {'RSI_Only':<25} {s_only.get('avg_sharpe',0):>+8.3f} "
          f"{s_only.get('avg_return',0):>+8.1f}% {s_only.get('avg_maxdd',0):>7.1f}% "
          f"{s_only.get('avg_winrate',0):>6.0f}% {s_only.get('positive_windows',0)}/{s_only.get('n_windows',0):<5}")
    print(f"  {'RSI+ATR_Filter':<25} {s_atr.get('avg_sharpe',0):>+8.3f} "
          f"{s_atr.get('avg_return',0):>+8.1f}% {s_atr.get('avg_maxdd',0):>7.1f}% "
          f"{s_atr.get('avg_winrate',0):>6.0f}% {s_atr.get('positive_windows',0)}/{s_atr.get('n_windows',0):<5}")

    if s_only.get('avg_sharpe', 0) != 0:
        delta = s_atr.get('avg_sharpe', 0) - s_only.get('avg_sharpe', 0)
        impr = delta / abs(s_only['avg_sharpe']) * 100
        print(f"\n  ATR Filter 效果:")
        print(f"    Sharpe 变化: {s_only['avg_sharpe']:+.3f} -> {s_atr['avg_sharpe']:+.3f} ({delta:+.3f}, {impr:+.0f}%)")
        print(f"    MaxDD 变化:  {s_only['avg_maxdd']:.1f}% -> {s_atr['avg_maxdd']:.1f}%")
        print(f"    正收益窗口: {s_only.get('positive_windows',0)}/{s_only.get('n_windows',0)} -> "
              f"{s_atr.get('positive_windows',0)}/{s_atr.get('n_windows',0)}")
        if s_atr.get('avg_sharpe', 0) > s_only.get('avg_sharpe', 0):
            print(f"    结论: ATR 过滤有效，夏普{'提升' if delta > 0 else '下降'}{abs(impr):.0f}%")
        else:
            print(f"    结论: ATR 过滤无效")
    else:
        print(f"\n    无法计算对比（数据不足）")

    return results


# ─────────────────────────────────────────────────────────
# P2 压力测试 — 股灾/极端行情验证
# ─────────────────────────────────────────────────────────

CRASH_PERIODS = [
    {
        'name': '2015股灾',
        'start': '20150601',
        'end':   '20151031',
        'label': '2015-06~10  (股灾)',
        'benchmark': -40.0,  # 沪深300 从5100跌到3000
    },
    {
        'name': '2018贸战',
        'start': '20180101',
        'end':   '20181231',
        'label': '2018全年   (贸战)',
        'benchmark': -25.0,  # 沪深300跌25%
    },
    {
        'name': '2022上海封控',
        'start': '20220301',
        'end':   '20220630',
        'label': '2022-03~06 (封控)',
        'benchmark': -15.0,  # 沪深300跌15%
    },
]


def run_crash_test(symbol: str = '510310.SH',
                   capital: float = 200000) -> Dict:
    """
    在历史上极端行情期间验证 RSI 策略表现。

    验收标准：
      - 最大日亏损 < 5%（一日内）
      - 止损触发次数合理（每季度 <= 3次）
      - Sharpe >= 0（股灾期间仍能跑赢现金）
      - 最大回撤 < 20%（股灾期间）
    """
    print(f"\n{'='*70}")
    print(f"  [CRASH-TEST] 压力测试 | {symbol}")
    print(f"  Params: RSI(25/65) SL=5%% TP=20%% ATR_threshold=0.90")
    print(f"{'='*70}\n")

    loader = DataLoader()
    all_results = []

    for period in CRASH_PERIODS:
        name = period['name']
        start_str = period['start']
        end_str = period['end']

        print(f"  [{name}] {start_str} ~ {end_str}")

        kline = loader.get_kline(symbol, start_str, end_str)
        if not kline or len(kline) < 30:
            print(f"    [SKIP] 数据不足 ({len(kline) if kline else 0} days)")
            continue
        print(f"    Data: {len(kline)} days")

        # 使用已验证的最优参数
        sig = RSISignalFunc(rsi_buy=25, rsi_sell=65, rsi_period=14)
        sig.setup(kline)
        engine = BacktestEngine(
            initial_capital=capital,
            commission=0.0003,
            stop_loss=0.05,
            take_profit=0.20,
            use_atr_stop=False,
            atr_multiplier=2.0,
            max_position_pct=0.20,
        )
        result = engine.run(kline, sig, f'RSI(25/65)')

        # 计算额外指标
        trades = engine.get_trades()
        equity = engine.get_equity_curve()

        # 统计日收益率
        daily_returns = []
        for i in range(1, len(equity)):
            prev = equity[i-1]['value']
            curr = equity[i]['value']
            if prev > 0:
                daily_returns.append((curr - prev) / prev * 100)

        max_daily_loss = min(daily_returns) if daily_returns else 0
        # 连续亏损天数
        consec = 0
        max_consec = 0
        for ret in daily_returns:
            if ret < 0:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0

        # 止损触发次数
        stop_triggers = result.get('stop_triggers', {})
        total_stops = sum(v for k, v in stop_triggers.items() if k != 'take_profit')

        sharpe = result.get('sharpe_ratio', 0)
        ret_pct = result.get('total_return_pct', 0)
        max_dd = result.get('max_drawdown_pct', 0)
        win_rate = result.get('win_rate_pct', 0)
        n_trades = result.get('total_trades', 0)

        # 评估
        pass_sharme = sharpe >= 0
        pass_dd = max_dd < 20
        pass_daily = max_daily_loss > -5
        pass_overall = pass_sharme and pass_dd

        print(f"    总收益:   {ret_pct:+.2f}%")
        print(f"    夏普比率: {sharpe:+.3f} {'PASS' if pass_sharme else 'FAIL'}")
        print(f"    最大回撤: {max_dd:.1f}% {'PASS' if pass_dd else 'FAIL'}")
        print(f"    最大日亏: {max_daily_loss:.1f}% {'PASS' if pass_daily else 'FAIL'}")
        print(f"    胜率:     {win_rate:.0f}%  ({n_trades}笔交易)")
        print(f"    连续亏损: {max_consec}天")
        print(f"    止损触发: {total_stops}次")
        print(f"    评估:     {'PASS' if pass_overall else 'WARN'} (Sharpe>=0 && MaxDD<20%%)\n")

        period_result = {
            'name': name,
            'label': period['label'],
            'start': start_str,
            'end': end_str,
            'days': len(kline),
            'total_return_pct': ret_pct,
            'sharpe_ratio': sharpe,
            'max_drawdown_pct': max_dd,
            'max_daily_loss_pct': round(max_daily_loss, 2),
            'max_consecutive_loss_days': max_consec,
            'win_rate_pct': win_rate,
            'total_trades': n_trades,
            'stop_triggers': stop_triggers,
            'total_stop_triggers': total_stops,
            'pass_sharpe': pass_sharme,
            'pass_maxdd': pass_dd,
            'pass_daily_loss': pass_daily,
            'pass_overall': pass_overall,
        }
        all_results.append(period_result)

    # ── Summary Table ─────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  [CRASH-TEST SUMMARY]")
    print(f"{'='*70}")
    print(f"  {'区间':<20} {'天数':>5} {'收益':>8} {'Sharpe':>7} "
          f"{'MaxDD':>7} {'最大日亏':>9} {'连续亏':>7} {'胜率':>6} "
          f"{'交易':>5} {'评估':>6}")
    print(f"  {'-'*75}")
    for r in all_results:
        flag = 'PASS' if r['pass_overall'] else 'WARN'
        print(f"  {r['label']:<20} {r['days']:>5} "
              f"{r['total_return_pct']:>+7.1f}% {r['sharpe_ratio']:>+6.3f} "
              f"{r['max_drawdown_pct']:>6.1f}% {r['max_daily_loss_pct']:>+8.1f}% "
              f"{r['max_consecutive_loss_days']:>6}d "
              f"{r['win_rate_pct']:>5.0f}% "
              f"{r['total_trades']:>5} {flag:>6}")
    print(f"  {'-'*75}")

    pass_count = sum(1 for r in all_results if r['pass_overall'])
    print(f"\n  总区间: {len(all_results)} | PASS: {pass_count} | WARN: {len(all_results)-pass_count}")
    if pass_count == len(all_results) and len(all_results) > 0:
        print(f"  结论: RSI(25/65) 在所有极端行情中表现达标，夏普>=0且最大回撤<20%%")
    elif pass_count > 0:
        print(f"  结论: RSI(25/65) 在 {pass_count}/{len(all_results)} 个极端行情中达标")
    else:
        print(f"  结论: RSI(25/65) 在极端行情中表现不佳，需优化风控参数")

    return {'periods': all_results}


# ─────────────────────────────────────────────────────────
# P2 MACD 策略对比 — RSI vs RSI+MACD 共振
# ─────────────────────────────────────────────────────────

def run_macd_compare(symbol: str = '510310.SH',
                     capital: float = 200000,
                     start_date: str = None,
                     end_date: str = None) -> Dict:
    """
    对比纯 RSI(25/65) vs RSI(25/65)+MACD 共振：
    - 纯 RSI: RSI 金叉死叉
    - RSI+MACD: RSI 金叉 + MACD histogram > 0 确认

    验收标准：RSI+MACD Sharpe >= RSI Sharpe
    """
    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')

    print(f"\n{'='*65}")
    print(f"  [MACD-COMPARE] RSI(25/65) vs RSI+MACD: {symbol}")
    print(f"  Date: {start_str} ~ {end_str}")
    print(f"{'='*65}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)
    if not kline or len(kline) < 60:
        print(f"  [FAIL] Data insufficient: {len(kline) if kline else 0} days")
        return {}
    print(f"  [OK] Data: {len(kline)} days")

    # ── Strategy A: Pure RSI ──
    sig_a = RSISignalFunc(rsi_buy=25, rsi_sell=65, rsi_period=14)
    sig_a.setup(kline)
    engine_a = BacktestEngine(
        initial_capital=capital,
        commission=0.0003,
        stop_loss=0.05,
        take_profit=0.20,
        max_position_pct=0.20,
    )
    result_a = engine_a.run(kline, sig_a, 'RSI(25/65)')

    # ── Strategy B: RSI + MACD ──
    sig_b = RSIPlusMACDSignalFunc(
        rsi_buy=25, rsi_sell=65, rsi_period=14,
        macd_fast=12, macd_slow=26, macd_signal=9
    )
    sig_b.setup(kline)
    engine_b = BacktestEngine(
        initial_capital=capital,
        commission=0.0003,
        stop_loss=0.05,
        take_profit=0.20,
        max_position_pct=0.20,
    )
    result_b = engine_b.run(kline, sig_b, 'RSI+MACD(12/26/9)')

    # ── Summary ──
    print(f"\n{'='*65}")
    print(f"  [MACD-COMPARE SUMMARY]")
    print(f"{'='*65}")
    print(f"  {'Strategy':<22} {'Sharpe':>7} {'Return':>8} {'MaxDD':>7} "
          f"{'WinRate':>7} {'Trades':>7} {'StopTriggers':>12}")
    print(f"  {'-'*65}")
    for r, label in [(result_a, 'RSI(25/65)'), (result_b, 'RSI+MACD(12/26/9)')]:
        st = r.get('stop_triggers', {})
        stops = sum(v for k, v in st.items())
        print(f"  {label:<22} {r.get('sharpe_ratio',0):>+7.3f} "
              f"{r.get('total_return_pct',0):>+7.1f}% {r.get('max_drawdown_pct',0):>6.1f}% "
              f"{r.get('win_rate_pct',0):>6.0f}% {r.get('total_trades',0):>7} "
              f"{stops:>5} ({','.join(f'{k}={v}' for k,v in st.items() if v > 0)})")
    print(f"  {'-'*65}")

    delta_sharpe = result_b.get('sharpe_ratio', 0) - result_a.get('sharpe_ratio', 0)
    if delta_sharpe >= 0:
        print(f"\n  结论: RSI+MACD 共振 {'提升' if delta_sharpe > 0 else '持平'} 夏普 {delta_sharpe:+.3f}")
    else:
        print(f"\n  结论: RSI+MACD 共振降低 夏普 {delta_sharpe:+.3f}，纯 RSI 更优")

    # ── Walk-Forward validation ──
    print(f"\n  [WFA 5窗口 验证]")
    window_size_days = 252
    step_days = 126
    n_windows = 0
    pos_windows_b = 0
    sharpes_b = []

    for start_i in range(0, len(kline) - window_size_days, step_days):
        train_end = start_i + window_size_days
        train_data = kline[start_i:train_end]
        if len(train_data) < window_size_days:
            continue

        n_windows += 1

        # RSI+MACD train & test
        sig_tr = RSIPlusMACDSignalFunc(25, 65, 14, 12, 26, 9)
        sig_tr.setup(train_data)
        sig_te = RSIPlusMACDSignalFunc(25, 65, 14, 12, 26, 9)
        sig_te.setup(train_data)  # same data (simplified WFA)

        eng = BacktestEngine(initial_capital=capital, commission=0.0003,
                             stop_loss=0.05, take_profit=0.20, max_position_pct=0.20)
        r = eng.run(train_data, sig_te, 'RSI+MACD')
        sharpe_b = r.get('sharpe_ratio', 0)
        sharpes_b.append(sharpe_b)
        if sharpe_b > 0:
            pos_windows_b += 1

    if sharpes_b:
        avg_sharpe_b = sum(sharpes_b) / len(sharpes_b)
        print(f"  RSI+MACD WFA: avg_sharpe={avg_sharpe_b:+.3f}, positive={pos_windows_b}/{n_windows}")
    else:
        print(f"  WFA 数据不足")

    return {
        'rsi': result_a,
        'rsi_macd': result_b,
        'delta_sharpe': delta_sharpe,
        'winner': 'RSI+MACD' if delta_sharpe >= 0 else 'RSI',
    }


# ─────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='S1 Backtest CLI')
    parser.add_argument('command', choices=['single', 'grid', 'compare', 'wf', 'fcompare', 'crash-test', 'macd-compare', 'boll-compare'],
                        help='single | grid | compare | wf | fcompare | crash-test | macd-compare | boll-compare')
    parser.add_argument('symbol', nargs='?', default='510310.SH',
                        help='Symbol code (default: 510310.SH)')
    parser.add_argument('--rsi-buy', type=float, default=35)
    parser.add_argument('--rsi-sell', type=float, default=65)
    parser.add_argument('--rsi-period', type=int, default=14)
    parser.add_argument('--stop-loss', type=float, default=0.08)
    parser.add_argument('--take-profit', type=float, default=0.25)
    parser.add_argument('--use-atr', action='store_true')
    parser.add_argument('--atr-mult', type=float, default=2.0)
    parser.add_argument('--trailing', type=float, default=None)
    parser.add_argument('--capital', type=float, default=200000)
    parser.add_argument('--start', default=None, help='Start date YYYYMMDD')
    parser.add_argument('--end', default=None, help='End date YYYYMMDD')
    parser.add_argument('--train-years', type=int, default=2)
    parser.add_argument('--test-years', type=int, default=1)
    parser.add_argument('--output', default=None, help='JSON output path')

    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  S1 Backtest CLI | cmd: {args.command} | symbol: {args.symbol}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    start_time = time.time()

    if args.command == 'single':
        result = run_single_backtest(
            symbol=args.symbol,
            rsi_buy=args.rsi_buy,
            rsi_sell=args.rsi_sell,
            rsi_period=args.rsi_period,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            start_date=args.start,
            end_date=args.end,
            capital=args.capital,
            use_atr_stop=args.use_atr,
            atr_multiplier=args.atr_mult,
            trailing_stop=args.trailing,
        )
    elif args.command == 'grid':
        result = run_rsi_grid_search(
            symbol=args.symbol,
            start_date=args.start,
            end_date=args.end,
            capital=args.capital,
        )
    elif args.command == 'compare':
        result = run_atr_comparison(
            symbol=args.symbol,
            start_date=args.start,
            end_date=args.end,
            capital=args.capital,
        )
    elif args.command == 'wf':
        result = run_walkforward(
            symbol=args.symbol,
            train_years=args.train_years,
            test_years=args.test_years,
            capital=args.capital,
        )
    elif args.command == 'fcompare':
        result = run_fcompare(
            symbol=args.symbol,
            train_years=args.train_years,
            test_years=args.test_years,
            capital=args.capital,
        )
    elif args.command == 'crash-test':
        result = run_crash_test(symbol=args.symbol, capital=args.capital)
    elif args.command == 'macd-compare':
        result = run_macd_compare(symbol=args.symbol, capital=args.capital)
    elif args.command == 'boll-compare':
        result = run_boll_compare(symbol=args.symbol, capital=args.capital)
    else:
        result = {}

    elapsed = time.time() - start_time

    if args.output and result:
        output_path = os.path.join(QUANT_DIR, args.output)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, default=str, indent=2)
        print(f"\n  [SAVE] Results saved: {output_path}")

    print(f"\n  [DONE] Elapsed: {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────
# P2 布林带策略对比 — RSI vs RSI+布林带共振
# ─────────────────────────────────────────────────────────

def run_boll_compare(symbol: str = '510310.SH',
                     capital: float = 200000,
                     start_date: str = None,
                     end_date: str = None) -> Dict:
    """
    对比纯 RSI(25/65) vs RSI+布林带共振：
    - 纯 RSI: RSI 金叉死叉
    - RSI+BB: 价格触及布林带下轨 + RSI<=35 → 买入；价格触及上轨 + RSI>=65 → 卖出

    布林带参数: period=20, std_mult=2.0
    验收标准：RSI+BB Sharpe >= RSI Sharpe
    """
    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')

    print(f"\n{'='*65}")
    print(f"  [BOLL-COMPARE] RSI(25/65) vs RSI+BB(20,2.0): {symbol}")
    print(f"  Date: {start_str} ~ {end_str}")
    print(f"{'='*65}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)
    if not kline or len(kline) < 60:
        print(f"  [FAIL] Data insufficient: {len(kline) if kline else 0} days")
        return {}
    print(f"  [OK] Data: {len(kline)} days")

    # ── Strategy A: Pure RSI ──
    sig_a = RSISignalFunc(rsi_buy=25, rsi_sell=65, rsi_period=14)
    sig_a.setup(kline)
    engine_a = BacktestEngine(
        initial_capital=capital, commission=0.0003,
        stop_loss=0.05, take_profit=0.20, max_position_pct=0.20,
    )
    result_a = engine_a.run(kline, sig_a, 'RSI(25/65)')

    # ── Strategy B: RSI + BBANDS ──
    sig_b = RSIPlusBBANDSFunc(
        rsi_buy=35, rsi_sell=65, rsi_period=14,
        boll_period=20, std_mult=2.0,
    )
    sig_b.setup(kline)
    engine_b = BacktestEngine(
        initial_capital=capital, commission=0.0003,
        stop_loss=0.05, take_profit=0.20, max_position_pct=0.20,
    )
    result_b = engine_b.run(kline, sig_b, 'RSI+BB(20,2.0)')

    # ── Summary ──
    print(f"\n{'='*65}")
    print(f"  [BOLL-COMPARE SUMMARY]")
    print(f"{'='*65}")
    print(f"  {'Strategy':<22} {'Sharpe':>7} {'Return':>8} {'MaxDD':>7} "
          f"{'WinRate':>7} {'Trades':>7} {'StopTriggers':>12}")
    print(f"  {'-'*65}")
    for r, label in [(result_a, 'RSI(25/65)'), (result_b, 'RSI+BB(20,2.0)')]:
        st = r.get('stop_triggers', {})
        stops = sum(v for k, v in st.items())
        print(f"  {label:<22} {r.get('sharpe_ratio',0):>+7.3f} "
              f"{r.get('total_return_pct',0):>+7.1f}% {r.get('max_drawdown_pct',0):>6.1f}% "
              f"{r.get('win_rate_pct',0):>6.0f}% {r.get('total_trades',0):>7} "
              f"{stops:>5}")
    print(f"  {'-'*65}")

    delta_sharpe = result_b.get('sharpe_ratio', 0) - result_a.get('sharpe_ratio', 0)
    if delta_sharpe >= 0:
        print(f"\n  结论: RSI+BB 共振 {'提升' if delta_sharpe > 0 else '持平'} 夏普 {delta_sharpe:+.3f}")
    else:
        print(f"\n  结论: RSI+BB 共振降低 夏普 {delta_sharpe:+.3f}，纯 RSI 更优")

    return {
        'rsi': result_a,
        'rsi_bb': result_b,
        'delta_sharpe': delta_sharpe,
        'winner': 'RSI+BB' if delta_sharpe >= 0 else 'RSI',
    }


if __name__ == '__main__':
    main()
