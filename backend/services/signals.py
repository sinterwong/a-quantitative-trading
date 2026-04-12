"""
signals.py — 盘中信号引擎
===========================
纯轻量实时信号检测，不依赖 AkShare/分钟线。

数据源：腾讯实时报价 (qt.gtimg.cn)
RSI近似：前日RSI + 当日价格动量方向（盘中无需分钟数据）
"""

import os
import sys
import json
import ssl
import logging
import urllib.request
from datetime import date, datetime, time as dtime
from typing import Optional, NamedTuple

logger = logging.getLogger('signals')

# A股盘中时间段（UTC+8）
MARKET_MORNING_START  = (9, 35)   # 9:35 开盘后可检查
MARKET_MORNING_END    = (11, 30)
MARKET_AFTERNOON_START = (13, 0)
MARKET_AFTERNOON_END   = (14, 55)
TRADING_DAYS = range(5)  # Mon-Fri


class SignalAlert(NamedTuple):
    symbol: str
    signal: str        # BUY | SELL | WATCH_BUY | WATCH_SELL | VOLATILE
    price: float
    pct: float         # 涨跌幅%
    prev_rsi: float
    day_chg: float
    reason: str
    emitted_at: str


# ─── 实时行情 ────────────────────────────────────────────────

def fetch_realtime(symbol: str) -> Optional[dict]:
    """
    获取腾讯实时行情字段。
    fields[3]=当前价, [4]=昨收, [5]=今开, [31]=涨跌额, [32]=涨跌幅%
    """
    sym = symbol.lower().replace('.sh', 'sh').replace('.sz', 'sz')
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
            fields = raw.split('~')
            if len(fields) < 50:
                return None
            price     = float(fields[3]) if fields[3] not in ('', '-') else 0.0
            prev_cls  = float(fields[4]) if fields[4] not in ('', '-') else 0.0
            day_chg   = float(fields[31]) if fields[31] not in ('', '-') else 0.0
            pct       = float(fields[32]) if fields[32] not in ('', '-') else 0.0
            return {
                'symbol':     symbol,
                'price':      price,
                'prev_close': prev_cls,
                'chg':        day_chg,
                'pct':        pct,
            }
    except Exception as e:
        logger.debug('fetch_realtime %s failed: %s', symbol, e)
        return None


def fetch_bulk(symbols: list[str]) -> dict[str, dict]:
    """批量获取实时行情（腾讯单次请求支持多符号）"""
    if not symbols:
        return {}
    sym_str = ','.join(s.lower().replace('.sh', 'sh').replace('.sz', 'sz')
                       for s in symbols)
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
                fields = line.split('~')
                if len(fields) < 50:
                    continue
                try:
                    price     = float(fields[3]) if fields[3] not in ('', '-') else 0.0
                    prev_cls  = float(fields[4]) if fields[4] not in ('', '-') else 0.0
                    pct       = float(fields[32]) if fields[32] not in ('', '-') else 0.0
                    day_chg   = price - prev_cls if prev_cls else 0.0
                    result[sym] = {
                        'symbol':     sym,
                        'price':      price,
                        'prev_close': prev_cls,
                        'pct':        pct,
                        'chg':        day_chg,
                    }
                except (ValueError, IndexError):
                    continue
            return result
    except Exception as e:
        logger.warning('fetch_bulk failed: %s', e)
        return {}


# ─── 历史数据（AkShare，非盘中） ─────────────────────────────

def _fetch_history_akshare(symbol: str, days: int = 60) -> Optional[list[float]]:
    """用 AkShare 获取日线收盘价列表（供 RSI 计算，非盘中用）"""
    try:
        import akshare as ak
        end   = date.today().strftime('%Y%m%d')
        start = (date.today().replace(day=max(1, date.today().day - days))
                 .strftime('%Y%m%d'))
        market, code = ('sh', symbol[2:]) if symbol.endswith('.SH') else ('sz', symbol[2:])
        df = ak.stock_zh_a_daily(symbol=code, adjust='')
        df = df[df['date'] >= start]
        closes = df['close'].tolist()
        return [float(c) for c in closes[-days:]] if closes else None
    except Exception as e:
        logger.debug('akshare history %s failed: %s', symbol, e)
        return None


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


# ─── RSI 近似（前日RSI + 当日动量） ─────────────────────────

def get_intraday_rsi(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """
    返回 (prev_rsi, day_chg_pct)
    - prev_rsi: 前日收盘价计算的14日RSI（日线）
    - day_chg_pct: 当日涨跌幅（相对昨收）
   盘中无需分钟线，用前日RSI水平 + 当日动量方向近似信号。
    """
    snap = fetch_realtime(symbol)
    if not snap or snap['price'] == 0:
        return None, None

    # 尝试用 AkShare 日线
    closes = _fetch_history_akshare(symbol, days=60)
    if closes and len(closes) >= 15:
        prev_rsi = _compute_rsi(closes)
    else:
        # Fallback：用 Sina 历史接口
        prev_rsi = None

    day_chg_pct = snap['pct'] / 100.0 if snap['pct'] else None
    return prev_rsi, day_chg_pct


# ─── 信号评估 ──────────────────────────────────────────────

RSI_BUY_THRESHOLD  = 35   # RSI 低于此值视为超卖
RSI_SELL_THRESHOLD  = 70   # RSI 高于此值视为超买
MOMENTUM_THRESHOLD  = 0.01  # 价格变动 >1% 视为有效动量


def evaluate_signal(symbol: str, rsi_buy: int = RSI_BUY_THRESHOLD,
                    rsi_sell: int = RSI_SELL_THRESHOLD) -> Optional[SignalAlert]:
    """
    评估个股信号，返回 SignalAlert 或 None。
    """
    prev_rsi, day_chg = get_intraday_rsi(symbol)
    snap = fetch_realtime(symbol)
    if prev_rsi is None or snap is None:
        return None

    price = snap['price']
    pct   = snap['pct']
    day_abs = abs(day_chg) if day_chg is not None else 0

    signal_type = None
    reason      = ''

    # 买入信号
    if prev_rsi <= rsi_buy:
        if day_chg is not None and day_chg < -MOMENTUM_THRESHOLD:
            signal_type = 'WATCH_BUY'
            reason = (f"RSI={prev_rsi:.0f}≤{rsi_buy}超卖区间，当日下跌{day_chg:.1%}，"
                      f"关注低吸机会｜现价{price}")
        elif day_chg is not None and day_chg > MOMENTUM_THRESHOLD:
            signal_type = 'RSI_BUY'
            reason = (f"RSI={prev_rsi:.0f}≤{rsi_buy}超卖，价格已反弹{day_chg:.1%}，"
                      f"强势信号｜现价{price}")

    # 卖出信号
    elif prev_rsi >= rsi_sell:
        if day_chg is not None and day_chg > MOMENTUM_THRESHOLD:
            signal_type = 'WATCH_SELL'
            reason = (f"RSI={prev_rsi:.0f}≥{rsi_sell}超买区间，当日上涨{day_chg:.1%}，"
                      f"关注止盈｜现价{price}")

    # 大幅波动警示（独立于 RSI 方向）
    if not signal_type and day_abs > 0.03:
        signal_type = 'VOLATILE'
        reason = f"价格当日波动{day_chg:.1%}（>{day_abs:.1%}），RSI={prev_rsi:.0f}｜现价{price}"

    if not signal_type:
        return None

    return SignalAlert(
        symbol=symbol,
        signal=signal_type,
        price=price,
        pct=pct,
        prev_rsi=prev_rsi,
        day_chg=day_chg or 0.0,
        reason=reason,
        emitted_at=datetime.now().strftime('%H:%M:%S'),
    )


def check_portfolio_signals(positions: list[dict]) -> list[SignalAlert]:
    """
    检查持仓列表的信号。
    positions: [{'symbol': '600900.SH', ...}, ...]
    """
    alerts = []
    symbols = [p.get('symbol') for p in positions if p.get('symbol')]
    # 批量获取行情，减少网络请求
    snaps = fetch_bulk(symbols)

    for pos in positions:
        sym = pos.get('symbol')
        if not sym or sym not in snaps:
            # 单个查询兜底
            snap = fetch_realtime(sym)
            if not snap:
                continue
            snaps[sym] = snap

        prev_rsi, day_chg = get_intraday_rsi(sym)
        if prev_rsi is None:
            continue

        snap  = snaps[sym]
        price = snap['price']
        pct   = snap['pct']
        day_abs = abs(day_chg) if day_chg is not None else 0
        rsi_buy  = int(pos.get('rsi_buy',  RSI_BUY_THRESHOLD))
        rsi_sell = int(pos.get('rsi_sell', RSI_SELL_THRESHOLD))

        signal_type = None
        reason      = ''

        if prev_rsi <= rsi_buy:
            if day_chg is not None and day_chg < -MOMENTUM_THRESHOLD:
                signal_type = 'WATCH_BUY'
                reason = f"RSI={prev_rsi:.0f}≤{rsi_buy}超卖，当日下跌{day_chg:.1%}｜现价{price}"
            elif day_chg is not None and day_chg > MOMENTUM_THRESHOLD:
                signal_type = 'RSI_BUY'
                reason = f"RSI={prev_rsi:.0f}≤{rsi_buy}超卖，价格反弹{day_chg:.1%}｜现价{price}"
        elif prev_rsi >= rsi_sell:
            if day_chg is not None and day_chg > MOMENTUM_THRESHOLD:
                signal_type = 'WATCH_SELL'
                reason = f"RSI={prev_rsi:.0f}≥{rsi_sell}超买，当日上涨{day_chg:.1%}｜现价{price}"

        if not signal_type and day_abs > 0.03:
            signal_type = 'VOLATILE'
            reason = f"波动{day_chg:.1%}（>{day_abs:.1%}），RSI={prev_rsi:.0f}｜现价{price}"

        if signal_type:
            alerts.append(SignalAlert(
                symbol=sym,
                signal=signal_type,
                price=price,
                pct=pct,
                prev_rsi=prev_rsi,
                day_chg=day_chg or 0.0,
                reason=reason,
                emitted_at=datetime.now().strftime('%H:%M:%S'),
            ))

    return alerts


# ─── 飞书消息构建 ──────────────────────────────────────────

SIGNAL_EMOJI = {
    'RSI_BUY':    '🟢',
    'WATCH_BUY':  '🔔',
    'WATCH_SELL': '⚠️',
    'VOLATILE':   '💥',
    'BUY':        '✅',
    'SELL':       '🔴',
}

SIGNAL_LABEL = {
    'RSI_BUY':    'RSI买入信号',
    'WATCH_BUY':  '关注买入',
    'WATCH_SELL': '关注卖出',
    'VOLATILE':   '大幅波动',
    'BUY':        '买入信号',
    'SELL':       '卖出信号',
}


def format_feishu_message(alerts: list[SignalAlert], check_time: str) -> str:
    if not alerts:
        return ''
    header = f"**📈 盘中信号提醒**（{check_time}）"
    lines  = [header]
    for a in alerts:
        emoji = SIGNAL_EMOJI.get(a.signal, '📊')
        label = SIGNAL_LABEL.get(a.signal, a.signal)
        sign  = '+' if a.pct > 0 else ''
        pct_str = f"{sign}{a.pct:.2%}" if isinstance(a.pct, float) else f"{sign}{a.pct}%"
        lines.append(
            f"{emoji} **{a.symbol}** {label}\n"
            f"   现价：**{a.price:.2f}**（{pct_str}）RSI={a.prev_rsi:.0f}\n"
            f"   {a.reason}"
        )
    return '\n'.join(lines)
