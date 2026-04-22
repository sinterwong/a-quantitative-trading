"""
core/walkforward.py — Walk-Forward Analysis 引擎（Phase 1 升级版）

改进要点（相对 scripts/quant/walkforward.py）：
1. 使用 core/backtest_engine.py（修复了前视偏差、印花税、Kelly 仓位）
2. 窗口粒度从年切换到月（train_months=18, test_months=6, step_months=6）
3. 2013-2026 数据可产生 ≥10 个滚动窗口（之前只有 1 个）
4. 新增 SensitivityAnalyzer：参数热力图（Sharpe vs RSI oversold/overbought）
5. 统计：各窗口 OOS Sharpe 分布、Sharpe > 0 的比例

用法示例：
    from core.walkforward import WalkForwardAnalyzer, SensitivityAnalyzer

    wfa = WalkForwardAnalyzer(
        df=df,           # pd.DataFrame with OHLCV
        symbol='510300',
        train_months=18,
        test_months=6,
        step_months=6,
    )
    results = wfa.run(factor_class=RSIFactor, param_grid={...})
    summary = wfa.summarize(results)
    SensitivityAnalyzer.plot_heatmap(results, 'outputs/sensitivity_heatmap.png')
"""

from __future__ import annotations

import os
import json
import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import numpy as np
import pandas as pd

from core.backtest_engine import BacktestConfig, BacktestEngine, BacktestResult
from core.factors.base import Factor

logger = logging.getLogger("core.walkforward")

# 确保 outputs/ 目录存在
_OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs')
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


# ─── 数据类 ──────────────────────────────────────────────────────────────────

@dataclass
class WFAWindowResult:
    """单个滚动窗口的结果"""
    window_idx: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    best_params: Dict[str, Any]
    train_sharpe: float
    test_sharpe: float
    test_return: float        # 测试期总收益率
    test_max_drawdown: float  # 测试期最大回撤
    test_win_rate: float
    test_n_trades: int
    test_annual_return: float


@dataclass
class WFASummary:
    """Walk-Forward 分析汇总"""
    n_windows: int
    n_positive_sharpe: int
    positive_sharpe_pct: float       # Sharpe > 0 的窗口比例
    avg_test_sharpe: float
    std_test_sharpe: float
    median_test_sharpe: float
    min_test_sharpe: float
    max_test_sharpe: float
    avg_test_return: float
    avg_max_drawdown: float
    avg_win_rate: float
    windows: List[WFAWindowResult] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"Walk-Forward Analysis 汇总（{self.n_windows} 个窗口）",
            f"  Sharpe > 0 比例:  {self.positive_sharpe_pct*100:.1f}% ({self.n_positive_sharpe}/{self.n_windows})",
            f"  OOS Sharpe:       avg={self.avg_test_sharpe:.3f}  std={self.std_test_sharpe:.3f}  "
            f"median={self.median_test_sharpe:.3f}  [{self.min_test_sharpe:.3f}, {self.max_test_sharpe:.3f}]",
            f"  OOS 年化收益:     avg={self.avg_test_return*100:.2f}%",
            f"  OOS 最大回撤:     avg={self.avg_max_drawdown*100:.2f}%",
            f"  OOS 胜率:         avg={self.avg_win_rate*100:.1f}%",
        ]
        return "\n".join(lines)


# ─── WalkForwardAnalyzer ──────────────────────────────────────────────────────

class WalkForwardAnalyzer:
    """
    Walk-Forward Analysis 引擎（月度窗口版）

    流程：
    1. 将全量数据按 train_months / test_months / step_months 切分
    2. 每个训练窗口：网格搜索参数，按 Sharpe 选最优
    3. 每个测试窗口：用训练最优参数验证 OOS 表现
    4. 汇总：各窗口 OOS Sharpe 分布，正 Sharpe 比例

    窗口示意（train=18m, test=6m, step=6m）：
      W1: train=[2013-01, 2014-06], test=[2014-07, 2014-12]
      W2: train=[2013-07, 2015-01], test=[2015-01, 2015-06]
      ...（步进 6 个月）
    """

    def __init__(
        self,
        df: pd.DataFrame,
        symbol: str,
        train_months: int = 18,
        test_months: int = 6,
        step_months: int = 6,
        config: Optional[BacktestConfig] = None,
    ):
        """
        Args:
            df:            OHLCV 日线数据，DatetimeIndex
            symbol:        标的代码（仅用于记录）
            train_months:  训练窗口长度（月）
            test_months:   测试窗口长度（月）
            step_months:   步进长度（月），通常等于 test_months
            config:        BacktestConfig；默认使用引擎缺省值
        """
        self.df = df.copy()
        self.symbol = symbol
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.config = config or BacktestConfig()

    def _month_offset(self, dt: date, months: int) -> date:
        """date 加 N 个月（保持月末对齐）"""
        m = dt.month - 1 + months
        year = dt.year + m // 12
        month = m % 12 + 1
        # 取该月最后一天
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        day = min(dt.day, last_day)
        return date(year, month, day)

    def _split_windows(self) -> List[Tuple[date, date, date, date]]:
        """生成所有 (train_start, train_end, test_start, test_end) 元组"""
        if self.df.empty:
            return []

        dates = self.df.index
        first_date = dates[0].date() if hasattr(dates[0], 'date') else dates[0]
        last_date = dates[-1].date() if hasattr(dates[-1], 'date') else dates[-1]

        windows = []
        train_start = first_date

        while True:
            train_end = self._month_offset(train_start, self.train_months)
            test_start = train_end
            test_end = self._month_offset(test_start, self.test_months)

            if test_end > last_date:
                break

            windows.append((train_start, train_end, test_start, test_end))
            train_start = self._month_offset(train_start, self.step_months)

        return windows

    def _slice_df(self, start: date, end: date) -> pd.DataFrame:
        """切取 [start, end) 的数据"""
        idx = self.df.index
        mask = (idx >= pd.Timestamp(start)) & (idx < pd.Timestamp(end))
        return self.df.loc[mask]

    def _backtest_with_params(
        self,
        df_slice: pd.DataFrame,
        factor_class: Type[Factor],
        params: Dict[str, Any],
    ) -> Optional[BacktestResult]:
        """用给定参数回测一个数据切片"""
        if df_slice.empty or len(df_slice) < 10:
            return None
        try:
            factor = factor_class(**params)
            factor.set_symbol(self.symbol)
            engine = BacktestEngine(self.config)
            engine.load_data(self.symbol, df_slice)
            engine.add_strategy(factor)
            return engine.run()
        except Exception as e:
            logger.debug(f"backtest failed with params={params}: {e}")
            return None

    def run(
        self,
        factor_class: Type[Factor],
        param_grid: Dict[str, List[Any]],
        min_trades: int = 4,
    ) -> List[WFAWindowResult]:
        """
        执行 Walk-Forward 分析。

        Args:
            factor_class:  因子类（Factor 子类），用 **params 实例化
            param_grid:    参数网格，例如 {'oversold': [20,25,30], 'overbought': [65,70,75]}
            min_trades:    训练期最少交易次数（过少则跳过该参数组合）

        Returns:
            各窗口 WFAWindowResult 列表
        """
        windows = self._split_windows()
        logger.info(f"WFA: {len(windows)} 个滚动窗口, 标的={self.symbol}")

        param_combos = list(self._generate_combos(param_grid))
        results: List[WFAWindowResult] = []

        for i, (tr_start, tr_end, te_start, te_end) in enumerate(windows):
            logger.info(
                f"  窗口 {i+1}/{len(windows)}: "
                f"train=[{tr_start}, {tr_end}), test=[{te_start}, {te_end})"
            )
            df_train = self._slice_df(tr_start, tr_end)
            df_test = self._slice_df(te_start, te_end)

            if df_train.empty or df_test.empty:
                logger.warning(f"  窗口 {i+1}: 数据为空，跳过")
                continue

            # === Phase 1: 训练集网格搜索 ===
            best_params: Dict[str, Any] = {}
            best_train_sharpe = -999.0
            valid_combos = 0

            for params in param_combos:
                res = self._backtest_with_params(df_train, factor_class, params)
                if res is None or res.n_trades < min_trades:
                    continue
                valid_combos += 1
                if res.sharpe > best_train_sharpe:
                    best_train_sharpe = res.sharpe
                    best_params = params

            if not best_params:
                logger.warning(f"  窗口 {i+1}: 无有效参数组合（{valid_combos}/{len(param_combos)}），跳过")
                continue

            logger.info(
                f"  窗口 {i+1} 训练: best_params={best_params}, "
                f"train_sharpe={best_train_sharpe:.3f}"
            )

            # === Phase 2: 测试集 OOS 验证 ===
            res_test = self._backtest_with_params(df_test, factor_class, best_params)
            if res_test is None:
                logger.warning(f"  窗口 {i+1}: 测试集回测失败，跳过")
                continue

            wr = WFAWindowResult(
                window_idx=i + 1,
                train_start=tr_start,
                train_end=tr_end,
                test_start=te_start,
                test_end=te_end,
                best_params=best_params,
                train_sharpe=best_train_sharpe,
                test_sharpe=res_test.sharpe,
                test_return=res_test.total_return,
                test_max_drawdown=res_test.max_drawdown_pct,
                test_win_rate=res_test.win_rate,
                test_n_trades=res_test.n_trades,
                test_annual_return=res_test.annual_return,
            )
            results.append(wr)
            logger.info(
                f"  窗口 {i+1} 测试: sharpe={wr.test_sharpe:.3f}, "
                f"return={wr.test_return*100:.1f}%, trades={wr.test_n_trades}"
            )

        return results

    @staticmethod
    def _generate_combos(param_grid: Dict[str, List[Any]]):
        """生成参数网格的所有组合"""
        keys = list(param_grid.keys())
        for combo in itertools.product(*[param_grid[k] for k in keys]):
            yield dict(zip(keys, combo))

    @staticmethod
    def summarize(results: List[WFAWindowResult]) -> WFASummary:
        """汇总所有窗口的结果"""
        if not results:
            return WFASummary(
                n_windows=0, n_positive_sharpe=0, positive_sharpe_pct=0,
                avg_test_sharpe=0, std_test_sharpe=0, median_test_sharpe=0,
                min_test_sharpe=0, max_test_sharpe=0,
                avg_test_return=0, avg_max_drawdown=0, avg_win_rate=0,
                windows=[],
            )
        sharpes = [r.test_sharpe for r in results]
        n_pos = sum(1 for s in sharpes if s > 0)
        return WFASummary(
            n_windows=len(results),
            n_positive_sharpe=n_pos,
            positive_sharpe_pct=n_pos / len(results),
            avg_test_sharpe=float(np.mean(sharpes)),
            std_test_sharpe=float(np.std(sharpes)),
            median_test_sharpe=float(np.median(sharpes)),
            min_test_sharpe=float(np.min(sharpes)),
            max_test_sharpe=float(np.max(sharpes)),
            avg_test_return=float(np.mean([r.test_return for r in results])),
            avg_max_drawdown=float(np.mean([r.test_max_drawdown for r in results])),
            avg_win_rate=float(np.mean([r.test_win_rate for r in results])),
            windows=results,
        )


# ─── SensitivityAnalyzer ────────────────────────────────────────────────────

class SensitivityAnalyzer:
    """
    参数稳健性检验（Sensitivity Analysis）

    对给定数据集，对两个参数做全量网格搜索，
    输出 Sharpe 热力图（PNG），验证策略是否依赖特定参数峰值。

    合格标准：峰值附近（±5）区间内 Sharpe 仍维持在 50% 以上水平，
    避免出现"单点敏感"（过拟合症状）。
    """

    @staticmethod
    def run(
        df: pd.DataFrame,
        symbol: str,
        factor_class: Type[Factor],
        param_axis1: Tuple[str, List[Any]],   # (param_name, values)
        param_axis2: Tuple[str, List[Any]],   # (param_name, values)
        fixed_params: Optional[Dict[str, Any]] = None,
        config: Optional[BacktestConfig] = None,
    ) -> pd.DataFrame:
        """
        对两个参数轴做网格扫描，返回 Sharpe 热力图 DataFrame。

        Args:
            df:           完整 OHLCV 数据
            symbol:       标的代码
            factor_class: 因子类
            param_axis1:  (参数名, 参数值列表) — 热力图的行
            param_axis2:  (参数名, 参数值列表) — 热力图的列
            fixed_params: 其他固定参数
            config:       BacktestConfig

        Returns:
            pd.DataFrame，行=axis1 值，列=axis2 值，值=Sharpe
        """
        name1, values1 = param_axis1
        name2, values2 = param_axis2
        fixed = fixed_params or {}
        cfg = config or BacktestConfig()

        sharpe_matrix = pd.DataFrame(
            index=values1,
            columns=values2,
            dtype=float,
        )

        for v1 in values1:
            for v2 in values2:
                params = {**fixed, name1: v1, name2: v2}
                try:
                    factor = factor_class(**params)
                    factor.set_symbol(symbol)
                    engine = BacktestEngine(cfg)
                    engine.load_data(symbol, df)
                    engine.add_strategy(factor)
                    res = engine.run()
                    sharpe_matrix.loc[v1, v2] = round(res.sharpe, 4)
                except Exception as e:
                    logger.debug(f"Sensitivity scan failed {params}: {e}")
                    sharpe_matrix.loc[v1, v2] = float('nan')

        return sharpe_matrix

    @staticmethod
    def plot_heatmap(
        sharpe_matrix: pd.DataFrame,
        output_path: str,
        title: str = "Parameter Sensitivity (Sharpe)",
        xlabel: str = "Param 2",
        ylabel: str = "Param 1",
    ) -> None:
        """
        将 Sharpe 热力图保存为 PNG。

        若 matplotlib 不可用，则保存为 CSV。
        """
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.colors as mcolors

            fig, ax = plt.subplots(figsize=(10, 7))
            data = sharpe_matrix.values.astype(float)

            # 色阶：红(负) → 白(0) → 绿(正)
            vmax = max(abs(float(np.nanmax(data))), abs(float(np.nanmin(data))), 0.1)
            norm = mcolors.TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
            im = ax.imshow(data, cmap='RdYlGn', norm=norm, aspect='auto')

            ax.set_xticks(range(len(sharpe_matrix.columns)))
            ax.set_xticklabels(sharpe_matrix.columns, rotation=45, ha='right')
            ax.set_yticks(range(len(sharpe_matrix.index)))
            ax.set_yticklabels(sharpe_matrix.index)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.set_title(title)

            # 在格子内标注数值
            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    val = data[i, j]
                    if not np.isnan(val):
                        color = 'black' if abs(val) < vmax * 0.7 else 'white'
                        ax.text(j, i, f'{val:.2f}', ha='center', va='center',
                                fontsize=8, color=color)

            plt.colorbar(im, ax=ax, label='Sharpe Ratio')
            plt.tight_layout()
            plt.savefig(output_path, dpi=120, bbox_inches='tight')
            plt.close(fig)
            logger.info(f"热力图已保存: {output_path}")

        except ImportError:
            # matplotlib 不可用，保存 CSV
            csv_path = output_path.replace('.png', '.csv')
            sharpe_matrix.to_csv(csv_path)
            logger.warning(f"matplotlib 不可用，热力图已保存为 CSV: {csv_path}")

    @staticmethod
    def peak_sensitivity_ratio(sharpe_matrix: pd.DataFrame) -> float:
        """
        计算峰值稳健度：峰值±1格区域的平均 Sharpe / 全局最大 Sharpe。
        > 0.5 认为参数稳健，< 0.3 可能过拟合。
        """
        data = sharpe_matrix.values.astype(float)
        flat = data.flatten()
        finite = flat[np.isfinite(flat)]
        if len(finite) == 0:
            return 0.0
        peak = float(np.nanmax(data))
        if peak <= 0:
            return 0.0
        # 找峰值位置
        idx = np.unravel_index(np.nanargmax(data), data.shape)
        r, c = idx
        rows = slice(max(0, r - 1), min(data.shape[0], r + 2))
        cols = slice(max(0, c - 1), min(data.shape[1], c + 2))
        neighbors = data[rows, cols].flatten()
        neighbors = neighbors[np.isfinite(neighbors)]
        if len(neighbors) == 0:
            return 0.0
        return float(np.mean(neighbors)) / peak
