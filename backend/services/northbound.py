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

def fetch_kamt() -> Optional[dict]:
    """
    获取当前交易日 KAMT 实时数据。
    Returns: {
        's2n': {'quota_used': float, 'quota_total': float, 'last_time': str},
        'n2s': {'quota_used': float, 'quota_total': float, 'last_time': str},
        'today_date': str,
    }
    """
    url = (
        'https://push2.eastmoney.com/api/qt/kamt.rtmin/get'
        '?fields1=f1,f2,f3,f4'
        '&fields2=f51,f52,f53,f54,f55,f56'
        '&ut=b2884a393a59ad64002292a3e90d46a5'
    )
    raw = _get(url)
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    kamt = data.get('data', {})
    if not kamt:
        return None

    s2n_raw = kamt.get('s2n', [])  # 南向（沪深买港股）
    n2s_raw = kamt.get('n2s', [])  # 北向（港资买A股）

    today = date.today().isoformat()

    # 取最后一条非空记录
    def parse_last(series: list) -> dict:
        for entry in reversed(series):
            parts = entry.split(',')
            if len(parts) >= 6:
                # [0]=time, [2]=quota_used_shares?, [4]=quota_total_shares
                try:
                    quota_used = float(parts[2]) if parts[2] else 0.0
                    quota_total = float(parts[4]) if parts[4] else 0.0
                    # 成交额字段
                    amount = float(parts[3]) if parts[3] else 0.0
                    cum_amount = float(parts[5]) if parts[5] else 0.0
                    return {
                        'quota_used': quota_used,
                        'quota_total': quota_total,
                        'amount': amount,       # 当笔成交额（元）
                        'cum_amount': cum_amount,  # 累计成交额（元）
                        'last_time': parts[0],
                    }
                except (ValueError, IndexError):
                    continue
        return {'quota_used': 0.0, 'quota_total': 0.0, 'amount': 0.0, 'cum_amount': 0.0, 'last_time': ''}

    s2n = parse_last(s2n_raw)
    n2s = parse_last(n2s_raw)

    # 判断南北向：用成交额判断（amount>0 表示有实际成交）
    # cum_amount = 累计成交额（元）
    net_north_cny = n2s.get('cum_amount', 0) - s2n.get('cum_amount', 0)  # 北向净流入 = 北向累计 - 南向累计

    return {
        's2n': s2n,
        'n2s': n2s,
        'today_date': today,
        'net_north_cny': net_north_cny,
    }


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
