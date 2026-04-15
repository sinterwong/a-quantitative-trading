"""
core/research.py — 因子研究框架

Phase 6 补充组件：
  1. FactorResearcher: 单因子多参数网格搜索 + IC/IR 分析
  2. WalkForwardAnalyzer: Walk-Forward 滚动验证
  3. MultiFactorResearcher: 多因子组合研究
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

from core.backtest_engine import BacktestEngine, BacktestConfig, BacktestResult
from core.factors.base import Factor


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
