"""
regime_detector.py — 市场环境识别引擎
======================================
四种环境：
  BULL     — 上证站上20日均线 AND 20日线>60日线（均线多头排列）
  BEAR     — 上证跌破20日均线 AND 20日线<60日线（均线空头排列）
  VOLATILE — ATR ratio > 0.85（高波动，均值回归失效）
  CALM     — ATR ratio ≤ 0.85 AND 趋势不明朗

每日 9:30 开盘前运行一次，结果缓存全天。
"""

import os
import json
import logging
import numpy as np
import pandas as pd
from datetime import date, timedelta as td
from typing import Optional

logger = logging.getLogger('regime')

# 清除代理
for _k in list(os.environ.keys()):
    if 'proxy' in _k.lower():
        del os.environ[_k]

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(THIS_DIR, 'regime_today.json')

# ─── 参数 ──────────────────────────────────────────────────────────────────

INDEX_SYMBOL = 'sh000001'  # 上证指数（AkShare格式）
MA_SHORT = 20
MA_LONG = 60
ATR_PERIOD = 14
ATR_LOOKBACK = 30
ATR_THRESHOLD = 0.85  # > 0.85 = VOLATILE


# ─── 数据获取（AkShare 专口）────────────────────────────────────────────────

def _get_index_data_akshare(symbol: str = INDEX_SYMBOL,
                             lookback: int = 80) -> Optional[dict]:
    """
    通过 AkShare 获取指数数据，计算 MA20 / MA60 / ATR ratio。
    Returns: {
        'dates': [...], 'closes': [...], 'ma20': [...], 'ma60': [...],
        'atr_ratio': float, 'atr': float
    }
    """
    try:
        import akshare as ak
        end = date.today().isoformat()
        start = (date.today() - td(days=lookback + 90)).isoformat()

        df = ak.stock_zh_index_daily(symbol=symbol)
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df = df[(df['date'] >= start) & (df['date'] <= end)].tail(lookback).reset_index(drop=True)

        if len(df) < MA_LONG + 5:
            logger.warning('Insufficient index data: %d bars', len(df))
            return None

        closes = df['close'].values.astype(float)
        highs  = df['high'].values.astype(float)
        lows   = df['low'].values.astype(float)
        dates  = df['date'].tolist()

        ma20 = pd.Series(closes).rolling(MA_SHORT).mean().values
        ma60 = pd.Series(closes).rolling(MA_LONG).mean().values

        # ATR ratio
        trs = np.maximum(
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
        atr_arr = pd.Series(trs).rolling(ATR_PERIOD).mean().values
        current_atr = float(atr_arr[-1])
        max_atr_recent = float(np.max(atr_arr[-ATR_LOOKBACK:]))
        atr_ratio = current_atr / max_atr_recent if max_atr_recent > 0 else 0.0

        return {
            'dates': dates,
            'closes': closes,
            'ma20': ma20,
            'ma60': ma60,
            'atr_ratio': atr_ratio,
            'atr': current_atr,
        }
    except Exception as e:
        logger.warning('_get_index_data_akshare failed: %s', e)
        return None


# ─── 核心检测逻辑 ──────────────────────────────────────────────────────────

def detect_regime() -> dict:
    """
    检测当前市场环境（使用 AkShare 上证指数数据）。

    Returns:
        {
            'regime': 'BULL' | 'BEAR' | 'VOLATILE' | 'CALM',
            'ma20': float,       # 当前 MA20
            'ma60': float,       # 当前 MA60
            'close': float,      # 最新收盘价
            'atr_ratio': float,  # ATR ratio
            'atr': float,        # 当前 ATR
            'reason': str,       # 判定原因
            'date': str,         # 判定日期
        }
    """
    today_str = date.today().isoformat()
    data = _get_index_data_akshare(lookback=80)

    if data is None:
        logger.warning('detect_regime: no data, defaulting to CALM')
        return {
            'regime': 'CALM',
            'ma20': 0.0, 'ma60': 0.0, 'close': 0.0,
            'atr_ratio': 0.0, 'atr': 0.0,
            'reason': '数据获取失败，默认CALM',
            'date': today_str,
        }

    closes = data['closes']
    ma20_arr = data['ma20']
    ma60_arr = data['ma60']
    atr_ratio = data['atr_ratio']
    atr = data['atr']

    # 取最新有效值
    valid_len = min(len(closes), len(ma20_arr), len(ma60_arr))
    closes  = closes[-valid_len:]
    ma20_arr = ma20_arr[-valid_len:]
    ma60_arr = ma60_arr[-valid_len:]

    close = float(closes[-1])
    ma20  = float(ma20_arr[-1])
    ma60  = float(ma60_arr[-1])

    # 趋势判断
    above_ma20      = close > ma20
    ma20_above_ma60 = ma20 > ma60
    below_ma20      = close < ma20
    ma20_below_ma60 = ma20 < ma60

    if above_ma20 and ma20_above_ma60:
        regime = 'BULL'
        reason = f'上证{close:.0f}>MA20({ma20:.0f})，均线多头排列'
    elif below_ma20 and ma20_below_ma60:
        regime = 'BEAR'
        reason = f'上证{close:.0f}<MA20({ma20:.0f})，均线空头排列'
    elif atr_ratio > ATR_THRESHOLD:
        regime = 'VOLATILE'
        reason = f'ATR ratio={atr_ratio:.2f}>0.85，高波动环境'
    else:
        regime = 'CALM'
        reason = f'ATR ratio={atr_ratio:.2f}<=0.85，趋势不明朗'

    result = {
        'regime': regime,
        'ma20': round(ma20, 2),
        'ma60': round(ma60, 2),
        'close': round(close, 2),
        'atr_ratio': round(atr_ratio, 3),
        'atr': round(atr, 3),
        'reason': reason,
        'date': today_str,
    }
    logger.info('Regime: %s — %s', regime, reason)
    return result


# ─── 缓存管理 ──────────────────────────────────────────────────────────────

def get_cached_regime(force_refresh: bool = False) -> dict:
    """
    获取今日市场环境（优先读缓存，未缓存或 force_refresh=True 时重新检测）。
    """
    today_str = date.today().isoformat()
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if cached.get('date') == today_str:
                logger.debug('Cached regime: %s', cached['regime'])
                return cached
        except Exception:
            pass

    result = detect_regime()
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info('Regime cached to %s', CACHE_FILE)
    except Exception as e:
        logger.warning('Failed to cache regime: %s', e)
    return result


def invalidate_cache():
    """清除缓存，强制重新检测（盘中复查时调用）。"""
    if os.path.exists(CACHE_FILE):
        os.unlink(CACHE_FILE)


# ─── 策略参数表（按环境）────────────────────────────────────────────────────

REGIME_PARAMS = {
    'BULL': {
        'description': '多头趋势，顺势持有',
        'rsi_buy': 25,
        'rsi_sell': 65,
        'atr_threshold': 0.90,
        'stop_loss': 0.05,
        'take_profit': 0.20,
        'atr_multiplier': 3.0,
        'allowed_strategies': ['RSI', 'RSI+MACD'],
    },
    'BEAR': {
        'description': '空头趋势，防守为主',
        'rsi_buy': 40,   # 更严格，避免抄底
        'rsi_sell': 70,
        'atr_threshold': 0.80,
        'stop_loss': 0.05,
        'take_profit': 0.15,
        'atr_multiplier': 3.0,
        'allowed_strategies': ['RSI'],
    },
    'VOLATILE': {
        'description': '高波动，均值回归失效，减少交易',
        'rsi_buy': 30,
        'rsi_sell': 60,
        'atr_threshold': 0.80,
        'stop_loss': 0.05,
        'take_profit': 0.25,
        'atr_multiplier': 3.0,
        'allowed_strategies': ['RSI'],
    },
    'CALM': {
        'description': '趋势不明，标准参数',
        'rsi_buy': 25,
        'rsi_sell': 65,
        'atr_threshold': 0.85,
        'stop_loss': 0.05,
        'take_profit': 0.20,
        'atr_multiplier': 3.0,
        'allowed_strategies': ['RSI', 'RSI+MACD'],
    },
}


def get_params_for_regime(regime: str) -> dict:
    """获取指定环境的策略参数。"""
    return REGIME_PARAMS.get(regime, REGIME_PARAMS['CALM'])


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    result = get_cached_regime(force_refresh=True)
    print()
    print('=' * 55)
    print(f'  市场环境检测 — {result["date"]}')
    print('=' * 55)
    print(f'  上证收盘:  {result["close"]}')
    print(f'  MA(20):   {result["ma20"]}')
    print(f'  MA(60):   {result["ma60"]}')
    print(f'  ATR ratio: {result["atr_ratio"]}  (阈值 0.85)')
    print(f'  当前ATR:  {result.get("atr", 0):.3f}')
    print(f'  环境:     [{result["regime"]}]')
    print(f'  原因:     {result["reason"]}')
    print()
    p = get_params_for_regime(result['regime'])
    print(f'  策略参数 ({result["regime"]}):')
    print(f'    RSI(买/卖): {p["rsi_buy"]}/{p["rsi_sell"]}')
    print(f'    ATR阈值:   {p["atr_threshold"]}')
    print(f'    止损/止盈: {p["stop_loss"]:.0%} / {p["take_profit"]:.0%}')
    print(f'    可用策略:  {p["allowed_strategies"]}')
    print()
