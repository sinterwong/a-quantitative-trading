#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
walkforward_job.py — Walk-Forward 自动训练任务
==============================================
支持两种运行模式：
  python walkforward_job.py                  # 单次运行
  python walkforward_job.py --daemon          # 守护进程模式（每季度自动重训）

训练标的选择逻辑（按优先级）：
1. 有持仓的标的优先重训
2. 宽基 ETF（沪深300、创业板）定期验证
3. 最多同时训练 3 个标的（避免 API 限制）

输出：
  - 控制台打印摘要
  - 结果持久化到 wf_results.db
  - 最新参数写入 Backend，供盘中信号引擎实时调用
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta

# 禁用代理
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

QUANT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quant')
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader
from backtest import BacktestEngine
from walkforward import WalkForwardAnalyzer
from signal_generator import SignalGenerator
from quant.monte_carlo import MonteCarloSimulator
from quant.benchmark import quick_benchmark

# ─── 持久化 ──────────────────────────────────────────────
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend')
sys.path.insert(0, BACKEND_DIR)
try:
    from services.walkforward_persistence import (
        save_wfa_results, get_latest_params, get_wf_summary
    )
    PERSISTENCE_OK = True
except Exception as e:
    PERSISTENCE_OK = False
    print(f"[WARN] persistence not available: {e}")


# ─── 参数网格 ─────────────────────────────────────────────
RSI_PARAM_GRID = {
    'rsi_buy': [25, 30, 35, 40],
    'rsi_sell': [60, 65, 70, 75],
    'stop_loss': [0.05, 0.08, 0.10],
    'take_profit': [0.20, 0.25, 0.30],
}

MACD_PARAM_GRID = {
    'fast': [8, 10, 12],
    'slow': [20, 26, 30],
    'signal': [7, 9, 12],
    'stop_loss': [0.06, 0.08, 0.10],
    'take_profit': [0.20, 0.25, 0.30],
}


# ─── 标的选择 ─────────────────────────────────────────────
def get_symbols_to_train(portfolio_symbols: list = None) -> list:
    """
    返回待训练标的列表，按优先级排序。
    最多返回 3 个（避免 API/速率问题）。
    """
    candidates = set()

    # 1. 已有持仓的标的优先
    if portfolio_symbols:
        candidates.update(portfolio_symbols)

    # 2. 宽基 ETF 定期验证（每 90 天）
    etfs = ['510300.SH', '159915.SZ', '512690.SH']
    for etf in etfs:
        candidates.add(etf)

    # 3. 热门板块代表（AI、半导体、新能源）
    hot_sectors = {
        'AI': '512760.SH',     # AI ETF
        '半导体': '512480.SH',  # 半导体 ETF
        '新能源': '515790.SH',  # 光伏 ETF
    }
    candidates.update(hot_sectors.values())

    # 限制数量
    return list(candidates)[:3]


# ─── 策略信号函数 ──────────────────────────────────────────
def make_signal_func(strategy_type: str):
    """
    根据策略类型返回 signal_func(data, i) -> 'buy'/'sell'/'hold'。
    BacktestEngine.run() 对每个 index i 调用 signal_func(data, i)。
    """

    def rsi_signal(data: list, params: dict):
        closes = [d['close'] for d in data]
        n = len(closes)
        period = 14
        oversold = params.get('rsi_buy', 35)
        overbought = params.get('rsi_sell', 65)

        # 预计算所有 RSI 值（避免每次调用重复计算）
        rsi_vals = [None] * n
        for i in range(period, n):
            segment = closes[i - period:i + 1]
            gains, losses = [], []
            for j in range(1, len(segment)):
                d = segment[j] - segment[j - 1]
                gains.append(d if d > 0 else 0.0)
                losses.append(-d if d < 0 else 0.0)
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            rsi_vals[i] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

        def signal_func(data: list, idx: int):
            if idx < period or rsi_vals[idx] is None or rsi_vals[idx - 1] is None:
                return 'hold'
            rsi = rsi_vals[idx]
            rsi_prev = rsi_vals[idx - 1]
            if rsi_prev < oversold <= rsi:
                return 'buy'
            if rsi_prev < overbought <= rsi:
                return 'sell'
            return 'hold'

        return signal_func

    if strategy_type == 'RSI':
        return rsi_signal
    elif strategy_type == 'MACD':
        # MACD 未实现，退化为 RSI
        return rsi_signal
    else:
        return rsi_signal


# ─── 主训练流程 ────────────────────────────────────────────
def run_walkforward_for_symbol(symbol: str,
                                strategy: str = 'RSI',
                                train_years: int = 2,
                                test_years: int = 1) -> dict:
    """对单个标的运行 Walk-Forward Analysis"""

    print(f"\n{'='*60}")
    print(f"  Symbol: {symbol} | Strategy: {strategy}")
    print(f"  Train: {train_years}y | Test: {test_years}y")
    print(f"{'='*60}")

    # 加载数据
    loader = DataLoader()
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=train_years * 365 + 60)).strftime('%Y%m%d')

    kline = loader.get_kline(symbol, start_date, end_date)
    if len(kline) < train_years * 252:
        print(f"  [SKIP] 数据不足，需要 ~{train_years*252} 天，实际 {len(kline)} 天")
        return None

    print(f"  Data: {kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]} ({len(kline)} days)")

    # 构建参数网格
    param_grid = RSI_PARAM_GRID if strategy == 'RSI' else MACD_PARAM_GRID

    # Walk-Forward
    wfa = WalkForwardAnalyzer(
        data=kline,
        strategy_func=make_signal_func(strategy),
        param_grid=param_grid,
        train_years=train_years,
        test_years=test_years,
    )

    results = wfa.run(
        stop_loss=0.08,
        take_profit=0.25,
        trailing_stop=0.12,
        min_trades=4,
    )

    summary = wfa.summarize(results)

    # 打印摘要
    if summary:
        print(f"\n  [WFA Summary] ({summary['n_windows']} windows):")
        print(f"     Sharpe: {summary['avg_sharpe']:.2f} "
              f"(min={summary['min_sharpe']:.2f}, max={summary['max_sharpe']:.2f})")
        print(f"     Return: {summary['avg_return']:+.1f}%  "
              f"(min={summary['min_return']:+.1f}%, max={summary['max_return']:+.1f}%)")
        print(f"     WinRate: {summary['avg_winrate']:.0f}%  |  "
              f"MaxDD: {summary['max_maxdd']:.1f}%")
        print(f"     正收益窗口: {summary['positive_windows']}/{summary['n_windows']}")
    else:
        print("  [WARN] No valid windows")

    # ── Monte Carlo 模拟（基于所有 window 的权益曲线）──
    mc_result = None
    if results and any('equity_curve' in r for r in results):
        try:
            sim = MonteCarloSimulator()
            sim.load_from_wfa(results)
            mc_result = sim.run(n_iterations=1000)
            sim.print_summary(mc_result)
        except Exception as e:
            print(f"  [WARN] Monte Carlo failed: {e}")

    # ── 沪深300 Benchmark 对比 ─────────────────────────
    bench_result = None
    if results and 'equity_curve' in results[-1]:
        try:
            bench_result = quick_benchmark(results[-1]['equity_curve'], '510310.SH')
            if 'error' not in bench_result:
                print(f"\n  [Benchmark] 沪深300 对比:")
                print(f"     Alpha(年化): {bench_result['alpha_annualized']:+.2%}")
                print(f"     Beta: {bench_result['beta']:.2f}")
                print(f"     信息比率: {bench_result['info_ratio']:.2f}")
                print(f"     跑赢天数: {bench_result['outperformance_days_pct']:.1%}")
                print(f"     策略MaxDD: {bench_result['strategy_maxdd_pct']:.1%}")
                print(f"     沪深300 MaxDD: {bench_result['benchmark_maxdd_pct']:.1%}")
                print(f"     相对MaxDD: {bench_result['relative_maxdd_pct']:+.1%}")
            else:
                print(f"  [WARN] Benchmark: {bench_result['error']}")
        except Exception as e:
            print(f"  [WARN] Benchmark failed: {e}")

    return {
        'symbol': symbol,
        'strategy': strategy,
        'summary': summary,
        'results': results,
        'mc': mc_result,
        'benchmark': bench_result,
    }


# ─── 持久化最新参数到 Backend ────────────────────────────────
def update_backend_params(symbol: str, strategy: str, params: Dict, test_sharpe: float):
    """将最优参数写入 Backend 的参数注册表，供信号引擎实时查询"""
    param_file = os.path.join(BACKEND_DIR, 'services', 'live_params.json')

    live_params = {}
    if os.path.exists(param_file):
        try:
            with open(param_file) as f:
                live_params = json.load(f)
        except:
            pass

    key = f"{symbol}_{strategy}"
    live_params[key] = {
        'symbol': symbol,
        'strategy': strategy,
        'params': params,
        'test_sharpe': test_sharpe,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }

    with open(param_file, 'w') as f:
        json.dump(live_params, f, indent=2)

    print(f"  [OK] Params written to {param_file}")


# ─── 主入口 ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Walk-Forward Training Job')
    parser.add_argument('--symbol', help='指定标的代码（如 600900.SH）')
    parser.add_argument('--strategy', default='RSI', choices=['RSI', 'MACD'])
    parser.add_argument('--train-years', type=int, default=2)
    parser.add_argument('--test-years', type=int, default=1)
    parser.add_argument('--daemon', action='store_true',
                        help='守护进程模式，每季度自动运行')
    args = parser.parse_args()

    print(f"\n[Walk-Forward Job] Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.symbol:
        symbols = [args.symbol]
    else:
        # 从 Backend 获取持仓标的
        try:
            import urllib.request
            with urllib.request.urlopen('http://127.0.0.1:5555/positions', timeout=5) as r:
                data = json.loads(r.read())
                holdings = [p['symbol'] for p in data.get('positions', []) if p.get('shares', 0) > 0]
        except:
            holdings = []
        symbols = get_symbols_to_train(portfolio_symbols=holdings)

    all_results = []
    for sym in symbols:
        try:
            result = run_walkforward_for_symbol(
                symbol=sym,
                strategy=args.strategy,
                train_years=args.train_years,
                test_years=args.test_years,
            )
            if result and result['summary']:
                all_results.append(result)

                # 持久化
                if PERSISTENCE_OK and result['results']:
                    save_wfa_results(
                        symbol=result['symbol'],
                        strategy=result['strategy'],
                        wfa_results=result['results'],
                        train_start='',
                        train_end='',
                        test_start='',
                        test_end='',
                    )
                    # 更新 Backend 实时参数
                    latest = result['results'][-1]
                    update_backend_params(
                        symbol=result['symbol'],
                        strategy=result['strategy'],
                        params=latest.get('_params', {}),
                        test_sharpe=latest.get('sharpe_ratio', 0),
                    )

                time.sleep(3)  # 避免 API 过载

        except Exception as e:
            print(f"  [ERROR] {sym}: {e}")
            import traceback
            traceback.print_exc()

    # 全局摘要
    if all_results:
        print(f"\n{'='*60}")
        print("  ALL SYMBOLS SUMMARY")
        print(f"{'='*60}")
        for r in all_results:
            s = r['summary']
            print(f"  {r['symbol']:12s} | Sharpe={s['avg_sharpe']:+.2f}  "
                  f"Return={s['avg_return']:+.1f}%  WinRate={s['avg_winrate']:.0f}%  "
                  f"MaxDD={s['max_maxdd']:.1f}%  windows={s['n_windows']}")

    print(f"\n[Walk-Forward Job] Done at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == '__main__':
    main()
