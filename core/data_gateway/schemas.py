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
from typing import Dict, List, Optional


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
    # MERGE_FIELDS 合并时由贡献源的 HealthTracker.score() 平均值给出，单源直接
    # 透传时为该源的健康度。范围 [0, 1]，越接近 1 表示数据可信度越高。
    confidence: float = 1.0

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
    net_margin: float = 0.0        # % 销售净利率（npMargin）
    gross_margin: float = 0.0      # % 销售毛利率（gpMargin）

    # 财报
    revenue_ttm: float = 0.0     # 元
    profit_ttm: float = 0.0      # 元
    revenue_yoy: float = 0.0     # %
    profit_yoy: float = 0.0      # %
    ocf_to_profit: float = 0.0   # 经营现金流/净利润（现金流质量）

    # 增长（来自 query_growth_data）
    eps_yoy: float = 0.0        # EPS YoY %
    asset_yoy: float = 0.0       # 总资产 YoY %

    # 市值
    market_cap: float = 0.0      # 亿
    float_cap: float = 0.0       # 亿

    # 分类
    industry: str = ""
    sector: str = ""

    timestamp: datetime = field(default_factory=datetime.now)
    # 出口时由 gateway 据 timestamp 与当前时间差填入，反映该快照在内存/磁盘
    # 缓存里待过的秒数。新鲜获取时 ≈ 0；命中 L1/L2 时为对应等待时长。
    stale_seconds: int = 0

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
class BalanceSheet:
    """股票资产负债表快照（来自 query_balance_data）。"""

    symbol: str = ""

    # 资产负债
    total_asset: float = 0.0     # 元
    total_liability: float = 0.0  # 元
    debt_to_equity: float = 0.0  # 资产负债率 %

    # 流动性
    current_ratio: float = 0.0    # 流动比率
    quick_ratio: float = 0.0      # 速动比率

    # 股东权益
    equity: float = 0.0           # 股东权益（元）

    timestamp: datetime = field(default_factory=datetime.now)
    # 同 Fundamentals.stale_seconds，由 gateway 在出口处计算。
    stale_seconds: int = 0

    @property
    def is_valid(self) -> bool:
        return bool(self.symbol)


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


@dataclass
class MarginSnapshot:
    """单日融资融券快照（取自 margin_flow 时序末行）。"""

    date: datetime = field(default_factory=datetime.now)
    margin_balance: float = 0.0       # 融资余额(元)
    net_buy: float = 0.0              # 融资净买入(元)
    short_balance: float = 0.0        # 融券余额(元)

    @property
    def is_valid(self) -> bool:
        return self.margin_balance > 0 or self.short_balance > 0


@dataclass
class FundFlowSnapshot:
    """单日资金流快照（取自 fund_flow 时序末行）。"""

    date: datetime = field(default_factory=datetime.now)
    main_net_inflow: float = 0.0      # 主力净流入(元)
    super_net_inflow: float = 0.0     # 超大单
    large_net_inflow: float = 0.0     # 大单
    medium_net_inflow: float = 0.0    # 中单
    small_net_inflow: float = 0.0     # 小单
    main_net_ratio: float = 0.0       # 主力净占比(%)

    @property
    def is_valid(self) -> bool:
        return any([
            self.main_net_inflow, self.super_net_inflow,
            self.large_net_inflow, self.medium_net_inflow, self.small_net_inflow,
        ])


@dataclass
class MacroSnapshot:
    """宏观指标当前最新值集合（取自 macro 各序列的末行）。"""

    pmi: float = 0.0                  # 制造业 PMI
    m2_yoy: float = 0.0               # M2 同比 %
    credit_yoy: float = 0.0           # 社融存量同比 %
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_valid(self) -> bool:
        return self.pmi > 0 or self.m2_yoy > 0 or self.credit_yoy > 0


@dataclass
class StockProfile:
    """聚合视图：一次 gw.profile(symbol) 返回的"信息包"。

    G2 设计核心：调用方不需要关心数据从哪来——任何切片可能来自不同 provider，
    任何切片缺失都不阻塞主流程，只反映在 completeness 上。

    切片来源由 provenance 字典披露（{slice_name: primary_provider}）。
    """

    symbol: str = ""
    as_of: datetime = field(default_factory=datetime.now)

    # 切片(任意一个为 None 表示该源不可用)
    quote: Optional[Quote] = None
    fundamentals: Optional[Fundamentals] = None
    balance_sheet: Optional[BalanceSheet] = None
    margin: Optional[MarginSnapshot] = None
    fund_flow_latest: Optional[FundFlowSnapshot] = None
    headlines: List[str] = field(default_factory=list)
    macro: Optional[MacroSnapshot] = None

    # 元数据
    completeness: float = 0.0                          # 0-1，已填充切片占比
    provenance: Dict[str, str] = field(default_factory=dict)  # slice → primary provider
    # 任意切片为 None / 空时记录其名称（quote/fundamentals/balance_sheet/margin/
    # fund_flow/headlines/macro），便于调用方分辨是"未启用"还是"启用但拉不到"。
    missing_capabilities: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """至少有 quote 或 fundamentals 才视为有效。"""
        return (
            (self.quote is not None and self.quote.is_valid)
            or (self.fundamentals is not None and self.fundamentals.is_valid)
        )


@dataclass
class NewsItem:
    """单条新闻/快讯条目（G5）。

    `_merged_list_fetch` 在多源 List[NewsItem] 上做"标题归一去重 + 时间倒序"，
    保留首次出现的条目。timestamp 缺失时该条排在末尾（按 source 顺序）。

    Attributes:
        title: 标题（已去前后空白，但未归一化前缀）。
        timestamp: 发布时间；若 provider 无法提取则为 None。
        source: 数据源标记（"eastmoney" / "akshare" 等），便于调试与
            provenance 复盘。provider 不强制写，gateway 在 merge 阶段
            可补上。
        content: 正文/摘要（可选）；当前主要供调试，不参与 dedupe。
    """
    title: str = ""
    timestamp: Optional[datetime] = None
    source: str = ""
    content: str = ""

    @property
    def has_timestamp(self) -> bool:
        return self.timestamp is not None


__all__ = [
    "Quote",
    "Fundamentals",
    "BalanceSheet",
    "SectorRanking",
    "SectorConstituent",
    "NorthFlow",
    "MarketIndexSnapshot",
    "MarginSnapshot",
    "FundFlowSnapshot",
    "MacroSnapshot",
    "NewsItem",
    "StockProfile",
]
