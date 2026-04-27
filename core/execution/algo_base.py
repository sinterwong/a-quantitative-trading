"""
core/execution/algo_base.py — 算法订单基类

定义 AlgoOrder 抽象接口和 OrderSlice（子单）数据结构。
所有算法执行器（VWAP / TWAP / POV）均继承此基类。

设计原则：
  - 算法订单将大单拆分为多个时间片子单（OrderSlice）
  - 子单列表由 generate_slices() 生成，调用方负责逐步发送
  - 支持估算市场冲击成本（通过 ImpactEstimator 注入）
  - 算法状态可序列化，方便日志和审计
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
import uuid


# ---------------------------------------------------------------------------
# OrderSlice — 子单
# ---------------------------------------------------------------------------

@dataclass
class OrderSlice:
    """
    算法订单的一个子单（时间片）。

    Attributes
    ----------
    slice_id : str
        子单 ID（UUID 前8位）
    parent_order_id : str
        所属算法订单 ID
    symbol : str
        标的代码
    direction : Literal['BUY', 'SELL']
        买卖方向
    target_shares : int
        该时间片的目标股数
    scheduled_time : datetime
        计划发送时间
    sent_shares : int
        实际已发送股数（发送后更新）
    filled_shares : int
        实际成交股数（回调更新）
    fill_price : float
        成交均价
    status : str
        'PENDING' / 'SENT' / 'FILLED' / 'CANCELLED'
    """
    slice_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8].upper())
    parent_order_id: str = ''
    symbol: str = ''
    direction: Literal['BUY', 'SELL'] = 'BUY'
    target_shares: int = 0
    scheduled_time: datetime = field(default_factory=datetime.now)
    sent_shares: int = 0
    filled_shares: int = 0
    fill_price: float = 0.0
    status: str = 'PENDING'
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def participation_rate(self) -> float:
        """该时间片占总量的比例（需调用方设置 metadata['total_shares']）。"""
        total = self.metadata.get('total_shares', 0)
        return self.target_shares / total if total > 0 else 0.0

    def to_dict(self) -> Dict:
        return {
            'slice_id': self.slice_id,
            'parent_order_id': self.parent_order_id,
            'symbol': self.symbol,
            'direction': self.direction,
            'target_shares': self.target_shares,
            'scheduled_time': self.scheduled_time.isoformat(),
            'filled_shares': self.filled_shares,
            'fill_price': self.fill_price,
            'status': self.status,
        }


# ---------------------------------------------------------------------------
# AlgoOrderResult — 执行结果汇总
# ---------------------------------------------------------------------------

@dataclass
class AlgoOrderResult:
    """算法订单执行汇总报告。"""
    order_id: str
    symbol: str
    direction: str
    target_shares: int
    filled_shares: int
    avg_fill_price: float
    slippage_bps: float          # 相对 VWAP/TWAP 参考价的滑点（基点）
    market_impact_bps: float     # 估算市场冲击（基点）
    n_slices: int
    duration_minutes: float
    slices: List[OrderSlice]
    completed_at: datetime = field(default_factory=datetime.now)

    @property
    def fill_rate(self) -> float:
        """成交率 = filled / target。"""
        return self.filled_shares / self.target_shares if self.target_shares > 0 else 0.0

    @property
    def total_cost_bps(self) -> float:
        """总成本（滑点 + 市场冲击），单位基点。"""
        return self.slippage_bps + self.market_impact_bps


# ---------------------------------------------------------------------------
# AlgoOrder — 算法订单抽象基类
# ---------------------------------------------------------------------------

class AlgoOrder(ABC):
    """
    算法订单基类。

    子类须实现 generate_slices()，根据历史成交量分布（或均匀）将大单拆分。
    simulate() 方法提供在历史数据上的模拟执行功能（用于回测 / 压力测试）。

    Parameters
    ----------
    symbol : str
        标的代码
    direction : Literal['BUY', 'SELL']
        方向
    total_shares : int
        目标总股数（需是 100 的整数倍，A 股最小交易单位）
    duration_minutes : int
        执行时长（分钟）
    reference_price : float
        参考价格（用于计算滑点，通常取下单时市价）
    """

    def __init__(
        self,
        symbol: str,
        direction: Literal['BUY', 'SELL'],
        total_shares: int,
        duration_minutes: int = 60,
        reference_price: float = 0.0,
    ) -> None:
        self.order_id = uuid.uuid4().hex[:12].upper()
        self.symbol = symbol
        self.direction = direction
        # A 股最小交易单位 100 股，向上取整
        self.total_shares = max(100, (total_shares // 100) * 100)
        self.duration_minutes = duration_minutes
        self.reference_price = reference_price
        self._slices: List[OrderSlice] = []
        self._created_at = datetime.now()

    @abstractmethod
    def generate_slices(
        self,
        volume_profile: Optional[List[float]] = None,
    ) -> List[OrderSlice]:
        """
        生成子单列表。

        Parameters
        ----------
        volume_profile : List[float] or None
            历史分钟成交量分布（归一化，sum=1）。
            None 时各实现自行处理（TWAP 直接均分，VWAP 用默认分布）。

        Returns
        -------
        List[OrderSlice]，按 scheduled_time 升序排列
        """

    def simulate(
        self,
        minute_prices: List[float],
        minute_volumes: List[float],
        volume_profile: Optional[List[float]] = None,
    ) -> AlgoOrderResult:
        """
        在历史分钟数据上模拟执行（用于回测 / 压力测试）。

        Parameters
        ----------
        minute_prices : List[float]
            每分钟的 VWAP 成交价（长度 = duration_minutes）
        minute_volumes : List[float]
            每分钟的成交量
        volume_profile : List[float] or None
            历史成交量分布（用于生成子单）

        Returns
        -------
        AlgoOrderResult
        """
        slices = self.generate_slices(volume_profile)

        # 模拟成交：子单股数 ≤ 当分钟实际可成交量的 30%（防止冲击过大）
        total_filled = 0
        total_value = 0.0

        for i, sl in enumerate(slices):
            if i >= len(minute_prices):
                break
            price = minute_prices[i]
            avail = int(minute_volumes[i] * 0.30)
            filled = min(sl.target_shares, max(avail, 100))
            filled = (filled // 100) * 100  # 取整到手
            sl.filled_shares = filled
            sl.fill_price = price
            sl.status = 'FILLED' if filled >= sl.target_shares else 'PARTIAL'
            total_filled += filled
            total_value += filled * price

        avg_price = total_value / total_filled if total_filled > 0 else self.reference_price

        # 计算 VWAP 参考价
        vwap_ref = (
            sum(p * v for p, v in zip(minute_prices, minute_volumes))
            / max(sum(minute_volumes), 1)
        ) if minute_volumes else self.reference_price

        slippage_bps = (
            (avg_price - vwap_ref) / vwap_ref * 10_000
            if vwap_ref > 0 else 0.0
        )
        if self.direction == 'BUY':
            slippage_bps = abs(slippage_bps)  # 买入：实际价高于参考 = 正滑点
        else:
            slippage_bps = -abs(slippage_bps)  # 卖出：实际价低于参考 = 正滑点

        from core.execution.impact_estimator import ImpactEstimator
        avg_vol = sum(minute_volumes) / len(minute_volumes) if minute_volumes else 1.0
        impact_bps = ImpactEstimator.estimate(self.total_shares, avg_vol * len(minute_volumes))

        return AlgoOrderResult(
            order_id=self.order_id,
            symbol=self.symbol,
            direction=self.direction,
            target_shares=self.total_shares,
            filled_shares=total_filled,
            avg_fill_price=round(avg_price, 3),
            slippage_bps=round(slippage_bps, 2),
            market_impact_bps=round(impact_bps, 2),
            n_slices=len(slices),
            duration_minutes=self.duration_minutes,
            slices=slices,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_slice(
        self,
        target_shares: int,
        scheduled_time: datetime,
        **meta,
    ) -> OrderSlice:
        """工厂方法：创建属于本订单的子单。"""
        return OrderSlice(
            parent_order_id=self.order_id,
            symbol=self.symbol,
            direction=self.direction,
            target_shares=max(100, (target_shares // 100) * 100),
            scheduled_time=scheduled_time,
            metadata={'total_shares': self.total_shares, **meta},
        )

    @property
    def slices(self) -> List[OrderSlice]:
        return list(self._slices)
