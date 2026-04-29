"""
core/strategies/pairs_trading.py — 均值回归配对交易策略

策略逻辑：
  对同行业的两只高相关股票（corr > min_corr），用 Engle-Granger 协整检验
  验证价差的均值回归性。价差偏离均值 ±entry_z σ 时反向入场。

A 股限制（无日内做空）：
  - 做空腿：使用行业 ETF 代替个股做空
    例：做空 600519.SH（贵州茅台）时用 512010.SH（白酒 ETF）近似
  - 或：只做"多-多"配对（同行业中强弱对换），放弃真正做空
  - 本实现支持两种模式：
    1. long_only=True（默认）：只在价差偏低时买入，价差过高时减仓
    2. long_only=False：模拟双边（适合港股/美股或有融券账户）

用法::

    from core.strategies.pairs_trading import PairsTradingStrategy, find_cointegrated_pairs
    import pandas as pd

    # 1. 自动筛选协整对
    price_df = pd.DataFrame({
        '000001.SZ': close_series_a,
        '600036.SH': close_series_b,
        '000002.SZ': close_series_c,
    })
    pairs = find_cointegrated_pairs(price_df, min_corr=0.85)

    # 2. 对单对执行策略
    strategy = PairsTradingStrategy(
        symbol_a='000001.SZ',
        symbol_b='600036.SH',
        entry_z=2.0,
        exit_z=0.5,
        long_only=True,
    )
    signals = strategy.generate_signals(price_df[['000001.SZ', '600036.SH']])
    print(strategy.backtest_stats(price_df[['000001.SZ', '600036.SH']]))

目标：历史 500 天回测，胜率 > 60%，Sharpe > 0.3
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 协整检验工具函数
# ---------------------------------------------------------------------------

def _engle_granger_pvalue(y: np.ndarray, x: np.ndarray) -> float:
    """
    简化版 Engle-Granger 协整检验（基于 ADF 检验残差）。

    不依赖 statsmodels，使用 numpy 实现 ADF 统计量，
    通过临界值表近似返回 p-value。

    Returns
    -------
    float
        近似 p-value（< 0.05 → 协整成立）
    """
    # OLS 回归 y = beta*x + alpha
    n = min(len(y), len(x))
    x_arr = x[:n]
    y_arr = y[:n]
    x_mat = np.column_stack([x_arr, np.ones(n)])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(x_mat, y_arr, rcond=None)
    except np.linalg.LinAlgError:
        return 1.0

    residuals = y_arr - x_mat @ coeffs

    # ADF 统计量（Dickey-Fuller，lag=1，无趋势项）
    dr = np.diff(residuals)
    r_lag = residuals[:-1]
    if len(dr) < 10 or np.std(r_lag) < 1e-10:
        return 1.0

    x_adf = r_lag.reshape(-1, 1)
    try:
        beta_adf, _, _, _ = np.linalg.lstsq(x_adf, dr, rcond=None)
    except np.linalg.LinAlgError:
        return 1.0

    gamma = float(beta_adf[0])
    fitted = x_adf @ beta_adf
    resid_adf = dr - fitted
    s2 = float(np.var(resid_adf, ddof=1)) if len(resid_adf) > 1 else 1e-10
    se_gamma = np.sqrt(s2 / float(np.sum(r_lag ** 2))) if np.sum(r_lag ** 2) > 0 else 1.0
    tau = gamma / (se_gamma + 1e-10)

    # 近似映射（Dickey-Fuller 临界值：n=100 时 -3.45 → p=0.01，-2.87 → p=0.05）
    if tau < -3.5:
        return 0.01
    elif tau < -2.9:
        return 0.05
    elif tau < -2.5:
        return 0.10
    elif tau < -1.9:
        return 0.20
    else:
        return 0.50


def find_cointegrated_pairs(
    price_df: pd.DataFrame,
    min_corr: float = 0.85,
    max_pvalue: float = 0.05,
    lookback_days: int = 252,
) -> List[Tuple[str, str, float, float]]:
    """
    在价格矩阵中自动筛选协整对。

    Parameters
    ----------
    price_df : pd.DataFrame
        列 = 标的代码，index = 日期，值 = close 收盘价
    min_corr : float
        最低 Spearman 相关系数阈值（默认 0.85）
    max_pvalue : float
        Engle-Granger 协整检验最大 p-value（默认 0.05）
    lookback_days : int
        使用最近 N 天数据检验（默认 252 天 ≈ 1 年）

    Returns
    -------
    List of (symbol_a, symbol_b, correlation, coint_pvalue)
        按相关系数降序排列
    """
    df = price_df.iloc[-lookback_days:].dropna(axis=1, how='any')
    symbols = list(df.columns)
    n = len(symbols)

    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = symbols[i], symbols[j]
            sa, sb = df[a].values, df[b].values

            # 相关系数筛选
            ra = pd.Series(sa).rank().values
            rb = pd.Series(sb).rank().values
            corr_val = float(np.corrcoef(ra, rb)[0, 1])
            if abs(corr_val) < min_corr:
                continue

            # 协整检验
            pval = _engle_granger_pvalue(sa, sb)
            if pval > max_pvalue:
                # 尝试反向
                pval2 = _engle_granger_pvalue(sb, sa)
                pval = min(pval, pval2)
            if pval <= max_pvalue:
                pairs.append((a, b, round(corr_val, 4), round(pval, 4)))

    return sorted(pairs, key=lambda x: -x[2])


# ---------------------------------------------------------------------------
# 主策略类
# ---------------------------------------------------------------------------

@dataclass
class PairsSignal:
    """单条配对交易信号。"""
    date: str
    spread_zscore: float        # 当前价差 z-score
    action_a: str               # 对 symbol_a 的操作：'BUY' | 'SELL' | 'HOLD'
    action_b: str               # 对 symbol_b 的操作
    spread: float               # 当前价差（对数价差）
    spread_mean: float
    spread_std: float


@dataclass
class PairsBacktestStats:
    """配对交易回测统计。"""
    total_trades: int           # 完整进出场次数（一进一出计 1 次）
    win_rate: float             # 胜率（盈利的交易次数比例）
    avg_pnl_per_trade: float    # 每笔均盈（价差单位）
    sharpe: float               # 策略 Sharpe 比率（年化）
    max_drawdown: float         # 最大回撤（价差单位）
    avg_holding_days: float     # 平均持仓天数
    total_return: float         # 总收益（价差累积）


class PairsTradingStrategy:
    """
    均值回归配对交易策略。

    对两只协整股票（symbol_a, symbol_b），计算对数价差的 z-score，
    在偏离均值 entry_z σ 时反向入场，回归到 exit_z σ 时平仓。

    Parameters
    ----------
    symbol_a : str
        标的 A（价差 = log(price_a) - hedge_ratio * log(price_b)）
    symbol_b : str
        标的 B（基准腿）
    entry_z : float
        入场阈值（默认 2.0σ）
    exit_z : float
        平仓阈值（默认 0.5σ）
    stop_z : float
        止损阈值（默认 4.0σ，超过则强平）
    lookback_days : int
        计算 z-score 的历史窗口（默认 60 天）
    long_only : bool
        True → 只做多价差偏低时买 A 卖 B；False → 双向（适合港股/美股）
    min_bars : int
        最少历史数据要求（默认 30）
    """

    def __init__(
        self,
        symbol_a: str,
        symbol_b: str,
        entry_z: float = 2.0,
        exit_z: float = 0.5,
        stop_z: float = 4.0,
        lookback_days: int = 60,
        long_only: bool = True,
        min_bars: int = 30,
    ) -> None:
        self.symbol_a = symbol_a
        self.symbol_b = symbol_b
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_z = stop_z
        self.lookback_days = lookback_days
        self.long_only = long_only
        self.min_bars = min_bars

    def _compute_hedge_ratio(self, log_a: np.ndarray, log_b: np.ndarray) -> float:
        """OLS 计算对冲比（hedge ratio）。"""
        x = np.column_stack([log_b, np.ones(len(log_b))])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(x, log_a, rcond=None)
            return float(coeffs[0])
        except np.linalg.LinAlgError:
            return 1.0

    def _zscore(
        self,
        spread_series: pd.Series,
        window: int,
    ) -> pd.Series:
        """滚动 z-score：(spread - rolling_mean) / rolling_std。"""
        mean = spread_series.rolling(window, min_periods=5).mean()
        std = spread_series.rolling(window, min_periods=5).std()
        return (spread_series - mean) / (std + 1e-10)

    def generate_signals(
        self,
        price_df: pd.DataFrame,
    ) -> List[PairsSignal]:
        """
        在历史数据上生成完整配对交易信号序列。

        Parameters
        ----------
        price_df : pd.DataFrame
            含 symbol_a / symbol_b 两列 close 价格，index 为日期

        Returns
        -------
        List[PairsSignal]
        """
        if self.symbol_a not in price_df.columns or self.symbol_b not in price_df.columns:
            return []

        df = price_df[[self.symbol_a, self.symbol_b]].dropna()
        if len(df) < self.min_bars:
            return []

        log_a = np.log(df[self.symbol_a].values + 1e-10)
        log_b = np.log(df[self.symbol_b].values + 1e-10)

        # 滚动对冲比（每 lookback_days 重新估计）
        hedge_ratios = []
        for i in range(len(df)):
            start = max(0, i - self.lookback_days + 1)
            hr = self._compute_hedge_ratio(log_a[start:i + 1], log_b[start:i + 1])
            hedge_ratios.append(hr)

        spread = pd.Series(
            log_a - np.array(hedge_ratios) * log_b,
            index=df.index,
        )
        zscore = self._zscore(spread, self.lookback_days)

        signals = []
        for i, (date, z) in enumerate(zip(df.index, zscore)):
            if np.isnan(z):
                continue

            s = float(spread.iloc[i])
            sm = float(spread.rolling(self.lookback_days, min_periods=5).mean().iloc[i])
            ss = float(spread.rolling(self.lookback_days, min_periods=5).std().iloc[i])

            # 信号逻辑
            if z < -self.entry_z:
                # 价差偏低：买 A，卖 B（预期价差回归）
                action_a, action_b = 'BUY', 'SELL'
            elif z > self.entry_z and not self.long_only:
                # 价差偏高：卖 A，买 B
                action_a, action_b = 'SELL', 'BUY'
            elif abs(z) <= self.exit_z:
                # 价差回归：平仓
                action_a, action_b = 'HOLD', 'HOLD'  # 由持仓状态决定实际操作
            elif abs(z) > self.stop_z:
                # 止损
                action_a, action_b = 'HOLD', 'HOLD'
            else:
                action_a, action_b = 'HOLD', 'HOLD'

            signals.append(PairsSignal(
                date=pd.Timestamp(date).strftime('%Y-%m-%d'),
                spread_zscore=round(float(z), 4),
                action_a=action_a,
                action_b=action_b,
                spread=round(s, 6),
                spread_mean=round(sm, 6),
                spread_std=round(ss, 6),
            ))

        return signals

    def backtest_stats(
        self,
        price_df: pd.DataFrame,
    ) -> PairsBacktestStats:
        """
        简单回测：统计历史信号的盈亏表现（价差单位）。

        Parameters
        ----------
        price_df : pd.DataFrame
            含 symbol_a / symbol_b 列

        Returns
        -------
        PairsBacktestStats
        """
        signals = self.generate_signals(price_df)
        if not signals:
            return PairsBacktestStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        # 模拟持仓与盈亏
        position = 0          # 1 = 做多价差，-1 = 做空价差，0 = 空仓
        entry_spread = 0.0
        entry_date_idx = 0
        pnl_list = []
        holding_days_list = []

        for i, sig in enumerate(signals):
            if position == 0:
                if sig.action_a == 'BUY':
                    position = 1
                    entry_spread = sig.spread
                    entry_date_idx = i
                elif sig.action_a == 'SELL' and not self.long_only:
                    position = -1
                    entry_spread = sig.spread
                    entry_date_idx = i
            else:
                z = sig.spread_zscore
                should_exit = (
                    abs(z) <= self.exit_z or
                    abs(z) > self.stop_z or
                    (position == 1 and sig.action_a == 'SELL') or
                    (position == -1 and sig.action_a == 'BUY')
                )
                if should_exit:
                    pnl = (sig.spread - entry_spread) * position
                    holding = i - entry_date_idx
                    pnl_list.append(pnl)
                    holding_days_list.append(holding)
                    position = 0

        if not pnl_list:
            return PairsBacktestStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        pnl_arr = np.array(pnl_list)
        wins = float(np.sum(pnl_arr > 0)) / len(pnl_arr)
        avg_pnl = float(np.mean(pnl_arr))

        # 累积净值序列（归一化）
        cum = np.cumsum(pnl_arr)
        peak = np.maximum.accumulate(cum)
        dd = cum - peak
        max_dd = float(dd.min()) if len(dd) > 0 else 0.0

        # Sharpe（假设年 250 交易日，每笔交易独立）
        if len(pnl_arr) > 1 and np.std(pnl_arr) > 1e-10:
            # 年化（粗略假设平均持仓 avg_holding_days 天）
            avg_hold = float(np.mean(holding_days_list)) if holding_days_list else 5.0
            trades_per_year = max(250 / max(avg_hold, 1), 1)
            sharpe = float(np.mean(pnl_arr) / np.std(pnl_arr) * np.sqrt(trades_per_year))
        else:
            sharpe = 0.0

        return PairsBacktestStats(
            total_trades=len(pnl_arr),
            win_rate=round(wins, 4),
            avg_pnl_per_trade=round(avg_pnl, 6),
            sharpe=round(sharpe, 4),
            max_drawdown=round(max_dd, 6),
            avg_holding_days=round(float(np.mean(holding_days_list)), 1),
            total_return=round(float(np.sum(pnl_arr)), 6),
        )
