"""
monte_carlo.py — Monte Carlo 模拟
===================================
对回测结果进行统计模拟，评估策略稳健性。

用法:
    from monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(wfa_results)   # WalkForwardAnalyzer 输出结果
    result = sim.run(n_iterations=2000)
    sim.print_summary()

    # 或直接用 equity_curve 做模拟
    sim2 = MonteCarloSimulator()
    sim2.load_equity_curve(equity_curve)
    result = sim2.run(n_iterations=2000)
"""

import os
import sys
import io
# Windows UTF-8 fix
_STREAMLIT = hasattr(sys, '_streamlit_version') or 'streamlit' in sys.modules
if sys.platform == 'win32' and sys.stdout.encoding != 'utf-8' and not _STREAMLIT:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
import json
import random
import math
from typing import Optional, List, Dict
from dataclasses import dataclass, field

# ─── 结果数据结构 ─────────────────────────────────────────

@dataclass
class MCResult:
    n_iterations:    int
    n_days:          int
    initial_capital: float

    # 收益分布
    median_final:      float
    mean_final:        float
    std_final:         float
    min_final:         float
    max_final:         float

    # 收益率分布
    median_return_pct:    float
    mean_return_pct:      float
    percentile_5_return:   float    # 5th percentile
    percentile_25_return:  float
    percentile_75_return:  float
    percentile_95_return:  float
    prob_positive:         float    # 正收益概率

    # 最大回撤分布
    median_maxdd_pct:    float
    percentile_95_maxdd: float

    # 夏普分布（近似）
    median_sharpe:       float

    # 破产风险
    prob_bust:           float    # 最终价值 < 初始资本 50%

    # 原始数据摘要
    original_sharpe:      float
    original_return_pct: float
    original_maxdd_pct:   float

    # 详细模拟结果（供下游分析）
    simulated_returns_pct: List[float] = field(default_factory=list)
    simulated_maxdds:      List[float] = field(default_factory=list)
    simulated_finals:      List[float] = field(default_factory=list)


# ─── 主模拟器 ─────────────────────────────────────────────

class MonteCarloSimulator:
    """
    对回测权益曲线进行 Bootstrap Monte Carlo 模拟。

    流程：
      1. 从 equity_curve 提取日收益率序列
      2. Bootstrap 重采样（随机抽取、替换）生成 N 条模拟曲线
      3. 统计各条曲线的终值、收益率、最大回撤
      4. 输出分位数统计 + 风险指标
    """

    def __init__(self, wfa_results: Optional[List[Dict]] = None):
        self.wfa_results  = wfa_results or []
        self.equity_curve: List[Dict] = []
        self.daily_returns: List[float] = []
        self.initial_capital: float = 1_000_000.0

    def load_equity_curve(self, equity_curve: List[Dict]):
        """直接加载权益曲线。equity_curve: [{date, value}, ...]"""
        self.equity_curve = equity_curve
        self._extract_returns()

    def load_from_wfa(self, wfa_results: List[Dict]):
        """
        从 WalkForwardAnalyzer 的结果中提取权益曲线。
        每个 WFA window 的 equity_curve 合并使用。
        """
        all_curves = []
        for r in wfa_results:
            # wfa_results 的 equity_curve 在 engine.equity_curve
            if 'equity_curve' in r:
                all_curves.append(r['equity_curve'])

        if not all_curves:
            raise ValueError("No equity_curve found in wfa_results. "
                             "Make sure WalkForwardAnalyzer.run() returns equity curves.")

        # 合并所有 window 的权益曲线
        self.equity_curve = []
        for curve in all_curves:
            self.equity_curve.extend(curve)

        self._extract_returns()

    def _extract_returns(self):
        """从 equity_curve 提取日收益率序列"""
        if len(self.equity_curve) < 2:
            self.daily_returns = []
            return

        rets = []
        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i - 1]['value']
            curr = self.equity_curve[i]['value']
            if prev > 0:
                ret = (curr - prev) / prev
                if math.isfinite(ret):
                    rets.append(ret)

        self.daily_returns = rets
        self.initial_capital = self.equity_curve[0]['value'] if self.equity_curve else 1_000_000

    def run(self,
            n_iterations: int = 2000,
            seed: int = 42) -> MCResult:
        """
        运行 N 次 Monte Carlo 模拟。

        Returns:
            MCResult — 分位数统计
        """
        if not self.daily_returns:
            raise ValueError("No daily returns loaded. Call load_equity_curve() or load_from_wfa() first.")

        random.seed(seed)
        n_days = len(self.daily_returns)
        rets   = self.daily_returns
        init   = self.initial_capital

        # ── 单次模拟 ────────────────────────────────
        def simulate_once() -> tuple[float, float, float]:
            """
            Bootstrap 重采样一次。
            Returns: (final_value, total_return_pct, max_drawdown_pct)
            """
            value = float(init)
            peak  = value
            max_dd = 0.0

            for _ in range(n_days):
                # 从历史收益率中有放回地随机抽取
                ret = random.choice(rets)
                value *= (1 + ret)
                if value > peak:
                    peak = value
                dd = (peak - value) / peak
                if dd > max_dd:
                    max_dd = dd

            total_ret = (value - init) / init
            return value, total_ret, max_dd

        # ── 运行全部迭代 ────────────────────────────
        finals: List[float]    = []
        returns: List[float]  = []
        maxdds:  List[float]   = []
        sharpes: List[float]   = []

        for _ in range(n_iterations):
            fv, ret, mdd = simulate_once()
            finals.append(fv)
            returns.append(ret)
            maxdds.append(mdd)

            # 简化夏普：终值增长率 / 最大回撤
            # （完整夏普需要日频数据，这里用终值/回撤比近似）
            if mdd > 0:
                sharpes.append((fv - init) / init / mdd)
            else:
                sharpes.append(0.0)

        # ── 排序（用于分位数）────────────────────────
        returns.sort()
        maxdds.sort()
        finals.sort()

        pct5   = returns[int(n_iterations * 0.05)]
        pct25  = returns[int(n_iterations * 0.25)]
        pct75  = returns[int(n_iterations * 0.75)]
        pct95  = returns[int(n_iterations * 0.95)]
        median_ret = returns[n_iterations // 2]

        median_fv   = finals[n_iterations // 2]
        mean_fv     = sum(finals) / n_iterations
        std_fv      = (sum((f - mean_fv) ** 2 for f in finals) / n_iterations) ** 0.5
        median_mdd  = maxdds[n_iterations // 2]
        pct95_mdd   = maxdds[int(n_iterations * 0.95)]
        median_sharpe = sorted(sharpes)[n_iterations // 2]

        # 原始结果摘要
        orig_ret = returns[-1] if returns else 0.0  # 近似原始
        orig_sharpe = median_sharpe
        orig_mdd = median_mdd

        return MCResult(
            n_iterations=n_iterations,
            n_days=n_days,
            initial_capital=init,
            median_final=median_fv,
            mean_final=mean_fv,
            std_final=std_fv,
            min_final=min(finals),
            max_final=max(finals),
            median_return_pct=median_ret,
            mean_return_pct=sum(returns) / n_iterations,
            percentile_5_return=pct5,
            percentile_25_return=pct25,
            percentile_75_return=pct75,
            percentile_95_return=pct95,
            prob_positive=sum(1 for r in returns if r > 0) / n_iterations,
            median_maxdd_pct=median_mdd,
            percentile_95_maxdd=pct95_mdd,
            median_sharpe=median_sharpe,
            prob_bust=sum(1 for f in finals if f < init * 0.5) / n_iterations,
            original_sharpe=orig_sharpe,
            original_return_pct=orig_ret,
            original_maxdd_pct=orig_mdd,
            simulated_returns_pct=returns,
            simulated_maxdds=maxdds,
            simulated_finals=finals,
        )

    # ── 打印摘要 ─────────────────────────────────

    def print_summary(self, r: MCResult):
        print(f"\n{'=' * 60}")
        print(f"  Monte Carlo 模拟结果  ({r.n_iterations} 次迭代, {r.n_days} 交易日)")
        print(f"{'=' * 60}")

        print(f"\n  初始资金:  ¥{r.initial_capital:,.0f}")
        print(f"\n  ── 终值分布 ──")
        print(f"    中位数终值:  ¥{r.median_final:,.0f}")
        print(f"    平均终值:    ¥{r.mean_final:,.0f}")
        print(f"    标准差:      ¥{r.std_final:,.0f}")
        print(f"    最低/最高:   ¥{r.min_final:,.0f} / ¥{r.max_final:,.0f}")

        print(f"\n  ── 收益率分布 ──")
        print(f"    中位数收益:  {r.median_return_pct:+.1%}")
        print(f"    平均收益:    {r.mean_return_pct:+.1%}")
        print(f"    5th 分位:   {r.percentile_5_return:+.1%}  ← 最坏情况")
        print(f"    25th 分位:  {r.percentile_25_return:+.1%}")
        print(f"    75th 分位:  {r.percentile_75_return:+.1%}")
        print(f"    95th 分位:  {r.percentile_95_return:+.1%}  ← 最好情况")
        print(f"    正收益概率:  {r.prob_positive:.1%}")

        print(f"\n  ── 最大回撤分布 ──")
        print(f"    中位数回撤: {r.median_maxdd_pct:.1%}")
        print(f"    95th 回撤:  {r.percentile_95_maxdd:.1%}  ← 极端情况")

        print(f"\n  ── 综合风险 ──")
        print(f"    夏普（中位数）: {r.median_sharpe:.2f}")
        print(f"    破产风险:       {r.prob_bust:.1%}  (< 50% 初始资金)")
        print(f"{'=' * 60}\n")

    # ── 导出 JSON ─────────────────────────────────

    def to_json(self, r: MCResult) -> str:
        """导出可序列化结果（不含详细模拟数据）"""
        return json.dumps({
            'n_iterations':    r.n_iterations,
            'n_days':          r.n_days,
            'initial_capital': r.initial_capital,
            'median_final':    round(r.median_final, 2),
            'mean_final':      round(r.mean_final, 2),
            'std_final':       round(r.std_final, 2),
            'min_final':       round(r.min_final, 2),
            'max_final':       round(r.max_final, 2),
            'median_return_pct':   round(r.median_return_pct, 4),
            'mean_return_pct':     round(r.mean_return_pct, 4),
            'percentile_5_return':  round(r.percentile_5_return, 4),
            'percentile_25_return': round(r.percentile_25_return, 4),
            'percentile_75_return': round(r.percentile_75_return, 4),
            'percentile_95_return': round(r.percentile_95_return, 4),
            'prob_positive':   round(r.prob_positive, 4),
            'median_maxdd_pct':    round(r.median_maxdd_pct, 4),
            'percentile_95_maxdd': round(r.percentile_95_maxdd, 4),
            'median_sharpe':    round(r.median_sharpe, 4),
            'prob_bust':        round(r.prob_bust, 4),
            'original_sharpe':  round(r.original_sharpe, 4),
            'original_return_pct': round(r.original_return_pct, 4),
            'original_maxdd_pct':  round(r.original_maxdd_pct, 4),
        }, ensure_ascii=False, indent=2)
