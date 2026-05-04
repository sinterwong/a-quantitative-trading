"""
core/ipo_cross_validator.py — 港股 IPO 多源交叉验证模块（Phase 7 IPO Stars）

功能：
  - 对 fetch_ipo_data_multi_source 的输出进行多源交叉验证
  - 量化每个字段的可信度，输出数据质量评分
  - 不一致时不抛异常，仅记录 warning

数据源优先级：
  1. 东方财富（eastmoney）— P0
  2. 港交所披露易（hkexnews）— P0
  3. 新闻舆情（news）— P1，基石投资者补充验证

Usage:
    from core.ipo_cross_validator import DataCrossValidator, CrossValidationReport

    validator = DataCrossValidator()
    report = validator.cross_validate('09619', multi_source_data)
    print(report.overall_confidence)
    enriched = validator.merge_with_confidence(raw_data, report)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("core.ipo_cross_validator")

# ---------------------------------------------------------------------------
# 权重配置（用于 overall_confidence 加权平均）
# ---------------------------------------------------------------------------
_FIELD_WEIGHTS: Dict[str, float] = {
    "issue_price_range": 0.30,   # 发行价区间
    "cornerstone_investors": 0.20,  # 基石投资者
    "industry": 0.15,           # 行业分类
    "sponsor": 0.15,            # 保荐人
    "fund_raised": 0.20,        # 募资规模
}

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class FieldValidationResult:
    """
    单个字段验证结果。

    Attributes
    ----------
    field_name : str
        字段名称。
    sources : List[Any]
        各数据源的值列表。
    source_count : int
        参与验证的数据源数量。
    is_consistent : bool
        多源是否一致。
    confidence : float
        可信度评分（0~1）。
    warning : Optional[str]
        不一致时的警告信息。
    """

    field_name: str
    sources: List[Any] = field(default_factory=list)
    source_count: int = 0
    is_consistent: bool = False
    confidence: float = 0.0
    warning: Optional[str] = None


@dataclass
class CrossValidationReport:
    """
    交叉验证完整报告。

    Attributes
    ----------
    stock_code : str
        股票代码。
    field_results : Dict[str, FieldValidationResult]
        各字段验证结果。
    overall_confidence : float
        加权平均整体可信度（0~1）。
    critical_warnings : List[str]
        需要人工复核的问题。
    suggestions : List[str]
        改进建议。
    """

    stock_code: str
    field_results: Dict[str, FieldValidationResult] = field(default_factory=dict)
    overall_confidence: float = 0.0
    critical_warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 交叉验证器
# ---------------------------------------------------------------------------


class DataCrossValidator:
    """
    多源数据交叉验证器。

    对东方财富、港交所披露易、新闻舆情等数据源进行交叉验证，
    量化每个字段的可信度，输出数据质量评分。
    """

    # ------------------------------------------------------------------
    # 发行价区间验证
    # ------------------------------------------------------------------

    @staticmethod
    def validate_issue_price_range(
        eastmoney_price: Tuple[float, float],
        hkex_price: Tuple[float, float],
    ) -> FieldValidationResult:
        """
        发行价区间验证。

        一致（区间重叠且中值差异 < 5%）：confidence = 1.0
        误差 < 5%：confidence = 0.8，warning = "轻微差异"
        误差 ≥ 5%：confidence = 0.4，warning = "显著差异，需人工复核"

        Parameters
        ----------
        eastmoney_price : Tuple[float, float]
            东方财富的发行价区间 (low, high)。
        hkex_price : Tuple[float, float]
            港交所的发行价区间 (low, high)。

        Returns
        -------
        FieldValidationResult
        """
        sources = [eastmoney_price, hkex_price]
        source_count = 2

        if eastmoney_price is None or hkex_price is None:
            return FieldValidationResult(
                field_name="issue_price_range",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.0,
                warning="数据缺失",
            )

        # 计算中值
        em_mid = (eastmoney_price[0] + eastmoney_price[1]) / 2
        hk_mid = (hkex_price[0] + hkex_price[1]) / 2

        # 计算相对误差
        max_val = max(abs(em_mid), abs(hk_mid), 1e-9)
        error = abs(em_mid - hk_mid) / max_val

        if error < 1e-9:   # 完全一致
            return FieldValidationResult(
                field_name="issue_price_range",
                sources=sources,
                source_count=source_count,
                is_consistent=True,
                confidence=1.0,
                warning=None,
            )
        elif error < 0.05:   # 轻微差异
            return FieldValidationResult(
                field_name="issue_price_range",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.8,
                warning="轻微差异",
            )
        else:   # 显著差异
            return FieldValidationResult(
                field_name="issue_price_range",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.4,
                warning="显著差异，需人工复核",
            )

    # ------------------------------------------------------------------
    # 行业分类验证
    # ------------------------------------------------------------------

    @staticmethod
    def validate_industry(
        eastmoney_industry: str,
        hkex_industry: str,
    ) -> FieldValidationResult:
        """
        行业分类验证。

        完全一致：confidence = 1.0
        同大类（如都是"制造业"）：confidence = 0.7
        完全不一致：confidence = 0.3

        Parameters
        ----------
        eastmoney_industry : str
            东方财富行业分类。
        hkex_industry : str
            港交所行业分类。

        Returns
        -------
        FieldValidationResult
        """
        sources = [eastmoney_industry, hkex_industry]
        source_count = 2

        if not eastmoney_industry or not hkex_industry:
            return FieldValidationResult(
                field_name="industry",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.0,
                warning="数据缺失",
            )

        em_ind = eastmoney_industry.strip()
        hk_ind = hkex_industry.strip()

        # 完全一致
        if em_ind == hk_ind:
            return FieldValidationResult(
                field_name="industry",
                sources=sources,
                source_count=source_count,
                is_consistent=True,
                confidence=1.0,
                warning=None,
            )

        # 检查同大类（取前两个字符或第一个词）
        em_prefix = re.sub(r'\W+', '', em_ind)[:2]
        hk_prefix = re.sub(r'\W+', '', hk_ind)[:2]

        if em_prefix and hk_prefix and em_prefix == hk_prefix:
            return FieldValidationResult(
                field_name="industry",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.7,
                warning=f"同大类：EM={em_ind}，HKEX={hk_ind}",
            )

        # 完全不一致
        return FieldValidationResult(
            field_name="industry",
            sources=sources,
            source_count=source_count,
            is_consistent=False,
            confidence=0.3,
            warning=f"完全不一致：EM={em_ind}，HKEX={hk_ind}",
        )

    # ------------------------------------------------------------------
    # 保荐人验证
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_sponsor(name: str) -> str:
        """保荐人名称标准化：移除空格、转小写、去除常见后缀。"""
        if not name:
            return ""
        n = re.sub(r'\s+', '', name.lower())
        # 去除 "limited", "ltd", "有限公司" 等后缀
        n = re.sub(r'(limited|ltd|有限公司|co\.?|inc\.?|corporation)$', '', n, flags=re.IGNORECASE)
        return n

    @staticmethod
    def validate_sponsor(
        eastmoney_sponsor: str,
        hkex_sponsor: str,
    ) -> FieldValidationResult:
        """
        保荐人验证。

        完全一致：confidence = 1.0
        包含关系（如 EM="中金公司"，HKEX="CICC"）：confidence = 0.8
        不一致：confidence = 0.4

        Parameters
        ----------
        eastmoney_sponsor : str
            东方财富保荐人。
        hkex_sponsor : str
            港交所保荐人。

        Returns
        -------
        FieldValidationResult
        """
        sources = [eastmoney_sponsor, hkex_sponsor]
        source_count = 2

        if not eastmoney_sponsor or not hkex_sponsor:
            return FieldValidationResult(
                field_name="sponsor",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.0,
                warning="数据缺失",
            )

        em_norm = DataCrossValidator._normalize_sponsor(eastmoney_sponsor)
        hk_norm = DataCrossValidator._normalize_sponsor(hkex_sponsor)

        # 完全一致
        if em_norm == hk_norm:
            return FieldValidationResult(
                field_name="sponsor",
                sources=sources,
                source_count=source_count,
                is_consistent=True,
                confidence=1.0,
                warning=None,
            )

        # 包含关系（一方包含另一方）
        if em_norm in hk_norm or hk_norm in em_norm:
            return FieldValidationResult(
                field_name="sponsor",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.8,
                warning=f"包含关系：EM={eastmoney_sponsor}，HKEX={hkex_sponsor}",
            )

        # 不一致
        return FieldValidationResult(
            field_name="sponsor",
            sources=sources,
            source_count=source_count,
            is_consistent=False,
            confidence=0.4,
            warning=f"不一致：EM={eastmoney_sponsor}，HKEX={hkex_sponsor}",
        )

    # ------------------------------------------------------------------
    # 基石投资者验证
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_investor(name: str) -> str:
        """基石投资者名称标准化。"""
        if not name:
            return ""
        return re.sub(r'\s+', '', name.strip().lower())

    @staticmethod
    def validate_cornerstone_investors(
        hkex_investors: List[str],
        news_investors: List[str],
    ) -> FieldValidationResult:
        """
        基石投资者验证。

        港交所招股书为主，新闻作为补充验证。
        基准 confidence = 0.8（港交所有数据，新闻未验证）。
        新闻中出现港交所未列的投资者 → confidence += 0.1（增强可信度）。
        新闻中未出现港交所列示的投资者 → confidence -= 0.2（可能有问题）。

        Parameters
        ----------
        hkex_investors : List[str]
            港交所披露的基石投资者列表。
        news_investors : List[str]
            新闻中提及的基石投资者列表。

        Returns
        -------
        FieldValidationResult
        """
        source_count = 0
        sources: List[Any] = []

        if hkex_investors is not None:
            source_count += 1
            sources.append(hkex_investors)
        if news_investors is not None:
            source_count += 1
            sources.append(news_investors)

        if not hkex_investors:
            return FieldValidationResult(
                field_name="cornerstone_investors",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.0,
                warning="港交所数据缺失",
            )

        hkex_set = {DataCrossValidator._normalize_investor(i) for i in hkex_investors if i}
        news_set = {DataCrossValidator._normalize_investor(i) for i in (news_investors or []) if i}

        # 基准 confidence = 0.8（仅港交所数据）
        confidence = 0.8
        warnings: List[str] = []

        # 新闻中出现港交所未列的投资者 → +0.1
        extra_in_news = news_set - hkex_set
        if extra_in_news:
            confidence += 0.1
            warnings.append(f"新闻中多出投资者：{', '.join(sorted(extra_in_news))}")

        # 新闻中未出现港交所列示的投资者 → -0.2
        missing_in_news = hkex_set - news_set
        if missing_in_news:
            confidence -= 0.2
            warnings.append(f"新闻中缺失港交所列示的投资者：{', '.join(sorted(missing_in_news))}")

        warning = "; ".join(warnings) if warnings else None
        is_consistent = not missing_in_news and not extra_in_news

        return FieldValidationResult(
            field_name="cornerstone_investors",
            sources=sources,
            source_count=source_count,
            is_consistent=is_consistent,
            confidence=max(0.0, min(1.0, confidence)),
            warning=warning,
        )

    # ------------------------------------------------------------------
    # 募资规模验证
    # ------------------------------------------------------------------

    @staticmethod
    def validate_fund_raised(
        eastmoney_amount: float,
        hkex_amount: float,
    ) -> FieldValidationResult:
        """
        募资规模验证。

        误差 < 10%：confidence = 0.9
        误差 10%~20%：confidence = 0.6
        误差 > 20%：confidence = 0.3，触发 critical_warning

        Parameters
        ----------
        eastmoney_amount : float
            东方财富募资规模（港元）。
        hkex_amount : float
            港交所募资规模（港元）。

        Returns
        -------
        FieldValidationResult
        """
        sources = [eastmoney_amount, hkex_amount]
        source_count = 2

        if eastmoney_amount is None or hkex_amount is None:
            return FieldValidationResult(
                field_name="fund_raised",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.0,
                warning="数据缺失",
            )

        if eastmoney_amount == 0 and hkex_amount == 0:
            return FieldValidationResult(
                field_name="fund_raised",
                sources=sources,
                source_count=source_count,
                is_consistent=True,
                confidence=1.0,
                warning=None,
            )

        max_val = max(abs(eastmoney_amount), abs(hkex_amount), 1e-9)
        error = abs(eastmoney_amount - hkex_amount) / max_val

        if error < 0.10:
            return FieldValidationResult(
                field_name="fund_raised",
                sources=sources,
                source_count=source_count,
                is_consistent=True,
                confidence=0.9,
                warning=None,
            )
        elif error <= 0.20:
            return FieldValidationResult(
                field_name="fund_raised",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.6,
                warning="募资规模差异 10%~20%",
            )
        else:
            return FieldValidationResult(
                field_name="fund_raised",
                sources=sources,
                source_count=source_count,
                is_consistent=False,
                confidence=0.3,
                warning="募资规模差异 > 20%，需人工复核",
            )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    @classmethod
    def cross_validate(
        cls,
        stock_code: str,
        multi_source_data: Dict[str, Any],
    ) -> CrossValidationReport:
        """
        对 fetch_ipo_data_multi_source 的输出进行完整交叉验证。

        流程：
          1. 提取各源数据
          2. 逐字段调用验证方法
          3. 计算整体 confidence（加权平均）
          4. 收集 critical_warnings
          5. 返回 CrossValidationReport

        Parameters
        ----------
        stock_code : str
            股票代码（5位，如 '09619'）。
        multi_source_data : Dict[str, Any]
            fetch_ipo_data_multi_source 返回的原始数据字典。

        Returns
        -------
        CrossValidationReport
        """
        field_results: Dict[str, FieldValidationResult] = {}
        critical_warnings: List[str] = []
        suggestions: List[str] = []

        # 1. 发行价区间
        em_price = multi_source_data.get("eastmoney", {}).get("issue_price_range")
        hk_price = multi_source_data.get("hkexnews", {}).get("issue_price_range")
        if em_price is not None or hk_price is not None:
            result = cls.validate_issue_price_range(
                tuple(em_price) if em_price else (None, None),
                tuple(hk_price) if hk_price else (None, None),
            )
            field_results["issue_price_range"] = result
            if result.warning and "需人工复核" in result.warning:
                critical_warnings.append(f"[{stock_code}] 发行价区间：{result.warning}")

        # 2. 行业分类
        em_industry = multi_source_data.get("eastmoney", {}).get("industry")
        hk_industry = multi_source_data.get("hkexnews", {}).get("industry")
        if em_industry is not None or hk_industry is not None:
            result = cls.validate_industry(
                em_industry or "",
                hk_industry or "",
            )
            field_results["industry"] = result
            if result.confidence < 0.5:
                suggestions.append(f"[{stock_code}] 行业分类可信度低，建议核实：{result.warning}")

        # 3. 保荐人
        em_sponsor = multi_source_data.get("eastmoney", {}).get("sponsor")
        hk_sponsor = multi_source_data.get("hkexnews", {}).get("sponsor")
        if em_sponsor is not None or hk_sponsor is not None:
            result = cls.validate_sponsor(
                em_sponsor or "",
                hk_sponsor or "",
            )
            field_results["sponsor"] = result
            if result.confidence < 0.5:
                suggestions.append(f"[{stock_code}] 保荐人信息不一致，建议核实：{result.warning}")

        # 4. 基石投资者
        hkex_investors = multi_source_data.get("hkexnews", {}).get("cornerstone_investors")
        news_investors = multi_source_data.get("news", {}).get("cornerstone_investors")
        if hkex_investors is not None:
            result = cls.validate_cornerstone_investors(
                hkex_investors or [],
                news_investors or [],
            )
            field_results["cornerstone_investors"] = result
            if result.warning and "缺失" in result.warning:
                suggestions.append(f"[{stock_code}] 基石投资者：{result.warning}")

        # 5. 募资规模
        em_amount = multi_source_data.get("eastmoney", {}).get("fund_raised")
        hk_amount = multi_source_data.get("hkexnews", {}).get("fund_raised")
        if em_amount is not None or hk_amount is not None:
            result = cls.validate_fund_raised(
                em_amount if em_amount is not None else 0.0,
                hk_amount if hk_amount is not None else 0.0,
            )
            field_results["fund_raised"] = result
            if result.warning and "需人工复核" in result.warning:
                critical_warnings.append(f"[{stock_code}] 募资规模：{result.warning}")

        # 计算加权 overall_confidence
        overall_confidence = cls._compute_weighted_confidence(field_results)

        # 通用建议
        if not field_results:
            suggestions.append(f"[{stock_code}] 无足够的交叉验证数据，建议补充数据源")
        elif overall_confidence < 0.6:
            suggestions.append(f"[{stock_code}] 整体数据质量偏低（{overall_confidence:.2f}），建议人工复核")

        return CrossValidationReport(
            stock_code=stock_code,
            field_results=field_results,
            overall_confidence=overall_confidence,
            critical_warnings=critical_warnings,
            suggestions=suggestions,
        )

    @classmethod
    def _compute_weighted_confidence(
        cls,
        field_results: Dict[str, FieldValidationResult],
    ) -> float:
        """
        计算加权平均整体可信度。

        权重：发行价区间 30%，基石 20%，行业 15%，保荐人 15%，募资 20%。

        Parameters
        ----------
        field_results : Dict[str, FieldValidationResult]

        Returns
        -------
        float
        """
        if not field_results:
            return 0.0

        total_weight = 0.0
        weighted_sum = 0.0

        for field_name, weight in _FIELD_WEIGHTS.items():
            result = field_results.get(field_name)
            if result is not None and result.source_count > 0:
                weighted_sum += result.confidence * weight
                total_weight += weight

        if total_weight <= 0.0:
            return 0.0

        return round(weighted_sum / total_weight, 4)

    # ------------------------------------------------------------------
    # 合并验证结果
    # ------------------------------------------------------------------

    @staticmethod
    def merge_with_confidence(
        raw_data: Dict[str, Any],
        validation: CrossValidationReport,
    ) -> Dict[str, Any]:
        """
        将验证结果合并到原始数据中，标注每个字段的 confidence。

        用于传递给分析引擎时附带数据质量信息。
        输出格式示例：
        {
            "stock_code": "09619",
            "issue_price_range": {
                "value": (50.0, 55.0),
                "confidence": 0.8,
                "warning": "轻微差异",
            },
            ...
        }

        Parameters
        ----------
        raw_data : Dict[str, Any]
            原始数据（fetch_ipo_data_multi_source 输出）。
        validation : CrossValidationReport
            交叉验证报告。

        Returns
        -------
        Dict[str, Any]
        """
        enriched: Dict[str, Any] = {
            "stock_code": validation.stock_code,
            "overall_confidence": validation.overall_confidence,
            "critical_warnings": validation.critical_warnings,
            "suggestions": validation.suggestions,
            "_raw": raw_data,
        }

        # 字段名到 raw_data 中的路径映射
        field_paths: Dict[str, List[str]] = {
            "issue_price_range": ["eastmoney", "issue_price_range"],
            "industry": ["eastmoney", "industry"],
            "sponsor": ["eastmoney", "sponsor"],
            "fund_raised": ["eastmoney", "fund_raised"],
            "cornerstone_investors": ["hkexnews", "cornerstone_investors"],
        }

        for field_name, result in validation.field_results.items():
            path = field_paths.get(field_name)
            raw_value = None
            if path:
                cur = raw_data
                for key in path:
                    if isinstance(cur, dict):
                        cur = cur.get(key)
                    else:
                        cur = None
                        break
                raw_value = cur

            enriched[field_name] = {
                "value": raw_value,
                "confidence": result.confidence,
                "is_consistent": result.is_consistent,
                "warning": result.warning,
                "source_count": result.source_count,
            }

        return enriched
