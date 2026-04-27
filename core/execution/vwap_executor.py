"""
core/execution/vwap_executor.py — VWAP 算法订单执行器

VWAP (Volume Weighted Average Price) 策略：
  - 按历史分钟成交量分布将大单拆分为 N 个子单
  - 目标：让实际成交均价贴近市场 VWAP，最小化市场冲击

算法逻辑：
  1. 获取标的历史分钟成交量（过去 N 天的同时段均值）
  2. 归一化为分布向量 P（sum=1）
  3. 每个时间片的目标股数 = total_shares × P[i]（向上取整到 100 股）
  4. 若无历史数据，退化为 TWAP（均匀分配）

用法：
    from core.execution.vwap_executor import VWAPExecutor

    executor = VWAPExecutor(
        symbol='000001.SZ',
        direction='BUY',
        total_shares=10000,
        duration_minutes=60,
        reference_price=15.5,
    )

    # 方式一：用历史成交量分布
    slices = executor.generate_slices(volume_profile=[...])

    # 方式二：从数据层自动获取历史分布（需 DataLayer 可用）
    slices = executor.generate_slices()

    # 回测模拟（不依赖实时行情）
    result = executor.simulate(minute_prices=[...], minute_volumes=[...])
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional
import numpy as np

from core.execution.algo_base import AlgoOrder, OrderSlice


# A 股主要交易时段（分钟偏移，相对于 09:30）
_A_SHARE_MORNING_MINUTES = list(range(0, 120))   # 09:30–11:30
_A_SHARE_AFTERNOON_MINUTES = list(range(150, 270))  # 13:00–15:00
_A_SHARE_TRADING_MINUTES = _A_SHARE_MORNING_MINUTES + _A_SHARE_AFTERNOON_MINUTES  # 240 分钟


def _default_volume_profile(n_slices: int) -> List[float]:
    """
    A 股典型 U 型成交量分布（开盘/收盘量大，盘中量小）。
    n_slices 个时间片的归一化分布。
    """
    if n_slices <= 0:
        return []
    # 用半正弦 + 两端增强近似 U 型分布
    t = np.linspace(0, np.pi, n_slices)
    profile = 0.3 + 0.7 * (np.sin(t) ** 0.3)  # 两端高、中间低
    # 进一步增强首尾
    if n_slices >= 4:
        profile[0] *= 2.0
        profile[-1] *= 1.5
    profile = profile / profile.sum()
    return profile.tolist()


class VWAPExecutor(AlgoOrder):
    """
    VWAP 算法执行器。

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
        参考价格（用于计算滑点）
    slice_interval : int
        子单间隔（分钟，默认 5）
    start_time : datetime or None
        开始执行时间（None = 当前时间）
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

    def generate_slices(
        self,
        volume_profile: Optional[List[float]] = None,
    ) -> List[OrderSlice]:
        """
        按 VWAP 分布生成子单列表。

        Parameters
        ----------
        volume_profile : List[float] or None
            归一化历史成交量分布（sum=1）。
            None 时尝试从 DataLayer 获取，仍失败则用 U 型默认分布。

        Returns
        -------
        List[OrderSlice]，按 scheduled_time 升序
        """
        n_slices = max(1, self.duration_minutes // self.slice_interval)

        # 获取成交量分布
        profile = self._resolve_profile(volume_profile, n_slices)

        # 按分布分配股数（最后一片吸收舍入误差）
        raw_shares = [self.total_shares * w for w in profile]
        slice_shares = [max(100, (int(s) // 100) * 100) for s in raw_shares]

        # 修正总量：因取整导致总量可能偏离
        allocated = sum(slice_shares)
        remainder = self.total_shares - allocated
        if remainder != 0 and slice_shares:
            # 将差额加到最后一片（确保总量正确）
            adj = (slice_shares[-1] + remainder // 100 * 100)
            slice_shares[-1] = max(100, adj)

        # 生成 OrderSlice
        slices: List[OrderSlice] = []
        for i, shares in enumerate(slice_shares):
            t = self.start_time + timedelta(minutes=i * self.slice_interval)
            sl = self._make_slice(
                target_shares=shares,
                scheduled_time=t,
                algo='VWAP',
                slice_index=i,
                slice_weight=profile[i],
            )
            slices.append(sl)

        self._slices = slices
        return slices

    def _resolve_profile(
        self,
        volume_profile: Optional[List[float]],
        n_slices: int,
    ) -> List[float]:
        """获取并验证成交量分布向量。"""
        if volume_profile is not None and len(volume_profile) > 0:
            # 重采样到 n_slices（简单线性插值）
            return self._resample_profile(volume_profile, n_slices)

        # 尝试从 DataLayer 获取历史分钟成交量
        try:
            profile = self._fetch_historical_profile(n_slices)
            if profile:
                return profile
        except Exception:
            pass

        # 退化：A 股 U 型默认分布
        return _default_volume_profile(n_slices)

    def _fetch_historical_profile(self, n_slices: int) -> Optional[List[float]]:
        """
        从 DataLayer 获取历史分钟成交量分布（近 5 日均值）。
        失败时返回 None。
        """
        try:
            from core.data_layer import DataLayer
            dl = DataLayer()
            df = dl.get_minute_bars(self.symbol, limit=n_slices * 5)
            if df is None or df.empty:
                return None
            # 按分钟序号分组取均值
            df = df.copy()
            df['minute_idx'] = range(len(df))
            df['bucket'] = df['minute_idx'] % n_slices
            avg_vol = df.groupby('bucket')['volume'].mean()
            total = avg_vol.sum()
            if total <= 0:
                return None
            profile = (avg_vol / total).tolist()
            return self._resample_profile(profile, n_slices)
        except Exception:
            return None

    @staticmethod
    def _resample_profile(profile: List[float], n_target: int) -> List[float]:
        """将 profile 线性插值到 n_target 个桶。"""
        if len(profile) == n_target:
            total = sum(profile)
            return [w / total for w in profile] if total > 0 else [1.0 / n_target] * n_target

        arr = np.array(profile, dtype=float)
        x_old = np.linspace(0, 1, len(arr))
        x_new = np.linspace(0, 1, n_target)
        resampled = np.interp(x_new, x_old, arr)
        resampled = np.clip(resampled, 0, None)
        total = resampled.sum()
        if total <= 0:
            return [1.0 / n_target] * n_target
        return (resampled / total).tolist()
