"""
northbound.py — 北向资金（沪深港通）监控服务
==============================================
数据来源：
  1. KAMT 实时分时数据（eastmoney push2）：每分钟沪/深股通北向资金累计金额
  2. 个股北向持仓变化（eastmoney push2 stock API）：f169=持股变化，f170=持股变化%

盘中检测：
  - 北向资金大幅净流入/出（>100亿阈值）
  - 南北向资金差额比异常
  - 持仓个股北向持股突变（>5%日变化）
"""

import os
import sys
import json
import ssl
import logging
import urllib.request
import urllib.error
from datetime import datetime, date
from typing import Optional, List, Dict
from .data_cache import cached_kamt  # P8: KAMT cache + fallback

logger = logging.getLogger('northbound')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = THIS_DIR
sys.path.insert(0, BACKEND_DIR)

# ─── API 调用 ─────────────────────────────────────────────────────────────

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
_REFERER = 'https://data.eastmoney.com/'


def _get(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': _USER_AGENT,
            'Referer': _REFERER,
        })
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        logger.debug('northbound GET failed: %s — %s', url[:60], e)
        return None


# ─── KAMT 北向资金实时 ───────────────────────────────────────────────────

# KAMT 字段解释（fields2）：
#   f51 = 时间
#   f52 = 南向/北向 金额累计?（次字段值恒为0，实际价格不在此字段）
#   f53 = 成交量（股数）
#   f54 = 成交额（元）
#   f55 = 累计成交量
#   f56 = 累计成交额
#
# s2n = south to north = 南向（沪深投资人买港股）
# n2s = north to south = 北向（港资买A股）

def fetch_kamt(force_refresh: bool = False) -> Optional[dict]:
    """
    P8 wrapper: delegates to cached_kamt (60s TTL + eastmoney fallback).
    force_refresh=True bypasses cache.
    """
    return cached_kamt(force_refresh=force_refresh)



def format_kamt_summary(kamt_data: dict) -> str:
    """格式化北向资金摘要文字"""
    s2n = kamt_data.get('s2n', {})
    n2s = kamt_data.get('n2s', {})

    # 配额使用率
    def quota_pct(used, total):
        if not total or total == 0:
            return 0
        return used / total * 100

    # 北向（港资买A股）
    north_used = n2s.get('quota_used', 0)
    north_total = n2s.get('quota_total', 0)
    north_amt = n2s.get('cum_amount', 0)

    # 南向（沪深买港股）
    south_used = s2n.get('quota_used', 0)
    south_total = s2n.get('quota_total', 0)
    south_amt = s2n.get('cum_amount', 0)

    net = kamt_data.get('net_north_cny', 0)

    # 转换为亿
    north_amt_yi = north_amt / 1e8
    south_amt_yi = south_amt / 1e8
    net_yi = net / 1e8

    lines = []
    if north_amt > 0 or south_amt > 0:
        if net >= 0:
            emoji = '🟢'
            direction = '净买入'
        else:
            emoji = '🔴'
            direction = '净卖出'
        lines.append(f'{emoji}北向今日{direction}: {abs(net_yi):.2f}亿元')
        if north_amt_yi > 0:
            lines.append(f'  北上（港资买A股）: {north_amt_yi:.2f}亿元')
        if south_amt_yi > 0:
            lines.append(f'  南下（沪深买港股）: {south_amt_yi:.2f}亿元')
    else:
        # 只有配额数据，没有成交额
        north_pct = quota_pct(north_used, north_total)
        south_pct = quota_pct(south_used, south_total)
        lines.append(f'北向资金配额使用: 沪股通 {north_pct:.0f}% / 南向 {south_pct:.0f}%')

    return '\n'.join(lines) if lines else '暂无北向资金数据'


# ─── 个股北向持仓变化 ───────────────────────────────────────────────────

def fetch_stock_northbound(symbol: str) -> Optional[dict]:
    """
    获取个股北向持仓变化数据（eastmoney push2 stock API）。
    f169 = 持股变化（股数）
    f170 = 持股变化（%）
    f47  = 北向持股量（股）
    f48  = 北向持股市值（元）
    """
    # 转换代码格式
    upper = symbol.upper()
    if upper.endswith('.SH'):
        secid = '1.' + upper[:-3]
    elif upper.endswith('.SZ'):
        secid = '0.' + upper[:-3]
    else:
        # 推断
        num = symbol.replace('.', '')
        if num.startswith(('6', '5')):
            secid = '1.' + num
        else:
            secid = '0.' + num

    url = (
        f'https://push2.eastmoney.com/api/qt/stock/get'
        f'?secid={secid}'
        '&fields=f43,f47,f48,f169,f170,f57,f58'
        '&ut=b2884a393a59ad64002292a3e90d46a5'
    )
    raw = _get(url)
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    fields = data.get('data', {})
    if not fields:
        return None

    return {
        'symbol': symbol,
        'name': fields.get('f58', symbol),
        'close': fields.get('f43', 0),
        'north_hold_shares': fields.get('f47', 0),    # 北向持股（股）
        'north_hold_value': fields.get('f48', 0),      # 北向持股市值（元）
        'north_change_shares': fields.get('f169', 0),  # 持股变化（股）
        'north_change_pct': fields.get('f170', 0),     # 持股变化（%）
    }


def fetch_bulk_northbound(symbols: List[str]) -> Dict[str, dict]:
    """批量获取个股北向持仓变化"""
    result = {}
    for sym in symbols:
        data = fetch_stock_northbound(sym)
        if data:
            result[sym] = data
    return result


# ─── 北向资金大幅波动检测 ─────────────────────────────────────────────────



# --- North Flow Direction Tracker ---
# Track 3-day north flow trend vs single-day impulse

NORTH_HISTORY_FILE = os.path.join(THIS_DIR, 'north_flow_history.json')
NORTH_HISTORY_DAYS = 10


def _load_north_history() -> dict:
    try:
        if os.path.exists(NORTH_HISTORY_FILE):
            with open(NORTH_HISTORY_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
                return {k: float(v) for k, v in raw.items()
                        if isinstance(k, str) and len(k) == 10 and '-' in k}
    except Exception:
        pass
    return {}


def _save_north_history(history: dict) -> None:
    try:
        with open(NORTH_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('_save_north_history failed: %s', e)


def _record_north_flow(net_north_cny: float, date_str: str = None) -> None:
    if date_str is None:
        date_str = date.today().isoformat()
    history = _load_north_history()
    if date_str not in history:
        history[date_str] = net_north_cny
        sorted_dates = sorted(history.keys())
        while len(sorted_dates) > NORTH_HISTORY_DAYS:
            oldest = sorted_dates.pop(0)
            history.pop(oldest, None)
        _save_north_history(history)


def get_north_flow_direction(threshold_yi: float = 50.0) -> dict:
    history = _load_north_history()
    today_str = date.today().isoformat()
    sorted_dates = sorted(history.keys(), reverse=True)
    today_net_yi = history.get(today_str, 0.0) / 1e8
    positive_days = 0
    total_3day = 0.0
    for d in sorted_dates[:3]:
        val_yi = history.get(d, 0.0) / 1e8
        total_3day += val_yi
        if val_yi >= threshold_yi:
            positive_days += 1
        else:
            break

    if today_net_yi < 0:
        direction = 'south'
        strength = 0
        reason = f'North outflow {today_net_yi:.0f}B (3-day {total_3day:.0f}B)'
    elif positive_days >= 3:
        direction = 'continuous'
        strength = 2
        reason = f'North {positive_days}-day continuous inflow (3-day {total_3day:.0f}B)'
    elif positive_days == 1:
        direction = 'impulse'
        strength = 1
        reason = f'North single-day impulse {today_net_yi:.0f}B (3-day {total_3day:.0f}B)'
    elif positive_days == 2:
        direction = 'impulse'
        strength = 1
        reason = f'North {positive_days}-day inflow (3-day {total_3day:.0f}B)'
    else:
        direction = 'neutral'
        strength = 0
        reason = f'North below threshold ({today_net_yi:.0f}B < {threshold_yi}B)'

    return {
        'direction': direction,
        'days': positive_days,
        'today_yi': round(today_net_yi, 1),
        'trend_yi': round(total_3day, 1),
        'strength': strength,
        'reason': reason,
    }


def record_today_north_from_kamt(kamt_data: dict) -> None:
    if not kamt_data:
        return
    net = kamt_data.get('net_north_cny', 0)
    if net != 0:
        _record_north_flow(net)



class NorthBoundAlertChecker:
    """
    检测北向资金是否出现大幅异动。
    触发条件：
      - 北向净流入/出 > threshold_yi 亿元（默认 100亿）
      - 北向资金方向突然逆转（从净买入→净卖出）
      - 个股北向持股变化 > alert_pct%（默认 5%）
    """

    ALERT_THRESHOLD_YI = 100.0  # 亿元

    def __init__(self, threshold_yi: float = 100.0):
        self.threshold_yi = threshold_yi
        self._prev_kamt: Optional[dict] = None

    def check(self, current_kamt: dict) -> List[dict]:
        """
        检查当前 KAMT 数据是否触发预警。
        Returns: list of alert dicts (empty = no alerts).
        """
        alerts = []
        prev = self._prev_kamt

        if prev is None:
            self._prev_kamt = current_kamt
            return alerts

        curr_net = current_kamt.get('net_north_cny', 0) / 1e8  # 亿
        prev_net = prev.get('net_north_cny', 0) / 1e8

        # 检测1：净流入超阈值
        if abs(curr_net) >= self.threshold_yi:
            if curr_net > 0:
                alerts.append({
                    'type': 'NORTH_FLOW_IN',
                    'label': '北向大幅净流入',
                    'value': curr_net,
                    'unit': '亿元',
                    'msg': f'🚀 北向资金净流入 {curr_net:.1f}亿元（>{self.threshold_yi:.0f}亿阈值）',
                })
            else:
                alerts.append({
                    'type': 'NORTH_FLOW_OUT',
                    'label': '北向大幅净流出',
                    'value': curr_net,
                    'unit': '亿元',
                    'msg': f'🚨 北向资金净流出 {abs(curr_net):.1f}亿元（>{self.threshold_yi:.0f}亿阈值）',
                })

        # 检测2：方向逆转（前净买入→今净卖出，或反之）
        if prev_net > 10 and curr_net < -10:  # 前>10亿净买，今>10亿净卖
            alerts.append({
                'type': 'NORTH_FLOW_REVERSE',
                'label': '北向资金逆转',
                'value': curr_net - prev_net,
                'unit': '亿元',
                'msg': f'⚠️ 北向资金逆转：前日净买入{prev_net:.1f}亿 → 今日净卖出{abs(curr_net):.1f}亿',
            })
        elif prev_net < -10 and curr_net > 10:
            alerts.append({
                'type': 'NORTH_FLOW_REVERSE',
                'label': '北向资金逆转',
                'value': curr_net - prev_net,
                'unit': '亿元',
                'msg': f'⚠️ 北向资金逆转：前日净卖出{abs(prev_net):.1f}亿 → 今日净买入{curr_net:.1f}亿',
            })

        self._prev_kamt = current_kamt
        return alerts
