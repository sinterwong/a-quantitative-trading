"""
tests/test_walkforward.py — Phase 1 Walk-Forward Analysis 验收测试

覆盖：
  1. _split_windows：窗口数量 ≥ 5（给定 13 年数据）
  2. _month_offset：月份偏移正确
  3. _slice_df：数据切片正确
  4. WalkForwardAnalyzer.run：端到端运行，结果结构正确
  5. WalkForwardAnalyzer.summarize：统计摘要字段齐全
  6. SensitivityAnalyzer.run：热力图矩阵维度正确
  7. SensitivityAnalyzer.peak_sensitivity_ratio：稳健度计算
"""

import sys
import os
import traceback
from datetime import datetime, date

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.walkforward import WalkForwardAnalyzer, SensitivityAnalyzer, WFAWindowResult
from core.backtest_engine import BacktestConfig
from core.factors.base import Factor, Signal

# ─── 测试框架 ──────────────────────────────────────────────────────────────────

_passed = 0
_failed = 0
_errors = []


def check(cond: bool, msg: str):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {msg}")
    else:
        _failed += 1
        _errors.append(msg)
        print(f"  FAIL: {msg}")


def section(name: str):
    print(f"\n=== {name} ===")


# ─── 辅助：合成 13 年日线数据 ─────────────────────────────────────────────────

def make_long_bars(years: int = 13) -> pd.DataFrame:
    """生成 years 年的合成日线数据（工作日）"""
    n = years * 252
    idx = pd.date_range('2013-01-02', periods=n, freq='B')
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0003, 0.015, n)
    price = 10.0 * np.exp(np.cumsum(returns))
    df = pd.DataFrame({
        'open':   price * 0.995,
        'high':   price * 1.01,
        'low':    price * 0.985,
        'close':  price,
        'volume': rng.integers(500_000, 2_000_000, n),
    }, index=idx)
    return df


# ─── 辅助：简单 RSI 风格因子（用于 WFA 测试）─────────────────────────────────

class SimpleRSIFactor(Factor):
    """极简 RSI 模拟因子：oversold/overbought 阈值控制信号方向"""
    name = "SimpleRSI"

    def __init__(self, period: int = 14, oversold: int = 35, overbought: int = 65):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        if len(data) < self.period + 1:
            return pd.Series(dtype=float)
        close = data['close']
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(self.period).mean()
        avg_loss = loss.rolling(self.period).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - 100 / (1 + rs)
        # z-score 归一化
        z = self.normalize(rsi.dropna())
        return z

    def signals(self, fv: pd.Series, price: float = 0.0) -> list:
        if len(fv) == 0:
            return []
        latest = fv.iloc[-1]
        if latest < -1.0:
            direction = 'BUY'
        elif latest > 1.0:
            direction = 'SELL'
        else:
            return []
        return [Signal(
            timestamp=datetime.now(),
            symbol=getattr(self, 'symbol', 'TEST'),
            direction=direction,
            price=price,
            strength=min(abs(latest), 1.0),
            factor_name=self.name,
        )]


# ─── Section 1: _split_windows ───────────────────────────────────────────────

section("_split_windows 窗口数量")

df13 = make_long_bars(13)
wfa = WalkForwardAnalyzer(df13, '510300', train_months=18, test_months=6, step_months=6)
windows = wfa._split_windows()

check(len(windows) >= 5, f"13 年数据应产生 ≥5 个窗口，实际={len(windows)}")
check(len(windows) >= 10, f"13 年数据应产生 ≥10 个窗口，实际={len(windows)}")

# 验证每个窗口结构
for i, (tr_s, tr_e, te_s, te_e) in enumerate(windows):
    check(tr_s < tr_e, f"窗口{i+1}: train_start < train_end")
    check(tr_e == te_s, f"窗口{i+1}: train_end == test_start")
    check(te_s < te_e, f"窗口{i+1}: test_start < test_end")

# 验证相邻窗口步进间隔是 step_months
if len(windows) >= 2:
    import calendar
    for i in range(len(windows) - 1):
        diff_months = (
            (windows[i+1][0].year - windows[i][0].year) * 12
            + windows[i+1][0].month - windows[i][0].month
        )
        check(
            diff_months == wfa.step_months,
            f"相邻窗口步进应={wfa.step_months}月，实际={diff_months}月"
        )

# ─── Section 2: _month_offset ────────────────────────────────────────────────

section("_month_offset 月份偏移")

wfa_dummy = WalkForwardAnalyzer(df13, 'X')
d = date(2020, 1, 31)
check(wfa_dummy._month_offset(d, 1) == date(2020, 2, 29), "1月31日 +1月 = 2月29日(2020闰年)")
check(wfa_dummy._month_offset(d, 2) == date(2020, 3, 31), "1月31日 +2月 = 3月31日")
check(wfa_dummy._month_offset(d, 6) == date(2020, 7, 31), "1月31日 +6月 = 7月31日")
check(wfa_dummy._month_offset(d, 12) == date(2021, 1, 31), "1月31日 +12月 = 次年1月31日")

# ─── Section 3: _slice_df ────────────────────────────────────────────────────

section("_slice_df 数据切片")

start = date(2015, 1, 1)
end = date(2015, 7, 1)
sliced = wfa._slice_df(start, end)
check(len(sliced) > 0, "切片结果非空")
check(sliced.index[0] >= pd.Timestamp(start), "切片起始 ≥ start")
check(sliced.index[-1] < pd.Timestamp(end), "切片终止 < end")
check({'open', 'high', 'low', 'close', 'volume'}.issubset(sliced.columns), "切片含必要列")

# ─── Section 4: WalkForwardAnalyzer.run（端到端）────────────────────────────

section("WalkForwardAnalyzer.run 端到端")

param_grid = {
    'period': [14],
    'oversold': [30, 35],
    'overbought': [65, 70],
}

# 使用较短数据（5 年）做快速测试
df5 = make_long_bars(5)
wfa5 = WalkForwardAnalyzer(
    df5, 'TEST',
    train_months=18, test_months=6, step_months=6,
    config=BacktestConfig(initial_equity=100_000, slippage_bps=5),
)
results = wfa5.run(SimpleRSIFactor, param_grid, min_trades=0)

check(isinstance(results, list), "run() 返回 list")
# 5 年数据 18m train + 6m test + 6m step → 至少 3 个窗口
expected_windows = wfa5._split_windows()
check(
    len(results) <= len(expected_windows),
    f"结果窗口数 ≤ 切分窗口数"
)

for r in results:
    check(isinstance(r, WFAWindowResult), f"窗口{r.window_idx} 类型正确")
    check(r.train_start < r.train_end, f"窗口{r.window_idx} train 时间顺序正确")
    check(r.test_start < r.test_end, f"窗口{r.window_idx} test 时间顺序正确")
    check(isinstance(r.best_params, dict), f"窗口{r.window_idx} best_params 是 dict")
    check(-10 < r.test_sharpe < 10, f"窗口{r.window_idx} sharpe 在合理范围")

# ─── Section 5: WalkForwardAnalyzer.summarize ────────────────────────────────

section("WalkForwardAnalyzer.summarize")

# 手动构造结果，不依赖网络
mock_results = [
    WFAWindowResult(
        window_idx=i, train_start=date(2020, 1, 1), train_end=date(2021, 6, 1),
        test_start=date(2021, 6, 1), test_end=date(2021, 12, 1),
        best_params={'period': 14}, train_sharpe=0.8,
        test_sharpe=s, test_return=0.05, test_max_drawdown=0.08,
        test_win_rate=0.55, test_n_trades=10, test_annual_return=0.10,
    )
    for i, s in enumerate([0.5, -0.2, 0.8, 0.3, -0.1, 1.2, 0.6])
]

summary = WalkForwardAnalyzer.summarize(mock_results)
check(summary.n_windows == 7, f"n_windows=7, 实际={summary.n_windows}")
check(summary.n_positive_sharpe == 5, f"正 Sharpe 窗口=5, 实际={summary.n_positive_sharpe}")
check(abs(summary.positive_sharpe_pct - 5/7) < 1e-9, f"positive_sharpe_pct=5/7")
check(abs(summary.avg_test_sharpe - np.mean([r.test_sharpe for r in mock_results])) < 1e-9,
      "avg_test_sharpe 正确")
check(summary.min_test_sharpe < 0, "min_test_sharpe < 0（存在亏损窗口）")
check(summary.max_test_sharpe > 1, "max_test_sharpe > 1")
check(len(summary.windows) == 7, "windows 列表长度=7")

# 空结果不崩溃
empty_summary = WalkForwardAnalyzer.summarize([])
check(empty_summary.n_windows == 0, "空结果 n_windows=0")

# ─── Section 6: SensitivityAnalyzer.run ──────────────────────────────────────

section("SensitivityAnalyzer.run 热力图矩阵")

df3 = make_long_bars(3)
oversold_vals = [25, 30, 35]
overbought_vals = [65, 70, 75]
matrix = SensitivityAnalyzer.run(
    df3, 'TEST',
    SimpleRSIFactor,
    param_axis1=('oversold', oversold_vals),
    param_axis2=('overbought', overbought_vals),
    fixed_params={'period': 14},
    config=BacktestConfig(initial_equity=100_000, slippage_bps=5),
)

check(isinstance(matrix, pd.DataFrame), "返回 pd.DataFrame")
check(matrix.shape == (3, 3), f"矩阵形状=(3,3), 实际={matrix.shape}")
check(list(matrix.index) == oversold_vals, f"行索引 = oversold_vals")
check(list(matrix.columns) == overbought_vals, f"列索引 = overbought_vals")

# ─── Section 7: peak_sensitivity_ratio ──────────────────────────────────────

section("SensitivityAnalyzer.peak_sensitivity_ratio")

# 全部相等 → 比例 = 1.0（完全稳健）
uniform = pd.DataFrame([[0.5, 0.5, 0.5], [0.5, 0.5, 0.5], [0.5, 0.5, 0.5]],
                        index=[1,2,3], columns=[1,2,3])
ratio_uniform = SensitivityAnalyzer.peak_sensitivity_ratio(uniform)
check(abs(ratio_uniform - 1.0) < 1e-9, f"均匀矩阵稳健度=1.0，实际={ratio_uniform:.3f}")

# 峰值在中央，四周接近 0 → 不稳健
peaked = pd.DataFrame(
    [[0.01, 0.01, 0.01],
     [0.01, 1.00, 0.01],
     [0.01, 0.01, 0.01]],
    index=[1,2,3], columns=[1,2,3]
)
ratio_peaked = SensitivityAnalyzer.peak_sensitivity_ratio(peaked)
check(ratio_peaked < 0.5, f"峰值矩阵稳健度 < 0.5，实际={ratio_peaked:.3f}")

# 全负 → 返回 0（峰值 ≤ 0 时）
neg = pd.DataFrame([[-0.5, -0.3], [-0.2, -0.1]], index=[1,2], columns=[1,2])
ratio_neg = SensitivityAnalyzer.peak_sensitivity_ratio(neg)
check(ratio_neg == 0.0, f"全负矩阵稳健度=0.0，实际={ratio_neg}")

# ─── Summary ─────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"WalkForward Phase 1: {_passed} passed, {_failed} failed")
if _errors:
    for e in _errors:
        print(f"  ✗ {e}")
if _failed > 0:
    sys.exit(1)
