"""
afternoon_report.py — P5 收盘自动化
=====================================
15:00 触发：
  1. 查询所有持仓 → 计算浮动盈亏
  2. 查询今日成交 → 计算已实现盈亏
  3. 记录 daily_meta
  4. 生成收盘晚报推送飞书
"""

import os
import sys
import json
import logging
import ssl
from datetime import datetime, date
from typing import Optional

# 避免代理干扰
for _k in list(os.environ.keys()):
    if 'proxy' in _k.lower():
        del os.environ[_k]

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
Q_DIR    = os.path.dirname(THIS_DIR)
BK_DIR   = os.path.join(Q_DIR, 'backend')
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, BK_DIR)

import urllib.request
import urllib.error

_log = logging.getLogger('afternoon_report')
BASE_URL = 'http://127.0.0.1:5555'


# ─── Backend API ───────────────────────────────────────────────────────────

def api_get(path: str) -> dict:
    try:
        req = urllib.request.Request(f'{BASE_URL}{path}')
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning('API GET %s failed: %s', path, e)
        return {}

def api_post(path: str, body: dict = None) -> dict:
    try:
        data = json.dumps(body or {}).encode('utf-8')
        req = urllib.request.Request(
            f'{BASE_URL}{path}', data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning('API POST %s failed: %s', path, e)
        return {}


# ─── 获取日终数据 ─────────────────────────────────────────────────────────

def get_portfolio_snapshot() -> dict:
    """获取收盘时点组合快照。"""
    summary = api_get('/portfolio/summary')
    trades  = api_get('/trades') or {}
    signals = api_get('/signals') or {}

    positions    = summary.get('positions', [])
    cash         = summary.get('cash', 0.0)
    total_equity = summary.get('total_equity', 0.0)
    unrealized    = summary.get('unrealized_pnl', 0.0)
    realized      = summary.get('realized_pnl', 0.0)

    # 今日成交
    today = date.today().isoformat()
    all_trades = trades.get('trades', [])
    today_trades = [
        t for t in all_trades
        if str(t.get('executed_at', '')).startswith(today)
    ]

    # 今日信号
    all_signals = signals.get('signals', [])
    today_signals = [
        s for s in all_signals
        if str(s.get('emitted_at', '')).startswith(today)
    ]

    return {
        'positions':    positions,
        'cash':         cash,
        'total_equity': total_equity,
        'unrealized_pnl': unrealized,
        'realized_pnl':   realized,
        'today_trades':  today_trades,
        'today_signals': today_signals,
    }


# ─── 计算日收益率 ────────────────────────────────────────────────────────

def calculate_daily_return(snapshot: dict) -> dict:
    """
    计算当日收益。
    需要日初净值才能计算准确收益率。
    """
    today = date.today().isoformat()

    # 尝试从 daily_meta 获取日初净值
    metas = api_get('/daily_meta') or {}
    meta_list = metas.get('daily_metas', [])
    today_meta = next((m for m in meta_list if m.get('date') == today), None)

    if today_meta:
        opening_equity = today_meta.get('equity', snapshot['total_equity'])
    else:
        # 找不到日初净值，用昨日收盘权益（近似）
        prev_meta = meta_list[-1] if meta_list else None
        opening_equity = prev_meta.get('equity', snapshot['total_equity']) if prev_meta else snapshot['total_equity']

    closing_equity = snapshot['total_equity']
    daily_pnl   = closing_equity - opening_equity
    daily_ret   = (daily_pnl / opening_equity * 100) if opening_equity > 0 else 0.0

    return {
        'opening_equity': opening_equity,
        'closing_equity': closing_equity,
        'daily_pnl':      daily_pnl,
        'daily_return_pct': daily_ret,
    }


# ─── 持仓浮动盈亏 ──────────────────────────────────────────────────────

def format_positions(positions: list) -> list:
    """格式化持仓信息，包含浮动盈亏。"""
    result = []
    for p in positions:
        shares      = p.get('shares', 0)
        entry_px    = p.get('entry_price', 0)
        latest_px   = p.get('latest_price', 0)
        cost_val    = p.get('cost_value', 0)
        current_val = p.get('current_value', 0)
        unreal_pnl  = p.get('unrealized_pnl', 0)
        unreal_pct  = p.get('unrealized_pnl_pct', 0)

        if shares <= 0:
            continue

        result.append({
            'symbol':    p.get('symbol', ''),
            'shares':   shares,
            'entry':    entry_px,
            'latest':   latest_px,
            'cost':     cost_val,
            'value':    current_val,
            'unreal_pnl': unreal_pnl,
            'unreal_pct': unreal_pct,
        })
    return result


# ─── 记录 daily_meta ──────────────────────────────────────────────────────

def record_daily_meta(snapshot: dict, return_info: dict):
    """将收盘数据写入 daily_meta 表。"""
    today = date.today().isoformat()
    try:
        api_post('/daily_meta', {
            'date':          today,
            'nav':           return_info.get('closing_equity', 0) / 100000,  # 假设净值基值
            'equity':        return_info['closing_equity'],
            'cash':          snapshot['cash'],
            'market_value': return_info['closing_equity'] - snapshot['cash'],
            'notes':         (
                f"closing | unreal={snapshot['unrealized_pnl']:.0f} "
                f"realized={snapshot['realized_pnl']:.0f} "
                f"daily_ret={return_info['daily_return_pct']:+.2f}%"
            ),
        })
        _log.info('daily_meta recorded: equity=%.0f daily_ret=%.2f%%',
                  return_info['closing_equity'], return_info['daily_return_pct'])
    except Exception as e:
        _log.warning('record_daily_meta failed: %s', e)


# ─── 生成收盘晚报文本 ────────────────────────────────────────────────────

def build_closing_report(snapshot: dict, return_info: dict) -> str:
    """生成收盘晚报文本。"""
    lines = [
        f"【收盘晚报】{date.today().isoformat()}",
        f"",
    ]

    # 大盘快照
    equity = return_info['closing_equity']
    daily_ret = return_info['daily_return_pct']
    daily_pnl = return_info['daily_pnl']
    opening_equity = return_info['opening_equity']

    lines.append(f"总权益: {equity:.0f}")
    lines.append(f"今日收益: {daily_pnl:+.0f} ({daily_ret:+.2f}%)")
    lines.append(f"  - 已实现: {snapshot['realized_pnl']:+.0f}")
    lines.append(f"  - 浮动:   {snapshot['unrealized_pnl']:+.0f}")
    lines.append(f"现金: {snapshot['cash']:.0f}")
    lines.append(f"")

    # 持仓状态
    positions = format_positions(snapshot['positions'])
    if positions:
        lines.append(f"持仓 ({len(positions)}只):")
        for p in positions:
            lines.append(
                f"  {p['symbol']} {p['shares']}股 "
                f"成本={p['entry']:.2f} 最新={p['latest']:.2f} "
                f"浮盈={p['unreal_pnl']:+.0f}({p['unreal_pct']:+.1f}%)"
            )
    else:
        lines.append("持仓: 空仓")
    lines.append("")

    # 今日成交
    today_trades = snapshot['today_trades']
    if today_trades:
        lines.append(f"今日成交 ({len(today_trades)}笔):")
        for t in today_trades:
            direction = t.get('direction', '')
            sym = t.get('symbol', '')
            shares = t.get('shares', 0)
            px = t.get('price', 0)
            pnl = t.get('pnl', 0)
            lines.append(
                f"  {direction} {sym} {shares}股 @{px:.2f}"
                f"{' pnl=%+.0f' % pnl if pnl else ''}"
            )
    else:
        lines.append("今日成交: 无")
    lines.append("")

    # 今日信号
    today_sigs = snapshot['today_signals']
    if today_sigs:
        lines.append(f"今日信号 ({len(today_sigs)}个):")
        for s in today_sigs[-5:]:  # 最近5个
            lines.append(
                f"  {s.get('signal','')} {s.get('symbol','')} "
                f"@{s.get('price',0):.2f} reason={s.get('reason','')[:40]}"
            )

    return '\n'.join(lines)


# ─── 推送飞书 ────────────────────────────────────────────────────────────

def feishu_push(text: str):
    """推送文本到飞书。"""
    app_id     = os.environ.get('FEISHU_APP_ID', '')
    app_secret = os.environ.get('FEISHU_APP_SECRET', '')
    if not app_id or not app_secret:
        _log.debug('Feishu not configured, skipping push')
        return

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        token_data = json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode()
        tok_req = urllib.request.Request(token_url, data=token_data,
                                          headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(tok_req, context=ctx, timeout=10) as r:
            token_result = json.loads(r.read())
        token = token_result.get('tenant_access_token', '')
        if not token:
            return

        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id'
        msg_body = {
            'receive_id': 'ou_b8add658ac094464606af32933a02d0b',
            'msg_type': 'text',
            'content': json.dumps({'text': text})
        }
        msg_req = urllib.request.Request(
            msg_url, data=json.dumps(msg_body).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
            method='POST'
        )
        with urllib.request.urlopen(msg_req, context=ctx, timeout=10) as r:
            _log.info('Closing report pushed: %s', r.read()[:100])
    except Exception as e:
        _log.warning('Feishu push failed: %s', e)


# ─── 主入口 ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now()
    _log.info('=== Afternoon Report started at %s ===', now.isoformat())

    # Step 1: 获取组合快照
    _log.info('[Step1] Fetching portfolio snapshot...')
    snapshot = get_portfolio_snapshot()
    _log.info('  Positions: %d, Cash: %.0f, Equity: %.0f',
              len(snapshot['positions']), snapshot['cash'], snapshot['total_equity'])

    # Step 2: 计算日收益
    _log.info('[Step2] Calculating daily return...')
    return_info = calculate_daily_return(snapshot)
    _log.info('  Daily PnL: %+.0f (%.2f%%)',
              return_info['daily_pnl'], return_info['daily_return_pct'])

    # Step 3: 记录 daily_meta
    _log.info('[Step3] Recording daily_meta...')
    record_daily_meta(snapshot, return_info)

    # Step 4: 生成晚报 + 推送
    _log.info('[Step4] Building closing report...')
    report_text = build_closing_report(snapshot, return_info)
    feishu_push(report_text)
    _log.info('Closing report:\n%s', report_text)

    _log.info('=== Afternoon Report completed at %s ===', datetime.now().isoformat())


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    run()
