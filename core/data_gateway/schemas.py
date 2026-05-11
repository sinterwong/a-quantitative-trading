# -*- coding: utf-8 -*-
"""
data_gateway.schemas — 业务定义的数据契约

系统根据自身需要定义数据形态,与具体 provider 完全解耦。
provider 只负责"把自家原始字段映射到这套契约的一个子集"。

字段来源(provenance)不存在于这些 dataclass 上,而是由
data_gateway.merge 维护的旁路记录。这避免数据类知道数据源。
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Quote:
    """统一实时行情快照(跨市场通用)。

    字段集是 data_layer.Quote 与 quote_data_source.QuoteData 的超集。
    """

    # ── 标识 ────────────────────────────────────────────────────────────────
    symbol: str = ""        # 标准化代码: 'sh600519' / 'hk00700' / 'usAAPL'
    name: str = ""
    code: str = ""          # 纯代码: '600519' / '00700' / 'AAPL'
    market: str = ""        # 'A' / 'INDEX' / 'HK' / 'US'

    # ── 价格 ────────────────────────────────────────────────────────────────
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    avg_price: float = 0.0

    # ── 涨跌 ────────────────────────────────────────────────────────────────
    change: float = 0.0
    pct_change: float = 0.0      # 涨跌幅 (%)

    # ── 成交 ────────────────────────────────────────────────────────────────
    volume: float = 0.0
    amount: float = 0.0
    turnover_rate: float = 0.0   # %
    volume_ratio: float = 0.0    # 量比

    # ── 盘口(level-1) ──────────────────────────────────────────────────────
    bid1_price: float = 0.0
    bid1_vol: float = 0.0
    ask1_price: float = 0.0
    ask1_vol: float = 0.0

    # ── 估值 ────────────────────────────────────────────────────────────────
    pe_ttm: float = 0.0
    pb: float = 0.0
    dividend_yield: float = 0.0  # %
    market_cap: float = 0.0      # 亿
    float_cap: float = 0.0       # 亿

    # ── 限制 ────────────────────────────────────────────────────────────────
    limit_up: float = 0.0
    limit_down: float = 0.0
    amplitude: float = 0.0       # 振幅 (%)

    # ── 52 周 ───────────────────────────────────────────────────────────────
    high_52w: float = 0.0
    low_52w: float = 0.0

    # ── 元数据 ──────────────────────────────────────────────────────────────
    currency: str = ""           # CNY / HKD / USD
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_valid(self) -> bool:
        return self.price > 0

    @property
    def day_change(self) -> float:
        return self.price - self.prev_close


@dataclass
class Fundamentals:
    """股票基本面快照。"""

    symbol: str = ""
    name: str = ""

    # 估值
    pe_ttm: float = 0.0
    pe_static: float = 0.0
    pb: float = 0.0
    ps_ttm: float = 0.0
    dividend_yield: float = 0.0  # %

    # 盈利
    roe_ttm: float = 0.0         # %
    eps_ttm: float = 0.0
    bps: float = 0.0

    # 财报
    revenue_ttm: float = 0.0     # 元
    profit_ttm: float = 0.0      # 元
    revenue_yoy: float = 0.0     # %
    profit_yoy: float = 0.0      # %
    ocf_to_profit: float = 0.0   # 经营现金流/净利润（现金流质量）

    # 市值
    market_cap: float = 0.0      # 亿
    float_cap: float = 0.0       # 亿

    # 分类
    industry: str = ""
    sector: str = ""

    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_valid(self) -> bool:
        return bool(self.symbol)


@dataclass
class SectorRanking:
    """板块排名(涨跌幅 + 资金流)。"""

    code: str = ""               # 板块代码: 'BK0716' / 'SINA_GNhwqc'
    name: str = ""               # '华为汽车'
    change_pct: float = 0.0      # %
    net_flow: float = 0.0        # 资金净流入(元)
    amount: float = 0.0          # 成交额(元)
    rank_perf: int = 0           # 涨幅排名(1=最强)
    rank_flow: int = 0           # 资金流排名(1=最强)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_valid(self) -> bool:
        return bool(self.code)


@dataclass
class SectorConstituent:
    """板块成分股(轻量行情)。"""

    symbol: str = ""             # 标准化代码: 'sh600519'
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    amount: float = 0.0
    volume: float = 0.0

    @property
    def is_valid(self) -> bool:
        return bool(self.symbol)


@dataclass
class NorthFlow:
    """北/南向资金快照(取代 data_layer.NorthFlowSnapshot)。"""

    net_north_yi: float = 0.0    # 北向净流入(亿,正=净买入)
    net_south_yi: float = 0.0    # 南向净流入(亿)
    direction: str = "NEUTRAL"   # BUY / SELL / NEUTRAL
    stale: bool = False
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MarketIndexSnapshot:
    """单一外盘/指数快照(取代 SPFutures/VIX/HSI 各自的 dataclass)。"""

    code: str = ""               # 'VIX' / 'SPX_FUT' / 'HSI' / 'usSPY' ...
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_valid(self) -> bool:
        return self.price > 0


__all__ = [
    "Quote",
    "Fundamentals",
    "SectorRanking",
    "SectorConstituent",
    "NorthFlow",
    "MarketIndexSnapshot",
]
