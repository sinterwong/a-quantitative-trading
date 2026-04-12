"""
benchmark.py — 基准对比分析
===========================
将策略表现与 CSI 300（沪深300）进行对比。

用法：
    from benchmark import BenchmarkAnalyzer

    analyzer = BenchmarkAnalyzer('510310.SH')  # 沪深300 ETF
    result = analyzer.compare(strategy_equity_curve)
    print(result)
"""

import sys
import os
from datetime import datetime
from typing import List, Dict, Optional

# 禁用代理
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

from data_loader import DataLoader


class BenchmarkAnalyzer:
    """
    基准对比分析器。
    支持：
    - CSI 300 (沪深300) — 510310.SH 或 000300.SH
    - 自定义基准代码
    """

    def __init__(self, benchmark_symbol: str = '510310.SH'):
        self.benchmark_symbol = benchmark_symbol
        self.loader = DataLoader()
        self._benchmark_curve: Optional[List[Dict]] = None

    def load_benchmark(self, start_date: str, end_date: str) -> List[Dict]:
        """加载基准指数历史数据"""
        curve = self.loader.get_kline(self.benchmark_symbol, start_date, end_date)
        if not curve:
            raise ValueError(f"Benchmark data unavailable for {self.benchmark_symbol}")
        self._benchmark_curve = curve
        return curve

    def get_benchmark_returns(self) -> List[float]:
        """从基准曲线提取日收益率"""
        if not self._benchmark_curve:
            raise ValueError("Call load_benchmark() first")
        rets = []
        for i in range(1, len(self._benchmark_curve)):
            prev = self._benchmark_curve[i-1]['close']
            curr = self._benchmark_curve[i]['close']
            if prev > 0:
                rets.append((curr - prev) / prev)
        return rets

    def compare(self,
                strategy_equity_curve: List[Dict],
                strategy_name: str = 'strategy') -> Dict:
        """
        将策略权益曲线与基准对比。

        Returns:
            dict with keys:
              - alpha         : 年化超额收益
              - beta          : 市场敏感度
              - tracking_error: 跟踪误差
              - info_ratio    : 信息比率（alpha / tracking_error）
              - relative_maxdd : 相对最大回撤
              - outperformance_days_pct: 跑赢基准的天数占比
        """
        if not self._benchmark_curve:
            raise ValueError("Call load_benchmark() first")

        # 对齐基准和策略的时间序列
        # 构建 {date: (strat_ret, bench_ret)} 配对
        bench_rets = self.get_benchmark_returns()
        bench_dates = [d['date'][:10] for d in self._benchmark_curve[1:]]

        strat_rets_map = {}
        for i in range(1, len(strategy_equity_curve)):
            d = strategy_equity_curve[i]['date']
            d_key = d[:10] if isinstance(d, str) and len(d) >= 10 else str(d)
            prev_v = strategy_equity_curve[i-1]['value']
            curr_v = strategy_equity_curve[i]['value']
            if prev_v > 0:
                strat_rets_map[d_key] = (curr_v - prev_v) / prev_v

        # 配对
        paired_bench = []
        paired_strat = []
        for i, bd in enumerate(bench_dates):
            if bd in strat_rets_map:
                paired_bench.append(bench_rets[i])
                paired_strat.append(strat_rets_map[bd])

        if len(paired_bench) < 30:
            return {'error': 'Insufficient paired data points', 'n_points': len(paired_bench)}

        # 计算年化超额收益（alpha）
        excess_rets = [s - b for s, b in zip(paired_strat, paired_bench)]
        mean_excess = sum(excess_rets) / len(excess_rets)
        n_days = len(paired_bench)
        years = n_days / 252
        alpha = mean_excess * 252  # 年化 alpha

        # Beta
        mean_bench = sum(paired_bench) / len(paired_bench)
        cov = sum((s - sum(paired_strat)/n_days) * (b - mean_bench)
                  for s, b in zip(paired_strat, paired_bench)) / n_days
        var_bench = sum((b - mean_bench) ** 2 for b in paired_bench) / n_days
        beta = cov / var_bench if var_bench > 0 else 1.0

        # 跟踪误差
        tracking_error = (sum((e - mean_excess) ** 2 for e in excess_rets) / n_days) ** 0.5 * (252 ** 0.5)

        # 信息比率
        info_ratio = alpha / tracking_error if tracking_error > 0 else 0.0

        # 跑赢基准天数
        out_days = sum(1 for e in excess_rets if e > 0)
        out_pct = out_days / len(excess_rets)

        # 相对最大回撤
        bench_peak = self._benchmark_curve[0]['close']
        bench_maxdd = 0.0
        strat_peak = strategy_equity_curve[0]['value']
        strat_maxdd = 0.0

        bench_val = self._benchmark_curve[0]['close']
        strat_val = strategy_equity_curve[0]['value']

        for i, bd in enumerate(bench_dates):
            # 基准
            bench_val = self._benchmark_curve[i+1]['close']
            if bench_val > bench_peak:
                bench_peak = bench_val
            bench_dd = (bench_peak - bench_val) / bench_peak
            if bench_dd > bench_maxdd:
                bench_maxdd = bench_dd
            # 策略
            if bd in strat_rets_map:
                strat_val = strategy_equity_curve[i+1]['value']
                if strat_val > strat_peak:
                    strat_peak = strat_val
                strat_dd = (strat_peak - strat_val) / strat_peak
                if strat_dd > strat_maxdd:
                    strat_maxdd = strat_dd

        relative_maxdd = strat_maxdd - bench_maxdd

        return {
            'benchmark': self.benchmark_symbol,
            'n_days': n_days,
            'years': round(years, 2),
            'alpha_annualized': round(alpha, 4),
            'beta': round(beta, 4),
            'tracking_error': round(tracking_error, 4),
            'info_ratio': round(info_ratio, 4),
            'outperformance_days_pct': round(out_pct, 4),
            'strategy_maxdd_pct': round(strat_maxdd, 4),
            'benchmark_maxdd_pct': round(bench_maxdd, 4),
            'relative_maxdd_pct': round(relative_maxdd, 4),
        }


def quick_benchmark(strategy_curve: List[Dict],
                    benchmark: str = '510310.SH') -> Dict:
    """一行调用：策略 vs 沪深300 Benchmark"""
    dates = [d['date'][:10] if isinstance(d['date'], str) else str(d['date'])
             for d in strategy_curve]
    if len(dates) < 2:
        return {'error': 'Insufficient data'}

    start = dates[0]
    end   = dates[-1]

    analyzer = BenchmarkAnalyzer(benchmark)
    analyzer.load_benchmark(start.replace('-', ''), end.replace('-', ''))
    return analyzer.compare(strategy_curve)
