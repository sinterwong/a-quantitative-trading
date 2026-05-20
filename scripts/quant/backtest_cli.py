#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/quant/backtest_cli.py — 回测 CLI 薄壳

用法:
  python backtest_cli.py single <SYMBOL> [选项]
  python backtest_cli.py wf     <SYMBOL> [选项]

命令:
  single  单次回测（RSI 因子，核心引擎）
  wf      Walk-Forward 滚动窗口验证

示例:
  python backtest_cli.py single 600519.SH --start 2022-01-01 --end 2024-12-31
  python backtest_cli.py wf 510310.SH --train-months 18 --test-months 6
"""

import argparse
import json
import os
import sys

# 把项目根加入 path
PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)


def _parse_args():
    parser = argparse.ArgumentParser(description='Backtest CLI')
    parser.add_argument('command', choices=['single', 'wf'],
                        help='single | wf')
    parser.add_argument('symbol', nargs='?', default='510310.SH')
    parser.add_argument('--start', default=None, help='开始日期 YYYY-MM-DD')
    parser.add_argument('--end', default=None, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=730, help='回测天数（无 --start 时）')
    parser.add_argument('--capital', type=float, default=200_000, help='初始资金')
    parser.add_argument('--commission', type=float, default=0.0003, help='佣金率')
    parser.add_argument('--rsi-buy', type=float, default=35, help='RSI 超卖阈值')
    parser.add_argument('--rsi-sell', type=float, default=65, help='RSI 超买阈值')
    parser.add_argument('--rsi-period', type=int, default=14, help='RSI 周期')
    parser.add_argument('--train-months', type=int, default=18, help='WF 训练窗口（月）')
    parser.add_argument('--test-months', type=int, default=6, help='WF 测试窗口（月）')
    parser.add_argument('--output', default=None, help='结果 JSON 输出路径')
    return parser.parse_args()


def run_single(args) -> dict:
    from core.use_cases.backtest import BacktestRequest, StrategySpec, run_backtest

    req = BacktestRequest(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        days=args.days,
        initial_equity=args.capital,
        commission_rate=args.commission,
        strategies=[
            StrategySpec(
                factor_name='RSIFactor',
                threshold=0.0,
                params={
                    'period': args.rsi_period,
                    'buy_threshold': args.rsi_buy,
                    'sell_threshold': args.rsi_sell,
                },
            )
        ],
    )
    resp = run_backtest(req)
    result = resp.to_dict()
    print(resp.summary_text)
    return result


def run_wf(args) -> dict:
    from core.walkforward import WalkForwardAnalyzer
    from core.data_gateway import get_gateway

    print(f"  拉取 {args.symbol} K 线…")
    gw = get_gateway()
    df = gw.kline(args.symbol, interval='daily', days=3650, limit=3650)
    if df is None or df.empty:
        print("  [ERROR] 无法获取 K 线数据")
        return {}

    from core.data_gateway import normalize_kline_index
    df = normalize_kline_index(df)
    if args.start:
        df = df[df.index >= args.start]
    if args.end:
        df = df[df.index <= args.end]

    from core.factors.rsi import RSIFactor
    wfa = WalkForwardAnalyzer(
        df=df,
        symbol=args.symbol,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.test_months,
    )
    param_grid = {
        'period': [14],
        'buy_threshold': [30, 35, 40],
        'sell_threshold': [60, 65, 70],
    }
    windows = wfa.run(factor_class=RSIFactor, param_grid=param_grid)
    summary = wfa.summarize(windows)
    print(f"  窗口数: {summary.get('n_windows', 0)}  "
          f"OOS Sharpe 均值: {summary.get('mean_oos_sharpe', 0):.3f}  "
          f"Sharpe>0 占比: {summary.get('pct_positive_sharpe', 0):.1%}")
    return summary


def main():
    args = _parse_args()
    print(f"\n{'='*60}")
    print(f"  Backtest CLI | cmd={args.command} | symbol={args.symbol}")
    print(f"{'='*60}")

    import time
    t0 = time.time()

    if args.command == 'single':
        result = run_single(args)
    elif args.command == 'wf':
        result = run_wf(args)
    else:
        result = {}

    if args.output and result:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, default=str, indent=2)
        print(f"\n  [SAVE] {args.output}")

    print(f"\n  [DONE] {time.time() - t0:.1f}s")


if __name__ == '__main__':
    main()
