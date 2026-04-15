"""
signals.py — A股盘中信号引擎 v2
================================
纯轻量实时信号检测，不依赖 AkShare/分钟数据。

A 股专用信号（替代旧版纯 RSI 逻辑）：
  - LIMIT_UP    : 涨停（放量=真拉升，缩量=诱多）
  - LIMIT_DOWN  : 跌停（已封死，无法卖出）
  - LIMIT_RISK  : 接近跌停（>8%，可能封板）
  - WATCH_LIMIT_UP : 接近涨停（>8%，可能封板）
  - RSI_BUY / WATCH_BUY : 超卖 + 反弹确认
  - RSI_SELL / WATCH_SELL : 超买 + 上涨乏力
  - VOLUME_CONFIRM : 缩量预警（上涨无量=诱多风险）

数据源：腾讯实时报价 (qt.gtimg.cn)
"""

import os
import sys
import json
import ssl
import logging
import urllib.request
import urllib.error
from datetime import date, datetime, time as dtime
from typing import Optional, NamedTuple

logger = logging.getLogger('signals')

# Lazy import to avoid circular dependency
_fundamentals_check = None
def _get_fundamentals_check():
    global _fundamentals_check
    if _fundamentals_check is None:
        try:
            from services.fundamentals import check_fundamentals_filter as f
            _fundamentals_check = f
        except Exception:
            pass
    return _fundamentals_check

# Lazy import northbound
_northbound_check = None
def _get_northbound_check():
    global _northbound_check
    if _northbound_check is None:
        try:
            from services.northbound import fetch_kamt
            _northbound_check = fetch_kamt
        except Exception:
            pass
    return _northbound_check

# 北向资金阈值（亿元）
NORTH_BUY_BOOST_THRESHOLD = 50.0  # 北向净流入 > 50亿 → RSI_BUY 信号强化
def _get_fundamentals_check():
    global _fundamentals_check
    if _fundamentals_check is None:
        try:
            from services.fundamentals import check_fundamentals_filter as f
            _fundamentals_check = f
        except Exception:
            pass
    return _fundamentals_check

# A股盘中时间段（UTC+8）
MARKET_MORNING_START    = (9, 35)   # 9:35 开盘后可检查
MARKET_MORNING_END      = (11, 30)
MARKET_AFTERNOON_START  = (13, 0)
MARKET_AFTERNOON_END    = (14, 55)
TRADING_DAYS = range(5)  # Mon-Fri

# ─── 涨跌停板参数 ────────────────────────────────────────────
# A股规则：
#   普通 A 股：±10%
#   ST / *ST 股：±5%
#   创业板（300开头）、科创板（688开头）：±20%
#   新股上市前5日：无限制（但这里用特殊判断）

ST_PREFIXES    = ('st', '*st', 'st*')
LIMIT_UP_PCT   = {
    'normal':    0.10,
    'st':        0.05,
    'chinext':   0.20,   # 300xxx
    'startup':   0.20,   # 688xxx
}


def get_limit_pct(symbol: str) -> float:
    """根据股票代码判断涨跌停限制幅度"""
    s = symbol.lower().replace('.sz', '').replace('.sh', '').replace('sz', '').replace('sh', '')
    if any(s.startswith(p) for p in ST_PREFIXES):
        return LIMIT_UP_PCT['st']
    if s.startswith('300') or s.startswith('159'):   # 创业板ETF/股票
        return LIMIT_UP_PCT['chinext']
    if s.startswith('688'):                           # 科创板
        return LIMIT_UP_PCT['startup']
    return LIMIT_UP_PCT['normal']


class SignalAlert(NamedTuple):
    symbol:      str
    signal:      str
    price:       float
    pct:         float        # 当日涨跌幅%
    prev_rsi:    Optional[float]
    volume_ratio: Optional[float]  # 放量倍数（vs 5日均量）
    day_chg:     float
    reason:      str
    emitted_at:  str


# ─── 实时行情 ────────────────────────────────────────────────

def fetch_realtime(symbol: str) -> Optional[dict]:
    """
    获取腾讯实时行情。

    腾讯原始格式（含 v_shXXXXXX=" 前缀）：
      v_sh600519="1~贵州茅台~600519~1453.96~1460.49~1459.14~28866~...

    清理后 split("~") 的字段布局（去掉 v_XXXXXX=" 后）：
      [0]=1, [1]=名称, [2]=代码, [3]=当前价, [4]=昨收, [5]=今开,
      [6]=成交量, [7]=内盘, [8]=外盘, [9]=最低, ...
      [29]=涨跌额, [30]=涨跌幅%, [31]=最高, [32]=最低,
      [34]=成交量, [35]=成交额, [36]=量比, [38]=量比?, [44]=市盈率
    """
    # 腾讯格式：sh600519 / sz000001
    upper_sym = symbol.upper()
    if upper_sym.endswith('.SH'):
        sym = 'sh' + upper_sym[:-3]
    elif upper_sym.endswith('.SZ'):
        sym = 'sz' + upper_sym[:-3]
    else:
        sym = symbol.lower()  # 兜底：假设已经是 sh/sz 格式
    url = f'https://qt.gtimg.cn/q={sym}'
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            # 去掉 v_shXXXXXX=" 前缀（如果有）后再 split
            eq_idx = raw.find('="')
            if eq_idx >= 0:
                raw = raw[eq_idx + 2:]
            fields = raw.split('~')
            if len(fields) < 40:
                return None
            try:
                price    = float(fields[3])  if fields[3]  not in ('', '-') else 0.0
                prev_cls = float(fields[4])  if fields[4]  not in ('', '-') else 0.0
                day_chg  = float(fields[31]) if fields[31] not in ('', '-') else 0.0
                pct      = float(fields[32]) if fields[32] not in ('', '-') else 0.0
                high     = float(fields[33]) if fields[33] not in ('', '-') else price
                low      = float(fields[34]) if fields[34] not in ('', '-') else price
                volume   = fields[36] if len(fields) > 36 else ''
                amount   = fields[37] if len(fields) > 37 else ''
                vol_ratio= float(fields[38]) if len(fields) > 38 and fields[38] not in ('', '-', '0') else None
                pe       = fields[39] if len(fields) > 39 else ''
                return {
                    'symbol':     symbol,
                    'price':      price,
                    'prev_close': prev_cls,
                    'high':       high,
                    'low':        low,
                    'chg':        day_chg,
                    'pct':        pct,
                    'volume':     volume,
                    'amount':     amount,
                    'vol_ratio':  vol_ratio,
                    'pe':         pe,
                }
            except (ValueError, IndexError) as e:
                logger.debug('parse error %s: %s', symbol, e)
                return None
    except Exception as e:
        logger.debug('fetch_realtime %s failed: %s', symbol, e)
        return None


def _to_tencent_sym(symbol: str) -> str:
    """将 '600519.SH' / '000001.SZ' 转为 'sh600519' / 'sz000001'"""
    upper = symbol.upper()
    if upper.endswith('.SH'):
        return 'sh' + upper[:-3]
    elif upper.endswith('.SZ'):
        return 'sz' + upper[:-3]
    return symbol.lower()


def fetch_bulk(symbols: list[str]) -> dict[str, dict]:
    """批量获取实时行情（腾讯单次请求支持多符号）"""
    if not symbols:
        return {}
    tencent_syms = [_to_tencent_sym(s) for s in symbols]
    sym_str = ','.join(tencent_syms)
    url = f'https://qt.gtimg.cn/q={sym_str}'
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            result = {}
            lines = raw.strip().split('\n')
            for i, line in enumerate(lines):
                if i >= len(symbols):
                    break
                sym = symbols[i]
                # 去掉 v_XXXXXX=" 前缀
                eq_idx = line.find('="')
                if eq_idx >= 0:
                    line = line[eq_idx + 2:]
                fields = line.split('~')
                if len(fields) < 40:
                    continue
                try:
                    price    = float(fields[3])  if fields[3]  not in ('', '-') else 0.0
                    prev_cls = float(fields[4])  if fields[4]  not in ('', '-') else 0.0
                    pct      = float(fields[32]) if fields[32] not in ('', '-') else 0.0
                    day_chg  = price - prev_cls if prev_cls else 0.0
                    high     = float(fields[33]) if len(fields) > 33 and fields[33] not in ('', '-') else price
                    low      = float(fields[34]) if len(fields) > 34 and fields[34] not in ('', '-') else price
                    vol_ratio= float(fields[38]) if len(fields) > 38 and fields[38] not in ('', '-', '0') else None
                    result[sym] = {
                        'symbol':     sym,
                        'price':      price,
                        'prev_close': prev_cls,
                        'high':       high,
                        'low':        low,
                        'pct':        pct,
                        'chg':        day_chg,
                        'vol_ratio':  vol_ratio,
                    }
                except (ValueError, IndexError):
                    continue
            return result
    except Exception as e:
        logger.warning('fetch_bulk failed: %s', e)
        return {}


# ─── 历史数据（用于 RSI 和 5日均量）─────────────────────────

def _fetch_history_sina(symbol: str, days: int = 6) -> Optional[list[dict]]:
    """
    用新浪财经接口获取日K线（最近几天），用于：
    1. 计算 RSI（14日）
    2. 计算 5 日均量
    返回 [{date, close, volume}, ...]，最近日期在最后。
    """
    if '.SH' in symbol:
        code = 'sh' + symbol.replace('.SH', '')
    else:
        code = 'sz' + symbol.replace('.SZ', '')
    url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={days}'
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn',
        })
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            content = resp.read().decode('utf-8')
            data = json.loads(content)
            if not data or not isinstance(data, list):
                return None
            result = []
            for item in data:
                try:
                    result.append({
                        'date':   item.get('day', ''),
                        'close':  float(item.get('close', 0)),
                        'volume': float(item.get('volume', 0)),
                    })
                except (ValueError, TypeError):
                    continue
            return result if len(result) >= 2 else None
    except Exception:
        return None


def _fetch_history_tencent(symbol: str, days: int = 20) -> Optional[list[dict]]:
    """
    用腾讯财经接口获取日K线（前复权），用于 ATR 计算（需要 H/L）。
    返回 [{date, open, close, high, low, volume}, ...]，日期升序。
    """
    upper = symbol.upper()
    if upper.endswith('.SH'):
        qt = 'sh' + upper[:-3]
    elif upper.endswith('.SZ'):
        qt = 'sz' + upper[:-3]
    else:
        qt = symbol.lower()
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?_var=kline_dayqfq&param={qt},day,,,{days},qfq')
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            raw = resp.read().decode('utf-8')
        eq = raw.find('=')
        if eq >= 0:
            raw = raw[eq + 1:]
        data = json.loads(raw)
        qfq = data.get('data', {}).get(qt, {})
        days_list = qfq.get('qfqday', []) or qfq.get('day', [])
        if not days_list:
            return None
        result = []
        for bar in days_list:
            if len(bar) < 6:
                continue
            try:
                result.append({
                    'date':   bar[0],
                    'open':   float(bar[1]),
                    'close':  float(bar[2]),
                    'high':   float(bar[3]),
                    'low':    float(bar[4]),
                    'volume': float(bar[5]),
                })
            except (ValueError, IndexError):
                continue
        return result if len(result) >= 15 else None
    except Exception:
        return None


def _compute_atr(symbol: str, period: int = 14) -> Optional[float]:
    """
    计算指定周期 ATR（Average True Range）。
    使用腾讯前复权日K线数据。
    """
    bars = _fetch_history_tencent(symbol, days=period + 5)
    if not bars or len(bars) < period + 1:
        return None
    closes = [b['close'] for b in bars]
    highs  = [b['high']  for b in bars]
    lows   = [b['low']   for b in bars]
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    # Initial ATR: simple average of first 'period' TRs
    atr = sum(trs[:period]) / period
    # Subsequent: smoothed ATR
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)


def _compute_atr_ratio(symbol: str, period: int = 14, lookback: int = 20) -> Optional[float]:
    """
    计算 ATR 比率：当前 ATR(period) / 近 lookback 日 ATR 最高值。
    - ratio > 0.85: 当前波动率处于近 20 日 85% 以上高位（市场顶部/底部预警）
    - ratio <= 0.85: 正常波动，可开仓
    用于过滤 RSI 均值回归策略在高波动期的假信号。
    返回 None 表示计算失败。
    """
    bars = _fetch_history_tencent(symbol, days=period + lookback + 5)
    if not bars or len(bars) < period + lookback:
        return None
    closes = [b['close'] for b in bars]
    highs  = [b['high']  for b in bars]
    lows   = [b['low']   for b in bars]
    n = len(closes)

    # Compute ATR(period) for each day
    atr_history = [None] * n
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        if i >= period:
            if atr_history[i - 1] is not None:
                atr_history[i] = (atr_history[i - 1] * (period - 1) + tr) / period
            else:
                atr_history[i] = sum(
                    max(
                        highs[j] - lows[j],
                        abs(highs[j] - closes[j - 1]),
                        abs(lows[j] - closes[j - 1]),
                    ) for j in range(i - period + 1, i + 1)
                ) / period

    # Current ATR = most recent valid value
    current_atr = None
    for i in range(n - 1, -1, -1):
        if atr_history[i] is not None:
            current_atr = atr_history[i]
            break
    if current_atr is None or current_atr <= 0:
        return None

    # ATR high over lookback window
    valid_atrs = [v for v in atr_history[n - lookback:] if v is not None]
    if not valid_atrs:
        return None
    atr_max = max(valid_atrs)
    if atr_max <= 0:
        return None

    return round(current_atr / atr_max, 4)


def get_atr_stop_loss(symbol: str, entry_price: float,
                      atr_period: int = 14, multiplier: float = 2.0) -> Optional[float]:
    """
    计算 ATR 止损价。
    公式: stop_price = entry_price - multiplier * ATR
    返回止损价（None 表示无法计算 ATR）。
    """
    atr = _compute_atr(symbol, period=atr_period)
    if atr is None or atr <= 0:
        return None
    stop_price = entry_price - multiplier * atr
    return round(stop_price, 2)


def check_position_stop_loss(symbol: str, entry_price: float,
                             current_price: float,
                             atr_period: int = 14, atr_multiplier: float = 2.0,
                             fixed_sl_pct: float = 0.08) -> tuple[bool, Optional[float], str]:
    """
    检查持仓是否触发止损。
    优先使用 ATR 止损；ATR 不可用时降级为固定百分比止损。

    Returns:
        (should_sell, stop_price, reason)
    """
    atr = _compute_atr(symbol, period=atr_period)
    if atr and atr > 0:
        stop_price = round(entry_price - atr_multiplier * atr, 2)
        stop_label = f'ATR({atr_multiplier}x)={atr:.2f}'
    else:
        stop_price = round(entry_price * (1 - fixed_sl_pct), 2)
        stop_label = f'固定{fixed_sl_pct*100:.0f}%'

    if current_price <= stop_price:
        return True, stop_price, f'触发止损 {stop_label}（当前价{current_price:.2f}≤止损价{stop_price:.2f})'
    return False, stop_price, f'未触发止损 {stop_label}（当前价{current_price:.2f}>止损价{stop_price:.2f})'


def check_fixed_take_profit(entry_price: float, current_price: float,
                            tp_pct: float = 0.25) -> tuple[bool, float, str]:
    """
    检查固定百分比止盈。
    涨跌幅达到 tp_pct 时触发。

    Returns:
        (should_sell, target_price, reason)
    """
    target_price = round(entry_price * (1 + tp_pct), 2)
    if current_price >= target_price:
        return True, target_price, f'触发固定止盈 {tp_pct*100:.0f}%（现价{current_price:.2f}≥目标价{target_price:.2f})'
    return False, target_price, f'未触发止盈 {tp_pct*100:.0f}%（现价{current_price:.2f}<目标价{target_price:.2f})'


def check_atr_trailing_stop(symbol: str, peak_price: float, entry_price: float,
                             current_price: float,
                             atr_period: int = 14, atr_multiplier: float = 2.0) -> tuple[bool, float, str]:
    """
    检查 ATR 移动止盈（Chandelier Exit）。
    峰值回撤超过 atr_multiplier × ATR 时触发。
    与固定止盈配合使用：固定止盈锁定目标，移动止盈跟踪回撤。

    Returns:
        (should_sell, stop_price, reason)
    """
    atr = _compute_atr(symbol, period=atr_period)
    if atr is None or atr <= 0:
        return False, 0.0, 'ATR计算失败，跳过移动止盈'

    stop_price = round(peak_price - atr_multiplier * atr, 2)
    drawdown_pct = (peak_price - current_price) / peak_price if peak_price > 0 else 0
    if current_price <= stop_price:
        return True, stop_price, (f'触发ATR移动止盈 {atr_multiplier}x（峰值{peak_price:.2f}，'
                                   f'回撤{drawdown_pct*100:.1f}%≥0%，当前价{current_price:.2f}≤追踪止损价{stop_price:.2f})')
    return False, stop_price, (f'未触发ATR移动止盈 {atr_multiplier}x（峰值{peak_price:.2f}，'
                               f'回撤{drawdown_pct*100:.1f}%，追踪止损价{stop_price:.2f})')


def _compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_avg_volume_ratio(symbol: str) -> Optional[float]:
    """
    返回今日成交量 / 5日平均成交量。
    >1.5 = 放量，<0.6 = 缩量。
    """
    hist = _fetch_history_sina(symbol, days=6)
    if not hist or len(hist) < 5:
        return None
    # hist 最近5天（去掉今天，今天K线可能还不完整）
    recent = hist[-5:-1] if len(hist) > 1 else hist[-4:]
    if len(recent) < 4:
        return None
    avg_vol = sum(d['volume'] for d in recent) / len(recent)
    if avg_vol <= 0:
        return None
    today_vol = hist[-1]['volume']
    return round(today_vol / avg_vol, 2)


# ─── 分钟级K线数据（用于信号二次确认）────────────────────────

def _fetch_minute_bars(symbol: str, scale: int = 15,
                       datalen: int = 16) -> Optional[list[dict]]:
    """
    用新浪财经接口获取分钟K线。
    scale: 5/15/30/60（分钟数）
    datalen: 返回的BAR数量（最多约100）
    返回 [{time, close, volume}, ...]，时间升序。
    """
    upper = symbol.upper()
    if upper.endswith('.SH'):
        code = 'sh' + upper[:-3]
    elif upper.endswith('.SZ'):
        code = 'sz' + upper[:-3]
    else:
        return None
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php'
           f'/CN_MarketData.getKLineData?symbol={code}&scale={scale}'
           f'&ma=no&datalen={datalen}')
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn',
        })
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            content = resp.read().decode('utf-8')
            data = json.loads(content)
            if not data or not isinstance(data, list):
                return None
            result = []
            for item in data:
                try:
                    result.append({
                        'time':  item.get('day', ''),
                        'close': float(item.get('close', 0)),
                        'volume': float(item.get('volume', 0)),
                    })
                except (ValueError, TypeError):
                    continue
            return result if len(result) >= 5 else None
    except Exception:
        return None


def get_minute_rsi(symbol: str, scale: int = 15) -> Optional[float]:
    """
    计算指定周期分钟线的 RSI。
    scale: 5/15/30/60（分钟）
    返回 0-100 的 RSI 值，计算失败返回 None。
    """
    bars = _fetch_minute_bars(symbol, scale=scale, datalen=20)
    if not bars or len(bars) < 15:
        return None
    closes = [b['close'] for b in bars]
    return _compute_rsi(closes, period=14)


def confirm_signal_minute(symbol: str, direction: str) -> tuple[bool, Optional[float], str]:
    """
    分钟级信号二次确认。

    逻辑：
      - RSI_BUY（日线超卖）：15min RSI < 55 → 确认买入（还有上涨空间）
                             15min RSI >= 70 → 拒绝（已超买，追高风险大）
                             否则中性（观望，确认力度一般）
      - RSI_SELL（日线超买）：15min RSI > 45 → 确认卖出（还有下跌空间）
                             15min RSI <= 30 → 拒绝（已超卖，空头力量枯竭）
                             否则中性

    Returns: (confirmed: bool, minute_rsi: float, reason: str)
    """
    minute_rsi = get_minute_rsi(symbol, scale=15)
    if minute_rsi is None:
        # 获取失败：中立，不阻止交易
        return True, None, '分钟RSI获取失败，跳过确认'

    if direction == 'BUY':
        if minute_rsi < 55:
            return True, minute_rsi, f'15min RSI={minute_rsi:.0f}<55，确认买入动力充足'
        elif minute_rsi >= 70:
            return False, minute_rsi, f'15min RSI={minute_rsi:.0f}≥70，已超买，拒绝追高'
        else:
            return True, minute_rsi, f'15min RSI={minute_rsi:.0f}∈[55,70)，中性确认'

    elif direction == 'SELL':
        if minute_rsi > 45:
            return True, minute_rsi, f'15min RSI={minute_rsi:.0f}>45，确认下跌空间存在'
        elif minute_rsi <= 30:
            return False, minute_rsi, f'15min RSI={minute_rsi:.0f}≤30，已超卖，拒绝做空'
        else:
            return True, minute_rsi, f'15min RSI={minute_rsi:.0f}∈[30,45]，中性确认'

    return True, minute_rsi, '未知方向，跳过确认'


# ─── RSI（前日日线 RSI，盘中近似）───────────────────────────

def get_intraday_rsi(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """
    返回 (prev_rsi, day_chg_pct)。
    - prev_rsi: 前日收盘价计算的14日RSI
    - day_chg_pct: 当日涨跌幅（小数，如 0.03）
    """
    snap = fetch_realtime(symbol)
    if not snap or snap['price'] == 0:
        return None, None

    closes = _fetch_history_sina(symbol, days=20)
    prev_rsi = None
    if closes and len(closes) >= 15:
        close_vals = [d['close'] for d in closes]
        prev_rsi = _compute_rsi(close_vals)

    day_chg_pct = snap['pct'] / 100.0 if snap['pct'] else None
    return prev_rsi, day_chg_pct


# ─── 涨跌停检测 ────────────────────────────────────────────

def check_limit_status(snap: dict, limit_pct: float) -> tuple[Optional[str], float]:
    """
    检测涨跌停状态。

    Returns:
        (signal_type, limit_distance)  — signal_type=None 表示无信号
        signal_type:
            'LIMIT_UP'       : 已涨停（价格达到上限）
            'LIMIT_DOWN'     : 已跌停（价格达到下限）
            'LIMIT_RISK_UP'  : 接近涨停（距离上限 <1%）
            'LIMIT_RISK_DOWN': 接近跌停（距离下限 <1%）
            'WATCH_LIMIT_UP' : 接近涨停（距离上限 1-3%）
            'WATCH_LIMIT_DOWN': 接近跌停（距离下限 1-3%）
        limit_distance: 距涨停/跌停价的百分比（正=距上限，负=距下限）
    """
    price      = snap.get('price', 0)
    prev_close = snap.get('prev_close', 0)
    if price == 0 or prev_close == 0:
        return None, 0.0

    upper_limit = prev_close * (1 + limit_pct)
    lower_limit = prev_close * (1 - limit_pct)

    # 距涨停/跌停的距离（正=距上限，负=距下限）
    dist_from_up   = (upper_limit - price) / price   # 正数=还没到涨停
    dist_from_down = (price - lower_limit) / price    # 正数=还没到跌停

    pct = snap.get('pct', 0)

    # 已涨停（价格达到涨停价，且当日有实际成交）
    if price >= upper_limit * 0.9999:   # 0.9999 容忍浮点误差
        return 'LIMIT_UP', 0.0

    # 已跌停
    if price <= lower_limit * 1.0001:
        return 'LIMIT_DOWN', 0.0

    # 接近跌停（距跌停价 <1%）
    if dist_from_down < 0.01:
        return 'LIMIT_RISK_DOWN', -dist_from_down

    # 接近涨停（距涨停价 <1%）
    if dist_from_up < 0.01:
        return 'LIMIT_RISK_UP', dist_from_up

    # 观察级（1-3%以内）
    if dist_from_down < 0.03:
        return 'WATCH_LIMIT_DOWN', -dist_from_down

    if dist_from_up < 0.03:
        return 'WATCH_LIMIT_UP', dist_from_up

    return None, 0.0


# ─── 信号评估（A 股专用）────────────────────────────────────

RSI_BUY_THRESHOLD  = 25
RSI_SELL_THRESHOLD = 65
MOMENTUM_THRESHOLD = 0.01    # 价格变动 >1% 视为有效动量
DEFAULT_ATR_THRESH = 0.85     # ATR ratio > 0.85 时为高波动，禁止开仓


def evaluate_signal(symbol: str,
                    rsi_buy:     int = RSI_BUY_THRESHOLD,
                    rsi_sell:    int = RSI_SELL_THRESHOLD,
                    atr_threshold: float = DEFAULT_ATR_THRESH,
                    positions: Optional[list] = None) -> Optional[SignalAlert]:
    """
    评估单只股票的全部信号。

    Args:
        positions: 当前持仓列表（每项含 symbol 字段），用于涨跌停时判断是否已有持仓。
    优先级：
      1. 涨跌停相关（最紧急）
      2. RSI 超买超卖
      3. 缩量预警

    涨跌停逻辑（position-aware）：
      LIMIT_UP + 有持仓 → WATCH_SELL（止盈预警，不追高买入）
      LIMIT_UP + 无持仓 → LIMIT_UP（禁止买入）
      LIMIT_DOWN + 有持仓 → RSI_SELL（紧急逃生）
      LIMIT_DOWN + 无持仓 → LIMIT_DOWN（禁止抄底）
    """
    snap = fetch_realtime(symbol)
    if not snap or snap['price'] == 0:
        return None

    limit_pct = get_limit_pct(symbol)
    limit_signal, limit_dist = check_limit_status(snap, limit_pct)

    prev_rsi, day_chg = get_intraday_rsi(symbol)
    pct   = snap['pct']
    price = snap['price']
    vol_ratio = snap.get('vol_ratio')  # 腾讯内置量比参考

    # ── 1. 涨跌停信号（position-aware）───────────────────────
    if limit_signal in ('LIMIT_UP', 'LIMIT_RISK_UP', 'WATCH_LIMIT_UP'):
        # 检查是否已有持仓
        has_position = any(
            (p.get('symbol') == symbol or p.get('symbol') == symbol.replace('.SH', '.SZ'))
            for p in (positions or [])
            if p.get('shares', 0) > 0
        )
        if has_position:
            # 涨停持仓 → 发出止盈预警（ WATCH_SELL，而不是追买）
            vol_note = '放量确认' if (vol_ratio and vol_ratio > 1.3) else '缩量诱多'
            return SignalAlert(
                symbol=symbol, signal='WATCH_SELL',
                price=price, pct=pct, prev_rsi=prev_rsi,
                volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
                reason=(f'涨停持仓预警！{vol_note}，持仓者注意止盈｜RSI={prev_rsi:.0f}｜现价{price}'),
                emitted_at=datetime.now().strftime('%H:%M:%S'),
            )
        # 无持仓 → 禁止追涨
        reason_map = {
            'LIMIT_UP':        f"涨停！{'【放量确认真拉升】' if (vol_ratio and vol_ratio > 1.3) else '【缩量诱多风险】'}",
            'LIMIT_RISK_UP':  f"逼近涨停（距涨停{'%.1f'%(limit_dist*100)}%），{'量能充沛或继续封板' if (vol_ratio and vol_ratio > 1.3) else '量能萎缩需警惕'}",
            'WATCH_LIMIT_UP': f"接近涨停（距涨停{'%.1f'%(limit_dist*100)}%），RSI={prev_rsi:.0f}" if prev_rsi else "接近涨停，注意追高风险",
        }
        return SignalAlert(
            symbol=symbol, signal=limit_signal,
            price=price, pct=pct, prev_rsi=prev_rsi,
            volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
            reason=reason_map[limit_signal],
            emitted_at=datetime.now().strftime('%H:%M:%S'),
        )

    if limit_signal in ('LIMIT_DOWN', 'LIMIT_RISK_DOWN', 'WATCH_LIMIT_DOWN'):
        # 检查是否已有持仓
        has_position = any(
            (p.get('symbol') == symbol or p.get('symbol') == symbol.replace('.SH', '.SZ'))
            for p in (positions or [])
            if p.get('shares', 0) > 0
        )
        if has_position:
            # 跌停持仓 → 紧急逃生（SELL，不等反弹）
            urgency = {'LIMIT_DOWN': '⚠️', 'LIMIT_RISK_DOWN': '🔴', 'WATCH_LIMIT_DOWN': '🚨'}
            return SignalAlert(
                symbol=symbol, signal='RSI_SELL',
                price=price, pct=pct, prev_rsi=prev_rsi,
                volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
                reason=(f'{urgency.get(limit_signal, "")}跌停逃生！尽快减仓｜RSI={prev_rsi:.0f}｜现价{price}'),
                emitted_at=datetime.now().strftime('%H:%M:%S'),
            )
        # 无持仓 → 禁止抄底
        urgency = {'LIMIT_DOWN': '⚠️', 'LIMIT_RISK_DOWN': '🔴', 'WATCH_LIMIT_DOWN': '🚨'}
        reason_map = {
            'LIMIT_DOWN':       "已跌停！【无法卖出】",
            'LIMIT_RISK_DOWN':  f"逼近跌停（距跌停{'%.1f'%(limit_dist*100)}%）{'⚠️即将封板！' if limit_dist < 0.005 else '逃生窗口缩小'}",
            'WATCH_LIMIT_DOWN': f"接近跌停（距跌停{'%.1f'%(limit_dist*100)}%），RSI={prev_rsi:.0f}" if prev_rsi else "接近跌停，注意风险",
        }
        return SignalAlert(
            symbol=symbol, signal=limit_signal,
            price=price, pct=pct, prev_rsi=prev_rsi,
            volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
            reason=reason_map[limit_signal],
            emitted_at=datetime.now().strftime('%H:%M:%S'),
        )

    # ── 2. RSI 超买超卖 ──────────────────────────────
    if prev_rsi is not None:
        # 计算 ATR ratio（用于过滤高波动期买入信号）
        atr_ratio = _compute_atr_ratio(symbol, period=14, lookback=20)
        vol_high = (atr_ratio is not None) and (atr_ratio > atr_threshold)
        atr_note = f' | [ATRratio={atr_ratio:.2f}>{atr_threshold} 高波动屏蔽]' if vol_high else ''

        if prev_rsi <= rsi_buy:
            if vol_high:
                # 高波动期：RSI 超卖信号被屏蔽，不发买入
                return SignalAlert(
                    symbol=symbol, signal='HOLD',
                    price=price, pct=pct, prev_rsi=prev_rsi,
                    volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
                    reason=(f'RSI={prev_rsi:.0f}≤{rsi_buy}超卖+ATRratio={atr_ratio:.2f}>{atr_threshold} '
                            f'高波动期，RSI均值回归策略失效，屏蔽买入｜现价{price}'),
                    emitted_at=datetime.now().strftime('%H:%M:%S'),
                )

            # ── 基本面检查（PE/PB 过滤）────────────────────
            fcheck = _get_fundamentals_check()
            if fcheck:
                passed, reason_brief = fcheck(symbol)
                if not passed:
                    return SignalAlert(
                        symbol=symbol, signal='HOLD',
                        price=price, pct=pct, prev_rsi=prev_rsi,
                        volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
                        reason=(f'RSI={prev_rsi:.0f}≤{rsi_buy}超卖｜基本面拒绝：{reason_brief}｜现价{price}'),
                        emitted_at=datetime.now().strftime('%H:%M:%S'),
                    )

            if day_chg is not None and day_chg < -MOMENTUM_THRESHOLD:
                signal = 'WATCH_BUY'
                reason = f'RSI={prev_rsi:.0f}≤{rsi_buy}超卖，当日下跌{'%.1f'%(day_chg*100)}%，关注低吸｜现价{price}'
            elif day_chg is not None and day_chg > MOMENTUM_THRESHOLD:
                signal = 'RSI_BUY'
                reason = f'RSI={prev_rsi:.0f}≤{rsi_buy}超卖+价格已反弹{'%.1f'%(day_chg*100)}%，强势｜现价{price}'
            else:
                signal = 'RSI_BUY'
                reason = f'RSI={prev_rsi:.0f}≤{rsi_buy}超卖区间｜现价{price}'

            # ── 北向共振检查 ───────────────────────────────
            nb_fetch = _get_northbound_check()
            north_boost = ''
            if nb_fetch:
                try:
                    nb_data = nb_fetch()
                    net_cny = nb_data.get('net_north_cny', 0) if nb_data else 0
                    net_billions = abs(net_cny) / 1e8
                    if net_cny > 0 and net_billions >= NORTH_BUY_BOOST_THRESHOLD:
                        # 北向大幅净流入 → 信号强化
                        north_boost = f'｜北向共振+{net_billions:.0f}亿'
                        reason = reason.replace('｜现价{price}', f'{north_boost}｜现价{price}')
                except Exception:
                    pass

            return SignalAlert(
                symbol=symbol, signal=signal,
                price=price, pct=pct, prev_rsi=prev_rsi,
                volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
                reason=reason, emitted_at=datetime.now().strftime('%H:%M:%S'),
            )

        if prev_rsi >= rsi_sell:
            if day_chg is not None and day_chg > MOMENTUM_THRESHOLD:
                signal = 'WATCH_SELL'
                reason = f"RSI={prev_rsi:.0f}≥{rsi_sell}超买，当日上涨{'%.1f'%(day_chg*100)}%，关注止盈｜现价{price}"
            else:
                signal = 'WATCH_SELL'
                reason = f"RSI={prev_rsi:.0f}≥{rsi_sell}超买区间｜现价{price}"

            return SignalAlert(
                symbol=symbol, signal=signal,
                price=price, pct=pct, prev_rsi=prev_rsi,
                volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
                reason=reason, emitted_at=datetime.now().strftime('%H:%M:%S'),
            )

    # ── 3. 大幅波动 ─────────────────────────────────
    day_abs = abs(day_chg) if day_chg is not None else 0
    if day_abs > 0.03:
        return SignalAlert(
            symbol=symbol, signal='VOLATILE',
            price=price, pct=pct, prev_rsi=prev_rsi,
            volume_ratio=vol_ratio, day_chg=day_chg or 0.0,
            reason=f"当日波动{'%.1f'%(day_chg*100)}%（RSI={prev_rsi:.0f}）｜现价{price}",
            emitted_at=datetime.now().strftime('%H:%M:%S'),
        )

    return None


def check_portfolio_signals(positions: list[dict]) -> list[SignalAlert]:
    """
    检查持仓列表的全部信号。
    positions: [{'symbol': '600900.SH', 'shares': ..., 'rsi_buy': 35, 'rsi_sell': 70}, ...]
    """
    alerts = []
    symbols = [p.get('symbol') for p in positions if p.get('symbol')]
    snaps   = fetch_bulk(symbols)

    for pos in positions:
        sym = pos.get('symbol')
        if not sym:
            continue

        # 尝试批量获取，否则单个兜底
        snap = snaps.get(sym)
        if not snap:
            snap = fetch_realtime(sym)

        if not snap:
            continue

        alert = evaluate_signal(
            sym,
            rsi_buy= int(pos.get('rsi_buy',  RSI_BUY_THRESHOLD)),
            rsi_sell=int(pos.get('rsi_sell', RSI_SELL_THRESHOLD)),
            atr_threshold=float(pos.get('atr_threshold', DEFAULT_ATR_THRESH)),
        )
        if alert:
            alerts.append(alert)

    # 涨跌停类信号优先（最紧急）
    URGENCY_ORDER = [
        'LIMIT_DOWN', 'LIMIT_RISK_DOWN', 'WATCH_LIMIT_DOWN',
        'LIMIT_UP', 'LIMIT_RISK_UP', 'WATCH_LIMIT_UP',
        'RSI_BUY', 'WATCH_BUY', 'WATCH_SELL', 'RSI_SELL',
        'VOLATILE',
    ]
    alerts.sort(key=lambda a: (
        URGENCY_ORDER.index(a.signal) if a.signal in URGENCY_ORDER else 99,
        -abs(a.pct) if a.pct else 0,
    ))
    return alerts


# ─── 飞书消息构建 ──────────────────────────────────────────

SIGNAL_EMOJI = {
    'LIMIT_UP':         '🔴🔴',
    'LIMIT_DOWN':       '🔴🔴',
    'LIMIT_RISK_UP':    '🔴',
    'LIMIT_RISK_DOWN':  '🔴',
    'WATCH_LIMIT_UP':   '🟠',
    'WATCH_LIMIT_DOWN': '🟠',
    'RSI_BUY':          '🟢',
    'WATCH_BUY':        '🔔',
    'WATCH_SELL':       '⚠️',
    'RSI_SELL':         '⚠️',
    'VOLATILE':         '💥',
    'BUY':              '✅',
    'SELL':             '🔴',
}

SIGNAL_LABEL = {
    'LIMIT_UP':         '【涨停】',
    'LIMIT_DOWN':       '【跌停】',
    'LIMIT_RISK_UP':    '【涨停预警】',
    'LIMIT_RISK_DOWN':  '【跌停预警】',
    'WATCH_LIMIT_UP':   '【接近涨停】',
    'WATCH_LIMIT_DOWN': '【接近跌停】',
    'RSI_BUY':          'RSI买入信号',
    'WATCH_BUY':        '关注买入',
    'WATCH_SELL':       '关注卖出',
    'RSI_SELL':         'RSI卖出信号',
    'VOLATILE':         '大幅波动',
    'BUY':              '买入信号',
    'SELL':             '卖出信号',
}


def format_feishu_message(alerts: list[SignalAlert], check_time: str) -> str:
    """构建飞书推送文本。"""
    if not alerts:
        return ''
    header = f"📈 **{check_time} 盘中信号**"
    lines  = [header]
    for a in alerts:
        emoji = SIGNAL_EMOJI.get(a.signal, '📊')
        label = SIGNAL_LABEL.get(a.signal, a.signal)
        sign  = '+' if a.pct > 0 else ''
        pct_str = f"{sign}{a.pct:.2%}"
        rsi_str = f"RSI={a.prev_rsi:.0f}" if a.prev_rsi else ""
        vol_str = f"量比={'%.1f'%(a.volume_ratio)}x" if a.volume_ratio else ""

        parts = [x for x in [rsi_str, vol_str] if x]
        info  = ' | '.join(parts)

        lines.append(
            f"{emoji} **{a.symbol}** {label}\n"
            f"   现价：**{a.price:.2f}**（{pct_str}）{f'｜{info}' if info else ''}\n"
            f"   {a.reason}"
        )
    return '\n'.join(lines)


# ─── 参数加载器（WFA 优化参数优先）───────────────────────────────

_DEFAULTS = dict(
    rsi_period=14, rsi_buy=25, rsi_sell=65,
    stop_loss=0.05, take_profit=0.20,
    atr_period=14, atr_multiplier=2.0,
    atr_threshold=0.85,
    min_hold_days=3,
)


def load_symbol_params(symbol: str) -> dict:
    """
    加载指定股票的完整参数集。
    优先级：
      1. live_params.json（WFA 输出）→ 覆盖全部字段
      2. params.json（手工配置）→ 补充 RSI / 止止损
      3. 硬编码默认值兜底

    ATR 参数暂不纳入 WFA 输出，手工配置或使用默认值。
    """
    import json as _json
    result = dict(_DEFAULTS)  # 浅拷贝默认值

    # 1. 尝试 params.json
    proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    params_file = os.path.join(proj, 'params.json')
    if os.path.exists(params_file):
        try:
            with open(params_file, 'r', encoding='utf-8') as f:
                params_all = _json.load(f)
            for strat_name, strat_conf in params_all.get('strategies', {}).items():
                if strat_conf.get('symbol', '').upper() == symbol.upper():
                    p = strat_conf.get('params', {})
                    result['rsi_period']  = p.get('rsi_period', result['rsi_period'])
                    result['rsi_buy']     = p.get('rsi_buy',    result['rsi_buy'])
                    result['rsi_sell']    = p.get('rsi_sell',   result['rsi_sell'])
                    result['stop_loss']   = p.get('stop_loss',  result['stop_loss'])
                    result['take_profit'] = p.get('take_profit', result['take_profit'])
                    result['min_hold_days'] = p.get('min_hold_days', result['min_hold_days'])
                    result['atr_threshold'] = p.get('atr_threshold', result['atr_threshold'])
                    break
        except Exception:
            pass

    # 2. 尝试 live_params.json（WFA 覆盖 RSI 参数）
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    live_file = os.path.join(backend_dir, 'services', 'live_params.json')
    if os.path.exists(live_file):
        try:
            with open(live_file, 'r', encoding='utf-8') as f:
                live = _json.load(f)
            # key 格式: {symbol}_RSI
            live_key = f"{symbol.upper()}_RSI"
            if live_key in live:
                wp = live[live_key].get('params', {})
                result['rsi_period'] = wp.get('rsi_period', result['rsi_period'])
                result['rsi_buy']    = wp.get('rsi_buy',    result['rsi_buy'])
                result['rsi_sell']   = wp.get('rsi_sell',   result['rsi_sell'])
                result['stop_loss']  = wp.get('stop_loss',  result['stop_loss'])
                result['take_profit']= wp.get('take_profit', result['take_profit'])
        except Exception:
            pass

    return result
