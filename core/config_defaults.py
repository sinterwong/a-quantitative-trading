"""Canonical default values for all magic numbers.

Why this module exists
----------------------
Before this file, the same constant lived in 3-5 places:
- ``config/trading.yaml`` (user-facing default)
- ``core/config.py`` dataclass defaults
- ``core/config.py`` two YAML-loader fallback values
- ``core/brokers/simulated.py`` broker config defaults

Changing commission rate from 万三 to 万五 required editing 5 files —
inevitably one would be missed and silently keep the old rate.

R3-4: Every numeric constant lives here. Other modules import these
symbols. YAML still overrides at runtime (config/trading.yaml > defaults
> env), but the *default* is single-sourced.

When you add a new tunable, add it here first, then expose in YAML if
end users should configure it.
"""

from __future__ import annotations

from typing import Final

# ─── A 股交易成本 ───────────────────────────────────────────────────────────
# 万三佣金 + 千一印花税 + 5 bps 滑点是国内大多数券商对个人客户的常见水位。
COMMISSION_RATE: Final[float] = 0.0003      # 佣金率（双向）
STAMP_TAX_RATE: Final[float] = 0.001        # 印花税（卖出方向）
SLIPPAGE_BPS: Final[float] = 5.0            # 滑点（基点）

# ─── 风控阈值 ──────────────────────────────────────────────────────────────
MAX_NET_EXPOSURE: Final[float] = 0.90       # 组合净敞口上限
MAX_DAILY_LOSS: Final[float] = 0.02         # 日亏损熔断
ATR_STOP_MULTIPLIER: Final[float] = 3.0     # Chandelier Exit
TAKE_PROFIT_PCT: Final[float] = 0.20        # 止盈线
TRAILING_DRAWDOWN: Final[float] = 0.10      # 跟踪止损回撤
MAX_DRAWDOWN: Final[float] = 0.15           # 组合最大回撤
MAX_SECTOR_WEIGHT: Final[float] = 0.30      # 单行业上限
VAR_LIMIT: Final[float] = 0.03              # 单日 VaR 上限
MAX_CORRELATION: Final[float] = 0.85        # 持仓相关性上限


__all__ = [
    'COMMISSION_RATE',
    'STAMP_TAX_RATE',
    'SLIPPAGE_BPS',
    'MAX_NET_EXPOSURE',
    'MAX_DAILY_LOSS',
    'ATR_STOP_MULTIPLIER',
    'TAKE_PROFIT_PCT',
    'TRAILING_DRAWDOWN',
    'MAX_DRAWDOWN',
    'MAX_SECTOR_WEIGHT',
    'VAR_LIMIT',
    'MAX_CORRELATION',
]
