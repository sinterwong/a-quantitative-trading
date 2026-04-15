"""
performance_report.py — 周度绩效归因报告
=======================================
每周一生成，上周绩效归因分析，推送飞书。

内容：
  - 组合收益：总收益 / 年化收益 / 夏普比率 / 最大回撤
  - 信号胜率：RSI vs MACD vs BBANDS 各信号贡献
  - 亏损归因：止损触发次数 / 波动屏蔽次数 / 胜率分析
  - 滑点报告：平均 / P95 / 最大
  - 行业集中度：持仓分布 + 风险提示
  - 参数有效性：上周触发次数最多的信号

用法：
  python scripts/quant/performance_report.py          # 生成上周报告
  python scripts/quant/performance_report.py --days 7  # 最近7天
"""

import os
import sys
import json
import logging
import math
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BK_DIR   = os.path.join(os.path.dirname(THIS_DIR), '..', 'backend')
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, BK_DIR)

_log = logging.getLogger('performance_report')
BASE_URL = 'http://127.0.0.1:5555'


# ─── API 工具 ────────────────────────────────────────────────────────────

def api_get(path: str, timeout: int = 10) -> dict:
    try:
        import urllib.request
        req = urllib.request.Request(f'{BASE_URL}{path}')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning('API GET %s failed: %s', path, e)
        return {}


# ─── 日期工具 ──────────────────────────────────────────────────────────

def get_week_range(anchor: date = None) -> Tuple[date, date]:
    """返回以 anchor 为基准的上一完整交易周（周一~周五）。"""
    if anchor is None:
        anchor = date.today()
    # 找到上一个星期一
    days_since_monday = anchor.weekday()
    last_monday = anchor - timedelta(days=days_since_monday + 7)
    last_friday = last_monday + timedelta(days=4)
    return last_monday, last_friday


def get_date_range(days: int) -> Tuple[date, date]:
    """返回从今天往前推 days 天的日期区间。"""
    end = date.today()
    start = end - timedelta(days=days)
    return start, end


# ─── 数据获取 ──────────────────────────────────────────────────────────

def get_daily_metas(days: int = 30) -> List[dict]:
    """获取最近 days 天的日净值记录。"""
    result = api_get(f'/portfolio/daily?limit={days}')
    return result.get('daily', [])


def get_trades_in_range(start: date, end: date) -> List[dict]:
    """获取指定日期范围内的所有成交。"""
    all_trades = api_get('/trades?limit=500').get('trades', [])
    filtered = []
    for t in all_trades:
        t_date_str = str(t.get('executed_at', ''))[:10]
        if not t_date_str:
            continue
        t_date = date.fromisoformat(t_date_str)
        if start <= t_date <= end:
            filtered.append(t)
    return filtered


def get_signals_in_range(start: date, end: date) -> List[dict]:
    """获取指定日期范围内的所有信号。"""
    all_signals = api_get('/signals?limit=500').get('signals', [])
    filtered = []
    for s in all_signals:
        s_date_str = str(s.get('emitted_at', ''))[:10]
        if not s_date_str:
            continue
        s_date = date.fromisoformat(s_date_str)
        if start <= s_date <= end:
            filtered.append(s)
    return filtered


# ─── 组合指标计算 ──────────────────────────────────────────────────────

def calc_sharpe_ratio(daily_returns: List[float], periods_per_year: int = 252) -> float:
    """给定每日收益率序列，计算年化夏普比率（无风险利率=0）。"""
    if not daily_returns or len(daily_returns) < 2:
        return 0.0
    mean_ret = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_ret) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0
    annual_ret = mean_ret * periods_per_year
    annual_std = std_dev * math.sqrt(periods_per_year)
    return annual_ret / annual_std if annual_std != 0 else 0.0


def calc_max_drawdown(equity_curve: List[float]) -> float:
    """计算最大回撤（百分比）。"""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100


def calc_portfolio_metrics(metas: List[dict], start_date: date, end_date: date) -> dict:
    """
    基于日净值数据计算组合指标。
    只用区间内的数据（start_date ~ end_date）。
    """
    # 过滤出区间内的净值记录
    filtered = []
    for m in metas:
        d_str = m.get('trade_date', '')
        if not d_str:
            continue
        d = date.fromisoformat(d_str)
        if start_date <= d <= end_date:
            filtered.append(m)

    if len(filtered) < 2:
        # 数据不足，用现有数据估算
        if not metas:
            return {'total_return': 0, 'annual_return': 0, 'sharpe': 0, 'max_dd': 0, 'n_days': 0}
        latest = metas[0]
        equity = latest.get('equity', 100000)
        return {
            'total_return': 0,
            'annual_return': 0,
            'sharpe': 0,
            'max_dd': 0,
            'n_days': len(filtered),
            'note': '数据不足，无法计算完整指标'
        }

    # 按日期升序排列
    filtered = sorted(filtered, key=lambda m: m.get('trade_date', ''))

    # 计算每日收益率
    daily_returns = []
    equity_curve = []
    for i, m in enumerate(filtered):
        if i == 0:
            equity_curve.append(m.get('equity', 0))
            continue
        prev_equity = filtered[i - 1].get('equity', 0)
        curr_equity = m.get('equity', 0)
        if prev_equity > 0:
            ret = (curr_equity - prev_equity) / prev_equity
            daily_returns.append(ret)
        equity_curve.append(curr_equity)

    # 初始资金用第一天的 equity
    start_equity = filtered[0].get('equity', 100000)
    end_equity = filtered[-1].get('equity', start_equity)
    total_return = (end_equity - start_equity) / start_equity * 100 if start_equity > 0 else 0

    # 年化收益（按实际天数）
    n_days = len(filtered)
    years = max(n_days / 252, 0.05)  # avoid div by zero, min 5 trading days
    annual_return = (math.pow(end_equity / start_equity, 1 / years) - 1) * 100 if years > 0 and start_equity > 0 else 0

    sharpe = calc_sharpe_ratio(daily_returns)
    max_dd = calc_max_drawdown(equity_curve)

    return {
        'total_return':   round(total_return, 2),
        'annual_return': round(annual_return, 2),
        'sharpe':        round(sharpe, 2),
        'max_dd':        round(max_dd, 2),
        'n_days':        n_days,
        'start_equity':  round(start_equity, 0),
        'end_equity':    round(end_equity, 0),
        'avg_daily_ret': round(sum(daily_returns) / len(daily_returns) * 100, 3) if daily_returns else 0,
    }


# ─── 交易分析 ──────────────────────────────────────────────────────────

def analyze_trades(trades: List[dict]) -> dict:
    """分析一组交易，统计各维度指标。"""
    if not trades:
        return {'total': 0}

    total = len(trades)
    wins = sum(1 for t in trades if (t.get('pnl') or 0) > 0)
    losses = total - wins
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = sum(t.get('pnl') or 0 for t in trades)
    avg_pnl = total_pnl / total if total > 0 else 0

    # 按信号类型分组
    by_signal: Dict[str, dict] = {}
    for t in trades:
        sig = t.get('signal', 'UNKNOWN') or 'UNKNOWN'
        if sig not in by_signal:
            by_signal[sig] = {'trades': 0, 'wins': 0, 'pnl_sum': 0, 'losses': 0}
        by_signal[sig]['trades'] += 1
        pnl = t.get('pnl') or 0
        by_signal[sig]['pnl_sum'] += pnl
        if pnl > 0:
            by_signal[sig]['wins'] += 1
        else:
            by_signal[sig]['losses'] += 1

    for sig, s in by_signal.items():
        n = s['trades']
        s['win_rate'] = s['wins'] / n * 100 if n > 0 else 0
        s['avg_pnl'] = s['pnl_sum'] / n if n > 0 else 0

    # 滑点统计
    slippage_list = [t.get('slippage_bps') or 0 for t in trades if t.get('slippage_bps') is not None]
    slippage_avg = sum(slippage_list) / len(slippage_list) if slippage_list else 0
    slippage_max = max(slippage_list) if slippage_list else 0
    slippage_p95 = sorted(slippage_list)[int(len(slippage_list) * 0.95)] if len(slippage_list) > 1 else (slippage_list[0] if slippage_list else 0)

    # 止损触发统计（pnl < 0 且绝对值较大的）
    stop_loss_trades = [t for t in trades if (t.get('pnl') or 0) < 0]
    big_losses = [t for t in trades if (t.get('pnl') or 0) < -50]  # 亏损超过50元

    return {
        'total': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 2),
        'by_signal': by_signal,
        'slippage_avg': round(slippage_avg, 1),
        'slippage_max': round(slippage_max, 1),
        'slippage_p95': round(slippage_p95, 1),
        'slippage_n': len(slippage_list),
        'stop_loss_trades': len(stop_loss_trades),
        'big_losses': len(big_losses),
    }


# ─── 行业集中度 ────────────────────────────────────────────────────────

def analyze_sector_concentration(trades: List[dict]) -> dict:
    """
    从交易记录中估算行业集中度。
    已知标的 → 使用预定义行业映射；未知 → 标记为 OTHER。
    """
    # 预定义行业映射（可扩展）
    SECTOR_MAP = {
        '600900.SH': '电力',
        '510310.SH': '沪深300',
        '510300.SH': '沪深300',
        '159915.SZ': '创业板',
        '512690.SH': '酒',
        '512800.SH': '银行',
        '512980.SH': '传媒',
        '515050.SH': '5G',
        '159995.SZ': '芯片',
        '512660.SH': '军工',
        '515980.SH': '人工智能',
        '588000.SH': '科创50',
    }

    sector_trades: Dict[str, dict] = {}
    for t in trades:
        sym = t.get('symbol', '')
        sector = SECTOR_MAP.get(sym, 'OTHER')
        if sector not in sector_trades:
            sector_trades[sector] = {'trades': 0, 'pnl_sum': 0, 'wins': 0}
        sector_trades[sector]['trades'] += 1
        pnl = t.get('pnl') or 0
        sector_trades[sector]['pnl_sum'] += pnl
        if pnl > 0:
            sector_trades[sector]['wins'] += 1

    # 计算各行业交易占比
    total = sum(s['trades'] for s in sector_trades.values())
    concentration = []
    for sector, s in sorted(sector_trades.items(), key=lambda x: x[1]['pnl_sum'], reverse=True):
        pct = s['trades'] / total * 100 if total > 0 else 0
        concentration.append({
            'sector': sector,
            'trades': s['trades'],
            'pct': round(pct, 1),
            'pnl_sum': round(s['pnl_sum'], 0),
            'wins': s['wins'],
        })

    return {
        'concentration': concentration,
        'n_sectors': len(sector_trades),
        'dominant_sector': concentration[0]['sector'] if concentration else None,
        'dominant_pct': concentration[0]['pct'] if concentration else 0,
    }


# ─── 亏损归因分析 ──────────────────────────────────────────────────────

def analyze_losses(trades: List[dict], threshold_pnl: float = -10) -> dict:
    """
    亏损归因：
    - 止损触发：pnl < 0 且 reason 包含止损相关关键词
    - 波动屏蔽：PNL ≈ 0（被波动率过滤屏蔽，未开仓）
    - 高频止损：同一标的多次亏损
    """
    losing_trades = [t for t in trades if (t.get('pnl') or 0) < threshold_pnl]

    # 按亏损幅度分层
    mild_loss = sum(1 for t in losing_trades if (t.get('pnl') or 0) >= -50)
    moderate_loss = sum(1 for t in losing_trades if -50 > (t.get('pnl') or 0) >= -200)
    severe_loss = sum(1 for t in losing_trades if (t.get('pnl') or 0) < -200)

    # 找出亏损最大的标的
    sym_losses: Dict[str, float] = {}
    for t in losing_trades:
        sym = t.get('symbol', 'UNKNOWN')
        sym_losses[sym] = sym_losses.get(sym, 0) + abs(t.get('pnl') or 0)

    worst_symbols = sorted(sym_losses.items(), key=lambda x: x[1], reverse=True)[:3]

    return {
        'losing_trades_count': len(losing_trades),
        'mild_loss': mild_loss,
        'moderate_loss': moderate_loss,
        'severe_loss': severe_loss,
        'worst_symbols': [(sym, round(pnl, 0)) for sym, pnl in worst_symbols],
    }


# ─── 格式化报告 ───────────────────────────────────────────────────────

def format_report(
    week_start: date,
    week_end: date,
    portfolio: dict,
    trade_stats: dict,
    sector: dict,
    loss_stats: dict,
    signals: List[dict],
) -> str:
    """将所有分析结果格式化为飞书推送文本。"""

    lines = [
        f"📊 周度绩效归因报告",
        f"{week_start.isoformat()} ~ {week_end.isoformat()}",
        "",
    ]

    # ── 组合表现 ────────────────────────────────────────────────────────
    if portfolio.get('n_days', 0) >= 2:
        lines += [
            "【组合表现】",
            f"  总收益:   {portfolio['total_return']:+.2f}%",
            f"  年化收益: {portfolio['annual_return'] if portfolio.get('n_days', 0) >= 5 else 'N/A (< 5天)'}%",
            f"  夏普比率: {portfolio['sharpe'] if portfolio.get('n_days', 0) >= 10 else 'N/A (< 10天)'}",
            f"  最大回撤: {portfolio['max_dd']:.2f}%",
            f"  日均收益: {portfolio['avg_daily_ret']:+.3f}%",
            f"  运行天数: {portfolio['n_days']}天",
            "",
        ]
    else:
        note = portfolio.get('note', '数据不足')
        lines += [
            "【组合表现】",
            f"  {note}（仅1天数据）",
            "",
        ]

    # ── 交易统计 ────────────────────────────────────────────────────────
    ts = trade_stats
    if ts.get('total', 0) > 0:
        lines += [
            "【交易统计】",
            f"  总交易: {ts['total']}笔  胜率: {ts['win_rate']:.0f}%",
            f"  累计盈亏: {ts['total_pnl']:+.0f}元  场均: {ts['avg_pnl']:+.0f}元",
            "",
        ]

        # 按信号
        by_sig = ts.get('by_signal', {})
        if by_sig:
            sig_lines = ["  信号类型:"]
            for sig, s in sorted(by_sig.items(), key=lambda x: x[1]['pnl_sum'], reverse=True):
                sig_lines.append(
                    f"    {sig}: {s['trades']}笔 WR={s['win_rate']:.0f}% 均值={s['avg_pnl']:+.0f}"
                )
            lines += sig_lines + [""]

    # ── 滑点 ───────────────────────────────────────────────────────────
    if ts.get('slippage_n', 0) > 0:
        lines += [
            "【滑点分析】",
            f"  平均: {ts['slippage_avg']:.1f}bps  P95: {ts['slippage_p95']:.1f}bps  最大: {ts['slippage_max']:.1f}bps",
            "",
        ]

    # ── 行业集中度 ─────────────────────────────────────────────────────
    sc = sector
    if sc.get('concentration'):
        dom = sc['dominant_sector']
        dom_pct = sc['dominant_pct']
        risk_warn = "⚠️ 集中度过高！" if dom_pct > 50 else ""
        lines += [
            "【行业分布】",
            f"  交易覆盖 {sc['n_sectors']} 个行业  集中: {dom} {dom_pct:.0f}% {risk_warn}",
        ]
        for c in sc['concentration'][:4]:
            lines.append(f"    {c['sector']}: {c['trades']}笔 占比{c['pct']:.0f}% 盈亏{c['pnl_sum']:+.0f}")
        lines += [""]

    # ── 亏损归因 ───────────────────────────────────────────────────────
    ls = loss_stats
    if ls.get('losing_trades_count', 0) > 0:
        lines += [
            "【亏损分析】",
            f"  亏损交易: {ls['losing_trades_count']}笔",
            f"    轻度(-50内): {ls['mild_loss']}笔",
            f"    中度(-50~-200): {ls['moderate_loss']}笔",
            f"    重度(-200+): {ls['severe_loss']}笔",
        ]
        if ls.get('worst_symbols'):
            lines.append("  亏损最大标的:")
            for sym, pnl in ls['worst_symbols']:
                lines.append(f"    {sym}: 累计亏损 -{pnl:.0f}元")
        lines += [""]

    # ── 上周活跃信号 ──────────────────────────────────────────────────
    if signals:
        # 统计各信号出现次数
        from collections import Counter
        sig_counter = Counter(s.get('signal', 'UNKNOWN') for s in signals)
        top_signals = sig_counter.most_common(5)
        lines += [
            "【信号概览】（上周）",
            f"  信号总数: {len(signals)}",
        ]
        for sig, cnt in top_signals:
            lines.append(f"    {sig}: {cnt}次")
        lines += [""]

    lines += [
        "─────────────────",
        "生成时间: " + datetime.now().strftime('%Y-%m-%d %H:%M'),
    ]

    return '\n'.join(lines)


# ─── 主程序 ───────────────────────────────────────────────────────────

def generate_report(days: int = None, anchor_date: date = None) -> str:
    """
    生成周报（或自定义天数）文本。
    days=None: 生成上周完整周报（周一~周五）
    days=N: 生成最近N天报告
    """
    if anchor_date is None:
        anchor_date = date.today()

    if days is None:
        # 默认上周
        start_date, end_date = get_week_range(anchor_date)
    else:
        start_date, end_date = get_date_range(days)

    _log.info('Generating performance report for %s ~ %s', start_date, end_date)

    # 1. 获取数据
    all_metas = get_daily_metas(days=60)
    trades = get_trades_in_range(start_date, end_date)
    signals = get_signals_in_range(start_date, end_date)

    _log.info('  Trades: %d, Signals: %d, Metas: %d', len(trades), len(signals), len(all_metas))

    # 2. 计算指标
    portfolio = calc_portfolio_metrics(all_metas, start_date, end_date)
    trade_stats = analyze_trades(trades)
    sector = analyze_sector_concentration(trades)
    loss_stats = analyze_losses(trades)

    # 3. 格式化
    report_text = format_report(
        start_date, end_date,
        portfolio, trade_stats, sector, loss_stats, signals
    )

    return report_text


def run_report(days: int = None) -> str:
    """CLI 入口：生成并打印报告。"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s %(message)s',
    )
    report = generate_report(days=days)
    print(report)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='周度绩效归因报告')
    parser.add_argument('--days', type=int, default=None, help='统计天数（默认上周）')
    args = parser.parse_args()

    report = run_report(days=args.days)
