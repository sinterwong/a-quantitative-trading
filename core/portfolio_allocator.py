"""
core/portfolio_allocator.py — 多账户/多策略资金分配（Backlog）

功能：
  - 在多个策略实例之间按权重分配总资金
  - 支持三种权重模式：等权 / 固定权重 / 风险平价（基于滚动波动率）
  - 持仓偏离 > rebalance_threshold 时触发再平衡
  - 与 SimulatedBroker / BrokerBase 协同工作，各策略独享子账户额度

用法：
    from core.portfolio_allocator import PortfolioAllocator, AllocConfig, WeightMode

    allocator = PortfolioAllocator(
        total_capital=1_000_000,
        config=AllocConfig(mode=WeightMode.EQUAL),
    )
    allocator.add_strategy('RSI',  weight=0.5)
    allocator.add_strategy('MACD', weight=0.3)
    allocator.add_strategy('OI',   weight=0.2)

    # 获取各策略当前可用资金额度
    budgets = allocator.get_budgets()   # {'RSI': 500000, 'MACD': 300000, 'OI': 200000}

    # 触发再平衡（需提供各策略当前持仓市值）
    current_mv = {'RSI': 480000, 'MACD': 350000, 'OI': 190000}
    if allocator.needs_rebalance(current_mv):
        new_budgets = allocator.rebalance(current_mv)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_OUTPUTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'outputs'
)
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 权重模式
# ---------------------------------------------------------------------------

class WeightMode(str, Enum):
    EQUAL       = 'equal'        # 等权：所有策略平分资金
    FIXED       = 'fixed'        # 固定权重：按用户指定比例
    RISK_PARITY = 'risk_parity'  # 风险平价：按滚动波动率倒数加权


# ---------------------------------------------------------------------------
# AllocConfig
# ---------------------------------------------------------------------------

@dataclass
class AllocConfig:
    """资金分配配置。"""
    mode: WeightMode = WeightMode.EQUAL
    rebalance_threshold: float = 0.05   # 实际偏离目标 5% 时触发再平衡
    min_strategy_weight: float = 0.05   # 单策略最低权重（避免过小）
    max_strategy_weight: float = 0.60   # 单策略最高权重
    risk_parity_window: int = 60        # 风险平价的波动率滚动窗口（天）
    reserve_ratio: float = 0.05         # 保留现金比例（不参与分配）


# ---------------------------------------------------------------------------
# StrategyAccount — 单策略账户
# ---------------------------------------------------------------------------

@dataclass
class StrategyAccount:
    """单策略的资金账户。"""
    name: str
    target_weight: float          # 目标权重（0~1）
    budget: float                 # 当前分配额度（元）
    used: float = 0.0             # 已使用（持仓市值）
    realized_pnl: float = 0.0    # 已实现盈亏
    daily_returns: List[float] = field(default_factory=list)

    @property
    def available(self) -> float:
        return max(self.budget - self.used, 0.0)

    @property
    def utilization(self) -> float:
        return self.used / self.budget if self.budget > 0 else 0.0

    def update_return(self, ret: float) -> None:
        self.daily_returns.append(ret)
        if len(self.daily_returns) > 252:
            self.daily_returns = self.daily_returns[-252:]

    @property
    def volatility(self) -> float:
        """滚动年化波动率（基于 daily_returns）。"""
        if len(self.daily_returns) < 5:
            return 1.0   # 数据不足时返回 1.0（等权处理）
        return float(np.std(self.daily_returns[-60:]) * np.sqrt(252))


# ---------------------------------------------------------------------------
# RebalanceRecord
# ---------------------------------------------------------------------------

@dataclass
class RebalanceRecord:
    """再平衡记录。"""
    timestamp: str
    trigger: str                          # 'drift' | 'manual' | 'periodic'
    before_weights: Dict[str, float]
    after_weights: Dict[str, float]
    before_budgets: Dict[str, float]
    after_budgets: Dict[str, float]
    max_drift: float


# ---------------------------------------------------------------------------
# PortfolioAllocator
# ---------------------------------------------------------------------------

class PortfolioAllocator:
    """
    多策略资金分配器。

    Parameters
    ----------
    total_capital : 总可用资金（元）
    config        : AllocConfig
    """

    def __init__(
        self,
        total_capital: float,
        config: Optional[AllocConfig] = None,
    ) -> None:
        self.total_capital = total_capital
        self.config = config or AllocConfig()
        self._accounts: Dict[str, StrategyAccount] = {}
        self._rebalance_history: List[RebalanceRecord] = []

    # ------------------------------------------------------------------
    # 策略注册
    # ------------------------------------------------------------------

    def add_strategy(
        self,
        name: str,
        weight: Optional[float] = None,
    ) -> 'PortfolioAllocator':
        """
        注册策略。

        Parameters
        ----------
        name   : 策略名称（唯一）
        weight : 固定权重（仅 WeightMode.FIXED 有效，None = 等权）

        Returns self（支持链式调用）
        """
        if name in self._accounts:
            logger.warning('[PortfolioAllocator] Strategy "%s" already registered, updating weight.', name)

        n = len(self._accounts) + 1
        # 先用临时权重，after _recompute_weights 统一调整
        self._accounts[name] = StrategyAccount(
            name=name,
            target_weight=weight if weight is not None else 1.0 / n,
            budget=0.0,
        )
        if weight is not None:
            self._accounts[name].target_weight = weight

        self._recompute_weights()
        self._recompute_budgets()
        return self

    def remove_strategy(self, name: str) -> None:
        """注销策略（资金归还总池，触发再平衡）。"""
        if name in self._accounts:
            del self._accounts[name]
            self._recompute_weights()
            self._recompute_budgets()

    # ------------------------------------------------------------------
    # 资金查询
    # ------------------------------------------------------------------

    def get_budgets(self) -> Dict[str, float]:
        """返回各策略当前资金额度（元）。"""
        return {name: acc.budget for name, acc in self._accounts.items()}

    def get_available(self) -> Dict[str, float]:
        """返回各策略可用资金（额度 - 持仓市值）。"""
        return {name: acc.available for name, acc in self._accounts.items()}

    def get_weights(self) -> Dict[str, float]:
        """返回各策略当前目标权重。"""
        return {name: acc.target_weight for name, acc in self._accounts.items()}

    def get_account(self, name: str) -> Optional[StrategyAccount]:
        return self._accounts.get(name)

    # ------------------------------------------------------------------
    # 使用量更新
    # ------------------------------------------------------------------

    def update_usage(self, name: str, market_value: float) -> None:
        """
        更新策略当前持仓市值。

        Parameters
        ----------
        name         : 策略名
        market_value : 当前持仓市值（元）
        """
        if name in self._accounts:
            self._accounts[name].used = max(market_value, 0.0)

    def record_return(self, name: str, daily_return: float) -> None:
        """记录策略当日收益率（用于风险平价权重更新）。"""
        if name in self._accounts:
            self._accounts[name].update_return(daily_return)

    # ------------------------------------------------------------------
    # 再平衡
    # ------------------------------------------------------------------

    def needs_rebalance(
        self,
        current_mv: Optional[Dict[str, float]] = None,
    ) -> bool:
        """
        判断是否需要再平衡。

        Parameters
        ----------
        current_mv : {策略名: 当前持仓市值}，None = 使用内部 used 字段
        """
        if current_mv:
            for name, mv in current_mv.items():
                if name in self._accounts:
                    self._accounts[name].used = mv

        total_used = sum(a.used for a in self._accounts.values())
        if total_used <= 0:
            return False

        for name, acc in self._accounts.items():
            actual_weight = acc.used / total_used if total_used > 0 else 0
            drift = abs(actual_weight - acc.target_weight)
            if drift > self.config.rebalance_threshold:
                logger.info(
                    '[PortfolioAllocator] %s drift=%.1f%% > threshold=%.1f%%',
                    name, drift * 100, self.config.rebalance_threshold * 100,
                )
                return True
        return False

    def rebalance(
        self,
        current_mv: Optional[Dict[str, float]] = None,
        trigger: str = 'manual',
    ) -> Dict[str, float]:
        """
        执行再平衡，重新计算各策略资金额度。

        Parameters
        ----------
        current_mv : {策略名: 当前持仓市值}
        trigger    : 触发原因（'drift' / 'manual' / 'periodic'）

        Returns
        -------
        新的 {策略名: 额度} 字典
        """
        if current_mv:
            for name, mv in current_mv.items():
                if name in self._accounts:
                    self._accounts[name].used = mv

        before_weights = self.get_weights().copy()
        before_budgets = self.get_budgets().copy()

        # 更新权重（风险平价模式需要收益历史）
        if self.config.mode == WeightMode.RISK_PARITY:
            self._update_risk_parity_weights()

        self._recompute_budgets()

        after_weights = self.get_weights().copy()
        after_budgets = self.get_budgets().copy()

        # 计算最大偏离
        max_drift = max(
            abs(after_weights.get(n, 0) - before_weights.get(n, 0))
            for n in set(after_weights) | set(before_weights)
        )

        record = RebalanceRecord(
            timestamp=datetime.now().isoformat(timespec='seconds'),
            trigger=trigger,
            before_weights=before_weights,
            after_weights=after_weights,
            before_budgets={k: round(v, 2) for k, v in before_budgets.items()},
            after_budgets={k: round(v, 2) for k, v in after_budgets.items()},
            max_drift=round(max_drift, 4),
        )
        self._rebalance_history.append(record)
        logger.info('[PortfolioAllocator] Rebalanced (%s) max_drift=%.1f%%',
                    trigger, max_drift * 100)

        return after_budgets

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------

    def summary(self) -> Dict:
        """返回当前分配状态摘要。"""
        total_budget = sum(a.budget for a in self._accounts.values())
        total_used   = sum(a.used   for a in self._accounts.values())
        return {
            'total_capital': self.total_capital,
            'total_budget': round(total_budget, 2),
            'total_used': round(total_used, 2),
            'reserve': round(self.total_capital - total_budget, 2),
            'n_strategies': len(self._accounts),
            'mode': self.config.mode.value,
            'strategies': {
                name: {
                    'weight': round(acc.target_weight, 4),
                    'budget': round(acc.budget, 2),
                    'used': round(acc.used, 2),
                    'available': round(acc.available, 2),
                    'utilization': round(acc.utilization, 4),
                    'volatility': round(acc.volatility, 4),
                }
                for name, acc in self._accounts.items()
            },
            'n_rebalances': len(self._rebalance_history),
        }

    def print_summary(self) -> None:
        s = self.summary()
        print(f'=== PortfolioAllocator ===')
        print(f'总资金：{s["total_capital"]:,.0f} | 模式：{s["mode"]}')
        print(f'已分配：{s["total_budget"]:,.0f} | 已使用：{s["total_used"]:,.0f} | '
              f'保留：{s["reserve"]:,.0f}')
        print()
        print(f'{"策略":<12} {"权重":>8} {"额度":>12} {"使用":>12} {"可用":>12} {"利用率":>8}')
        print('-' * 68)
        for name, info in s['strategies'].items():
            print(f'{name:<12} {info["weight"]:>7.1%} '
                  f'{info["budget"]:>11,.0f} {info["used"]:>11,.0f} '
                  f'{info["available"]:>11,.0f} {info["utilization"]:>7.1%}')

    def save_history(self, path: Optional[str] = None) -> str:
        if path is None:
            path = os.path.join(
                _OUTPUTS_DIR,
                f'portfolio_allocator_history_{datetime.now().strftime("%Y%m%d")}.json',
            )
        data = {
            'summary': self.summary(),
            'rebalance_history': [
                {
                    'timestamp': r.timestamp,
                    'trigger': r.trigger,
                    'max_drift': r.max_drift,
                    'after_weights': r.after_weights,
                    'after_budgets': r.after_budgets,
                }
                for r in self._rebalance_history
            ],
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _recompute_weights(self) -> None:
        """根据 mode 计算各策略目标权重（保证总和=1）。"""
        n = len(self._accounts)
        if n == 0:
            return

        if self.config.mode == WeightMode.EQUAL:
            w = 1.0 / n
            for acc in self._accounts.values():
                acc.target_weight = w

        elif self.config.mode == WeightMode.FIXED:
            total = sum(acc.target_weight for acc in self._accounts.values())
            if total > 0:
                for acc in self._accounts.values():
                    acc.target_weight /= total

        elif self.config.mode == WeightMode.RISK_PARITY:
            self._update_risk_parity_weights()

        # 约束：min/max weight
        self._apply_weight_constraints()

    def _update_risk_parity_weights(self) -> None:
        """风险平价：权重 = 1/vol / Σ(1/vol)。"""
        inv_vols = {}
        for name, acc in self._accounts.items():
            vol = acc.volatility
            inv_vols[name] = 1.0 / max(vol, 0.001)

        total_inv = sum(inv_vols.values())
        if total_inv > 0:
            for name, acc in self._accounts.items():
                acc.target_weight = inv_vols[name] / total_inv

        self._apply_weight_constraints()

    def _apply_weight_constraints(self) -> None:
        """约束权重在 [min, max] 区间并归一化。"""
        lo = self.config.min_strategy_weight
        hi = self.config.max_strategy_weight
        for acc in self._accounts.values():
            acc.target_weight = max(lo, min(hi, acc.target_weight))
        total = sum(acc.target_weight for acc in self._accounts.values())
        if total > 0:
            for acc in self._accounts.values():
                acc.target_weight /= total

    def _recompute_budgets(self) -> None:
        """根据目标权重重新分配额度。"""
        deployable = self.total_capital * (1 - self.config.reserve_ratio)
        for acc in self._accounts.values():
            acc.budget = round(deployable * acc.target_weight, 2)
