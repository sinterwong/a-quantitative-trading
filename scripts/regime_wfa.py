"""
regime_wfa.py — P6.2 WFA 参数自适应
=====================================
每月第一个交易日自动运行，对 watchlist 中每个标的执行 Walk-Forward 分析，
比较最优参数与当前 live_params.json 中的参数，若有显著变化则推送飞书审批。

Usage:
  python scripts/regime_wfa.py                    # 完整 WFA 分析 + 飞书通知
  python scripts/regime_wfa.py --dry-run         # 仅分析，不发送通知
  python scripts/regime_wfa.py --symbol 510310.SH  # 仅分析指定标的
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime
from typing import Optional, Dict, List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BK_DIR = os.path.join(os.path.dirname(THIS_DIR), 'backend')
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, os.path.join(THIS_DIR, 'quant'))

_log = logging.getLogger('regime_wfa')
FEISHU_WEBHOOK = os.environ.get('FEISHU_WEBHOOK', '')

# ─── 参数 ──────────────────────────────────────────────────────────────
LIVE_PARAMS_PATH = os.path.join(BK_DIR, 'services', 'live_params.json')
WATCHLIST_URL = 'http://127.0.0.1:5555/watchlist'

# ATR 过滤参数（当前最稳健）
DEFAULT_ATR_THRESHOLD = 0.90

class _RSISignalFunc:
    """
    RSI signal class that pre-computes RSI once via setup(),
    then answers __call__(data, idx) in O(1).
    Compatible with WalkForwardAnalyzer strategy_func interface:
      strategy_func(data, params) returns an RSISignalFunc instance.
    """
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period', 'rsi_vals')

    def __init__(self, rsi_buy: float, rsi_sell: float, rsi_period: int = 14):
        self.rsi_buy = float(rsi_buy)
        self.rsi_sell = float(rsi_sell)
        self.rsi_period = rsi_period
        self.rsi_vals: list = None

    def setup(self, data: list):
        """Pre-compute RSI for all bars. Call once per data slice."""
        closes = [d['close'] for d in data]
        n = len(closes)
        period = self.rsi_period
        rsi = [None] * n
        for i in range(period, n):
            gain = loss = 0.0
            for j in range(i - period + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0: gain += d
                else:     loss -= d
            avg_gain = gain / period
            avg_loss = loss / period
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rsi[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.rsi_vals = rsi

    def __call__(self, data: list, idx: int) -> str:
        """Return 'buy'/'sell'/'hold' for bar at idx."""
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




# WFA 参数网格
RSI_BUY_GRID = [20, 25, 30, 35, 40]
RSI_SELL_GRID = [60, 65, 70, 75, 80]
STOP_LOSS_GRID = [0.05]
TAKE_PROFIT_GRID = [0.20]

# 自适应阈值
MIN_SHARPE_IMPROVEMENT = 0.10  # 最小夏普提升要求
AUTO_APPROVE_RSI_DIFF = 5       # RSI 差值 ≤ 5 → 自动批准（小幅调整）


# ─── 数据获取 ─────────────────────────────────────────────────────────

def get_watchlist() -> List[str]:
    try:
        import urllib.request
        req = urllib.request.Request(WATCHLIST_URL)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return [w['symbol'] for w in data.get('watchlist', [])]
    except Exception as e:
        _log.warning('Failed to get watchlist: %s', e)
        return []


def load_live_params() -> dict:
    if os.path.exists(LIVE_PARAMS_PATH):
        with open(LIVE_PARAMS_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_live_params(params: dict):
    os.makedirs(os.path.dirname(LIVE_PARAMS_PATH), exist_ok=True)
    with open(LIVE_PARAMS_PATH, 'w', encoding='utf-8') as f:
        json.dump(params, f, indent=2, ensure_ascii=False)


def get_latest_data(symbol: str, train_years: int = 2, test_years: int = 1) -> Optional[list]:
    """获取日线数据用于 WFA（使用 YYYYMMDD 日期范围）。"""
    try:
        from datetime import date, timedelta
        from quant.data_loader import DataLoader
        today = date.today()
        total_days = train_years * 252 * 2 + test_years * 252 + 120
        end_str = today.strftime('%Y%m%d')
        start_str = (today - timedelta(days=total_days)).strftime('%Y%m%d')
        dl = DataLoader()
        klines = dl.get_kline(symbol, start_str, end_str)
        if klines and len(klines) > 200:
            return klines
        return None
    except Exception as e:
        _log.warning('Failed to get data for %s: %s', symbol, e)
        return None


# ─── WFA 分析 ─────────────────────────────────────────────────────────

def run_wfa_for_symbol(symbol: str, train_years: int = 2, test_years: int = 1) -> Optional[dict]:
    """
    对单个标的运行 WFA，返回最优参数和统计信息。
    """
    try:
        from quant.walkforward import WalkForwardAnalyzer
        from backtest import BacktestEngine, TechnicalIndicators as TI

        klines = get_latest_data(symbol, train_years=train_years, test_years=test_years)
        if not klines:
            return None

        # RSI strategy_func: returns _RSISignalFunc instance (matches backtest_cli pattern)
        def rsi_strategy(data, params):
            sig = _RSISignalFunc(
                rsi_buy=params.get('rsi_buy', 25),
                rsi_sell=params.get('rsi_sell', 65),
                rsi_period=14
            )
            sig.setup(data)
            return sig

        param_grid = {
            'rsi_buy': RSI_BUY_GRID,
            'rsi_sell': RSI_SELL_GRID,
        }

        wfa = WalkForwardAnalyzer(
            data=klines,
            strategy_func=rsi_strategy,
            param_grid=param_grid,
            train_years=train_years,
            test_years=test_years,
        )

        results = wfa.run(stop_loss=0.05, take_profit=0.20, trailing_stop=None, min_trades=4)
        summary = wfa.summarize(results)

        if not summary or summary.get('n_windows', 0) == 0:
            return None

        # 找出最常被选中的 RSI 参数
        from collections import Counter
        param_counter = Counter()
        for r in results:
            p = r.get('_params', {})
            param_counter[(p.get('rsi_buy'), p.get('rsi_sell'))] += 1

        most_common = param_counter.most_common(1)[0]
        best_params = {'rsi_buy': most_common[0][0], 'rsi_sell': most_common[0][1]}
        best_sharpe = summary['avg_sharpe']

        return {
            'symbol': symbol,
            'best_params': best_params,
            'avg_sharpe': round(best_sharpe, 3),
            'win_rate': round(summary.get('win_rate_pct', 0), 1),
            'positive_windows': summary.get('positive_windows', 0),
            'n_windows': summary.get('n_windows', 0),
            'avg_maxdd': round(summary.get('avg_maxdd', 0), 2),
            'param_consistency': round(most_common[1] / summary['n_windows'] * 100, 0),
        }

    except Exception as e:
        _log.error('WFA failed for %s: %s', symbol, e)
        import traceback
        _log.error(traceback.format_exc())
        return None


# ─── 参数比较 ─────────────────────────────────────────────────────────

def compare_params(current: dict, optimized: dict, current_sharpe: float, new_sharpe: float) -> dict:
    """
    比较当前参数和优化后参数，返回变更描述和批准建议。
    """
    curr_rsi = (current.get('rsi_buy'), current.get('rsi_sell'))
    opt_rsi = (optimized.get('rsi_buy'), optimized.get('rsi_sell'))

    rsi_changed = curr_rsi != opt_rsi
    rsi_diff = abs(opt_rsi[0] - curr_rsi[0]) + abs(opt_rsi[1] - curr_rsi[1])

    sharpe_improvement = new_sharpe - current_sharpe

    # 批准决策
    if not rsi_changed:
        decision = 'NO_CHANGE'
        approved = True
    elif sharpe_improvement >= MIN_SHARPE_IMPROVEMENT and rsi_diff <= AUTO_APPROVE_RSI_DIFF:
        decision = 'AUTO_APPROVE'
        approved = True
    elif sharpe_improvement >= MIN_SHARPE_IMPROVEMENT:
        decision = 'REVIEW_REQUIRED'
        approved = False
    elif sharpe_improvement >= 0 and rsi_diff <= 5:
        decision = 'AUTO_APPROVE_MINOR'
        approved = True
    else:
        decision = 'REJECT_NO_IMPROVEMENT'
        approved = False

    return {
        'current_params': current,
        'optimized_params': optimized,
        'rsi_changed': rsi_changed,
        'rsi_diff': rsi_diff,
        'sharpe_improvement': round(sharpe_improvement, 3),
        'current_sharpe': round(current_sharpe, 3),
        'new_sharpe': round(new_sharpe, 3),
        'decision': decision,
        'approved': approved,
    }


# ─── 飞书通知 ─────────────────────────────────────────────────────────

def send_feishu(message: str):
    """发送飞书文本消息。"""
    if not FEISHU_WEBHOOK:
        _log.warning('FEISHU_WEBHOOK not set, skipping notification')
        return

    try:
        import urllib.request
        payload = json.dumps({'msg_type': 'text', 'content': {'text': message}}).encode()
        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            _log.info('Feishu sent: %s', r.read()[:100])
    except Exception as e:
        _log.error('Failed to send Feishu: %s', e)


def format_feishu_report(changes: List[dict], all_results: List[dict]) -> str:
    """格式化飞书通知消息。"""
    lines = [
        'P6.2 参数自适应审查',
        f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        '',
    ]

    approved = [c for c in changes if c['approved']]
    pending = [c for c in changes if not c['approved']]
    no_change = [c for c in changes if c['decision'] == 'NO_CHANGE']

    lines.append(f'分析标的: {len(all_results)}个')
    lines.append(f'待批准变更: {len(pending)}个  已自动批准: {len(approved)}个  无变化: {len(no_change)}个')
    lines.append('')

    if pending:
        lines.append('【需人工审批】')
        for c in pending:
            lines.append(f"  {c['symbol']}: RSI({c['current_params'].get('rsi_buy')}/{c['current_params'].get('rsi_sell')}) -> RSI({c['optimized_params'].get('rsi_buy')}/{c['optimized_params'].get('rsi_sell')})")
            lines.append(f"    夏普: {c['current_sharpe']:.3f} -> {c['new_sharpe']:.3f} ({c['sharpe_improvement']:+.3f})")
            lines.append(f"    原因: {'Sharpe 提升 ' + str(c['sharpe_improvement']) + ' ≥ ' + str(MIN_SHARPE_IMPROVEMENT) if c['sharpe_improvement'] >= MIN_SHARPE_IMPROVEMENT else 'RSI 微调'}")
        lines.append('')

    if approved:
        lines.append('【已自动批准】')
        for c in approved:
            if c['decision'] == 'NO_CHANGE':
                continue
            lines.append(f"  {c['symbol']}: RSI({c['current_params'].get('rsi_buy')}/{c['current_params'].get('rsi_sell')}) -> RSI({c['optimized_params'].get('rsi_buy')}/{c['optimized_params'].get('rsi_sell')})")
            lines.append(f"    夏普: {c['current_sharpe']:.3f} -> {c['new_sharpe']:.3f} ({c['sharpe_improvement']:+.3f})")
        lines.append('')

    lines.append('─────────────────')
    lines.append('回复 "批准" 确认所有待审批变更，或回复具体标的编号。')

    return '\n'.join(lines)


# ─── 主程序 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='P6.2 WFA 参数自适应')
    parser.add_argument('--dry-run', action='store_true', help='仅分析，不发送飞书通知')
    parser.add_argument('--symbol', type=str, default=None, help='仅分析指定标的')
    parser.add_argument('--auto-approve', action='store_true', help='自动批准所有变更（跳过人工确认）')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s %(message)s'
    )

    _log.info('P6.2 WFA Parameter Adaptation started')

    # 获取标的列表
    if args.symbol:
        symbols = [args.symbol]
    else:
        symbols = get_watchlist()
        if not symbols:
            symbols = ['510310.SH', '159915.SZ', '600900.SH']

    _log.info('Symbols: %s', symbols)

    # 加载当前参数
    live_params = load_live_params()

    # 对每个标的运行 WFA
    all_results = []
    changes = []

    for sym in symbols:
        _log.info('Running WFA for %s...', sym)
        result = run_wfa_for_symbol(sym)
        if not result:
            _log.warning('WFA failed for %s', sym)
            all_results.append({'symbol': sym, 'error': 'WFA failed'})
            continue

        all_results.append(result)
        key = f'{sym}_RSI'
        current_entry = live_params.get(key, {})
        current_params = current_entry.get('params', {})
        current_sharpe = current_entry.get('test_sharpe', 0)

        optimized_params = result['best_params']
        new_sharpe = result['avg_sharpe']

        comparison = compare_params(current_params, optimized_params, current_sharpe, new_sharpe)
        comparison['symbol'] = sym
        changes.append(comparison)

        # 输出结果
        decision_icon = {
            'NO_CHANGE': '=',
            'AUTO_APPROVE': 'OK',
            'AUTO_APPROVE_MINOR': 'ok',
            'REVIEW_REQUIRED': '?!',
            'REJECT_NO_IMPROVEMENT': 'X',
        }.get(comparison['decision'], '?')

        _log.info(
            '  %s %s: RSI(%s/%s) -> RSI(%s/%s)  sharpe %s->%s (%s) [%s]',
            decision_icon, sym,
            current_params.get('rsi_buy', '?'), current_params.get('rsi_sell', '?'),
            optimized_params.get('rsi_buy'), optimized_params.get('rsi_sell'),
            current_sharpe, new_sharpe, comparison['sharpe_improvement'],
            comparison['decision']
        )

    # 自动批准并写入
    if args.auto_approve:
        for c in changes:
            if c['approved'] and c['rsi_changed']:
                sym = c['symbol']
                key = f'{sym}_RSI'
                if key not in live_params:
                    live_params[key] = {'symbol': sym, 'strategy': 'RSI'}
                live_params[key]['params'] = {
                    'rsi_buy': c['optimized_params']['rsi_buy'],
                    'rsi_sell': c['optimized_params']['rsi_sell'],
                    'stop_loss': 0.05,
                    'take_profit': 0.20,
                    'atr_threshold': DEFAULT_ATR_THRESHOLD,
                }
                live_params[key]['test_sharpe'] = c['new_sharpe']
                live_params[key]['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
                _log.info('  AUTO-APPROVED: %s RSI(%s/%s)', sym,
                          c['optimized_params']['rsi_buy'], c['optimized_params']['rsi_sell'])

        save_live_params(live_params)
        _log.info('Updated live_params.json')

    # 发送飞书通知（除非 dry-run）
    pending = [c for c in changes if not c['approved'] and c['rsi_changed']]
    if pending and not args.dry_run:
        report = format_feishu_report(changes, all_results)
        send_feishu(report)
    elif not pending:
        _log.info('No parameter changes needed. All good.')

    # 打印汇总
    print()
    print('P6.2 WFA Summary')
    print('=' * 50)
    for r in all_results:
        if 'error' in r:
            print(f"  {r['symbol']}: ERROR - {r['error']}")
        else:
            print(f"  {r['symbol']}: RSI({r['best_params']['rsi_buy']}/{r['best_params']['rsi_sell']})  Sharpe={r['avg_sharpe']:.3f}  WR={r['win_rate']:.0f}%  ({r['n_windows']} windows)")
    print()
    print(f'Pending review: {len(pending)}')
    print(f'All approved/no change: {len(changes) - len(pending)}')

    return changes


if __name__ == '__main__':
    main()
