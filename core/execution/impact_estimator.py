"""
core/execution/impact_estimator.py — 市场冲击估算

基于 Almgren-Chriss 简化模型估算大单执行的市场冲击成本。

简化假设：
  - 冲击 ≈ 10 × sqrt(participation_rate) 基点
  - 其中 participation_rate = 本次交易量 / 市场总成交量
  - 线性冲击（临时冲击）：5 × participation_rate 基点
  - 平方根冲击（永久冲击）：5 × sqrt(participation_rate) 基点

参考文献：
  Almgren & Chriss (2000), "Optimal execution of portfolio transactions"

用法：
    from core.execution.impact_estimator import ImpactEstimator

    # 估算总市场冲击（基点）
    bps = ImpactEstimator.estimate(order_qty=50000, market_daily_vol=2_000_000)
    # → 约 5.6 bps（参与率 2.5%）

    # 分解冲击
    perm, temp = ImpactEstimator.decompose(50000, 2_000_000)
"""

from __future__ import annotations

import math
from typing import Tuple


class ImpactEstimator:
    """
    市场冲击估算器（静态方法集合）。

    模型参数（可通过子类或 monkey-patch 调整）：
      PERMANENT_COEFF : 永久冲击系数（默认 5.0 bps，对应 sqrt 项）
      TEMPORARY_COEFF : 临时冲击系数（默认 5.0 bps，对应线性项）
    """

    PERMANENT_COEFF: float = 5.0   # bps
    TEMPORARY_COEFF: float = 5.0   # bps

    @classmethod
    def estimate(
        cls,
        order_qty: int,
        market_daily_vol: float,
        participation_cap: float = 0.30,
    ) -> float:
        """
        估算总市场冲击（基点）。

        Parameters
        ----------
        order_qty : int
            本次交易股数
        market_daily_vol : float
            当日市场预期成交量（股数）
        participation_cap : float
            参与率上限（默认 30%，超过则截断）

        Returns
        -------
        float — 市场冲击，单位基点（bps）
        """
        if market_daily_vol <= 0 or order_qty <= 0:
            return 0.0

        participation_rate = min(order_qty / market_daily_vol, participation_cap)
        perm, temp = cls._decompose(participation_rate)
        return perm + temp

    @classmethod
    def decompose(
        cls,
        order_qty: int,
        market_daily_vol: float,
    ) -> Tuple[float, float]:
        """
        分解市场冲击为永久冲击和临时冲击（均为基点）。

        Returns
        -------
        (permanent_bps, temporary_bps)
        """
        if market_daily_vol <= 0 or order_qty <= 0:
            return 0.0, 0.0
        participation_rate = min(order_qty / market_daily_vol, 0.30)
        return cls._decompose(participation_rate)

    @classmethod
    def _decompose(cls, participation_rate: float) -> Tuple[float, float]:
        permanent = cls.PERMANENT_COEFF * math.sqrt(participation_rate)
        temporary = cls.TEMPORARY_COEFF * participation_rate
        return round(permanent, 3), round(temporary, 3)

    @classmethod
    def estimate_cost(
        cls,
        order_qty: int,
        market_daily_vol: float,
        price: float,
    ) -> float:
        """
        估算市场冲击的绝对金额（元）。

        Parameters
        ----------
        order_qty : int
            交易股数
        market_daily_vol : float
            当日市场总成交量（股数）
        price : float
            参考价格（元/股）

        Returns
        -------
        float — 预估冲击成本（元）
        """
        impact_bps = cls.estimate(order_qty, market_daily_vol)
        return order_qty * price * impact_bps / 10_000

    @classmethod
    def max_order_size(
        cls,
        market_daily_vol: float,
        max_impact_bps: float = 10.0,
    ) -> int:
        """
        反推：在 max_impact_bps 约束下，最大可下单量。

        基于 impact ≈ PERMANENT_COEFF × sqrt(qty/vol) + TEMPORARY_COEFF × qty/vol
        对 sqrt(x) 求解（近似用 sqrt 项主导时）：
          x = (max_impact_bps / PERMANENT_COEFF)^2

        Parameters
        ----------
        market_daily_vol : float
            当日市场成交量
        max_impact_bps : float
            允许的最大冲击基点

        Returns
        -------
        int — 最大建议下单股数（100 的整数倍）
        """
        if max_impact_bps <= 0 or market_daily_vol <= 0:
            return 0
        # 近似解：只考虑永久冲击（保守估计）
        max_participation = (max_impact_bps / cls.PERMANENT_COEFF) ** 2
        max_qty = int(market_daily_vol * min(max_participation, 0.30))
        return (max_qty // 100) * 100
