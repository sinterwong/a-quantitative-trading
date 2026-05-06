"""
scorer.py — IPO Stars 四维评分引擎
==================================
权重模型（来自 IPO-stars.md 第 3 节）：

    市场情绪    45%  —  最终认购倍数、暗盘交易额预测、恒指近期走势
    筹码结构    25%  —  回拨比例、基石占比、稳价人历史胜率
    主题/稀缺性  20%  —  LLM 热点匹配度、行业近期胜率
    基本面/估值  10%  —  PS/PE 相对行业折价、Pre-IPO 成本安全垫
"""

import logging
from typing import Dict, List, Optional

from .models import IPOCandidate, ScoringResult, PricingStrategy

logger = logging.getLogger('ipo_stars.scorer')

# ─── 默认权重 ─────────────────────────────────────────────────

DEFAULT_WEIGHTS: Dict[str, float] = {
    'market_sentiment': 0.45,
    'chips_structure': 0.25,
    'narrative': 0.20,
    'valuation': 0.10,
}

# ─── 顶级基石白名单 ──────────────────────────────────────────

DEFAULT_CORNERSTONE_WHITELIST = [
    'GIC', 'Temasek', '红杉', 'Sequoia', '高瓴', 'Hillhouse',
    'BlackRock', 'Fidelity', 'Capital Group', 'Tiger Global',
    'Coatue', 'DST Global', 'Silver Lake', '中投', 'CIC',
    '淡马锡', '新加坡政府投资', 'Abu Dhabi', 'ADIA',
]


class IPOScorer:
    """
    四维评分引擎。

    用法：
        scorer = IPOScorer()
        results = scorer.score(candidate, market_ctx)
        total = sum(r.weighted_score for r in results)
        recommendation = scorer.recommend(total)
        pricing = scorer.compute_pricing(candidate, total)
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        cornerstone_whitelist: Optional[List[str]] = None,
        llm_service=None,
    ):
        self.weights = weights or DEFAULT_WEIGHTS.copy()
        self.cornerstone_whitelist = cornerstone_whitelist or DEFAULT_CORNERSTONE_WHITELIST
        self.llm = llm_service

    # ─── 主入口 ───────────────────────────────────────────────

    def score(
        self,
        candidate: IPOCandidate,
        market_ctx: Optional[Dict] = None,
    ) -> List[ScoringResult]:
        """
        运行四维评分，返回 ScoringResult 列表。

        Args:
            candidate:  IPOCandidate 实例
            market_ctx: 大盘环境数据 dict（来自 fetcher.fetch_market_context）

        Returns:
            四个 ScoringResult，weighted_score 之和即综合得分
        """
        ctx = market_ctx or {}
        return [
            self._score_sentiment(candidate, ctx),
            self._score_chips(candidate),
            self._score_narrative(candidate),
            self._score_valuation(candidate),
        ]

    # ─── A. 市场情绪 (45%) ────────────────────────────────────

    def _score_sentiment(
        self, candidate: IPOCandidate, market_ctx: Dict,
    ) -> ScoringResult:
        """
        市场情绪评分。

        子因子：
            - 公开发售超购倍数（主权重）
            - 恒生科技指数 5日 Bias（正向加分）
            - 同行业近期新股首日平均表现
        """
        details = {}
        sub_scores = []

        # 1) 超购倍数 → 归一化到 [0, 1]
        mult = candidate.public_offer_multiple
        if mult >= 100:
            s = 1.0
        elif mult >= 50:
            s = 0.8
        elif mult >= 15:
            s = 0.6
        elif mult >= 5:
            s = 0.4
        elif mult > 0:
            s = 0.2
        else:
            s = 0.0
        details['oversubscription'] = {'multiple': mult, 'sub_score': s}
        sub_scores.append(s * 0.6)  # 超购占情绪维度 60%

        # 2) 大盘 Bias
        bias = market_ctx.get('hstech_bias_5d', 0.0)
        bias_score = max(0.0, min(1.0, 0.5 + bias * 10))  # bias=0 → 0.5
        details['hstech_bias'] = {'bias_5d': bias, 'sub_score': bias_score}
        sub_scores.append(bias_score * 0.2)

        # 3) 板块动量（同行业近 3 只新股首日平均）
        sector_perf = market_ctx.get('sector_ipo_performance', [])
        if sector_perf:
            avg_ret = sum(p.get('first_day_return', 0) for p in sector_perf) / len(sector_perf)
            sector_score = max(0.0, min(1.0, 0.5 + avg_ret * 5))
        else:
            avg_ret = 0.0
            sector_score = 0.5
        details['sector_momentum'] = {'avg_return': avg_ret, 'sub_score': sector_score}
        sub_scores.append(sector_score * 0.2)

        raw_score = sum(sub_scores)
        weight = self.weights['market_sentiment']

        return ScoringResult(
            dimension='market_sentiment',
            score=round(raw_score, 4),
            weight=weight,
            weighted_score=round(raw_score * weight, 4),
            details=details,
        )

    # ─── B. 筹码结构 (25%) ───────────────────────────────────

    def _score_chips(self, candidate: IPOCandidate) -> ScoringResult:
        """
        筹码结构评分。

        子因子：
            - 基石投资者占比（占比 > 50% 利好）
            - 基石投资者质量（匹配白名单）
            - 回拨机制预警（回拨 > 50% 利空）
            - 稳价人（有稳价人加分）
        """
        details = {}
        sub_scores = []

        # 1) 基石占比
        cs_pct = candidate.cornerstone_pct
        cs_score = min(1.0, cs_pct / 0.6) if cs_pct > 0 else 0.0
        details['cornerstone_pct'] = {'pct': cs_pct, 'sub_score': cs_score}
        sub_scores.append(cs_score * 0.35)

        # 2) 基石质量（匹配白名单数量）
        names = candidate.cornerstone_names
        if names:
            name_list = [n.strip() for n in names.split(',')]
            matches = sum(
                1 for n in name_list
                if any(w.lower() in n.lower() for w in self.cornerstone_whitelist)
            )
            quality_score = min(1.0, matches / 3)  # 3个顶级基石 → 满分
        else:
            matches = 0
            quality_score = 0.0
        details['cornerstone_quality'] = {'matches': matches, 'sub_score': quality_score}
        sub_scores.append(quality_score * 0.25)

        # 3) 回拨预警（回拨越高，散户筹码越多 → 利空）
        clawback = candidate.clawback_pct
        if clawback >= 0.50:
            cb_score = 0.2   # 触发 50% 回拨，高风险
        elif clawback >= 0.30:
            cb_score = 0.5
        elif clawback > 0:
            cb_score = 0.8   # 低回拨，筹码集中
        else:
            cb_score = 0.5   # 未知
        details['clawback'] = {'pct': clawback, 'sub_score': cb_score}
        sub_scores.append(cb_score * 0.20)

        # 4) 稳价人
        stabilizer_score = 0.7 if candidate.stabilizer else 0.3
        details['stabilizer'] = {'name': candidate.stabilizer, 'sub_score': stabilizer_score}
        sub_scores.append(stabilizer_score * 0.20)

        raw_score = sum(sub_scores)
        weight = self.weights['chips_structure']

        return ScoringResult(
            dimension='chips_structure',
            score=round(raw_score, 4),
            weight=weight,
            weighted_score=round(raw_score * weight, 4),
            details=details,
        )

    # ─── C. 主题 / 稀缺性 (20%) ──────────────────────────────

    def _score_narrative(self, candidate: IPOCandidate) -> ScoringResult:
        """
        故事力 / 稀缺性评分。

        子因子：
            - LLM 热点匹配度（如有 LLM 可用）
            - 行业稀缺性（是否港股该赛道首股）

        无 LLM 时降级为基于行业关键词的简单匹配。
        """
        details = {}

        # 热门行业关键词（硬编码基线，LLM 可增强）
        hot_keywords = [
            '人工智能', 'AI', '具身智能', '低空经济', '机器人',
            '芯片', '半导体', '新能源', '自动驾驶', 'SaaS',
            '大模型', 'AGI', '量子计算', '脑机接口',
        ]

        industry = candidate.industry or ''
        keyword_matches = sum(1 for kw in hot_keywords if kw.lower() in industry.lower())
        keyword_score = min(1.0, keyword_matches / 2)
        details['keyword_matches'] = keyword_matches
        details['keyword_score'] = keyword_score

        # LLM 增强（如可用）
        llm_score = None
        if self.llm:
            try:
                result = self.llm.analyze_news(
                    f"港股新股行业: {industry}, 公司名: {candidate.name}"
                )
                llm_score = getattr(result, 'confidence', 0.5)
                details['llm_sentiment'] = llm_score
            except Exception as e:
                logger.warning('LLM narrative analysis failed: %s', e)

        # 综合
        if llm_score is not None:
            raw_score = keyword_score * 0.4 + llm_score * 0.6
        else:
            raw_score = keyword_score

        weight = self.weights['narrative']

        return ScoringResult(
            dimension='narrative',
            score=round(raw_score, 4),
            weight=weight,
            weighted_score=round(raw_score * weight, 4),
            details=details,
        )

    # ─── D. 基本面 / 估值 (10%) ──────────────────────────────

    def _score_valuation(self, candidate: IPOCandidate) -> ScoringResult:
        """
        估值评分。

        子因子：
            - Pre-IPO 溢价率（招股价 / Pre-IPO 成本）
            - 发行定价区间宽度（区间越窄，定价越明确）
        """
        details = {}

        # 1) Pre-IPO 溢价率
        offer_mid = (candidate.offer_price_low + candidate.offer_price_high) / 2
        pre_ipo = candidate.pre_ipo_cost
        if pre_ipo > 0 and offer_mid > 0:
            premium = offer_mid / pre_ipo
            # 溢价 < 1.5 → 安全垫充足；溢价 > 3 → 危险
            if premium <= 1.2:
                premium_score = 1.0
            elif premium <= 1.5:
                premium_score = 0.8
            elif premium <= 2.0:
                premium_score = 0.5
            elif premium <= 3.0:
                premium_score = 0.3
            else:
                premium_score = 0.1
        else:
            premium = 0.0
            premium_score = 0.5  # 未知
        details['pre_ipo_premium'] = {'ratio': round(premium, 2), 'sub_score': premium_score}

        # 2) 定价区间宽度（相对中位数）
        if offer_mid > 0:
            spread = (candidate.offer_price_high - candidate.offer_price_low) / offer_mid
            spread_score = max(0.0, min(1.0, 1.0 - spread * 5))  # 0% → 1.0, 20% → 0
        else:
            spread = 0.0
            spread_score = 0.5
        details['price_spread'] = {'spread_pct': round(spread, 4), 'sub_score': spread_score}

        raw_score = premium_score * 0.7 + spread_score * 0.3
        weight = self.weights['valuation']

        return ScoringResult(
            dimension='valuation',
            score=round(raw_score, 4),
            weight=weight,
            weighted_score=round(raw_score * weight, 4),
            details=details,
        )

    # ─── 推荐等级 ─────────────────────────────────────────────

    @staticmethod
    def recommend(total_score: float) -> str:
        """综合得分 → 推荐等级。"""
        if total_score >= 0.70:
            return '重点参与'
        if total_score >= 0.45:
            return '建议观察'
        return '放弃'

    @staticmethod
    def heat_level(candidate: IPOCandidate) -> str:
        """认购倍数 → 热度等级。"""
        m = candidate.public_offer_multiple
        if m >= 100:
            return '火爆'
        if m >= 30:
            return '较热'
        if m >= 5:
            return '一般'
        return '冷淡'

    @staticmethod
    def control_level(candidate: IPOCandidate) -> str:
        """基石 + 配售 → 控盘程度。"""
        pct = candidate.cornerstone_pct
        if pct >= 0.60:
            return '极高'
        if pct >= 0.40:
            return '高'
        if pct >= 0.20:
            return '中等'
        return '低'

    # ─── 挂单价计算 ───────────────────────────────────────────

    @staticmethod
    def compute_pricing(
        candidate: IPOCandidate,
        total_score: float,
    ) -> List[PricingStrategy]:
        """
        基于最终定价 + 综合得分生成三档挂单价 + 止损价。

        若最终定价未出，使用招股价上限作参考。
        """
        ref_price = candidate.offer_price_final or candidate.offer_price_high
        if ref_price <= 0:
            return []

        # 根据得分调整幅度
        if total_score >= 0.70:
            conserv_adj, neutral_adj, aggress_adj = -0.02, 0.05, 0.10
        elif total_score >= 0.45:
            conserv_adj, neutral_adj, aggress_adj = -0.03, 0.02, 0.05
        else:
            conserv_adj, neutral_adj, aggress_adj = -0.05, -0.02, 0.01

        stop_loss_pct = -0.05  # 破发 -5% 止损

        return [
            PricingStrategy(
                style='conservative',
                label='保守型',
                price=round(ref_price * (1 + conserv_adj), 2),
                reference=f'参考定价 {ref_price} 折让 {abs(conserv_adj)*100:.0f}%，确保成交不追高',
                stop_loss=round(ref_price * (1 + stop_loss_pct), 2),
            ),
            PricingStrategy(
                style='neutral',
                label='中性型',
                price=round(ref_price * (1 + neutral_adj), 2),
                reference=f'参考定价 + {neutral_adj*100:.0f}%，博取首日开盘脉冲',
                stop_loss=round(ref_price * (1 + stop_loss_pct), 2),
            ),
            PricingStrategy(
                style='aggressive',
                label='进取型',
                price=round(ref_price * (1 + aggress_adj), 2),
                reference=f'参考定价 + {aggress_adj*100:.0f}%，适合高确定性标的',
                stop_loss=round(ref_price * (1 + stop_loss_pct), 2),
            ),
        ]
