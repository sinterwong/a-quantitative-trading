"""
core/ipo_analyst_engine.py — Phase 7 IPO Stars 资深分析师分析引擎

功能：
  整合 5 个分析模块，输出完整的港股 IPO 分析报告：
    ① 可比 IPO 定价锚点引擎（ComparableIPOEngine）
    ② 机构持仓结构分析（InvestorStructureAnalyzer）
    ③ 发行条款性价比评分（TermsValuationScorer）
    ④ 市场情绪窗口评估（MarketWindowEvaluator）
    ⑤ 挂单策略生成器（OrderSheetGenerator）

Usage:
    from core.ipo_analyst_engine import IPOAnalystEngine

    engine = IPOAnalystEngine()
    report = engine.analyze(
        stock_code='09619',
        multi_source_data={...},   # IPODataSource.get_all_sources() 输出
        validated_data={...},      # DataCrossValidator.merge_with_confidence() 输出
        market_sentiment={...}    # CompositeMarketDataSource 输出（可选）
    )
    print(report.summary())
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np

logger = logging.getLogger('core.ipo_analyst_engine')

# ---------------------------------------------------------------------------
# 内部数据结构（与 TODO.md 保持一致）
# ---------------------------------------------------------------------------


@dataclass
class ComparableIPO:
    """
    可比 IPO 记录。

    Attributes
    ----------
    stock_code : str
        股票代码。
    name : str
        股票名称。
    listing_date : date
        上市日期。
    industry : str
        所属行业。
    issue_price : float
        发行价（港元）。
    first_day_return : float
        首日收益率（小数，如 0.20 表示涨 20%）。
    grey_market_return : float
        暗盘收益率（小数）。
    fund_raised_hkd : float
        实际募资额（亿港元）。
    days_ago : int
        距离今天的天数。
    time_weight : float
        时间衰减权重（近期更高）。
    """
    stock_code: str
    name: str
    listing_date: date
    industry: str
    issue_price: float
    first_day_return: float = 0.0
    grey_market_return: float = 0.0
    fund_raised_hkd: float = 0.0
    days_ago: int = 0
    time_weight: float = 1.0


@dataclass
class LimitOrderRec:
    """
    限价单建议。

    Attributes
    ----------
    conservative_price : float
        保守档（破发概率低时参考）。
    neutral_price : float
        中性档（推荐参考）。
    aggressive_price : float
        进取档（高胜率时可追）。
    logic : str
        定价逻辑说明。
    anchor_comparable : str
        锚定的可比公司名称。
    stop_price : float
        止损参考价（发行价 × (1 - 止损%)）。
    stop_loss_pct : float
        止损比例。
    """
    conservative_price: float
    neutral_price: float
    aggressive_price: float
    logic: str = ''
    anchor_comparable: str = ''
    stop_price: float = 0.0
    stop_loss_pct: float = 0.05


@dataclass
class IPOAnalysisReport:
    """
    单只港股新股完整分析报告。

    Attributes
    ----------
    stock_code : str
        股票代码。
    name_cn : str
        中文名称。
    name_en : str
        英文名称。
    listing_date : date
        上市日期。
    issue_price_range : Tuple[float, float]
        发行价区间（港元）。
    mid_price : float
        发行价区间中值。
    overall_rating : Literal['BUY', 'NEUTRAL', 'SKIP']
        综合评级。
    confidence : float
        置信度（0~1）。
    comparable_ipos : List[ComparableIPO]
        可比公司列表。
    predicted_first_day_return_p50 : float
        首日预测涨幅（中性，p50）。
    predicted_first_day_return_p75 : float
        乐观情况（p75）。
    predicted_first_day_return_p25 : float
        保守情况（p25）。
    cornerstone_signals : List[str]
        基石/机构信号列表。
    retail_float_ratio : float
        公开发售占比。
    terms_score : float
        发行条款评分（0~1）。
    ps_discount_vs_sector : float
        PS 折让%（负数=溢价）。
    optimal_scale : bool
        是否在 5~15 亿最优区间。
    market_sentiment_score : float
        市场情绪评分（0~1）。
    hstech_recent_change : float
        恒生科技近期涨跌幅%。
    recent_ipo_win_rate : float
        近 30 天打新胜率。
    theme_momentum : float
        主题动量（0~1）。
    dark_pool_recommendation : LimitOrderRec
        暗盘限价单建议。
    first_day_recommendation : LimitOrderRec
        首日限价单建议。
    risk_factors : List[str]
        主要风险提示。
    key_positive_signals : List[str]
        主要利好信号。
    data_quality_score : Dict[str, float]
        各字段数据质量评分。
    generated_at : datetime
        报告生成时间。
    institutional_score : float
        机构持仓结构评分（0~1）。
    """
    stock_code: str
    name_cn: str
    name_en: str
    listing_date: date
    issue_price_range: Tuple[float, float]
    mid_price: float
    overall_rating: Literal['BUY', 'NEUTRAL', 'SKIP']
    confidence: float = 0.5
    comparable_ipos: List[ComparableIPO] = field(default_factory=list)
    predicted_first_day_return_p50: float = 0.0
    predicted_first_day_return_p75: float = 0.0
    predicted_first_day_return_p25: float = 0.0
    cornerstone_signals: List[str] = field(default_factory=list)
    retail_float_ratio: float = 0.0
    terms_score: float = 0.5
    ps_discount_vs_sector: float = 0.0
    optimal_scale: bool = False
    market_sentiment_score: float = 0.5
    hstech_recent_change: float = 0.0
    recent_ipo_win_rate: float = 0.5
    theme_momentum: float = 0.5
    dark_pool_recommendation: Optional[LimitOrderRec] = None
    first_day_recommendation: Optional[LimitOrderRec] = None
    risk_factors: List[str] = field(default_factory=list)
    key_positive_signals: List[str] = field(default_factory=list)
    data_quality_score: Dict[str, float] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.now)
    institutional_score: float = 0.5

    def summary(self) -> str:
        """生成单行摘要。"""
        return (
            f"IPOAnalysisReport({self.stock_code} {self.name_cn}): "
            f"rating={self.overall_rating} "
            f"confidence={self.confidence:.2f} "
            f"p50={self.predicted_first_day_return_p50*100:.1f}% "
            f"p75={self.predicted_first_day_return_p75*100:.1f}% "
            f"p25={self.predicted_first_day_return_p25*100:.1f}%"
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化报告为字典。"""
        def _serialize(obj: Any) -> Any:
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, (np.integer, np.floating)):
                return float(obj)
            if hasattr(obj, '__dataclass_fields__'):
                return {k: _serialize(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_serialize(i) for i in obj]
            if isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            return obj

        result = _serialize(self.__dict__)
        # 移除不可序列化的 datetime 额外处理
        result['generated_at'] = self.generated_at.isoformat()
        return result


# ---------------------------------------------------------------------------
# 历史新股数据存储（港股）
# ---------------------------------------------------------------------------

class IPORecordStore:
    """
    港股历史新股数据库（内存 + Parquet 持久化）。

    提供按行业/募资规模查找可比 IPO 的能力。

    缓存策略：
      - 内存缓存（进程生命周期）
      - Parquet 持久化（TTL=7 天）
    """

    def __init__(self, data_dir: Optional[str] = None):
        """
        Parameters
        ----------
        data_dir : str, optional
            Parquet 文件目录，默认使用 data/ipo/ 目录。
        """
        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'data', 'ipo'
            )
        self._data_dir = data_dir
        os.makedirs(self._data_dir, exist_ok=True)
        self._parquet_path = os.path.join(self._data_dir, 'hk_ipo_history.parquet')

        self._memory_cache: Optional[List[Dict[str, Any]]] = None
        self._cache_dt: Optional[datetime] = None
        self._ttl_seconds: float = 7 * 86400

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_comparable_ipos(
        self,
        industry: str,
        fund_raised_hkd: float,
        days_lookback: int = 180,
        min_sample: int = 1,
    ) -> List[ComparableIPO]:
        """
        查找可比 IPO。

        匹配条件：
          1. 同行业（证监会行业分类二级）
          2. 募资额在 ±50% 范围内
          3. 在 days_lookback 天以内

        Parameters
        ----------
        industry : str
            目标行业。
        fund_raised_hkd : float
            目标募资额（亿港元）。
        days_lookback : int
            回溯天数（默认 180 天）。
        min_sample : int
            最小样本数，低于此数时返回空列表。

        Returns
        -------
        List[ComparableIPO]
            可比 IPO 列表（按时间加权排序）。
        """
        records = self._get_all_records()
        if not records:
            return []

        cutoff = datetime.now() - timedelta(days=days_lookback)
        comparables: List[ComparableIPO] = []

        fund_min = fund_raised_hkd * 0.5
        fund_max = fund_raised_hkd * 1.5

        for rec in records:
            # 行业匹配（模糊匹配，支持子类）
            rec_industry = rec.get('industry', '')
            if not self._industry_match(industry, rec_industry):
                continue

            # 募资规模匹配
            rec_fund = rec.get('fund_raised_hkd', 0.0)
            if rec_fund <= 0 or rec_fund < fund_min or rec_fund > fund_max:
                continue

            # 时间过滤
            rec_date = rec.get('listing_date')
            if isinstance(rec_date, str):
                rec_date = datetime.fromisoformat(rec_date).date()
            elif isinstance(rec_date, datetime):
                rec_date = rec_date.date()

            if rec_date < cutoff.date():
                continue

            # 计算时间权重
            days_ago = (datetime.now().date() - rec_date).days if rec_date else 9999
            time_weight = self._time_weight(days_ago, days_lookback)

            comp = ComparableIPO(
                stock_code=rec.get('stock_code', ''),
                name=rec.get('name', ''),
                listing_date=rec_date or date(2000, 1, 1),
                industry=rec_industry,
                issue_price=rec.get('issue_price', 0.0),
                first_day_return=rec.get('first_day_return', 0.0),
                grey_market_return=rec.get('grey_market_return', 0.0),
                fund_raised_hkd=rec_fund,
                days_ago=days_ago,
                time_weight=time_weight,
            )
            comparables.append(comp)

        if len(comparables) < min_sample:
            logger.debug(
                'get_comparable_ipos: only %d records found (min=%d), '
                'industry=%s fund=%.1f',
                len(comparables), min_sample, industry, fund_raised_hkd
            )
            return []

        # 按时间权重降序排序
        comparables.sort(key=lambda x: x.time_weight, reverse=True)
        return comparables

    def add_record(self, record: Dict[str, Any]) -> None:
        """追加一条 IPO 记录到内存缓存（下次持久化时写入）。"""
        if self._memory_cache is None:
            self._memory_cache = []
        self._memory_cache.append(record)
        logger.debug('add_record: added %s', record.get('stock_code', '?'))

    def get_all_records(self) -> List[Dict[str, Any]]:
        """获取所有历史记录（合并缓存和持久化数据）。"""
        return self._get_all_records()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_all_records(self) -> List[Dict[str, Any]]:
        """获取所有记录（内存优先，失败则加载 Parquet）。"""
        now = datetime.now()

        # 内存缓存检查
        if (self._memory_cache is not None
                and self._cache_dt is not None
                and (now - self._cache_dt).total_seconds() < self._ttl_seconds):
            return self._memory_cache

        # Parquet 加载
        df = self._load_parquet()
        if df is not None and not df.empty:
            records = df.to_dict(orient='records')
            self._memory_cache = records
            self._cache_dt = now
            logger.info('_get_all_records: loaded %d records from Parquet', len(records))
            return records

        # 空数据
        if self._memory_cache is None:
            self._memory_cache = []
        return self._memory_cache

    def _load_parquet(self) -> Optional[Any]:  # noqa: ANN401
        """从 Parquet 加载数据。"""
        if not os.path.exists(self._parquet_path):
            return None
        try:
            import pandas as pd
            df = pd.read_parquet(self._parquet_path)
            logger.info('_load_parquet: %d rows', len(df))
            return df
        except Exception as e:
            logger.warning('_load_parquet failed: %s', e)
            return None

    def _industry_match(self, target: str, candidate: str) -> bool:
        """模糊行业匹配（支持子类关系）。"""
        if not target or not candidate:
            return False
        t = target.strip().lower()
        c = candidate.strip().lower()
        if t == c:
            return True
        # 包含关系（行业子类匹配父类）
        if t in c or c in t:
            return True
        # 常见同义映射
        synonyms = {
            ('软件', 'software'): True,
            ('半导体', 'semiconductor'): True,
            ('医药', 'biotech'): True,
            ('医疗', 'healthcare'): True,
            ('消费', 'consumer'): True,
            ('科技', 'tech'): True,
        }
        for (a, b), _ in synonyms.items():
            if (a in t and b in c) or (b in t and a in c):
                return True
        return False

    @staticmethod
    def _time_weight(days_ago: int, lookback: int) -> float:
        """
        时间衰减权重。

        策略：指数衰减，half_life = lookback / 3
        即离现在越近的 IPO 权重越高。
        """
        if days_ago <= 0:
            return 1.0
        half_life = lookback / 3.0
        return np.exp(-0.693 * days_ago / half_life)


# ---------------------------------------------------------------------------
# 模块①：可比 IPO 定价锚点引擎
# ---------------------------------------------------------------------------

class ComparableIPOEngine:
    """
    找到真正的可比 IPO，锚定定价区间。

    步骤：
      1. 从 IPORecordStore 找同行业 + 募资额 ±50% 的新股
      2. 按时间加权（近期权重更高）
      3. 计算 p25/p50/p75 首日涨幅

    置信度规则：
      - 样本量 >= 10：confidence >= 0.8
      - 样本量 >= 5：confidence >= 0.6
      - 样本量 >= 3：confidence >= 0.4
      - 样本量 < 3：confidence >= 0.2
    """

    def __init__(self, store: Optional[IPORecordStore] = None):
        self._store = store or IPORecordStore()

    def analyze(
        self,
        industry: str,
        fund_raised_hkd: float,
        days_lookback: int = 180,
    ) -> Dict[str, Any]:
        """
        分析可比 IPO 并返回预测结果。

        Returns
        -------
        Dict[str, Any]
            {
                'comparable_ipos': List[ComparableIPO],
                'predicted_return_p25': float,
                'predicted_return_p50': float,
                'predicted_return_p75': float,
                'sample_size': int,
                'confidence': float,
                'anchor_comparable': str,  # 最可比的公司名称
            }
        """
        comparables = self._store.get_comparable_ipos(
            industry=industry,
            fund_raised_hkd=fund_raised_hkd,
            days_lookback=days_lookback,
            min_sample=1,
        )

        if not comparables:
            logger.debug(
                'ComparableIPOEngine.analyze: no comparables found '
                'for industry=%s fund=%.1f',
                industry, fund_raised_hkd
            )
            return {
                'comparable_ipos': [],
                'predicted_return_p25': 0.0,
                'predicted_return_p50': 0.0,
                'predicted_return_p75': 0.0,
                'sample_size': 0,
                'confidence': 0.0,
                'anchor_comparable': '',
            }

        # 提取收益率序列
        returns = [c.first_day_return for c in comparables]
        weights = np.array([c.time_weight for c in comparables])
        total_weight = weights.sum()

        if total_weight <= 0:
            weighted_returns = returns
        else:
            weighted_returns = np.average(returns, weights=weights, axis=0)

        # 计算加权分位数
        p25 = float(np.percentile(weighted_returns, 25)) if len(weighted_returns) >= 4 else float(np.min(weighted_returns))
        p50 = float(np.percentile(weighted_returns, 50)) if len(weighted_returns) >= 4 else float(np.median(weighted_returns))
        p75 = float(np.percentile(weighted_returns, 75)) if len(weighted_returns) >= 4 else float(np.max(weighted_returns))

        # 置信度
        n = len(comparables)
        if n >= 10:
            confidence = 0.9
        elif n >= 5:
            confidence = 0.7
        elif n >= 3:
            confidence = 0.5
        else:
            confidence = 0.3

        # 最可比公司：时间权重最高的
        anchor = max(comparables, key=lambda c: c.time_weight)
        anchor_name = f"{anchor.name} ({anchor.stock_code})"

        logger.info(
            'ComparableIPOEngine: found %d comparables, p25=%.1f%% p50=%.1f%% p75=%.1f%%',
            n, p25 * 100, p50 * 100, p75 * 100
        )

        return {
            'comparable_ipos': comparables,
            'predicted_return_p25': p25,
            'predicted_return_p50': p50,
            'predicted_return_p75': p75,
            'sample_size': n,
            'confidence': confidence,
            'anchor_comparable': anchor_name,
        }


# ---------------------------------------------------------------------------
# 模块②：机构持仓结构分析
# ---------------------------------------------------------------------------

class InvestorStructureAnalyzer:
    """
    基石/锚定投资者分析。

    评估维度：
      - 一线机构基石认购（如高瓴/红杉/淡马锡）
      - 预 IPO 成本（折价程度）
      - 禁售期长短
      - 公开发售占比（散户筹码比例）
    """

    TOP_TIER_INSTITUTIONS: List[str] = [
        '高瓴资本', 'Hillhouse', 'HHLR',
        '红杉资本', 'Sequoia', '红杉中国',
        '淡马锡', 'Temasek',
        '中金资本', 'CICC Capital',
        '腾讯投资', 'Tencent Investment', '腾讯', 'Tencent',
        '阿里资本', 'Alibaba Capital', '阿里巴巴', 'Alibaba',
        '软银', 'Softbank',
        'Fidelity', 'Fidelity Investments',
        '贝莱德', 'BlackRock',
        '先锋领航', 'Vanguard',
        '橡树资本', 'Oaktree',
        'GGV', '纪源资本',
        '启明创投', 'Qiming',
        '创新工场', 'Sinovation',
        '经纬中国', 'Matrix Partners',
        '鼎晖投资', 'CDH Investments',
        '高榕资本', 'Gaorong Capital',
    ]

    LOCKUP_SCORES: Dict[str, float] = {
        '无锁定期': 0.0,
        '首日即解禁': 0.0,
        '1个月': 0.1,
        '3个月': 0.2,
        '6个月': 0.5,
        '一年锁定期': 0.8,
        '三年锁定期': 1.0,
    }

    def analyze(
        self,
        cornerstone_investors: List[str],
        pre_ipo_cost: Optional[float],
        lockup_period: str,
        retail_float_ratio: float,
        issue_price: float,
    ) -> Dict[str, Any]:
        """
        分析机构持仓结构。

        Parameters
        ----------
        cornerstone_investors : List[str]
            基石投资者名称列表。
        pre_ipo_cost : float, optional
            预 IPO 认购价（港元），若无则 None。
        lockup_period : str
            锁定期描述（如 '6个月禁售'）。
        retail_float_ratio : float
            公开发售占比（0~1）。
        issue_price : float
            发行价（港元）。

        Returns
        -------
        Dict[str, Any]
            {
                'signals': List[str],
                'positive_signals': List[str],
                'negative_signals': List[str],
                'institutional_score': float,  # 0~1
            }
        """
        signals: List[str] = []
        positive: List[str] = []
        negative: List[str] = []
        score_parts: List[float] = []

        # ── 1. 一线机构基石 ──────────────────────────────────────────────
        top_tier_count = 0
        for inv in cornerstone_investors:
            for tier in self.TOP_TIER_INSTITUTIONS:
                if tier.lower() in inv.lower():
                    top_tier_count += 1
                    signals.append(f'{inv} 锚定认购')
                    positive.append(f'一线机构参与：{inv}')
                    break

        if top_tier_count >= 2:
            score_parts.append(0.35)
        elif top_tier_count == 1:
            score_parts.append(0.25)
        else:
            score_parts.append(0.0)
            if cornerstone_investors:
                signals.append(f'基石投资者 {len(cornerstone_investors)} 家')
            else:
                signals.append('无基石投资者')

        # ── 2. 预 IPO 成本 ───────────────────────────────────────────────
        if pre_ipo_cost is not None and pre_ipo_cost > 0 and issue_price > 0:
            cost_discount = (issue_price - pre_ipo_cost) / issue_price
            if cost_discount > 0.2:
                signals.append(f'预 IPO 折价 {cost_discount*100:.0f}%（成本优势明显）')
                positive.append(f'Pre-IPO 折价 {cost_discount*100:.0f}%')
                score_parts.append(0.20)
            elif cost_discount > 0:
                signals.append(f'预 IPO 折价 {cost_discount*100:.0f}%')
                score_parts.append(0.10)
            elif cost_discount < -0.1:
                signals.append(f'预 IPO 溢价 {abs(cost_discount)*100:.0f}%（风险警示）')
                negative.append(f'Pre-IPO 溢价 {abs(cost_discount)*100:.0f}%')
                score_parts.append(-0.10)
            else:
                score_parts.append(0.0)

        # ── 3. 禁售期 ───────────────────────────────────────────────────
        lockup_score = 0.0
        for key, val in self.LOCKUP_SCORES.items():
            if key in lockup_period:
                lockup_score = val
                break
        if lockup_score >= 0.8:
            signals.append('一年以上禁售（机构信心强）')
            positive.append('长禁售期锁仓')
            score_parts.append(0.20)
        elif lockup_score >= 0.5:
            signals.append('6个月禁售')
            score_parts.append(0.15)
        elif lockup_score > 0:
            signals.append(f'{lockup_period}')
            score_parts.append(0.05)
        else:
            signals.append('无锁定期或首日解禁')
            negative.append('无锁定期（抛压较大）')
            score_parts.append(0.0)

        # ── 4. 公开发售占比 ──────────────────────────────────────────────
        if retail_float_ratio > 0.5:
            signals.append(f'公开发售占比 {retail_float_ratio*100:.0f}%（散户筹码多）')
            negative.append(f'散户筹码占比高 {retail_float_ratio*100:.0f}%')
            score_parts.append(-0.10)
        elif retail_float_ratio < 0.2:
            signals.append(f'公开发售占比 {retail_float_ratio*100:.0f}%（筹码集中）')
            positive.append('机构持仓集中，抛压可控')
            score_parts.append(0.15)
        else:
            score_parts.append(0.05)

        # ── 5. 保荐人加分 ───────────────────────────────────────────────
        # 保荐人质量已隐含在 cornerstone 分析中，此处略

        # 综合打分（归一化到 0~1）
        raw_score = sum(score_parts)
        institutional_score = float(np.clip(raw_score, 0.0, 1.0))

        logger.info(
            'InvestorStructureAnalyzer: score=%.3f signals=%s',
            institutional_score, signals
        )

        return {
            'signals': signals,
            'positive_signals': positive,
            'negative_signals': negative,
            'institutional_score': institutional_score,
        }


# ---------------------------------------------------------------------------
# 模块③：发行条款性价比评分
# ---------------------------------------------------------------------------

class TermsValuationScorer:
    """
    发行条款综合评分。

    评估维度：
      - PS 折让（相对行业均值）
      - 募资规模是否在最优区间（5~15 亿港元）
      - 定价区间宽度（过宽说明不确定性高）
    """

    OPTIMAL_FUND_RAISED_LOW: float = 5.0   # 亿港元
    OPTIMAL_FUND_RAISED_HIGH: float = 15.0  # 亿港元

    def analyze(
        self,
        ps_discount_vs_sector: float,
        fund_raised_hkd: float,
        issue_price_range: Tuple[float, float],
        industry_avg_ps: float = 8.0,
    ) -> Dict[str, Any]:
        """
        分析发行条款。

        Parameters
        ----------
        ps_discount_vs_sector : float
            PS 折让%（负数 = 溢价，如 -20 表示比行业便宜 20%）。
        fund_raised_hkd : float
            实际募资额（亿港元）。
        issue_price_range : Tuple[float, float]
            发行价区间（低位, 高位）。
        industry_avg_ps : float
            行业均值 PS（默认 8.0）。

        Returns
        -------
        Dict[str, Any]
            {
                'terms_score': float,
                'ps_discount_vs_sector': float,
                'optimal_scale': bool,
                'price_range_width': float,
                'positive_signals': List[str],
                'negative_signals': List[str],
            }
        """
        positive: List[str] = []
        negative: List[str] = []
        score_parts: List[float] = []

        # ── 1. PS 折让评分 ──────────────────────────────────────────────
        # ps_discount_vs_sector: 负数 = 比行业便宜（好），正数 = 比行业贵（差）
        if ps_discount_vs_sector <= -30:
            positive.append(f'PS 折让极大 {ps_discount_vs_sector:.0f}%（估值极具吸引力）')
            score_parts.append(0.40)
        elif ps_discount_vs_sector <= -15:
            positive.append(f'PS 折让 {ps_discount_vs_sector:.0f}%（估值偏低）')
            score_parts.append(0.30)
        elif ps_discount_vs_sector <= 0:
            positive.append(f'PS 折让 {ps_discount_vs_sector:.0f}%（估值合理）')
            score_parts.append(0.20)
        elif ps_discount_vs_sector <= 15:
            negative.append(f'PS 溢价 {ps_discount_vs_sector:.0f}%（估值偏高）')
            score_parts.append(0.05)
        else:
            negative.append(f'PS 溢价 {ps_discount_vs_sector:.0f}%（估值泡沫风险）')
            score_parts.append(-0.10)

        # ── 2. 募资规模评分 ─────────────────────────────────────────────
        optimal = (self.OPTIMAL_FUND_RAISED_LOW <= fund_raised_hkd
                   <= self.OPTIMAL_FUND_RAISED_HIGH)
        if optimal:
            positive.append(
                f'募资规模 {fund_raised_hkd:.1f} 亿（最优区间 '
                f'{self.OPTIMAL_FUND_RAISED_LOW}~{self.OPTIMAL_FUND_RAISED_HIGH} 亿）'
            )
            score_parts.append(0.20)
        elif fund_raised_hkd < self.OPTIMAL_FUND_RAISED_LOW:
            negative.append(f'募资规模偏小 {fund_raised_hkd:.1f} 亿（<{self.OPTIMAL_FUND_RAISED_LOW}亿）')
            score_parts.append(0.05)
        else:
            negative.append(f'募资规模偏大 {fund_raised_hkd:.1f} 亿（>{self.OPTIMAL_FUND_RAISED_HIGH}亿）')
            score_parts.append(0.05)

        # ── 3. 定价区间宽度 ────────────────────────────────────────────
        low, high = issue_price_range
        if low > 0 and high > low:
            width_pct = (high - low) / low * 100
        else:
            width_pct = 0.0

        if width_pct <= 5:
            positive.append(f'定价区间紧凑 {width_pct:.1f}%（发行价确定性强）')
            score_parts.append(0.10)
        elif width_pct <= 10:
            score_parts.append(0.05)
        elif width_pct <= 20:
            negative.append(f'定价区间较宽 {width_pct:.1f}%（不确定性偏高）')
            score_parts.append(0.0)
        else:
            negative.append(f'定价区间极宽 {width_pct:.1f}%（估值存在较大分歧）')
            score_parts.append(-0.05)

        # ── 4. 综合评分 ─────────────────────────────────────────────────
        raw_score = sum(score_parts)
        terms_score = float(np.clip(raw_score, 0.0, 1.0))

        logger.info(
            'TermsValuationScorer: score=%.3f fund=%.1f optimal=%s width=%.1f%%',
            terms_score, fund_raised_hkd, optimal, width_pct
        )

        return {
            'terms_score': terms_score,
            'ps_discount_vs_sector': ps_discount_vs_sector,
            'optimal_scale': optimal,
            'price_range_width': width_pct,
            'positive_signals': positive,
            'negative_signals': negative,
        }


# ---------------------------------------------------------------------------
# 模块④：市场情绪窗口评估
# ---------------------------------------------------------------------------

class MarketWindowEvaluator:
    """
    市场窗口综合评分。

    评估维度：
      - 恒生科技近期涨跌幅（科技情绪温度计）
      - 近 30 天港股打新胜率
      - 认购倍数（资金供需信号）
      - VIX 恐慌指数
    """

    def analyze(
        self,
        hstech_recent_change: float,
        recent_ipo_win_rate: float,
        subscription_multiple: Optional[float] = None,
        vix: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        评估市场情绪窗口。

        Parameters
        ----------
        hstech_recent_change : float
            恒生科技近期涨跌幅%（近 5 日）。
        recent_ipo_win_rate : float
            近 30 天打新胜率（0~1）。
        subscription_multiple : float, optional
            认购倍数（如 10.5 表示 10.5x）。
        vix : float, optional
            VIX 恐慌指数。

        Returns
        -------
        Dict[str, Any]
            {
                'market_sentiment_score': float,
                'hstech_recent_change': float,
                'recent_ipo_win_rate': float,
                'theme_momentum': float,
                'funding_signal': str,
                'sentiment_label': str,
            }
        """
        score_parts: List[float] = []

        # ── 1. 恒生科技动量 ─────────────────────────────────────────────
        hstech_score: float
        if hstech_recent_change >= 5.0:
            hstech_score = 1.0
        elif hstech_recent_change >= 2.0:
            hstech_score = 0.8
        elif hstech_recent_change >= 0:
            hstech_score = 0.6
        elif hstech_recent_change >= -3.0:
            hstech_score = 0.4
        elif hstech_recent_change >= -5.0:
            hstech_score = 0.2
        else:
            hstech_score = 0.0
        score_parts.append(hstech_score * 0.30)  # 权重 30%

        # ── 2. 打新胜率 ─────────────────────────────────────────────────
        win_rate_score: float
        if recent_ipo_win_rate >= 0.7:
            win_rate_score = 1.0
        elif recent_ipo_win_rate >= 0.5:
            win_rate_score = 0.7
        elif recent_ipo_win_rate >= 0.3:
            win_rate_score = 0.4
        else:
            win_rate_score = 0.1
        score_parts.append(win_rate_score * 0.35)  # 权重 35%

        # ── 3. VIX ─────────────────────────────────────────────────────
        vix_score: float = 0.5  # 默认中性
        if vix is not None:
            if vix <= 12:
                vix_score = 1.0
            elif vix <= 15:
                vix_score = 0.9
            elif vix <= 18:
                vix_score = 0.7
            elif vix <= 22:
                vix_score = 0.5
            elif vix <= 25:
                vix_score = 0.3
            else:
                vix_score = 0.1
        score_parts.append(vix_score * 0.15)  # 权重 15%

        # ── 4. 认购倍数 ─────────────────────────────────────────────────
        funding_signal: str
        sub_score: float = 0.5
        if subscription_multiple is not None:
            if subscription_multiple >= 100:
                funding_signal = 'oversubscribed'
                sub_score = 1.0
            elif subscription_multiple >= 10:
                funding_signal = 'oversubscribed'
                sub_score = 0.8
            elif subscription_multiple >= 1:
                funding_signal = 'normal'
                sub_score = 0.5
            else:
                funding_signal = 'under'
                sub_score = 0.2
        else:
            funding_signal = 'unknown'
        score_parts.append(sub_score * 0.20)  # 权重 20%

        # ── 综合评分 ────────────────────────────────────────────────────
        raw_score = sum(score_parts)
        market_sentiment_score = float(np.clip(raw_score, 0.0, 1.0))

        # ── 主题动量 ───────────────────────────────────────────────────
        theme_momentum = float(np.clip(hstech_score * 0.6 + win_rate_score * 0.4, 0.0, 1.0))

        # ── 情绪标签 ───────────────────────────────────────────────────
        if market_sentiment_score >= 0.65:
            sentiment_label = 'hot'
        elif market_sentiment_score >= 0.40:
            sentiment_label = 'neutral'
        else:
            sentiment_label = 'cold'

        logger.info(
            'MarketWindowEvaluator: score=%.3f label=%s hstech=%.1f%% '
            'win_rate=%.1f%% funding=%s',
            market_sentiment_score, sentiment_label,
            hstech_recent_change, recent_ipo_win_rate * 100, funding_signal
        )

        return {
            'market_sentiment_score': market_sentiment_score,
            'hstech_recent_change': hstech_recent_change,
            'recent_ipo_win_rate': recent_ipo_win_rate,
            'theme_momentum': theme_momentum,
            'funding_signal': funding_signal,
            'sentiment_label': sentiment_label,
        }


# ---------------------------------------------------------------------------
# 模块⑤：挂单策略生成器
# ---------------------------------------------------------------------------

class OrderSheetGenerator:
    """
    生成暗盘和首日的三档限价单建议。

    逻辑：
      - 中性档 = 发行价 × (1 + predicted_return_p50)
      - 进取档 = 发行价 × (1 + predicted_return_p75)
      - 保守档 = 发行价 × (1 + predicted_return_p25)，不低于发行价

    止损价 = 发行价 × (1 - stop_loss_pct)
    """

    def generate(
        self,
        issue_price: float,
        mid_price: float,
        predicted_return_p50: float,
        predicted_return_p75: float,
        predicted_return_p25: float,
        anchor_comparable: str,
        stop_loss_pct: float = 0.05,
    ) -> Dict[str, LimitOrderRec]:
        """
        生成限价单建议。

        Parameters
        ----------
        issue_price : float
            发行价（港元）。
        mid_price : float
            发行价区间中值（港元）。
        predicted_return_p50 : float
            中性预测收益率（小数）。
        predicted_return_p75 : float
            乐观预测收益率（小数）。
        predicted_return_p25 : float
            保守预测收益率（小数）。
        anchor_comparable : str
            锚定的可比公司名称。
        stop_loss_pct : float
            止损比例（默认 5%）。

        Returns
        -------
        Dict[str, LimitOrderRec]
            {
                'dark_pool': LimitOrderRec,
                'first_day': LimitOrderRec,
            }
        """
        # 保守档：p25，但不低于发行价（破发时不追加买入）
        conservative = max(issue_price, issue_price * (1 + predicted_return_p25))

        # 中性档：p50
        neutral = issue_price * (1 + predicted_return_p50)

        # 进取档：p75
        aggressive = issue_price * (1 + predicted_return_p75)

        # 止损价
        stop_price = issue_price * (1 - stop_loss_pct)

        # 暗盘逻辑
        dark_logic = (
            f'锚定可比 {anchor_comparable}，暗盘合理价 = 发行价 × (1 + p50 {predicted_return_p50*100:.1f}%)'
            f'；保守档参考 p25 {predicted_return_p25*100:.1f}%，止损参考发行价 × (1-{stop_loss_pct*100:.0f}%)'
        )

        # 首日逻辑
        first_logic = (
            f'锚定可比 {anchor_comparable}，首日中性目标 = 发行价 × (1 + p50 {predicted_return_p50*100:.1f}%)'
            f'；进取参考 p75 {predicted_return_p75*100:.1f}%，保守参考 p25 {predicted_return_p25*100:.1f}%。'
            f'止损：{stop_price:.2f}港元（-{stop_loss_pct*100:.0f}%）'
        )

        dark_rec = LimitOrderRec(
            conservative_price=round(conservative, 3),
            neutral_price=round(neutral, 3),
            aggressive_price=round(aggressive, 3),
            logic=dark_logic,
            anchor_comparable=anchor_comparable,
            stop_price=round(stop_price, 3),
            stop_loss_pct=stop_loss_pct,
        )

        first_rec = LimitOrderRec(
            conservative_price=round(conservative, 3),
            neutral_price=round(neutral, 3),
            aggressive_price=round(aggressive, 3),
            logic=first_logic,
            anchor_comparable=anchor_comparable,
            stop_price=round(stop_price, 3),
            stop_loss_pct=stop_loss_pct,
        )

        logger.info(
            'OrderSheetGenerator: issue=%.2f conservative=%.2f neutral=%.2f '
            'aggressive=%.2f stop=%.2f',
            issue_price, conservative, neutral, aggressive, stop_price
        )

        return {
            'dark_pool': dark_rec,
            'first_day': first_rec,
        }


# ---------------------------------------------------------------------------
# 主引擎
# ---------------------------------------------------------------------------

class IPOAnalystEngine:
    """
    资深分析师分析引擎。

    整合 5 个模块，输出完整的 IPOAnalysisReport。

    Usage:
        engine = IPOAnalystEngine()
        report = engine.analyze(
            stock_code='09619',
            multi_source_data=multi_source_data,
            validated_data=validated_data,
            market_sentiment=market_sentiment,  # optional
        )
    """

    def __init__(
        self,
        store: Optional[IPORecordStore] = None,
        data_source: Optional[Any] = None,  # noqa: ANN401
    ):
        """
        Parameters
        ----------
        store : IPORecordStore, optional
            历史新股数据存储。
        data_source : Any, optional
            IPODataSource 实例（用于获取市场情绪数据）。
        """
        self._store = store or IPORecordStore()
        self._data_source = data_source

        self.comparable_engine = ComparableIPOEngine(store=self._store)
        self.investor_analyzer = InvestorStructureAnalyzer()
        self.terms_scorer = TermsValuationScorer()
        self.market_evaluator = MarketWindowEvaluator()
        self.order_sheet_gen = OrderSheetGenerator()

    def analyze(
        self,
        stock_code: str,
        multi_source_data: Dict[str, Any],
        validated_data: Dict[str, Any],
        market_sentiment: Optional[Dict[str, Any]] = None,
    ) -> IPOAnalysisReport:
        """
        主分析流程：

          1. 提取 validated_data 中的关键字段
          2. 可比 IPO 锚点 → p25/p50/p75 预测
          3. 机构持仓结构分析
          4. 发行条款评分
          5. 市场情绪评估
          6. 生成三档限价单建议
          7. 综合评级
          8. 组装 IPOAnalysisReport

        Parameters
        ----------
        stock_code : str
            股票代码。
        multi_source_data : Dict[str, Any]
            IPODataSource.get_all_sources() 的原始输出。
        validated_data : Dict[str, Any]
            DataCrossValidator.merge_with_confidence() 的输出。
        market_sentiment : Dict[str, Any], optional
            CompositeMarketDataSource 的输出。

        Returns
        -------
        IPOAnalysisReport
        """
        # ── 提取关键字段 ─────────────────────────────────────────────────
        ipo_info = multi_source_data.get('ipo_info') or {}

        # 基本信息
        name_cn = ipo_info.get('name_cn', ipo_info.get('name', stock_code))
        name_en = ipo_info.get('name_en', '')
        listing_date_str = ipo_info.get('listing_date')
        if isinstance(listing_date_str, str):
            listing_date = datetime.fromisoformat(listing_date_str).date()
        else:
            listing_date = listing_date_str or date.today()

        # 发行价
        issue_price = float(ipo_info.get('issue_price', 0.0))
        issue_price_low = float(ipo_info.get('issue_price_low', issue_price))
        issue_price_high = float(ipo_info.get('issue_price_high', issue_price))
        if issue_price == 0 and issue_price_low > 0:
            issue_price = (issue_price_low + issue_price_high) / 2
        mid_price = (issue_price_low + issue_price_high) / 2

        # 行业和募资
        industry = validated_data.get('industry', ipo_info.get('industry', '未知'))
        fund_raised_hkd = float(
            validated_data.get('fund_raised_hkd', ipo_info.get('fund_raised_hkd', 0.0))
        )

        # 基石投资者
        cornerstone_investors = validated_data.get(
            'cornerstone_investors', ipo_info.get('cornerstone_investors', [])
        )

        # 预 IPO 成本
        pre_ipo_cost: Optional[float] = None
        if 'pre_ipo_cost' in validated_data:
            pre_ipo_cost = float(validated_data['pre_ipo_cost'])

        # 锁定期
        lockup_period = str(validated_data.get('lockup_period', '未知'))

        # 公开发售占比
        retail_float_ratio = float(
            validated_data.get('retail_float_ratio', ipo_info.get('retail_float_ratio', 0.3))
        )

        # PS 折让
        ps_discount = float(validated_data.get('ps_discount_vs_sector', 0.0))
        industry_avg_ps = float(validated_data.get('industry_avg_ps', 8.0))

        # ── 模块①：可比 IPO ─────────────────────────────────────────────
        comp_result = self.comparable_engine.analyze(
            industry=industry,
            fund_raised_hkd=fund_raised_hkd,
        )
        comparable_ipos = comp_result['comparable_ipos']
        p25 = comp_result['predicted_return_p25']
        p50 = comp_result['predicted_return_p50']
        p75 = comp_result['predicted_return_p75']
        comp_confidence = comp_result['confidence']
        anchor_comparable = comp_result['anchor_comparable']

        # ── 模块②：机构结构 ─────────────────────────────────────────────
        inv_result = self.investor_analyzer.analyze(
            cornerstone_investors=cornerstone_investors,
            pre_ipo_cost=pre_ipo_cost,
            lockup_period=lockup_period,
            retail_float_ratio=retail_float_ratio,
            issue_price=issue_price,
        )
        institutional_score = inv_result['institutional_score']
        cornerstone_signals = inv_result['signals']

        # ── 模块③：发行条款 ──────────────────────────────────────────────
        terms_result = self.terms_scorer.analyze(
            ps_discount_vs_sector=ps_discount,
            fund_raised_hkd=fund_raised_hkd,
            issue_price_range=(issue_price_low, issue_price_high),
            industry_avg_ps=industry_avg_ps,
        )
        terms_score = terms_result['terms_score']
        optimal_scale = terms_result['optimal_scale']

        # ── 模块④：市场情绪 ─────────────────────────────────────────────
        if market_sentiment is None:
            market_sentiment = {}

        hstech_change = float(market_sentiment.get('hstech_recent_change', 0.0))
        recent_win_rate = float(market_sentiment.get('recent_ipo_win_rate', 0.5))
        sub_multiple = market_sentiment.get('subscription_multiple')
        vix = market_sentiment.get('vix')

        if sub_multiple is not None:
            sub_multiple = float(sub_multiple)
        if vix is not None:
            vix = float(vix)

        sentiment_result = self.market_evaluator.analyze(
            hstech_recent_change=hstech_change,
            recent_ipo_win_rate=recent_win_rate,
            subscription_multiple=sub_multiple,
            vix=vix,
        )
        market_sentiment_score = sentiment_result['market_sentiment_score']
        theme_momentum = sentiment_result['theme_momentum']
        sentiment_label = sentiment_result['sentiment_label']

        # ── 综合评级 ─────────────────────────────────────────────────────
        overall_rating = self._compute_overall_rating(
            institutional_score=institutional_score,
            terms_score=terms_score,
            market_sentiment_score=market_sentiment_score,
            confidence=comp_confidence,
        )

        # ── 模块⑤：挂单策略 ─────────────────────────────────────────────
        if issue_price > 0:
            order_recs = self.order_sheet_gen.generate(
                issue_price=issue_price,
                mid_price=mid_price,
                predicted_return_p50=p50,
                predicted_return_p75=p75,
                predicted_return_p25=p25,
                anchor_comparable=anchor_comparable,
            )
            dark_pool_rec = order_recs['dark_pool']
            first_day_rec = order_recs['first_day']
        else:
            dark_pool_rec = None
            first_day_rec = None

        # ── 风险和利好 ───────────────────────────────────────────────────
        risk_factors: List[str] = list(inv_result.get('negative_signals', []))
        risk_factors += terms_result.get('negative_signals', [])
        if sentiment_label == 'cold':
            risk_factors.append('市场情绪冷淡（窗口偏差）')
        if comp_confidence < 0.4:
            risk_factors.append('可比 IPO 样本不足，预测置信度低')

        positive_signals: List[str] = list(inv_result.get('positive_signals', []))
        positive_signals += terms_result.get('positive_signals', [])
        if overall_rating == 'BUY':
            positive_signals.append('综合评级 BUY，建议积极参与')

        # ── 数据质量 ─────────────────────────────────────────────────────
        data_quality: Dict[str, float] = {}
        if isinstance(validated_data.get('field_results'), dict):
            for field_name, result in validated_data['field_results'].items():
                data_quality[field_name] = getattr(result, 'confidence', 0.5)
        data_quality.setdefault('overall', validated_data.get('overall_confidence', 0.5))

        # ── 组装报告 ─────────────────────────────────────────────────────
        report = IPOAnalysisReport(
            stock_code=stock_code,
            name_cn=name_cn,
            name_en=name_en,
            listing_date=listing_date,
            issue_price_range=(issue_price_low, issue_price_high),
            mid_price=mid_price,
            overall_rating=overall_rating,
            confidence=comp_confidence,
            comparable_ipos=comparable_ipos,
            predicted_first_day_return_p50=p50,
            predicted_first_day_return_p75=p75,
            predicted_first_day_return_p25=p25,
            cornerstone_signals=cornerstone_signals,
            retail_float_ratio=retail_float_ratio,
            terms_score=terms_score,
            ps_discount_vs_sector=ps_discount,
            optimal_scale=optimal_scale,
            market_sentiment_score=market_sentiment_score,
            hstech_recent_change=hstech_change,
            recent_ipo_win_rate=recent_win_rate,
            theme_momentum=theme_momentum,
            dark_pool_recommendation=dark_pool_rec,
            first_day_recommendation=first_day_rec,
            risk_factors=risk_factors,
            key_positive_signals=positive_signals,
            data_quality_score=data_quality,
            generated_at=datetime.now(),
            institutional_score=institutional_score,
        )

        logger.info('IPOAnalystEngine.analyze: %s', report.summary())
        return report

    def _compute_overall_rating(
        self,
        institutional_score: float,
        terms_score: float,
        market_sentiment_score: float,
        confidence: float,
    ) -> Literal['BUY', 'NEUTRAL', 'SKIP']:
        """
        综合评级逻辑：

          - BUY：三个分数都在 0.6 以上，且 confidence >= 0.6
          - SKIP：任一分数 < 0.3，或 confidence < 0.4
          - NEUTRAL：介于两者之间
        """
        scores = [institutional_score, terms_score, market_sentiment_score]

        # SKIP 条件
        if any(s < 0.3 for s in scores):
            return 'SKIP'
        if confidence < 0.4:
            return 'SKIP'

        # BUY 条件
        if all(s >= 0.6 for s in scores) and confidence >= 0.6:
            return 'BUY'

        return 'NEUTRAL'
