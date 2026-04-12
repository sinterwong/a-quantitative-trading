"""
Walk-Forward Analysis (WFA) 引擎
滚动窗口验证 - 用历史数据选参数，未来数据验证
避免过拟合，接近真实策略期望
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import BacktestEngine, TechnicalIndicators as TI
import itertools


class WalkForwardAnalyzer:
    """
    Walk-Forward Analysis

    流程：
    1. 用 [train_start ~ train_end] 数据优化参数
    2. 用 [test_start ~ test_end] 数据验证最优参数
    3. 滚动窗口，不断重复
    4. 汇总所有验证期的表现
    """

    def __init__(self, data, strategy_func, param_grid, train_years=2, test_years=1):
        """
        Args:
            data: K线数据
            strategy_func: 策略信号函数(data, params)
            param_grid: 参数网格
            train_years: 训练集年数
            test_years: 测试集年数
        """
        self.data = data
        self.strategy_func = strategy_func
        self.param_grid = param_grid
        self.train_years = train_years
        self.test_years = test_years

        # 计算有多少个完整滚动窗口
        total_days = len(data)
        total_years = total_days / 252
        self.n_windows = int((total_years - train_years) / test_years)
        if self.n_windows < 1:
            self.n_windows = 1

    def _generate_param_combinations(self):
        keys = list(self.param_grid.keys())
        for combo in itertools.product(*[self.param_grid[k] for k in keys]):
            yield dict(zip(keys, combo))

    def run(self, stop_loss=None, take_profit=None, trailing_stop=None, min_trades=4):
        """
        运行Walk-Forward分析

        Returns:
            list of dicts: 每期的训练/测试结果
        """
        results = []
        total_days = len(self.data)

        train_days = self.train_years * 252
        test_days = self.test_years * 252

        for w in range(self.n_windows):
            train_start_idx = w * test_days
            train_end_idx = train_start_idx + train_days
            test_start_idx = train_end_idx
            test_end_idx = min(test_start_idx + test_days, total_days)

            if train_end_idx >= total_days:
                break
            if test_start_idx >= total_days:
                break

            train_data = self.data[train_start_idx:train_end_idx]
            test_data = self.data[test_start_idx:test_end_idx]

            train_start_date = train_data[0]['date'].split()[0] if isinstance(train_data[0]['date'], str) else str(train_data[0]['date'])
            train_end_date = train_data[-1]['date'].split()[0] if isinstance(train_data[-1]['date'], str) else str(train_data[-1]['date'])
            test_start_date = test_data[0]['date'].split()[0] if isinstance(test_data[0]['date'], str) else str(test_data[0]['date'])
            test_end_date = test_data[-1]['date'].split()[0] if isinstance(test_data[-1]['date'], str) else str(test_data[-1]['date'])

            print(f"\n  Window {w+1}/{self.n_windows}:")
            print(f"    Train: {train_start_date} ~ {train_end_date} ({len(train_data)} days)")
            print(f"    Test:  {test_start_date} ~ {test_end_date} ({len(test_data)} days)")

            # === Phase 1: 训练集找最优参数 ===
            train_results = []

            for params in self._generate_param_combinations():
                engine = BacktestEngine(
                    initial_capital=1000000,
                    commission=0.0003,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop=trailing_stop
                )
                signal = self.strategy_func(train_data, params)
                result = engine.run(train_data, signal, "train")

                if result['total_trades'] >= min_trades:
                    result['_params'] = params
                    train_results.append(result)

            if not train_results:
                print(f"    [SKIP] No valid train results")
                continue

            # 按夏普选训练集最优
            train_results.sort(key=lambda x: x['sharpe_ratio'], reverse=True)
            best_train = train_results[0]
            best_params = best_train['_params']

            print(f"    Train Best: Sharpe={best_train['sharpe_ratio']:.2f}, "
                  f"Return={best_train['total_return_pct']:+.1f}%, "
                  f"WinRate={best_train['win_rate_pct']:.0f}%, Params={best_params}")

            # === Phase 2: 测试集验证 ===
            engine_test = BacktestEngine(
                initial_capital=1000000,
                commission=0.0003,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop=trailing_stop
            )
            signal_test = self.strategy_func(test_data, best_params)
            test_result = engine_test.run(test_data, signal_test, "test")
            test_result['_params'] = best_params
            test_result['_train_sharpe'] = best_train['sharpe_ratio']
            test_result['_train_return'] = best_train['total_return_pct']
            test_result['_window'] = w + 1
            test_result['_train_period'] = f"{train_start_date}~{train_end_date}"
            test_result['_test_period'] = f"{test_start_date}~{test_end_date}"
            # 保存权益曲线（供 Monte Carlo 模拟）
            test_result['equity_curve'] = engine_test.get_equity_curve()

            print(f"    Test Result: Sharpe={test_result['sharpe_ratio']:.2f}, "
                  f"Return={test_result['total_return_pct']:+.1f}%, "
                  f"WinRate={test_result['win_rate_pct']:.0f}%, Trades={test_result['total_trades']}")

            results.append(test_result)

        return results

    def summarize(self, wfa_results):
        """汇总WFA结果"""
        if not wfa_results:
            return {}

        sharpe_list = [r['sharpe_ratio'] for r in wfa_results]
        return_list = [r['total_return_pct'] for r in wfa_results]
        wr_list = [r['win_rate_pct'] for r in wfa_results]
        dd_list = [r['max_drawdown_pct'] for r in wfa_results]

        n = len(wfa_results)
        positive_sharpe = sum(1 for s in sharpe_list if s > 0)

        # 年化收益
        annualized_returns = [r['annualized_return_pct'] for r in wfa_results]

        summary = {
            'n_windows': n,
            'positive_windows': positive_sharpe,
            'win_rate_pct': positive_sharpe / n * 100 if n > 0 else 0,
            'avg_sharpe': sum(sharpe_list) / n,
            'min_sharpe': min(sharpe_list),
            'max_sharpe': max(sharpe_list),
            'avg_return': sum(return_list) / n,
            'min_return': min(return_list),
            'max_return': max(return_list),
            'avg_winrate': sum(wr_list) / n,
            'avg_maxdd': sum(dd_list) / n,
            'max_maxdd': max(dd_list),
            'avg_annualized': sum(annualized_returns) / n,
            'results': wfa_results
        }

        return summary
