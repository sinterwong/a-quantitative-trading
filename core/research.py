"""
core/research.py — 因子研究框架

组件：
  1. FactorResearcher: 单因子多参数网格搜索 + IC/IR 分析
  2. WalkForwardAnalyzer: Walk-Forward 滚动验证
  3. FactorICAnalyzer: 因子月度 IC 时序分析（P2-D）
  4. StrategyCorrelationAnalyzer: 多策略每日收益相关矩阵（P2-A）
  5. RegimeBacktestAnalyzer: 按市场 Regime 分层回测分析（P2-E）
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
from datetime import datetime, timedelta, date
import numpy as np
import pandas as pd
import os

from core.backtest_engine import BacktestEngine, BacktestConfig, BacktestResult
from core.factors.base import Factor

_OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs')
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


def _spearman_ic(x: np.ndarray, y: np.ndarray) -> float:
    """计算 Spearman 相关系数（不依赖 scipy）。"""
    if len(x) < 3:
        return 0.0
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    corr = np.corrcoef(rx, ry)[0, 1]
    return float(corr) if not np.isnan(corr) else 0.0


@dataclass
class FactorAnalysisResult:
    """单因子分析结果"""
    factor_name: str
    params: Dict
    train_sharpe: float
    test_sharpe: float
    sharpe_improvement: float      # test - train
    n_trades_train: int
    n_trades_test: int
    ic_train: float               # IC（预测相关性）
    ir_train: float              # IR = IC / std(IC)
    status: str                  # 'promising' | 'stable' | 'rejected'

    def is_promising(self) -> bool:
        return self.test_sharpe > 0.3 and self.sharpe_improvement > -0.2

    def summary(self) -> str:
        return (
            f"{self.factor_name} {self.params}: "
            f"train_sharpe={self.train_sharpe:.3f}, "
            f"test_sharpe={self.test_sharpe:.3f}, "
            f"IC={self.ic_train:.4f}, IR={self.ir_train:.4f}, "
            f"[{self.status}]"
        )


class FactorResearcher:
    """
    单因子多参数网格搜索。

    用法：
      researcher = FactorResearcher()
      result = researcher.research(
          factor_class=RSIFactor,
          data={'TEST': df},
          param_grid={
              'period': [7, 14, 21],
              'buy_threshold': [20, 25, 30],
              'sell_threshold': [70, 75, 80],
          },
          train_years=2,
          test_years=1,
      )
    """

    def __init__(self, min_test_sharpe: float = 0.1):
        self.min_test_sharpe = min_test_sharpe

    def research(
        self,
        factor_class: type,
        data: Dict[str, pd.DataFrame],
        param_grid: Dict,
        train_days: int = 504,   # ~2年
        test_days: int = 252,    # ~1年
        metric: str = 'sharpe',
    ) -> List[FactorAnalysisResult]:
        """
        网格搜索最优参数。
        返回按测试集 Sharpe 排序的结果列表。
        """
        from itertools import product

        # 生成所有参数组合
        keys = list(param_grid.keys())
        values = [param_grid[k] for k in keys]
        combos = list(product(*values))

        results = []
        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                r = self._evaluate_params(factor_class, data, params, train_days, test_days)
                results.append(r)
            except Exception as e:
                print(f"[FactorResearcher] {factor_class.__name__} {params}: error {e}")

        # 按测试集 Sharpe 降序
        results.sort(key=lambda x: x.test_sharpe, reverse=True)
        return results

    def _evaluate_params(
        self,
        factor_class: type,
        data: Dict[str, pd.DataFrame],
        params: Dict,
        train_days: int,
        test_days: int,
    ) -> FactorAnalysisResult:
        """评估一组参数"""
        # 分割训练/测试集
        train_data = {}
        test_data = {}
        for sym, df in data.items():
            if len(df) >= train_days + test_days:
                train_data[sym] = df.iloc[-train_days - test_days:-test_days]
                test_data[sym] = df.iloc[-test_days:]
            else:
                train_data[sym] = df
                test_data[sym] = pd.DataFrame()

        config = BacktestConfig(initial_equity=100_000)

        # 训练集
        engine_train = BacktestEngine(config)
        for sym, df in train_data.items():
            engine_train.load_data(sym, df)
        factor_train = factor_class(**params)
        engine_train.add_strategy(factor_train)
        result_train = engine_train.run()

        # 测试集
        engine_test = BacktestEngine(config)
        for sym, df in test_data.items():
            if not df.empty:
                engine_test.load_data(sym, df)
        factor_test = factor_class(**params)
        engine_test.add_strategy(factor_test)
        result_test = engine_test.run() if test_data else None

        train_sharpe = result_train.sharpe
        test_sharpe = result_test.sharpe if result_test else 0.0

        # IC 计算（简化：用每日收益与信号方向的相关性）
        ic_train = self._compute_ic(result_train)

        # 判断状态
        if test_sharpe >= 0.5:
            status = 'promising'
        elif test_sharpe >= 0.1:
            status = 'stable'
        else:
            status = 'rejected'

        return FactorAnalysisResult(
            factor_name=factor_class.__name__,
            params=params,
            train_sharpe=train_sharpe,
            test_sharpe=test_sharpe,
            sharpe_improvement=test_sharpe - train_sharpe,
            n_trades_train=result_train.n_trades,
            n_trades_test=result_test.n_trades if result_test else 0,
            ic_train=ic_train,
            ir_train=0,  # 简化：IR 需要多期 IC
            status=status,
        )

    def _compute_ic(self, result: BacktestResult) -> float:
        """计算 IC（预测相关性）：信号方向与次日收益的相关性"""
        if len(result.daily_stats) < 10:
            return 0.0
        # 简化：用每日收益的符号与交易方向的相关性
        returns = [s.daily_return for s in result.daily_stats if s.n_trades > 0]
        if not returns:
            return 0.0
        return np.corrcoef(range(len(returns)), returns)[0, 1] if len(returns) > 1 else 0.0


class WalkForwardAnalyzer:
    """
    Walk-Forward 分析。
    滚动窗口：
      train(window) → test(window) → next
    返回每个窗口的结果列表。
    """

    def __init__(self, train_days: int = 504, test_days: int = 21):
        """
        train_days: 训练窗口（默认 504 天 ≈ 2年）
        test_days: 测试窗口（默认 21 天 ≈ 1个月）
        """
        self.train_days = train_days
        self.test_days = test_days

    def analyze(
        self,
        factor: Factor,
        data: Dict[str, pd.DataFrame],
    ) -> List[FactorAnalysisResult]:
        """
        对每个标的执行 WFA。
        返回所有窗口结果。
        """
        all_results = []

        for symbol, df in data.items():
            if len(df) < self.train_days + self.test_days:
                continue

            n_windows = (len(df) - self.train_days) // self.test_days
            for i in range(n_windows):
                train_start = i * self.test_days
                train_end = train_start + self.train_days
                test_start = train_end
                test_end = test_start + self.test_days

                if test_end > len(df):
                    break

                train_df = df.iloc[train_start:train_end]
                test_df = df.iloc[test_start:test_end]

                config = BacktestConfig(initial_equity=100_000)
                engine_train = BacktestEngine(config)
                engine_train.load_data(symbol, train_df)
                engine_train.add_strategy(factor)
                result_train = engine_train.run()

                engine_test = BacktestEngine(config)
                engine_test.load_data(symbol, test_df)
                engine_test.add_strategy(factor)
                result_test = engine_test.run()

                all_results.append(FactorAnalysisResult(
                    factor_name=factor.name,
                    params={},
                    train_sharpe=result_train.sharpe,
                    test_sharpe=result_test.sharpe,
                    sharpe_improvement=result_test.sharpe - result_train.sharpe,
                    n_trades_train=result_train.n_trades,
                    n_trades_test=result_test.n_trades,
                    ic_train=0,
                    ir_train=0,
                    status='stable' if result_test.sharpe > 0 else 'rejected',
                ))

        return all_results

    def aggregate(self, results: List[FactorAnalysisResult]) -> Dict:
        """汇总 WFA 结果"""
        if not results:
            return {}

        sharpes = [r.test_sharpe for r in results]
        n_promising = sum(1 for r in results if r.status == 'promising')

        return {
            'n_windows': len(results),
            'avg_test_sharpe': np.mean(sharpes),
            'median_test_sharpe': np.median(sharpes),
            'sharpe_std': np.std(sharpes),
            'n_promising': n_promising,
            'promising_rate': n_promising / len(results) if results else 0,
            'best_result': max(results, key=lambda r: r.test_sharpe) if results else None,
        }


# ---------------------------------------------------------------------------
# FactorICAnalyzer — 因子月度 IC 时序分析（P2-D）
# ---------------------------------------------------------------------------

@dataclass
class ICTimeseriesResult:
    """因子 IC 时序分析结果"""
    factor_name: str
    monthly_ic: pd.Series          # index=月份字符串 'YYYY-MM'，值=IC
    ic_mean: float
    ic_std: float
    ir: float                      # IC / std(IC)
    ic_positive_rate: float        # IC > 0 的月份比例
    ic_by_regime: Dict[str, float] # {'BULL': x, 'BEAR': y, 'VOLATILE': z, 'CALM': w}

    def summary(self) -> str:
        return (
            f"{self.factor_name}: IC均值={self.ic_mean:.4f}  "
            f"IR={self.ir:.3f}  IC>0占比={self.ic_positive_rate:.1%}"
        )


class FactorICAnalyzer:
    """
    因子月度 IC 时序分析。

    IC = 因子值与下月收益的 Spearman 相关系数。
    分析维度：
      1. 月度 IC 序列（时间稳定性）
      2. IC 在不同 Regime（牛/熊/震荡/平稳）下的均值差异

    用法：
        analyzer = FactorICAnalyzer()
        result = analyzer.analyze(
            factor=RSIFactor(),
            data=df,           # 日线 OHLCV
            regime_series=regime_s,  # 可选，pd.Series[str] index 对齐 data
        )
        analyzer.plot_heatmap([result1, result2], 'outputs/ic_heatmap.png')
    """

    def analyze(
        self,
        factor: Factor,
        data: pd.DataFrame,
        regime_series: Optional[pd.Series] = None,
    ) -> ICTimeseriesResult:
        """
        Parameters
        ----------
        factor : Factor
            待分析的因子
        data : pd.DataFrame
            日线 OHLCV，index 为 datetime
        regime_series : pd.Series, optional
            与 data 对齐的 Regime 标签序列（'BULL'/'BEAR'/'VOLATILE'/'CALM'）
        """
        factor_vals = factor.evaluate(data)
        fwd_return = data['close'].pct_change().shift(-1)

        df_work = pd.DataFrame({
            'factor': factor_vals,
            'fwd_ret': fwd_return,
        }, index=data.index).dropna()

        if regime_series is not None:
            df_work['regime'] = regime_series.reindex(df_work.index).fillna('CALM')
        else:
            df_work['regime'] = 'CALM'

        # 按月分组计算 IC
        df_work['month'] = df_work.index.to_period('M').astype(str)
        monthly_ic: Dict[str, float] = {}

        for month, grp in df_work.groupby('month'):
            if len(grp) < 5:
                continue
            corr = _spearman_ic(grp['factor'].values, grp['fwd_ret'].values)
            monthly_ic[month] = corr

        ic_series = pd.Series(monthly_ic).sort_index()
        ic_mean = float(ic_series.mean()) if len(ic_series) else 0.0
        ic_std = float(ic_series.std()) if len(ic_series) > 1 else 1e-8
        ir = ic_mean / ic_std if ic_std > 1e-8 else 0.0
        ic_pos_rate = float((ic_series > 0).mean()) if len(ic_series) else 0.0

        # 按 Regime 分层 IC
        ic_by_regime: Dict[str, float] = {}
        for regime_label in ('BULL', 'BEAR', 'VOLATILE', 'CALM'):
            grp = df_work[df_work['regime'] == regime_label]
            if len(grp) < 5:
                ic_by_regime[regime_label] = float('nan')
                continue
            ic_by_regime[regime_label] = _spearman_ic(grp['factor'].values, grp['fwd_ret'].values)

        return ICTimeseriesResult(
            factor_name=factor.name,
            monthly_ic=ic_series,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ir=ir,
            ic_positive_rate=ic_pos_rate,
            ic_by_regime=ic_by_regime,
        )

    def analyze_multiple(
        self,
        factors: List[Factor],
        data: pd.DataFrame,
        regime_series: Optional[pd.Series] = None,
    ) -> List[ICTimeseriesResult]:
        """对多个因子批量分析。"""
        return [self.analyze(f, data, regime_series) for f in factors]

    @staticmethod
    def plot_heatmap(
        results: List[ICTimeseriesResult],
        output_path: str = '',
    ) -> None:
        """
        绘制因子 IC 热力图（月份 × 因子）。

        Parameters
        ----------
        results : list of ICTimeseriesResult
        output_path : str
            输出 PNG 路径；默认写到 outputs/factor_ic_heatmap.png
        """
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.colors as mcolors
        except ImportError:
            print("[FactorICAnalyzer] matplotlib 未安装，跳过绘图")
            return

        if not results:
            return

        # 构建矩阵
        all_months = sorted(set(
            m for r in results for m in r.monthly_ic.index
        ))
        factor_names = [r.factor_name for r in results]
        matrix = pd.DataFrame(index=all_months, columns=factor_names, dtype=float)
        for r in results:
            for month, ic in r.monthly_ic.items():
                matrix.loc[month, r.factor_name] = ic

        matrix = matrix.fillna(0.0)

        fig, ax = plt.subplots(figsize=(max(8, len(factor_names) * 2), max(6, len(all_months) * 0.4)))
        norm = mcolors.TwoSlopeNorm(vmin=-0.2, vcenter=0.0, vmax=0.2)
        im = ax.imshow(matrix.values.T, aspect='auto', norm=norm, cmap='RdYlGn')

        ax.set_xticks(range(len(all_months)))
        ax.set_xticklabels(all_months, rotation=90, fontsize=8)
        ax.set_yticks(range(len(factor_names)))
        ax.set_yticklabels(factor_names)
        ax.set_title('Factor IC Heatmap (Monthly × Factor)')
        plt.colorbar(im, ax=ax, label='IC (Spearman)')
        plt.tight_layout()

        if not output_path:
            output_path = os.path.join(_OUTPUTS_DIR, 'factor_ic_heatmap.png')
        plt.savefig(output_path, dpi=120)
        plt.close(fig)
        print(f"[FactorICAnalyzer] 热力图已保存: {output_path}")

    @staticmethod
    def summary_table(results: List[ICTimeseriesResult]) -> pd.DataFrame:
        """
        返回汇总表 DataFrame，列：factor_name / ic_mean / ic_std / ir /
        ic_positive_rate / ic_BULL / ic_BEAR / ic_VOLATILE / ic_CALM
        """
        rows = []
        for r in results:
            row = {
                'factor_name': r.factor_name,
                'ic_mean': round(r.ic_mean, 4),
                'ic_std': round(r.ic_std, 4),
                'ir': round(r.ir, 3),
                'ic_positive_rate': round(r.ic_positive_rate, 3),
            }
            for regime in ('BULL', 'BEAR', 'VOLATILE', 'CALM'):
                row[f'ic_{regime}'] = round(r.ic_by_regime.get(regime, float('nan')), 4)
            rows.append(row)
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# StrategyCorrelationAnalyzer — 策略收益相关性（P2-A）
# ---------------------------------------------------------------------------

@dataclass
class StrategyCorrelationResult:
    """策略相关性分析结果"""
    strategy_names: List[str]
    daily_returns: pd.DataFrame      # 列=策略名，index=日期
    corr_matrix: pd.DataFrame        # 相关矩阵
    max_correlation: float           # 最高两策略相关系数（排除自相关）
    is_diversified: bool             # 最高相关系数 < 阈值（默认 0.4）
    diversification_threshold: float

    def summary(self) -> str:
        lines = ["策略相关性分析:"]
        lines.append(f"  策略数量: {len(self.strategy_names)}")
        lines.append(f"  最高两策略相关系数: {self.max_correlation:.3f}")
        lines.append(f"  Alpha 多样化达标: {'是' if self.is_diversified else '否'} (阈值 {self.diversification_threshold})")
        lines.append("  相关矩阵:")
        for line in str(self.corr_matrix.round(3)).split('\n'):
            lines.append(f"    {line}")
        return '\n'.join(lines)


class StrategyCorrelationAnalyzer:
    """
    多策略每日收益相关性分析。

    用法：
        engine1 = BacktestEngine(...); engine1.add_strategy(rsi_factor)
        engine2 = BacktestEngine(...); engine2.add_strategy(macd_factor)

        analyzer = StrategyCorrelationAnalyzer()
        result = analyzer.analyze({
            'RSI': engine1.run(),
            'MACD': engine2.run(),
        })
        analyzer.plot_heatmap(result, 'outputs/strategy_correlation.png')
    """

    def __init__(self, diversification_threshold: float = 0.4):
        self.diversification_threshold = diversification_threshold

    def analyze(
        self,
        strategy_results: Dict[str, BacktestResult],
    ) -> StrategyCorrelationResult:
        """
        计算多策略日收益相关矩阵。

        Parameters
        ----------
        strategy_results : dict
            {策略名: BacktestResult}
        """
        returns_dict: Dict[str, pd.Series] = {}
        for name, result in strategy_results.items():
            daily_ret = pd.Series(
                [s.daily_return for s in result.daily_stats],
                index=[s.date for s in result.daily_stats],
                name=name,
            )
            returns_dict[name] = daily_ret

        returns_df = pd.DataFrame(returns_dict).dropna(how='all').fillna(0.0)
        corr_matrix = returns_df.corr()

        # 最高非对角线相关系数
        names = list(strategy_results.keys())
        max_corr = 0.0
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                v = float(corr_matrix.loc[names[i], names[j]])
                if not np.isnan(v):
                    max_corr = max(max_corr, abs(v))

        return StrategyCorrelationResult(
            strategy_names=names,
            daily_returns=returns_df,
            corr_matrix=corr_matrix,
            max_correlation=max_corr,
            is_diversified=max_corr < self.diversification_threshold,
            diversification_threshold=self.diversification_threshold,
        )

    @staticmethod
    def plot_heatmap(
        result: StrategyCorrelationResult,
        output_path: str = '',
    ) -> None:
        """绘制相关矩阵热力图并保存。"""
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError:
            print("[StrategyCorrelationAnalyzer] matplotlib 未安装，跳过绘图")
            return

        mat = result.corr_matrix
        n = len(mat)
        fig, ax = plt.subplots(figsize=(max(4, n * 1.5), max(3, n * 1.5)))
        im = ax.imshow(mat.values, vmin=-1, vmax=1, cmap='RdYlGn')

        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        ax.set_xticklabels(mat.columns, rotation=45, ha='right')
        ax.set_yticklabels(mat.index)

        for i in range(n):
            for j in range(n):
                ax.text(j, i, f"{mat.values[i, j]:.2f}",
                        ha='center', va='center', fontsize=10)

        ax.set_title(f'Strategy Return Correlation\n(max pairwise={result.max_correlation:.3f})')
        plt.colorbar(im, ax=ax)
        plt.tight_layout()

        if not output_path:
            output_path = os.path.join(_OUTPUTS_DIR, 'strategy_correlation.png')
        plt.savefig(output_path, dpi=120)
        plt.close(fig)
        print(f"[StrategyCorrelationAnalyzer] 相关矩阵已保存: {output_path}")


# ---------------------------------------------------------------------------
# RegimeBacktestAnalyzer — Regime 分状态回测分析（P2-E）
# ---------------------------------------------------------------------------

@dataclass
class RegimePerformance:
    """单个 Regime 状态下的绩效统计"""
    regime: str
    n_days: int
    n_trades: int
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    avg_daily_return: float
    daily_return_std: float

    def summary_row(self) -> Dict:
        return {
            'Regime': self.regime,
            '交易日数': self.n_days,
            '交易次数': self.n_trades,
            '总收益': f"{self.total_return*100:.2f}%",
            '年化收益': f"{self.annual_return*100:.2f}%",
            'Sharpe': f"{self.sharpe:.3f}",
            '最大回撤': f"{self.max_drawdown*100:.2f}%",
            '胜率': f"{self.win_rate*100:.1f}%",
        }


@dataclass
class RegimeAnalysisResult:
    """Regime 分层回测完整结果"""
    regime_performance: Dict[str, RegimePerformance]   # regime → 绩效
    overall_performance: RegimePerformance              # 全局绩效
    regime_day_counts: Dict[str, int]                  # regime → 天数

    def to_dataframe(self) -> pd.DataFrame:
        rows = [p.summary_row() for p in self.regime_performance.values()]
        rows.append(self.overall_performance.summary_row())
        return pd.DataFrame(rows)

    def print_report(self) -> None:
        print("\n" + "=" * 70)
        print("  Regime 分层回测绩效")
        print("=" * 70)
        df = self.to_dataframe()
        print(df.to_string(index=False))
        print("=" * 70)


class RegimeBacktestAnalyzer:
    """
    按市场 Regime 对回测结果分层统计。

    接受一个已完成的 BacktestResult（含 daily_stats + trades），
    以及对应的 regime_series（日期 → Regime 标签）。
    按 BULL / BEAR / VOLATILE / CALM 分别统计：胜率、Sharpe、最大回撤。

    用法：
        analyzer = RegimeBacktestAnalyzer()
        result = RegimeBacktestAnalyzer.build_regime_series(
            start='2018-01-01', end='2026-01-01'
        )   # 从上证指数离线计算

        analysis = analyzer.analyze(backtest_result, regime_series)
        analysis.print_report()
        df = analysis.to_dataframe()
        df.to_csv('outputs/regime_performance.csv', index=False)
    """

    @staticmethod
    def build_regime_series(
        data: pd.DataFrame,
        ma_short: int = 20,
        ma_long: int = 60,
        atr_period: int = 14,
        atr_lookback: int = 30,
        atr_volatile_threshold: float = 0.85,
    ) -> pd.Series:
        """
        从价格数据（指数或标的本身）计算历史 Regime 序列。

        Parameters
        ----------
        data : pd.DataFrame
            含 close/high/low 列的日线数据
        返回 pd.Series[str]，index 为 data.index
        """
        close = data['close'].values.astype(float)
        high = data['high'].values.astype(float)
        low = data['low'].values.astype(float)

        ma20 = pd.Series(close).rolling(ma_short).mean().values
        ma60 = pd.Series(close).rolling(ma_long).mean().values

        trs = np.maximum(
            high[1:] - low[1:],
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        )
        atr_arr = np.concatenate([[np.nan], pd.Series(trs).rolling(atr_period).mean().values])
        max_atr = pd.Series(atr_arr).rolling(atr_lookback).max().values

        regimes = []
        for i in range(len(close)):
            c, m20, m60 = close[i], ma20[i], ma60[i]
            atr = atr_arr[i]
            mxatr = max_atr[i]
            if np.isnan(m20) or np.isnan(m60):
                regimes.append('CALM')
                continue
            if c > m20 and m20 > m60:
                regimes.append('BULL')
            elif c < m20 and m20 < m60:
                regimes.append('BEAR')
            elif not np.isnan(atr) and not np.isnan(mxatr) and mxatr > 0 and atr / mxatr > atr_volatile_threshold:
                regimes.append('VOLATILE')
            else:
                regimes.append('CALM')

        return pd.Series(regimes, index=data.index, name='regime')

    def analyze(
        self,
        result: BacktestResult,
        regime_series: pd.Series,
    ) -> RegimeAnalysisResult:
        """
        按 Regime 对回测 DailyStats 分层统计。

        Parameters
        ----------
        result : BacktestResult
            BacktestEngine.run() 的返回值
        regime_series : pd.Series
            index 为 date，值为 'BULL'/'BEAR'/'VOLATILE'/'CALM'
        """
        # 构建每日数据 DataFrame
        daily_df = pd.DataFrame([
            {
                'date': s.date,
                'daily_return': s.daily_return,
                'n_trades': s.n_trades,
                'equity': s.equity,
            }
            for s in result.daily_stats
        ]).set_index('date')

        if daily_df.empty:
            empty = RegimePerformance(
                regime='ALL', n_days=0, n_trades=0,
                total_return=0, annual_return=0, sharpe=0,
                max_drawdown=0, win_rate=0, avg_daily_return=0, daily_return_std=0,
            )
            return RegimeAnalysisResult(
                regime_performance={},
                overall_performance=empty,
                regime_day_counts={},
            )

        # 对齐 regime
        daily_df['regime'] = regime_series.reindex(daily_df.index).fillna('CALM')

        # 全局绩效
        overall = self._calc_performance('ALL', daily_df, result.trades)

        # 按 Regime 分层
        regime_perf: Dict[str, RegimePerformance] = {}
        regime_day_counts: Dict[str, int] = {}
        for label in ('BULL', 'BEAR', 'VOLATILE', 'CALM'):
            sub = daily_df[daily_df['regime'] == label]
            regime_day_counts[label] = len(sub)
            if len(sub) >= 5:
                sub_trades = [
                    t for t in result.trades
                    if hasattr(t.timestamp, 'date') and t.timestamp.date() in sub.index
                ]
                regime_perf[label] = self._calc_performance(label, sub, sub_trades)

        return RegimeAnalysisResult(
            regime_performance=regime_perf,
            overall_performance=overall,
            regime_day_counts=regime_day_counts,
        )

    @staticmethod
    def _calc_performance(
        label: str,
        daily_df: pd.DataFrame,
        trades: list,
    ) -> RegimePerformance:
        """从每日数据计算绩效指标。"""
        rets = daily_df['daily_return'].fillna(0.0)
        n_days = len(rets)
        n_trades = int(daily_df['n_trades'].sum())

        if n_days == 0:
            return RegimePerformance(
                regime=label, n_days=0, n_trades=0,
                total_return=0, annual_return=0, sharpe=0,
                max_drawdown=0, win_rate=0, avg_daily_return=0, daily_return_std=0,
            )

        # 净值曲线
        equity = daily_df['equity'].values
        total_return = float(equity[-1] / equity[0] - 1) if equity[0] > 0 else 0.0
        annual_return = float((1 + total_return) ** (252 / max(n_days, 1)) - 1)

        # Sharpe
        avg_ret = float(rets.mean())
        std_ret = float(rets.std()) if len(rets) > 1 else 1e-8
        sharpe = float(avg_ret / std_ret * np.sqrt(252)) if std_ret > 1e-8 else 0.0

        # 最大回撤
        cum = (1 + rets).cumprod()
        peak = cum.cummax()
        drawdown = float(((cum - peak) / peak).min())

        # 胜率（从 trades 中的 pnl）
        win_rate = 0.0
        sell_trades = [t for t in trades if t.direction == 'SELL' and t.pnl != 0]
        if sell_trades:
            wins = sum(1 for t in sell_trades if t.pnl > 0)
            win_rate = wins / len(sell_trades)

        return RegimePerformance(
            regime=label,
            n_days=n_days,
            n_trades=n_trades,
            total_return=total_return,
            annual_return=annual_return,
            sharpe=sharpe,
            max_drawdown=drawdown,
            win_rate=win_rate,
            avg_daily_return=avg_ret,
            daily_return_std=std_ret,
        )
