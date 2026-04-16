"""
report_sender.py — 定时报告生成与推送
======================================
支持：
  - 9:00 早报（隔夜外盘 + 今日关注标的）
  - 15:30 收盘总结（持仓表现 + 今日信号 + 市场涨跌统计）
  - 自定义时间推送

多渠道推送（Channel 抽象层）：
  - 飞书：始终启用（硬编码配置）
  - Telegram：通过环境变量 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 启用
  - 未来扩展：Discord / Email 等

用法：
  python report_sender.py --type morning
  python report_sender.py --type close
  python report_sender.py --type auto
"""

import os
import sys
import io

if sys.platform == 'win32' and sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import json
import ssl
import logging
import urllib.request
import argparse
from datetime import datetime
from typing import Optional

# 禁用代理
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

# ─── 路径设置 ────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/services -> backend -> quant_repo
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))

# ─── 飞书配置 ────────────────────────────────────────────────
FEISHU_APP_ID     = 'cli_a9217a3f3f389cc2'
FEISHU_APP_SECRET = '5kOAKAmFzhySMYQB9nV5ndInIlWS43mt'
FEISHU_USER_ID    = 'ou_b8add658ac094464606af32933a02d0b'

# ─── Backend 配置 ────────────────────────────────────────────
BACKEND_URL = 'http://127.0.0.1:5555'

# ─── Channel 初始化 ────────────────────────────────────────────
from channels import ReportMessage, MessageType, global_manager
from channels.feishu import FeishuChannel

_channels_inited = False


def _init_channels():
    """
    初始化所有渠道（延迟调用，线程安全）。

    飞书：始终启用（硬编码配置）
    Telegram：需要同时设置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 环境变量才启用
    """
    global _channels_inited
    if _channels_inited:
        return
    _channels_inited = True

    gm = global_manager()

    # 飞书（始终注册，主渠道）
    feishu_ch = FeishuChannel(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        default_receive_id=FEISHU_USER_ID,
    )
    gm.register(feishu_ch, primary=True)
    logging.info("[ChannelManager] Feishu registered (primary=True)")

    # Telegram（环境变量控制，可选启用）
    tg_token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    tg_chat  = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if tg_token and tg_chat:
        from channels.telegram import TelegramChannel
        tg_ch = TelegramChannel(bot_token=tg_token, chat_id=tg_chat)
        tg_primary = os.environ.get('TELEGRAM_AS_PRIMARY', '').strip() == '1'
        gm.register(tg_ch, primary=tg_primary)
        logging.info("[ChannelManager] Telegram registered (primary=%s)", tg_primary)
    else:
        logging.info("[ChannelManager] Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable")


def _get_manager():
    _init_channels()
    return global_manager()


# ─── 腾讯实时行情 ────────────────────────────────────────────

def _to_tencent_sym(symbol: str) -> str:
    u = symbol.upper()
    if u.endswith('.SH'): return 'sh' + u[:-3]
    if u.endswith('.SZ'): return 'sz' + u[:-3]
    return symbol.lower()


def get_realtime(symbol: str) -> Optional[dict]:
    sym = _to_tencent_sym(symbol)
    url = f'https://qt.gtimg.cn/q={sym}'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            eq = raw.find('="')
            if eq >= 0: raw = raw[eq+2:]
            f = raw.split('~')
            if len(f) < 40: return None
            return {
                'price':      float(f[3])  if f[3]  not in ('', '-') else 0.0,
                'prev_close': float(f[4])  if f[4]  not in ('', '-') else 0.0,
                'pct':        float(f[32]) if f[32] not in ('', '-') else 0.0,
                'chg':        float(f[31]) if f[31] not in ('', '-') else 0.0,
                'high':       float(f[33]) if len(f) > 33 and f[33] not in ('', '-') else 0.0,
                'low':        float(f[34]) if len(f) > 34 and f[34] not in ('', '-') else 0.0,
                'vol_ratio':  float(f[38]) if len(f) > 38 and f[38] not in ('', '-', '0') else None,
                'volume':     f[36] if len(f) > 36 else '',
                'name':       f[1]  if len(f) > 1 else '',
            }
    except Exception:
        return None


def get_bulk_realtime(symbols: list[str]) -> dict[str, dict]:
    if not symbols: return {}
    syms = [_to_tencent_sym(s) for s in symbols]
    url = f'https://qt.gtimg.cn/q={",".join(syms)}'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            result = {}
            for i, line in enumerate(raw.strip().split('\n')):
                if i >= len(symbols): break
                sym = symbols[i]
                eq = line.find('="')
                if eq >= 0: line = line[eq+2:]
                f = line.split('~')
                if len(f) < 40: continue
                result[sym] = {
                    'price':      float(f[3])  if f[3]  not in ('', '-') else 0.0,
                    'prev_close': float(f[4])  if f[4]  not in ('', '-') else 0.0,
                    'pct':        float(f[32]) if f[32] not in ('', '-') else 0.0,
                    'chg':        float(f[31]) if f[31] not in ('', '-') else 0.0,
                    'high':       float(f[33]) if len(f) > 33 and f[33] not in ('', '-') else 0.0,
                    'low':        float(f[34]) if len(f) > 34 and f[34] not in ('', '-') else 0.0,
                    'vol_ratio':  float(f[38]) if len(f) > 38 and f[38] not in ('', '-', '0') else None,
                    'name':       f[1] if len(f) > 1 else sym,
                }
            return result
    except Exception:
        return {}


def get_limit_pct(symbol: str) -> float:
    s = symbol.upper().replace('.SZ','').replace('.SH','')
    if s.startswith(('ST','*ST','ST*')): return 0.05
    if s.startswith(('300','688')): return 0.20
    return 0.10


# ─── 外盘行情 ────────────────────────────────────────────────

def _fetch_tencent(url: str, timeout: float = 4.0) -> Optional[dict]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            eq = raw.find('="')
            if eq >= 0: raw = raw[eq+2:]
            f = raw.split('~')
            if len(f) < 40: return None
            return {
                'price':      float(f[3])  if f[3]  not in ('', '-') else 0.0,
                'prev_close': float(f[4])  if f[4]  not in ('', '-') else 0.0,
                'pct':        float(f[32]) if f[32] not in ('', '-') else 0.0,
                'chg':        float(f[31]) if f[31] not in ('', '-') else 0.0,
                'name':       f[1] if len(f) > 1 else '',
            }
    except Exception:
        return None


def get_market_overview() -> list:
    items = [
        ('sh000001', '上证指数'),
        ('sh000300', '沪深300'),
        ('sh518880', '黄金ETF'),
    ]
    result = []
    for sym, name in items:
        data = _fetch_tencent(f'https://qt.gtimg.cn/q={sym}')
        if data and data.get('price'):
            pct = data['pct']
            sign = '+' if pct > 0 else ''
            result.append(f"  {name}：{data['price']:.2f}（{sign}{pct:.2f}%）")
    return result


# ─── Backend 数据 ────────────────────────────────────────────

def backend_get(endpoint: str) -> dict:
    url = f'{BACKEND_URL}{endpoint}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# ─── 报告内容生成 ────────────────────────────────────────────

def build_morning_report() -> str:
    """
    生成早报内容（丰富版）：
      1. 调用 scripts.morning_report.build_report() 获取结构化早报
         （市场情绪 + 大盘指数 + 关注标的 + 精选资讯）
      2. 附加持仓开盘参考（report_sender 特有）
    """
    # ── 调用丰富版早报 ───────────────────────────────────────────
    try:
        from morning_report import build_report as _build_rich_report
        report = _build_rich_report(
            include_sentiment=True,
            include_indices=True,
            include_stocks=True,
            include_news=True,
        )
    except Exception as e:
        # Fallback 到简单版（网络异常时）
        import logging
        logging.warning("[report_sender] morning_report.build_report() failed: %s, using fallback", e)
        report = _build_morning_report_fallback()

    # ── 附加持仓开盘参考（追加到报告末尾） ─────────────────────
    portfolio = backend_get('/portfolio/summary')
    positions = portfolio.get('positions', [])
    holding_symbols = [p['symbol'] for p in positions if p.get('shares', 0) > 0]

    append_lines = []
    if holding_symbols:
        snaps = get_bulk_realtime(holding_symbols)
        append_lines.append('━━━ 持仓开盘参考 ━━━')
        for sym in holding_symbols:
            snap = snaps.get(sym, {})
            if snap.get('price'):
                pct = snap.get('pct', 0)
                sign = '+' if pct > 0 else ''
                name = snap.get('name', sym)
                limit_pct = get_limit_pct(sym)
                upper = snap['prev_close'] * (1 + limit_pct)
                append_lines.append(
                    f"  {name}({sym})：{snap['price']:.2f}（{sign}{pct:.2f}%）"
                    f" | 涨停{upper:.2f}"
                )
        append_lines.append('')

    if append_lines:
        report = report + '\n\n' + '\n'.join(append_lines)

    return report


def _build_morning_report_fallback() -> str:
    """当 scripts.morning_report 不可用时的降级版本"""
    now = datetime.now()
    date_str = now.strftime('%Y年%m月%d日（%A）')
    time_str = now.strftime('%H:%M')
    lines = [
        f"🌅 **【早报】{date_str} {time_str}**",
        "",
        "━━━ 市场概况 ━━━",
    ]
    overview = get_market_overview()
    if overview:
        lines.extend(overview)
    else:
        lines.append("  （数据获取失败）")
    lines.append("")
    lines.append("⚠️ 仅供参考，不构成投资建议")
    return '\n'.join(lines)


def build_close_report() -> str:
    now = datetime.now()
    date_str = now.strftime('%Y年%m月%d日')

    lines = [
        f"📉 **【收盘总结】{date_str} {now.strftime('%H:%M')}**",
        "",
        "━━━ 大盘表现 ━━━",
    ]

    indices = {
        'sh000001': '上证指数',
        'sz399001': '深证成指',
        'sz399006': '创业板指',
    }
    snaps = get_bulk_realtime(list(indices.keys()))
    for sym, name in indices.items():
        snap = snaps.get(sym, {})
        if snap.get('price'):
            pct = snap.get('pct', 0)
            sign = '+' if pct > 0 else ''
            lines.append(f"  {name}：{snap['price']:.2f}（{sign}{pct:.2f}%）")

    lines.append("")

    portfolio = backend_get('/portfolio/summary')
    total_equity = portfolio.get('total_equity', 0)
    positions = portfolio.get('positions', [])
    holding = [(p['symbol'], p) for p in positions if p.get('shares', 0) > 0]

    if holding:
        snaps = get_bulk_realtime([s for s, _ in holding])
        lines.append("━━━ 持仓今日表现 ━━━")
        total_pnl = 0.0
        for sym, pos in holding:
            snap = snaps.get(sym, {})
            if snap.get('price'):
                shares = pos.get('shares', 0)
                entry = pos.get('entry_price', 0)
                mv = shares * snap['price']
                cost = shares * entry
                pnl = mv - cost
                total_pnl += pnl
                pct = snap.get('pct', 0)
                sign = '+' if pct > 0 else ''
                limit_pct = get_limit_pct(sym)
                upper = snap['prev_close'] * (1 + limit_pct)
                dist_up = (upper - snap['price']) / snap['price'] if snap['price'] else None
                risk_note = ''
                if pct <= -9.5:
                    risk_note = ' 【跌停风险】'
                elif dist_up is not None and dist_up < 0.02:
                    risk_note = f' 【距涨停{dist_up:.1%}】'
                lines.append(
                    f"  {sym}({shares}股成本¥{entry:.2f})：{snap['price']:.2f}（{sign}{pct:.2f}）"
                    f" 市值¥{mv:,.0f}{' +' if pnl >= 0 else ' '}{pnl:,.0f}{risk_note}"
                )
        lines.append("")
        lines.append(f"  当日浮动盈亏：{'+' if total_pnl >= 0 else ''}¥{total_pnl:,.0f}")
        lines.append(f"  组合总市值：¥{total_equity:,.0f}")
        lines.append("")
    else:
        lines.append("━━━ 持仓：空仓 ━━━")
        lines.append("")

    signals = backend_get('/signals?limit=10')
    sig_list = signals.get('signals', [])
    today = now.strftime('%Y-%m-%d')
    today_signals = [s for s in sig_list if today in s.get('emitted_at', '')]
    if today_signals:
        lines.append("━━━ 今日信号 ━━━")
        for s in today_signals[-5:]:
            sig = s.get('signal', '')
            sym = s.get('symbol', '')
            pct = s.get('pct', 0)
            sign = '+' if pct > 0 else ''
            reason = s.get('reason', '')[:30]
            lines.append(f"  [{sig}] {sym} {sign}{pct:.2f}% - {reason}")
        lines.append("")

    if holding:
        lines.append("━━━ 涨跌停风险 ━━━")
        for sym, pos in holding:
            snap = get_realtime(sym)
            if not snap or not snap.get('price'): continue
            prev = snap['prev_close']
            lpct = get_limit_pct(sym)
            upper = prev * (1 + lpct)
            lower = prev * (1 - lpct)
            dist_up = (upper - snap['price']) / snap['price']
            dist_down = (snap['price'] - lower) / snap['price']
            if dist_up < 0.01:
                lines.append(f"  🔴 {sym} 逼近涨停！")
            elif dist_down < 0.01:
                lines.append(f"  🔴 {sym} 逼近跌停！尽快处理")
            elif dist_up < 0.03:
                lines.append(f"  🟠 {sym} 接近涨停（{dist_up:.1%}）")
            elif dist_down < 0.03:
                lines.append(f"  🟠 {sym} 接近跌停（{dist_down:.1%}）")

    lines.append("")
    lines.append("⚠️ 仅供参考，不构成投资建议")
    return '\n'.join(lines)


# ─── 主入口 ────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description='定时报告推送')
    parser.add_argument('--type', choices=['morning', 'close', 'auto'], default='auto',
                        help='morning=早报, close=收盘总结, auto=根据时间自动判断')
    args = parser.parse_args()

    if args.type == 'auto':
        now = datetime.now()
        cur_min = now.hour * 60 + now.minute
        report_type = 'morning' if cur_min < 15 * 60 + 30 else 'close'
    else:
        report_type = args.type

    if report_type == 'morning':
        content = build_morning_report()
        label = '早报'
    else:
        content = build_close_report()
        label = '收盘总结'

    sys.stdout.write(f"\n【{label}】生成中...\n")
    sys.stdout.write(content[:300] + '\n...\n')
    sys.stdout.flush()

    # 通过 ChannelManager 推送（所有已注册渠道）
    gm = _get_manager()
    enabled = [ch.name for ch in gm.enabled_channels]
    sys.stdout.write(f"[ChannelManager] active channels: {enabled}\n")
    sys.stdout.flush()

    msg = ReportMessage(
        title=f"【{label}】" + datetime.now().strftime('%Y-%m-%d %H:%M'),
        body=content,
        msg_type=MessageType.TEXT,
        tags=[report_type],
    )

    results = gm.send_all(msg)
    ok_channels = [name for name, ok in results.items() if ok]
    fail_channels = [name for name, ok in results.items() if not ok]

    if ok_channels:
        sys.stdout.write(f"[OK] {label}推送成功: {ok_channels}\n")
    if fail_channels:
        sys.stdout.write(f"[WARN] 部分渠道失败: {fail_channels}\n")
    if not ok_channels:
        sys.exit(1)


if __name__ == '__main__':
    main()
