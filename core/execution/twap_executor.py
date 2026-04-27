"""
core/execution/twap_executor.py — TWAP 算法订单执行器

TWAP (Time Weighted Average Price) 策略：
  - 将大单在 duration_minutes 内均匀拆分为 N 个等量子单
  - 每隔 slice_interval 分钟发送一个子单
  - 最简单的算法执行策略，适合流动性好的标的

相比 VWAP 的区别：
  - TWAP：时间均匀分配（不考虑成交量）
  - VWAP：按成交量分布分配（在高成交量时段多挂单）

适用场景：
  - 大盘蓝筹（流动性充足，成交量分布对成本影响有限）
  - 需要确定性执行进度的情况

用法：
    from core.execution.twap_executor import TWAPExecutor

    executor = TWAPExecutor(
        symbol='600519.SH',
        direction='BUY',
        total_shares=5000,
        duration_minutes=120,
        reference_price=1800.0,
    )
    slices = executor.generate_slices()
    result = executor.simulate(minute_prices=[...], minute_volumes=[...])
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from core.execution.algo_base import AlgoOrder, OrderSlice


class TWAPExecutor(AlgoOrder):
    """
    TWAP 算法执行器。

    Parameters
    ----------
    symbol : str
        标的代码
    direction : Literal['BUY', 'SELL']
        方向
    total_shares : int
        目标总股数
    duration_minutes : int
        执行时长（分钟，默认 60）
    reference_price : float
        参考价格
    slice_interval : int
        子单间隔（分钟，默认 5）
    start_time : datetime or None
        开始执行时间（None = 当前时间）
    jitter_pct : float
        时间随机抖动比例（0–1），防止被识别为算法订单
        例如 0.1 = 在 ±10% 时间窗口内随机调整发单时刻
    """

    def __init__(
        self,
        symbol: str,
        direction: str = 'BUY',
        total_shares: int = 1000,
        duration_minutes: int = 60,
        reference_price: float = 0.0,
        slice_interval: int = 5,
        start_time: Optional[datetime] = None,
        jitter_pct: float = 0.0,
    ) -> None:
        super().__init__(
            symbol=symbol,
            direction=direction,  # type: ignore[arg-type]
            total_shares=total_shares,
            duration_minutes=duration_minutes,
            reference_price=reference_price,
        )
        self.slice_interval = max(1, slice_interval)
        self.start_time = start_time or datetime.now()
        self.jitter_pct = max(0.0, min(jitter_pct, 0.5))

    def generate_slices(
        self,
        volume_profile: Optional[List[float]] = None,
    ) -> List[OrderSlice]:
        """
        均匀生成子单列表（TWAP 不使用 volume_profile）。

        Returns
        -------
        List[OrderSlice]，按 scheduled_time 升序
        """
        n_slices = max(1, self.duration_minutes // self.slice_interval)

        # 均匀分配股数
        base_shares = (self.total_shares // n_slices // 100) * 100
        base_shares = max(100, base_shares)

        slices: List[OrderSlice] = []
        allocated = 0

        for i in range(n_slices):
            # 最后一片吸收剩余量
            if i == n_slices - 1:
                remaining = self.total_shares - allocated
                shares = max(100, (remaining // 100) * 100)
            else:
                shares = base_shares

            # 时间计算（可选随机抖动）
            t = self.start_time + timedelta(minutes=i * self.slice_interval)
            if self.jitter_pct > 0:
                import random
                jitter_sec = int(self.slice_interval * 60 * self.jitter_pct)
                t += timedelta(seconds=random.randint(-jitter_sec, jitter_sec))

            sl = self._make_slice(
                target_shares=shares,
                scheduled_time=t,
                algo='TWAP',
                slice_index=i,
                slice_weight=1.0 / n_slices,
            )
            slices.append(sl)
            allocated += shares

        self._slices = slices
        return slices

    @property
    def slice_count(self) -> int:
        """子单数量。"""
        return max(1, self.duration_minutes // self.slice_interval)

    @property
    def shares_per_slice(self) -> int:
        """每片目标股数（近似，最后一片可能不同）。"""
        n = self.slice_count
        return max(100, (self.total_shares // n // 100) * 100)
