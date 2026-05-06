"""
models.py — IPO Stars 数据模型
"""

from typing import NamedTuple, Dict, List, Optional


# ─── IPO 标的元数据 ───────────────────────────────────────────

class IPOCandidate(NamedTuple):
    """港股新股候选标的"""
    code: str                       # 港股代码，如 "09696"
    name: str                       # 股票名称
    status: str                     # upcoming | subscripting | allotted | listed | closed
    listing_date: str               # 预计上市日期 (YYYY-MM-DD)
    offer_price_low: float          # 招股价下限
    offer_price_high: float         # 招股价上限
    offer_price_final: float        # 最终定价（配售结果后，未公布为 0）
    issue_size: float               # 发行规模（亿港元）
    sponsor: str                    # 保荐人
    stabilizer: str                 # 第一稳价人
    cornerstone_names: str          # 基石投资者名单（逗号分隔）
    cornerstone_pct: float          # 基石投资者认购占比 (0~1)
    public_offer_multiple: float    # 公开发售超购倍数
    clawback_pct: float             # 回拨比例 (0~1)
    margin_multiple: float          # 孖展倍数（综合各券商）
    industry: str                   # 二级行业分类
    pre_ipo_cost: float             # Pre-IPO 最后一轮融资单价（0 表示未知）


# ─── 评分结果 ─────────────────────────────────────────────────

class ScoringResult(NamedTuple):
    """单维度评分输出"""
    dimension: str                  # sentiment | chips | narrative | valuation
    score: float                    # 0.0 ~ 1.0
    weight: float                   # 权重
    weighted_score: float           # score * weight
    details: Dict                   # 计算明细


# ─── 挂单策略 ─────────────────────────────────────────────────

class PricingStrategy(NamedTuple):
    """挂单价建议"""
    style: str                      # conservative | neutral | aggressive
    label: str                      # 保守型 | 中性型 | 进取型
    price: float                    # 建议挂单价
    reference: str                  # 参考依据描述
    stop_loss: float                # 止损参考价


# ─── 分析报告 ─────────────────────────────────────────────────

class AnalysisReport(NamedTuple):
    """完整分析报告输出"""
    code: str
    name: str
    final_score: float              # 综合得分 0~1
    recommendation: str             # 重点参与 | 建议观察 | 放弃
    heat_level: str                 # 火爆 | 较热 | 一般 | 冷淡
    control_level: str              # 极高 | 高 | 中等 | 低
    scoring_breakdown: List         # List[ScoringResult]
    pricing_strategies: List        # List[PricingStrategy]
    risk_alerts: List               # List[str]
    key_factors: List               # List[str] 关键影响因子
    analyzed_at: str                # ISO datetime
