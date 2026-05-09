"""
core/brokers/fill_simulator.py — 共享 paper / simulated 撮合工具

P2-14: 抽取 backend.services.broker.PaperBroker、core.oms.EventDrivenPaperBroker、
core.brokers.simulated.SimulatedBroker 共享的滑点/涨跌停/佣金/撮合价计算，
避免 3 处近重复实现继续漂移。

使用方式：
    from core.brokers.fill_simulator import (
        simulate_fill_price,
        slippage_bps_actual,
        is_limit_breach,
        compute_commission,
    )

注意：本模块只提供「纯函数」，不持久化、不调用网络、不依赖 BrokerBase。
"""

from __future__ import annotations

import random
from typing import Tuple

DEFAULT_SLIPPAGE_BPS: float = 15.0
DEFAULT_LIMIT_PCT: float = 0.10           # A 股主板涨跌停 ±10%
DEFAULT_COMMISSION_RATE: float = 0.0003   # 万 3
DEFAULT_MIN_COMMISSION: float = 5.0       # 最低 5 元


def simulate_fill_price(
    ref_price: float,
    direction: str,
    price_type: str = 'market',
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    rng: random.Random = None,
) -> float:
    """模拟撮合价（市价 = 参考价 ± 随机滑点；限价 = 参考价直接成交）。"""
    if ref_price <= 0:
        return 0.0
    if price_type != 'market':
        return round(ref_price, 2)
    r = rng or random
    slip = r.uniform(-slippage_bps, slippage_bps) / 10_000.0
    return round(ref_price * (1 + slip), 2)


def slippage_bps_actual(fill_price: float, signal_price: float) -> float:
    """已成交价相对信号价的实际滑点（bps，正值=贵买/贱卖）。"""
    if signal_price <= 0:
        return 0.0
    return round((fill_price - signal_price) / signal_price * 10_000.0, 2)


def is_limit_breach(
    direction: str,
    fill_price: float,
    prev_close: float,
    limit_pct: float = DEFAULT_LIMIT_PCT,
) -> bool:
    """判定 fill_price 是否触及涨跌停。买入触涨停 / 卖出触跌停 → 拒单。"""
    if prev_close <= 0:
        return False
    limit_up = prev_close * (1 + limit_pct)
    limit_down = prev_close * (1 - limit_pct)
    if direction.upper() == 'BUY' and fill_price >= limit_up:
        return True
    if direction.upper() == 'SELL' and fill_price <= limit_down:
        return True
    return False


def compute_commission(
    fill_price: float,
    shares: int,
    rate: float = DEFAULT_COMMISSION_RATE,
    min_amount: float = DEFAULT_MIN_COMMISSION,
) -> float:
    """佣金 = max(min, fill_price × shares × rate)。"""
    if fill_price <= 0 or shares <= 0:
        return 0.0
    return max(min_amount, round(fill_price * shares * rate, 4))


def fill_summary(
    ref_price: float,
    direction: str,
    shares: int,
    price_type: str = 'market',
    signal_price: float = 0.0,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    rng: random.Random = None,
) -> Tuple[float, float, float]:
    """一次性返回 (fill_price, commission, slippage_bps_actual)。"""
    fill_price = simulate_fill_price(
        ref_price=ref_price,
        direction=direction,
        price_type=price_type,
        slippage_bps=slippage_bps,
        rng=rng,
    )
    commission = compute_commission(fill_price, shares)
    actual_slip = slippage_bps_actual(
        fill_price=fill_price,
        signal_price=signal_price if signal_price > 0 else ref_price,
    )
    return fill_price, commission, actual_slip


__all__ = [
    'DEFAULT_SLIPPAGE_BPS',
    'DEFAULT_LIMIT_PCT',
    'DEFAULT_COMMISSION_RATE',
    'DEFAULT_MIN_COMMISSION',
    'simulate_fill_price',
    'slippage_bps_actual',
    'is_limit_breach',
    'compute_commission',
    'fill_summary',
]
