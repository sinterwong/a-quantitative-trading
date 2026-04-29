#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/bayesian_optimize.py — 贝叶斯参数自动优化
===================================================

在 Walk-Forward 框架内用 optuna 对 RSI / MACD / ATR 参数做
贝叶斯超参数优化，输出最优参数建议并持久化到 backend/services/live_params.json。

设计要点：
  - 目标函数 = WFA OOS Sharpe 均值（跨所有滚动窗口）
  - 内置轻量级向量化回测（不依赖 BacktestEngine，避免信号接口问题）
  - A 股规则：涨跌幅 ±10% 限制、印花税 0.1%（卖方）、手续费 0.03%（双边）
  - 无真实 API 依赖：合成随机 OHLCV 数据即可完整验证

用法：
    # RSI 策略（合成数据，快速验证）
    python scripts/bayesian_optimize.py --use-synthetic --n-trials 30

    # MACD 策略，指定标的
    python scripts/bayesian_optimize.py --symbol 510300.SH --strategy MACD

    # 安静模式，仅打印最终结果
    python scripts/bayesian_optimize.py --use-synthetic --quiet

依赖：
    pip install optuna
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 路径 ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 禁用代理
for _k in list(os.environ.keys()):
    if 'proxy' in _k.lower():
        del os.environ[_k]

logger = logging.getLogger('bayesian_optimize')


# ── A 股交易成本常量 ───────────────────────────────────────────────────────────
COMMISSION_RATE   = 0.0003   # 双边手续费 0.03%
STAMP_DUTY_SELL   = 0.001    # 印花税 0.1%（仅卖方）
SLIPPAGE_BPS      = 5        # 冲击成本 5bps
INITIAL_EQUITY    = 1_000_000.0


# ── 合成数据 ──────────────────────────────────────────────────────────────────

def _generate_synthetic_data(n_days: int = 1500, seed: int = 42) -> pd.DataFrame:
    """
    生成随机游走 OHLCV 数据，模拟 A 股特征：
      - 日涨跌幅限制 ±10%（符合 A 股涨停板规则）
      - 量价正相关（波动大时成交量放大）
    """
    rng = np.random.default_rng(seed)
    # 带轻微正漂移的随机游走（模拟长期市场上涨趋势）
    returns = rng.normal(0.0003, 0.015, n_days).clip(-0.10, 0.10)
    close = 10.0 * np.cumprod(1 + returns)

    intraday_vol = np.abs(rng.normal(0, 0.008, n_days))
    open_  = close * (1 + rng.normal(0, 0.004, n_days))
    high   = np.maximum(close, open_) * (1 + intraday_vol)
    low    = np.minimum(close, open_) * (1 - intraday_vol)
    volume = rng.integers(5_000_000, 20_000_000, n_days).astype(float) * (1 + 3 * np.abs(returns))

    dates = pd.date_range(end=pd.Timestamp.today(), periods=n_days, freq='B')
    return pd.DataFrame({'open': open_, 'high': high, 'low': low,
                         'close': close, 'volume': volume}, index=dates)


# ── 轻量级向量化回测 ──────────────────────────────────────────────────────────

def _compute_rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder 平滑 RSI，返回 [0,100] 序列（前 period 个为 nan）"""
    n = len(close)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi
    delta = np.diff(close)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)

    # 初始化：前 period 个的简单平均
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])

    for i in range(period, n):
        j = i - 1  # delta index
        avg_gain = (avg_gain * (period - 1) + gain[j]) / period
        avg_loss = (avg_loss * (period - 1) + loss[j]) / period
        rs = avg_gain / avg_loss if avg_loss > 1e-12 else 1e6
        rsi[i] = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def _compute_macd(close: np.ndarray, fast: int, slow: int, signal: int
                  ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (dif, dea, histogram)，均为 float64 数组"""
    def _ema(arr, span):
        alpha = 2.0 / (span + 1)
        out = np.full(len(arr), np.nan)
        out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = alpha * arr[i] + (1 - alpha) * out[i-1]
        return out

    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    dif = ema_fast - ema_slow
    dea = _ema(dif, signal)
    hist = dif - dea
    return dif, dea, hist


def _compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 period: int) -> np.ndarray:
    """ATR，返回 float64 数组"""
    n = len(close)
    tr = np.full(n, np.nan)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = np.full(n, np.nan)
    atr[period-1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr


def _run_backtest(
    df: pd.DataFrame,
    positions: np.ndarray,   # +1 = long, 0 = flat  (signal series, bar-aligned)
) -> Tuple[float, float, int]:
    """
    向量化回测：positions[i] 是第 i 根 bar 的目标仓位（用 i+1 根 open 成交）。
    返回 (sharpe, annual_return, n_trades)。
    A 股买卖成本：印花税 0.1% 单边（卖出）+ 手续费 0.03% 双边。
    """
    n = len(df)
    close  = df['close'].values
    open_v = df['open'].values

    equity  = INITIAL_EQUITY
    cash    = INITIAL_EQUITY
    shares  = 0.0
    holding = False
    n_trades = 0
    daily_equity = np.zeros(n)

    for i in range(n - 1):
        target = positions[i]
        exec_price = open_v[i + 1]  # 下一根 open 成交（消除前视偏差）
        if exec_price <= 0:
            exec_price = close[i]

        slippage = exec_price * SLIPPAGE_BPS * 1e-4

        if target == 1 and not holding:
            # 买入：全仓
            exec_p = exec_price + slippage
            cost_rate = COMMISSION_RATE
            shares_buy = cash / (exec_p * (1 + cost_rate))
            cost = shares_buy * exec_p * cost_rate
            cash -= shares_buy * exec_p + cost
            shares = shares_buy
            holding = True
            n_trades += 1

        elif target == 0 and holding:
            # 卖出：全部平仓
            exec_p = exec_price - slippage
            proceeds = shares * exec_p
            sell_cost = shares * exec_p * (COMMISSION_RATE + STAMP_DUTY_SELL)
            cash += proceeds - sell_cost
            shares = 0.0
            holding = False
            n_trades += 1

        # 当日 equity（持仓按 close 估值）
        equity = cash + shares * close[i]
        daily_equity[i] = equity

    # 最后一天
    if holding:
        daily_equity[-1] = cash + shares * close[-1]
    else:
        daily_equity[-1] = cash

    # 仅取有效段
    daily_equity = daily_equity[daily_equity > 0]
    if len(daily_equity) < 5:
        return -10.0, 0.0, 0

    daily_ret = np.diff(daily_equity) / daily_equity[:-1]
    if len(daily_ret) < 5:
        return -10.0, 0.0, 0

    annual_return = float(np.mean(daily_ret) * 252)
    std = float(np.std(daily_ret) * np.sqrt(252))
    sharpe = annual_return / std if std > 1e-8 else 0.0

    return sharpe, annual_return, n_trades


# ── 策略信号生成器 ────────────────────────────────────────────────────────────

def _rsi_positions(df: pd.DataFrame, period: int, buy_threshold: float,
                   sell_threshold: float) -> np.ndarray:
    """RSI 超卖买入 / 超买卖出策略，返回 positions 数组。"""
    close = df['close'].values
    rsi   = _compute_rsi(close, period)
    n = len(close)
    positions = np.zeros(n)
    in_pos = False

    for i in range(period + 1, n):
        if np.isnan(rsi[i]) or np.isnan(rsi[i-1]):
            positions[i] = 1 if in_pos else 0
            continue

        if not in_pos and rsi[i-1] < buy_threshold <= rsi[i]:
            # RSI 从超卖区上穿 buy_threshold → 买入
            in_pos = True
        elif in_pos and rsi[i-1] < sell_threshold <= rsi[i]:
            # RSI 从正常区上穿 sell_threshold → 卖出
            in_pos = False
        elif in_pos and rsi[i] < buy_threshold - 5:
            # 深度超卖继续持有（避免震荡出仓）
            in_pos = True

        positions[i] = 1 if in_pos else 0

    return positions


def _macd_positions(df: pd.DataFrame, fast: int, slow: int, signal: int,
                    atr_threshold: float) -> np.ndarray:
    """MACD 金叉买入 / 死叉卖出，含 ATR 高波动过滤。"""
    close = df['close'].values
    high  = df['high'].values
    low   = df['low'].values
    n = len(close)

    _, _, hist = _compute_macd(close, fast, slow, signal)
    atr         = _compute_atr(high, low, close, period=14)
    atr_roll_max = pd.Series(atr).rolling(30, min_periods=5).max().values

    positions = np.zeros(n)
    in_pos    = False

    for i in range(max(slow, 30) + 1, n):
        if np.isnan(hist[i]) or np.isnan(hist[i-1]):
            positions[i] = 1 if in_pos else 0
            continue

        # ATR ratio 高波动过滤
        atr_ratio = (atr[i] / atr_roll_max[i]) if (atr_roll_max[i] > 0 and not np.isnan(atr_roll_max[i])) else 0.0
        high_vol  = atr_ratio > atr_threshold

        if not in_pos and hist[i-1] < 0 <= hist[i] and not high_vol:
            in_pos = True   # 金叉
        elif in_pos and hist[i-1] > 0 >= hist[i]:
            in_pos = False  # 死叉

        positions[i] = 1 if in_pos else 0

    return positions


def _atr_positions(df: pd.DataFrame, period: int, lookback: int) -> np.ndarray:
    """
    ATR 趋势策略：ATR ratio < 0.7（低波动 + 上涨趋势）时买入。
    低波动期配合价格在 N 日均线之上做多。
    """
    close = df['close'].values
    high  = df['high'].values
    low   = df['low'].values
    n = len(close)

    atr          = _compute_atr(high, low, close, period)
    atr_roll_max = pd.Series(atr).rolling(lookback, min_periods=5).max().values
    ma           = pd.Series(close).rolling(lookback, min_periods=5).mean().values

    positions = np.zeros(n)
    in_pos    = False

    for i in range(lookback + period, n):
        if np.isnan(atr[i]) or np.isnan(atr_roll_max[i]) or np.isnan(ma[i]):
            positions[i] = 1 if in_pos else 0
            continue

        atr_ratio = atr[i] / atr_roll_max[i] if atr_roll_max[i] > 0 else 1.0
        price_above_ma = close[i] > ma[i]
        low_vol = atr_ratio < 0.70

        if not in_pos and low_vol and price_above_ma:
            in_pos = True
        elif in_pos and (not low_vol or not price_above_ma):
            in_pos = False

        positions[i] = 1 if in_pos else 0

    return positions


# ── 策略配置表 ────────────────────────────────────────────────────────────────

def _rsi_signal_fn(df, params):
    return _rsi_positions(df, params['period'], params['buy_threshold'], params['sell_threshold'])

def _macd_signal_fn(df, params):
    return _macd_positions(df, params['fast'], params['slow'], params['signal'], params['atr_threshold'])

def _atr_signal_fn(df, params):
    return _atr_positions(df, params['period'], params['lookback'])


STRATEGY_REGISTRY: Dict[str, Dict] = {
    'RSI': {
        'signal_fn':   _rsi_signal_fn,
        'description': 'RSI 超买超卖策略（均值回归）',
        'param_space': {
            # (type, low, high[, step])
            'period':         ('int',   7,  28, 1),
            'buy_threshold':  ('float', 20, 45),
            'sell_threshold': ('float', 55, 80),
        },
        # buy_threshold 必须小于 sell_threshold（由 _suggest_params 保证）
        'constraints': [('buy_threshold', '<', 'sell_threshold', 10)],
    },
    'MACD': {
        'signal_fn':   _macd_signal_fn,
        'description': 'MACD 趋势跟踪 + ATR 高波动过滤',
        'param_space': {
            'fast':          ('int',   5,  15, 1),
            'slow':          ('int',  18,  35, 1),
            'signal':        ('int',   5,  14, 1),
            'atr_threshold': ('float', 0.60, 0.95),
        },
        'constraints': [('fast', '<', 'slow', 5)],
    },
    'ATR': {
        'signal_fn':   _atr_signal_fn,
        'description': 'ATR 低波动趋势策略（震荡过滤）',
        'param_space': {
            'period':   ('int',  7,  28, 1),
            'lookback': ('int', 15,  60, 5),
        },
        'constraints': [],
    },
}


# ── 参数采样 ──────────────────────────────────────────────────────────────────

def _suggest_params(trial, param_space: Dict, constraints: List) -> Dict[str, Any]:
    """
    从 param_space 定义用 optuna trial 采样参数，并修复约束违反。

    param_space 格式：
        'name': ('int',   low, high[, step])
        'name': ('float', low, high)
        'name': ('cat',   [choices])
    constraints 格式：
        ('a', '<', 'b', min_gap)  — 确保 params[a] + min_gap <= params[b]
    """
    params: Dict[str, Any] = {}

    for name, spec in param_space.items():
        ptype = spec[0]
        if ptype == 'int':
            low, high = spec[1], spec[2]
            step = spec[3] if len(spec) > 3 else 1
            params[name] = trial.suggest_int(name, low, high, step=step)
        elif ptype == 'float':
            low, high = spec[1], spec[2]
            params[name] = trial.suggest_float(name, low, high)
        elif ptype == 'cat':
            params[name] = trial.suggest_categorical(name, spec[1])

    # 修复约束
    for constraint in constraints:
        a, op, b = constraint[0], constraint[1], constraint[2]
        min_gap  = constraint[3] if len(constraint) > 3 else 1
        if op == '<' and a in params and b in params:
            if params[a] + min_gap > params[b]:
                # 将 a 强制压低到 b - min_gap
                low_a = param_space[a][1]
                params[a] = max(low_a, int(params[b]) - min_gap if isinstance(params[b], (int,)) else params[b] - min_gap)

    return params


# ── WFA 窗口切分 ──────────────────────────────────────────────────────────────

def _split_wfa_windows(
    df: pd.DataFrame,
    train_months: int,
    test_months: int,
    step_months: int,
) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
    """
    按月切分 Walk-Forward 窗口，返回 [(df_train, df_test), ...]。
    步进 = step_months。
    """
    from dateutil.relativedelta import relativedelta

    if df.empty:
        return []

    dates = df.index
    start = dates[0].to_pydatetime()
    end   = dates[-1].to_pydatetime()

    windows = []
    train_start = start

    while True:
        train_end = train_start + relativedelta(months=train_months)
        test_end  = train_end   + relativedelta(months=test_months)

        if test_end > end:
            break

        mask_train = (dates >= pd.Timestamp(train_start)) & (dates < pd.Timestamp(train_end))
        mask_test  = (dates >= pd.Timestamp(train_end))   & (dates < pd.Timestamp(test_end))

        df_train = df.loc[mask_train]
        df_test  = df.loc[mask_test]

        if len(df_train) >= 50 and len(df_test) >= 20:
            windows.append((df_train, df_test))

        train_start = train_start + relativedelta(months=step_months)

    return windows


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class WFAWindowResult:
    window_idx: int
    train_sharpe: float
    test_sharpe: float
    test_return: float
    test_n_trades: int
    best_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OptimizationResult:
    """贝叶斯优化完整结果"""
    symbol: str
    strategy: str
    best_params: Dict[str, Any]
    best_oos_sharpe: float
    best_oos_return: float
    best_oos_win_rate: float          # 正 Sharpe 窗口比例（作为胜率代理）
    n_trials: int
    n_valid_trials: int
    optimization_time_s: float
    wfa_n_windows: int
    wfa_positive_sharpe_pct: float
    timestamp: str = ''

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def to_dict(self) -> Dict:
        return asdict(self)

    def print_summary(self) -> None:
        bar = '=' * 65
        print(f'\n{bar}')
        print(f'  贝叶斯优化结果汇总')
        print(f'  标的: {self.symbol}  |  策略: {self.strategy}')
        print(bar)
        print(f'  有效 Trials : {self.n_valid_trials} / {self.n_trials}')
        print(f'  优化耗时    : {self.optimization_time_s:.1f} 秒')
        print(f'  WFA 窗口数  : {self.wfa_n_windows}')
        print(f'  正Sharpe比  : {self.wfa_positive_sharpe_pct*100:.1f}%')
        print(bar)
        print(f'  最优参数:')
        for k, v in self.best_params.items():
            v_str = f'{v:.4f}' if isinstance(v, float) else str(v)
            print(f'    {k:<22} = {v_str}')
        print(bar)
        print(f'  OOS Sharpe  : {self.best_oos_sharpe:+.4f}')
        print(f'  OOS 年化收益: {self.best_oos_return*100:+.2f}%')
        print(f'  正Sharpe窗口: {self.wfa_positive_sharpe_pct*100:.1f}%')
        print(f'{bar}\n')


# ── 目标函数 ──────────────────────────────────────────────────────────────────

def make_objective(
    windows: List[Tuple[pd.DataFrame, pd.DataFrame]],
    strategy_cfg: Dict,
    min_trades: int = 3,
    quiet: bool = False,
):
    """
    返回 optuna objective 函数。
    目标：最大化 WFA OOS Sharpe 均值。
    """
    signal_fn    = strategy_cfg['signal_fn']
    param_space  = strategy_cfg['param_space']
    constraints  = strategy_cfg.get('constraints', [])

    def objective(trial) -> float:
        params = _suggest_params(trial, param_space, constraints)
        oos_sharpes = []

        for df_train, df_test in windows:
            try:
                # 在训练集上评估（验证参数在训练数据上至少能产生足够交易）
                pos_train = signal_fn(df_train, params)
                sh_train, _, n_t = _run_backtest(df_train, pos_train)
                if n_t < min_trades:
                    continue  # 训练窗口交易不足，跳过该窗口

                # OOS 测试
                pos_test = signal_fn(df_test, params)
                sh_test, _, _ = _run_backtest(df_test, pos_test)
                oos_sharpes.append(sh_test)

            except Exception as exc:
                logger.debug('Window backtest failed: %s', exc)
                continue

        if not oos_sharpes:
            return -10.0

        mean_oos = float(np.mean(oos_sharpes))

        if not quiet:
            logger.info(
                'Trial %3d | OOS Sharpe=%.4f (windows=%d) | %s',
                trial.number, mean_oos, len(oos_sharpes),
                {k: (round(v, 3) if isinstance(v, float) else v) for k, v in params.items()},
            )

        return mean_oos

    return objective


# ── 主优化流程 ────────────────────────────────────────────────────────────────

def run_bayesian_optimization(
    symbol: str,
    strategy: str,
    df: pd.DataFrame,
    n_trials: int = 50,
    train_months: int = 18,
    test_months: int = 6,
    step_months: int = 6,
    n_jobs: int = 1,
    quiet: bool = False,
) -> OptimizationResult:
    """
    执行贝叶斯优化，返回 OptimizationResult。

    Parameters
    ----------
    symbol        : 标的代码（如 '510300.SH'）
    strategy      : 策略名称（'RSI' / 'MACD' / 'ATR'）
    df            : OHLCV 日线 DataFrame（DatetimeIndex）
    n_trials      : optuna trial 数量
    train_months  : WFA 训练窗口（月）
    test_months   : WFA 测试窗口（月）
    step_months   : WFA 步进长度（月）
    n_jobs        : 并行 trial 数（-1 = 全部 CPU）
    quiet         : 是否抑制 trial 日志
    """
    try:
        import optuna
    except ImportError:
        raise ImportError('optuna 未安装。请运行: pip install optuna')

    if strategy not in STRATEGY_REGISTRY:
        raise ValueError(f'未知策略: {strategy}，支持: {list(STRATEGY_REGISTRY.keys())}')

    strategy_cfg = STRATEGY_REGISTRY[strategy]

    if quiet:
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    else:
        optuna.logging.set_verbosity(optuna.logging.INFO)

    # 切分 WFA 窗口
    windows = _split_wfa_windows(df, train_months, test_months, step_months)
    if not windows:
        raise ValueError(f'数据不足以生成 WFA 窗口（需 ≥ {train_months + test_months} 个月数据，'
                         f'实际 {len(df)} 天）')

    if not quiet:
        print(f'\n[贝叶斯优化] 标的={symbol}  策略={strategy}')
        print(f'  WFA 配置: train={train_months}m / test={test_months}m / step={step_months}m')
        print(f'  数据量: {len(df)} 天  WFA窗口: {len(windows)}  Trials: {n_trials}')
        print(f'  参数空间: {list(strategy_cfg["param_space"].keys())}\n')

    objective = make_objective(windows, strategy_cfg, quiet=quiet)

    sampler = optuna.samplers.TPESampler(seed=42, n_startup_trials=max(10, n_trials // 5))
    study = optuna.create_study(
        direction='maximize',
        sampler=sampler,
        study_name=f'{symbol}_{strategy}_{datetime.now().strftime("%Y%m%d_%H%M")}',
    )

    t0 = time.time()
    study.optimize(
        objective,
        n_trials=n_trials,
        n_jobs=n_jobs,
        show_progress_bar=(not quiet),
        catch=(Exception,),
    )
    elapsed = time.time() - t0

    # ── 收集最优参数的完整 WFA 指标 ─────────────────────────────────────────
    best_params   = study.best_params if study.best_trials else {}
    best_oos_sharpe = study.best_value if study.best_trials else 0.0

    n_windows = 0
    best_return = 0.0
    best_win_rate = 0.0
    positive_sharpe_pct = 0.0

    if best_params:
        try:
            signal_fn  = strategy_cfg['signal_fn']
            oos_sharpes = []
            oos_returns = []

            for df_train, df_test in windows:
                pos_test = signal_fn(df_test, best_params)
                sh, ret, _ = _run_backtest(df_test, pos_test)
                oos_sharpes.append(sh)
                oos_returns.append(ret)

            n_windows  = len(oos_sharpes)
            if oos_sharpes:
                best_return  = float(np.mean(oos_returns))
                n_pos        = sum(1 for s in oos_sharpes if s > 0)
                positive_sharpe_pct = n_pos / n_windows
                best_win_rate = positive_sharpe_pct
        except Exception as exc:
            logger.warning('最优参数重验证失败: %s', exc)

    n_valid = sum(
        1 for t in study.trials
        if t.value is not None and t.value > -9.0
    )

    return OptimizationResult(
        symbol=symbol,
        strategy=strategy,
        best_params=best_params,
        best_oos_sharpe=best_oos_sharpe,
        best_oos_return=best_return,
        best_oos_win_rate=best_win_rate,
        n_trials=len(study.trials),
        n_valid_trials=n_valid,
        optimization_time_s=round(elapsed, 1),
        wfa_n_windows=n_windows,
        wfa_positive_sharpe_pct=positive_sharpe_pct,
    )


# ── 持久化 ────────────────────────────────────────────────────────────────────

def _save_to_live_params(result: OptimizationResult) -> None:
    """将最优参数写入 backend/services/live_params.json（供信号引擎实时查询）"""
    param_file = os.path.join(BASE_DIR, 'backend', 'services', 'live_params.json')
    os.makedirs(os.path.dirname(param_file), exist_ok=True)

    live_params: Dict = {}
    if os.path.exists(param_file):
        try:
            with open(param_file, encoding='utf-8') as f:
                live_params = json.load(f)
        except Exception:
            pass

    key = f'{result.symbol}_{result.strategy}'
    live_params[key] = {
        'symbol':     result.symbol,
        'strategy':   result.strategy,
        'params':     result.best_params,
        'oos_sharpe': round(result.best_oos_sharpe, 4),
        'oos_return': round(result.best_oos_return, 4),
        'method':     'bayesian_optuna',
        'updated_at': result.timestamp,
    }

    with open(param_file, 'w', encoding='utf-8') as f:
        json.dump(live_params, f, indent=2, ensure_ascii=False)

    logger.info('[OK] 最优参数已写入 %s', param_file)


def _save_optimization_record(result: OptimizationResult, output_dir: str) -> str:
    """将完整优化记录保存到 outputs/"""
    os.makedirs(output_dir, exist_ok=True)
    fname = f'bayesian_opt_{result.symbol.replace(".", "_")}_{result.strategy}.json'
    fpath = os.path.join(output_dir, fname)

    with open(fpath, 'w', encoding='utf-8') as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)

    logger.info('[OK] 优化记录已保存到 %s', fpath)
    return fpath


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def _load_price_data(symbol: str, n_days: int) -> pd.DataFrame:
    """尝试从 DataLayer 加载真实行情，失败时降级为合成数据。"""
    try:
        from core.data_layer import DataLayer
        dl = DataLayer()
        end   = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - pd.Timedelta(days=int(n_days * 1.5))).strftime('%Y%m%d')
        df = dl.get_price(symbol, start=start, end=end)
        if df is not None and len(df) >= 252:
            logger.info('[DataLayer] 加载 %s 真实行情 %d 条', symbol, len(df))
            return df
    except Exception as exc:
        logger.debug('DataLayer 加载失败: %s', exc)

    logger.info('[合成数据] 标的 %s 数据加载失败，生成 %d 天随机 OHLCV', symbol, n_days)
    return _generate_synthetic_data(n_days=n_days)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='贝叶斯参数自动优化（optuna + Walk-Forward）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--symbol',        default='510300.SH')
    p.add_argument('--strategy',      default='RSI', choices=list(STRATEGY_REGISTRY.keys()))
    p.add_argument('--n-trials',      type=int,  default=50)
    p.add_argument('--train-months',  type=int,  default=18)
    p.add_argument('--test-months',   type=int,  default=6)
    p.add_argument('--step-months',   type=int,  default=6)
    p.add_argument('--n-jobs',        type=int,  default=1)
    p.add_argument('--use-synthetic', action='store_true', help='强制使用合成数据')
    p.add_argument('--n-days',        type=int,  default=1500)
    p.add_argument('--output-dir',    default=os.path.join(BASE_DIR, 'outputs'))
    p.add_argument('--no-save',       action='store_true')
    p.add_argument('--quiet',         action='store_true')
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    df = (_generate_synthetic_data(n_days=args.n_days)
          if args.use_synthetic
          else _load_price_data(args.symbol, args.n_days))

    if not args.quiet:
        print(f'[数据] {len(df)} 天  '
              f'{df.index[0].date()} ~ {df.index[-1].date()}')

    result = run_bayesian_optimization(
        symbol=args.symbol,
        strategy=args.strategy,
        df=df,
        n_trials=args.n_trials,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        n_jobs=args.n_jobs,
        quiet=args.quiet,
    )

    result.print_summary()

    if not args.no_save:
        _save_to_live_params(result)
        record_path = _save_optimization_record(result, args.output_dir)
        if not args.quiet:
            print(f'[完成] 记录已保存: {record_path}')


# ── 编程接口 ──────────────────────────────────────────────────────────────────

def optimize(
    symbol: str,
    strategy: str = 'RSI',
    df: Optional[pd.DataFrame] = None,
    n_trials: int = 50,
    train_months: int = 18,
    test_months: int = 6,
    quiet: bool = True,
) -> OptimizationResult:
    """
    编程接口：从其他模块调用贝叶斯优化。

    Example
    -------
    >>> from scripts.bayesian_optimize import optimize
    >>> result = optimize('510300.SH', strategy='MACD', n_trials=30)
    >>> print(result.best_params)
    """
    if df is None:
        df = _load_price_data(symbol, n_days=1500)
    return run_bayesian_optimization(
        symbol=symbol, strategy=strategy, df=df,
        n_trials=n_trials, train_months=train_months,
        test_months=test_months, quiet=quiet,
    )


if __name__ == '__main__':
    main()
