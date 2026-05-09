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

    模型参数：
      PERMANENT_COEFF : 永久冲击系数（默认 5.0 bps，对应 sqrt 项）
      TEMPORARY_COEFF : 临时冲击系数（默认 5.0 bps，对应线性项）

    P1-7：支持从 config/trading.yaml execution 节点加载系数。
    调用 ImpactEstimator.load_from_config() 在启动时同步。
    """

    PERMANENT_COEFF: float = 5.0   # bps
    TEMPORARY_COEFF: float = 5.0   # bps

    @classmethod
    def load_from_config(cls) -> bool:
        """
        从配置同步系数。优先级（高 → 低）：
          1. outputs/tca_calibration.json（P1-12 反馈闭环写出）
          2. config/trading.yaml execution.impact_*_coeff
          3. 类默认值

        Returns
        -------
        True 表示成功更新，False 表示降级到默认值。
        """
        # 1. 反馈调整结果（最高优先级）
        try:
            import json
            import os
            cal_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'outputs', 'tca_calibration.json',
            )
            if os.path.exists(cal_path):
                with open(cal_path, encoding='utf-8') as f:
                    cal = json.load(f)
                perm = cal.get('impact_permanent_coeff')
                temp = cal.get('impact_temporary_coeff')
                if perm is not None and temp is not None:
                    cls.PERMANENT_COEFF = float(perm)
                    cls.TEMPORARY_COEFF = float(temp)
                    return True
        except Exception:
            pass

        # 2. trading.yaml
        try:
            from core.config import load_config
            cfg = load_config()
            cls.PERMANENT_COEFF = float(cfg.execution.impact_permanent_coeff)
            cls.TEMPORARY_COEFF = float(cfg.execution.impact_temporary_coeff)
            return True
        except Exception:
            return False

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
