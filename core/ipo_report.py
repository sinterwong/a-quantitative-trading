"""
core/ipo_report.py — A股 IPO 打新分析报告数据结构（feature/ipo-stars）

功能：
  - 定义 IPO 打新分析的完整数据结构（dataclass）
  - 覆盖：新股基本信息 / 中签率 / 上市表现 / 收益率 / 风险指标
  - 支持按时间段、板块、上市板块等维度聚合

用法：
    from core.ipo_report import (
        IPOAnalysisReport, IPOStockRecord, IPOStatistics,
        IPOPerformanceMetrics, IPOStarRating,
    )

    report = IPOAnalysisReport(...)
    print(report.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("core.ipo_report")

# ---------------------------------------------------------------------------
# 常量 / 枚举
# ---------------------------------------------------------------------------


class MarketBoard(Enum):
    """上市板块。"""
    MAIN_BOARD = "主板"
    GEM = "创业板"       # Growth Enterprise Market
    STAR = "科创板"       # STAR Market
    NEEQ = "北交所"       # New OTC Equity Market


class LockupPeriod(Enum):
    """锁定期类型。"""
    NONE = "无锁定期"
    FIRST_DAY = "首日即解禁"
    ONE_YEAR = "一年锁定期"
    THREE_YEAR = "三年锁定期"


class IPORating(Enum):
    """IPO 综合评级（星级）。"""
    ONE_STAR = 1
    TWO_STARS = 2
    THREE_STARS = 3
    FOUR_STARS = 4
    FIVE_STARS = 5

    @classmethod
    def from_score(cls, score: float) -> IPORating:
        """根据综合打分（0~1）返回评级。"""
        if score >= 0.8:
            return cls.FIVE_STARS
        elif score >= 0.6:
            return cls.FOUR_STARS
        elif score >= 0.4:
            return cls.THREE_STARS
        elif score >= 0.2:
            return cls.TWO_STARS
        else:
            return cls.ONE_STAR


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class IPOStockRecord:
    """
    单只新股记录。

    Attributes
    ----------
    stock_code : str
        股票代码（如 '601888'）。
    stock_name : str
        股票名称。
    ipo_date : date
        申购日期。
    listing_date : date
        上市日期。
    board : MarketBoard
        上市板块。
    issue_price : float
        发行价（元）。
    issue_pe : float
        发行市盈率（摊薄）。
    lot_rate : float
        网上中签率（小数，如 0.001 表示千分之一）。
    num_shares_offered : int
        公开募股数量（股）。
    total_proceeds : float
        募集资金总额（万元）。
    lockup_period : LockupPeriod
        锁定期类型。
    listing_opening_price : float
        上市首日开盘价（元）。
    listing_close_price : float
        上市首日收盘价（元）。
    listing_high_price : float
        上市首日最高价（元）。
    listing_low_price : float
        上市首日最低价（元）。
    turnover_rate : float
        首日换手率（小数）。
    listing_first_day_return : float
        上市首日收益率（小数，涨跌幅）。
    cumulative_return_30d : float
        上市后30天累计收益率。
    cumulative_return_90d : float
        上市后90天累计收益率。
    cumulative_return_1y : float
        上市后1年累计收益率。
    is_st : bool
        是否为 ST/*ST。
    industry : str
        所属行业。
    note : str
        备注信息。
    """

    stock_code: str
    stock_name: str
    ipo_date: date
    listing_date: date
    board: MarketBoard
    issue_price: float
    issue_pe: float
    lot_rate: float
    num_shares_offered: int
    total_proceeds: float          # 万元
    lockup_period: LockupPeriod
    listing_opening_price: float
    listing_close_price: float
    listing_high_price: float
    listing_low_price: float
    turnover_rate: float
    listing_first_day_return: float   # 小数，如 0.44 表示涨幅44%
    cumulative_return_30d: float = 0.0
    cumulative_return_90d: float = 0.0
    cumulative_return_1y: float = 0.0
    is_st: bool = False
    industry: str = ""
    note: str = ""

    # ------------------------------------------------------------------ #
    # 计算属性
    # ------------------------------------------------------------------ #

    @property
    def first_day_limit_up(self) -> bool:
        """上市首日是否触及涨停（主板10%，科创/创业/北交44%）。"""
        if self.board == MarketBoard.MAIN_BOARD:
            return self.listing_first_day_return >= 0.099  # ~10%
        else:  # STAR/GEM/NEEQ 20%涨跌停，但新股首日最高44%
            return self.listing_first_day_return >= 0.43

    @property
    def price_change(self) -> float:
        """上市首日价格变动（元）。"""
        return self.listing_close_price - self.issue_price

    @property
    def star_rating(self) -> IPORating:
        """
        根据中签率和首日收益率计算星级。

        打分规则（可调）：
          - 中签率（权重 40%）：越低打分越高（稀缺性）
          - 首日收益率（权重 40%）：越高打分越高
          - 发行PE（权重 20%）：相对行业均值的折溢价
        """
        # 中签率打分（越低越好，取对数归一化）
        lot_score = 1.0 - np.clip(np.log1p(self.lot_rate * 1000) / 4.0, 0.0, 1.0)
        # 首日收益打分（越高越好）
        ret_score = np.clip(self.listing_first_day_return / 1.0, 0.0, 1.0)
        # PE打分（假设合理区间 10~50）
        pe_score = 1.0 - np.clip((self.issue_pe - 20) / 40.0, 0.0, 1.0) if self.issue_pe > 0 else 0.5

        composite = 0.4 * lot_score + 0.4 * ret_score + 0.2 * pe_score
        return IPORating.from_score(composite)


@dataclass
class IPOStatistics:
    """
    IPO 打新统计汇总。

    Attributes
    ----------
    period_start : date
        统计区间起始日。
    period_end : date
        统计区间截止日。
    total_ipo_count : int
        区间内 IPO 总数量。
    star5_count : int
        五星评级数量。
    star4_count : int
        四星评级数量。
    star3_count : int
        三星评级数量。
    avg_first_day_return : float
        首日平均收益率。
    median_first_day_return : float
        首日收益率中位数。
    std_first_day_return : float
        首日收益率标准差。
    avg_lot_rate : float
        平均中签率。
    median_lot_rate : float
        中签率中位数。
    total_proceeds : float
        区间内募集资金总额（万元）。
    limit_up_count : int
        触及涨停的新股数量。
    loss_count : int
        首日破发（负收益）数量。
    loss_rate : float
        破发率（小数）。
    """

    period_start: date
    period_end: date
    total_ipo_count: int
    star5_count: int = 0
    star4_count: int = 0
    star3_count: int = 0
    avg_first_day_return: float = 0.0
    median_first_day_return: float = 0.0
    std_first_day_return: float = 0.0
    avg_lot_rate: float = 0.0
    median_lot_rate: float = 0.0
    total_proceeds: float = 0.0
    limit_up_count: int = 0
    loss_count: int = 0
    loss_rate: float = 0.0

    def __str__(self) -> str:
        return (
            f"IPO 统计 [{self.period_start} ~ {self.period_end}]\n"
            f"  总数量: {self.total_ipo_count}  |  五星: {self.star5_count}  四星: {self.star4_count}  三星: {self.star3_count}\n"
            f"  首日收益: avg={self.avg_first_day_return*100:.2f}%  "
            f"median={self.median_first_day_return*100:.2f}%  std={self.std_first_day_return*100:.2f}%\n"
            f"  中签率: avg={self.avg_lot_rate*100:.4f}%  median={self.median_lot_rate*100:.4f}%\n"
            f"  破发率: {self.loss_rate*100:.2f}%  涨停: {self.limit_up_count}\n"
            f"  募集总额: {self.total_proceeds:,.0f} 万元"
        )


@dataclass
class IPOPerformanceMetrics:
    """
    单只 IPO 的绩效指标（用于组合模拟）。

    Attributes
    ----------
    stock_code : str
        股票代码。
    allocation_rate : float
        理论配置权重（小数）。
    expected_return : float
        预期收益率（小数），基于历史统计。
    max_loss : float
        最大回撤/亏损（小数）。
    sharpe_like : float
        夏普比（简化版：无风险收益率假设 3% 年化）。
    risk_adjusted_score : float
        风险调整后打分（0~1）。
    win_probability : float
        盈利概率（小数，基于历史胜率）。
    holding_days : int
        建议持有天数。
    confidence : float
        置信度（0~1），样本量不足时降低。
    """

    stock_code: str
    allocation_rate: float = 0.0
    expected_return: float = 0.0
    max_loss: float = 0.0
    sharpe_like: float = 0.0
    risk_adjusted_score: float = 0.0
    win_probability: float = 0.0
    holding_days: int = 1
    confidence: float = 1.0


@dataclass
class BoardBreakdown:
    """
    按上市板块分组的汇总。

    Attributes
    ----------
    board : MarketBoard
        板块。
    ipo_count : int
        该板块 IPO 数量。
    avg_first_day_return : float
        该板块平均首日收益率。
    median_first_day_return : float
        该板块首日收益率中位数。
    total_proceeds : float
        该板块募集资金总额（万元）。
    star5_count : int
        五星评级数量。
    loss_rate : float
        该板块破发率。
    """

    board: MarketBoard
    ipo_count: int = 0
    avg_first_day_return: float = 0.0
    median_first_day_return: float = 0.0
    total_proceeds: float = 0.0
    star5_count: int = 0
    loss_rate: float = 0.0


@dataclass
class IndustryBreakdown:
    """
    按行业分组的汇总。

    Attributes
    ----------
    industry : str
        行业名称。
    ipo_count : int
        该行业 IPO 数量。
    avg_first_day_return : float
        平均首日收益率。
    median_first_day_return : float
        首日收益率中位数。
    star5_count : int
        五星评级数量。
    """

    industry: str
    ipo_count: int = 0
    avg_first_day_return: float = 0.0
    median_first_day_return: float = 0.0
    star5_count: int = 0


@dataclass
class IPOAnalysisReport:
    """
    A 股 IPO 打新分析报告（主结构）。

    Attributes
    ----------
    report_id : str
        报告唯一标识（UUID 或时间戳）。
    generated_at : datetime
        报告生成时间。
    period_start : date
        统计区间起始日。
    period_end : date
        统计区间截止日。
    stocks : List[IPOStockRecord]
        新股记录列表。
    statistics : IPOStatistics
        统计汇总。
    performance : Dict[str, IPOPerformanceMetrics]
        各股票绩效指标，key = stock_code。
    board_breakdown : List[BoardBreakdown]
        按板块分组的汇总。
    industry_breakdown : List[IndustryBreakdown]
        按行业分组的汇总。
    notes : List[str]
        备注信息（如异常值说明、数据缺失说明）。
    metadata : Dict[str, str]
        额外元数据（如数据源、日期等）。
    """

    report_id: str
    generated_at: datetime
    period_start: date
    period_end: date
    stocks: List[IPOStockRecord] = field(default_factory=list)
    statistics: Optional[IPOStatistics] = None
    performance: Dict[str, IPOPerformanceMetrics] = field(default_factory=dict)
    board_breakdown: List[BoardBreakdown] = field(default_factory=list)
    industry_breakdown: List[IndustryBreakdown] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #

    def add_stock(self, record: IPOStockRecord) -> None:
        """追加一只新股记录。"""
        self.stocks.append(record)

    def get_by_board(self, board: MarketBoard) -> List[IPOStockRecord]:
        """筛选指定板块的新股。"""
        return [s for s in self.stocks if s.board == board]

    def get_by_rating(self, rating: IPORating) -> List[IPOStockRecord]:
        """筛选指定星级的新股。"""
        return [s for s in self.stocks if s.star_rating == rating]

    def get_top_n(self, n: int = 10, by: str = "first_day_return") -> List[IPOStockRecord]:
        """
        返回首日收益率 top-N 新股。

        Parameters
        ----------
        n : int
            返回数量。
        by : str
            排序依据字段（支持 'first_day_return', 'lot_rate', 'issue_pe'）。
        """
        if by == "first_day_return":
            return sorted(self.stocks, key=lambda s: s.listing_first_day_return, reverse=True)[:n]
        elif by == "lot_rate":
            return sorted(self.stocks, key=lambda s: s.lot_rate)[:n]
        elif by == "issue_pe":
            return sorted(self.stocks, key=lambda s: s.issue_pe)[:n]
        else:
            return self.stocks[:n]

    def summary(self) -> str:
        """生成单行摘要（用于日志和展示）。"""
        if self.statistics is None:
            return f"IPOAnalysisReport(id={self.report_id}, n={len(self.stocks)})"
        s = self.statistics
        return (
            f"IPOReport({self.period_start}~{self.period_end}): "
            f"{s.total_ipo_count}只 "
            f"avg_return={s.avg_first_day_return*100:.1f}% "
            f"loss_rate={s.loss_rate*100:.1f}% "
            f"五星={s.star5_count}"
        )

    def to_dict(self) -> Dict:
        """序列化报告为字典（JSON 可直接使用）。"""
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at.isoformat(),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "n_stocks": len(self.stocks),
            "statistics": {
                "period_start": self.statistics.period_start.isoformat() if self.statistics else None,
                "period_end": self.statistics.period_end.isoformat() if self.statistics else None,
                "total_ipo_count": self.statistics.total_ipo_count if self.statistics else 0,
                "avg_first_day_return": self.statistics.avg_first_day_return if self.statistics else 0.0,
                "median_first_day_return": self.statistics.median_first_day_return if self.statistics else 0.0,
                "loss_rate": self.statistics.loss_rate if self.statistics else 0.0,
            } if self.statistics else None,
            "notes": self.notes,
            "metadata": self.metadata,
        }
