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

from .models import IPOCandidate, ScoringResult, PricingStrategy, DarkPriceEstimate

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

    # ─── 暗盘价预估 ─────────────────────────────────────────────

    def estimate_dark_price_range(
        self,
        candidate: IPOCandidate,
        total_score: float,
        market_ctx: Optional[Dict] = None,
    ) -> Optional[DarkPriceEstimate]:
        """
        暗盘价区间预估：LLM 决策 + 结构化数据。

        流程：
            1. 收集结构化信号（超购、基石、回拨、大盘等）
            2. 将信号组装成 prompt 提交 LLM，由 LLM 综合判断给出区间
            3. LLM 不可用时降级为纯规则估算

        Returns:
            DarkPriceEstimate 或 None（无定价信息时）
        """
        ref_price = candidate.offer_price_final or candidate.offer_price_high
        if ref_price <= 0:
            return None

        # 收集结构化信号
        signals = self._collect_dark_signals(candidate, total_score, market_ctx)

        # 尝试 LLM 决策
        if self.llm:
            try:
                return self._llm_dark_estimate(candidate, ref_price, signals)
            except Exception as e:
                logger.warning('LLM dark price estimate failed, fallback: %s', e)

        # 降级：纯规则估算
        return self._rule_dark_estimate(ref_price, signals)

    @staticmethod
    def _collect_dark_signals(
        candidate: IPOCandidate,
        total_score: float,
        market_ctx: Optional[Dict] = None,
    ) -> Dict:
        """将候选标的 + 评分 + 大盘环境收集为结构化信号 dict。"""
        ctx = market_ctx or {}
        offer_mid = (candidate.offer_price_low + candidate.offer_price_high) / 2
        pre_ipo_premium = (
            round(offer_mid / candidate.pre_ipo_cost, 2)
            if candidate.pre_ipo_cost > 0 and offer_mid > 0
            else None
        )
        return {
            'code': candidate.code,
            'name': candidate.name,
            'industry': candidate.industry,
            'offer_price_final': candidate.offer_price_final,
            'offer_price_range': f'{candidate.offer_price_low}-{candidate.offer_price_high}',
            'public_offer_multiple': candidate.public_offer_multiple,
            'margin_multiple': candidate.margin_multiple,
            'cornerstone_pct': candidate.cornerstone_pct,
            'cornerstone_names': candidate.cornerstone_names,
            'clawback_pct': candidate.clawback_pct,
            'stabilizer': candidate.stabilizer,
            'pre_ipo_premium': pre_ipo_premium,
            'total_score': round(total_score, 4),
            'hstech_bias_5d': ctx.get('hstech_bias_5d'),
            'hsi_vix': ctx.get('hsi_vix'),
            'sector_ipo_avg_return': (
                round(
                    sum(p.get('first_day_return', 0)
                        for p in ctx.get('sector_ipo_performance', []))
                    / len(ctx['sector_ipo_performance']), 4
                )
                if ctx.get('sector_ipo_performance')
                else None
            ),
        }

    def _llm_dark_estimate(
        self, candidate: IPOCandidate, ref_price: float, signals: Dict,
    ) -> DarkPriceEstimate:
        """调用 LLM 进行暗盘价判断。"""
        import json as _json

        # 组装 prompt
        signal_text = '\n'.join(
            f'  - {k}: {v}' for k, v in signals.items() if v is not None
        )
        prompt = (
            f"以下是港股新股 {signals['name']}({signals['code']}) 的结构化数据：\n"
            f"{signal_text}\n\n"
            f"IPO 定价: {ref_price} 港元\n\n"
            f"请基于以上数据，综合判断该新股暗盘交易的可能价格区间。"
        )

        system = (
            "你是港股打新专家，擅长根据 IPO 结构化数据预判暗盘价走势。\n"
            "你需要综合考虑：超购倍数、孖展融资倍数、基石投资者锁仓比例、"
            "回拨机制、稳价人、大盘情绪、行业热度、Pre-IPO 溢价等因素，"
            "给出暗盘价的 [下限, 中位, 上限] 三点估计。\n\n"
            "你必须严格按以下 JSON 格式输出，不要添加任何其他内容：\n"
            "{\n"
            '  "low": 暗盘价下限（浮点数），\n'
            '  "mid": 暗盘价中位预估（浮点数），\n'
            '  "high": 暗盘价上限（浮点数），\n'
            '  "confidence": "high" | "medium" | "low"，\n'
            '  "reasoning": ["判断依据1", "判断依据2", ...]\n'
            "}\n\n"
            "注意事项：\n"
            "- low/mid/high 必须是具体价格（港元），不是百分比\n"
            "- 必须满足 low <= mid <= high\n"
            "- reasoning 中每条依据要具体引用数据，不要泛泛而谈\n"
            "- 超购倍数极高(>100x)时暗盘通常大幅高开；认购冷淡(<5x)有破发风险\n"
            "- 基石锁仓 >50% 时流通筹码少，暗盘波动区间应收窄\n"
            "- 回拨 >=50% 意味着散户拿到大量筹码，暗盘可能承压"
        )

        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': prompt},
        ]

        response = self.llm.provider.chat(messages, temperature=0.2, max_tokens=512)
        raw = response.content.strip()

        # 去除可能的 markdown 代码块包装
        if raw.startswith('```'):
            lines = raw.split('\n')
            raw = '\n'.join(lines[1:-1]).strip()

        parsed = _json.loads(raw)

        low = float(parsed['low'])
        mid = float(parsed['mid'])
        high = float(parsed['high'])
        confidence = parsed.get('confidence', 'medium')
        reasoning = parsed.get('reasoning', [])

        # 安全校验
        if not (low <= mid <= high):
            low, mid, high = sorted([low, mid, high])
        if confidence not in ('high', 'medium', 'low'):
            confidence = 'medium'

        premium_pct = round((mid / ref_price - 1) * 100, 2)

        return DarkPriceEstimate(
            low=round(low, 2),
            mid=round(mid, 2),
            high=round(high, 2),
            premium_pct=premium_pct,
            confidence=confidence,
            basis=reasoning if isinstance(reasoning, list) else [str(reasoning)],
        )

    @staticmethod
    def _rule_dark_estimate(
        ref_price: float, signals: Dict,
    ) -> DarkPriceEstimate:
        """LLM 不可用时的规则降级估算。"""
        basis = []

        # 超购倍数 → 基础溢价
        mult = signals.get('public_offer_multiple', 0)
        if mult >= 100:
            base_premium = 0.12
            basis.append(f'超购 {mult:.0f}x，认购火爆')
        elif mult >= 50:
            base_premium = 0.08
            basis.append(f'超购 {mult:.0f}x，热度较高')
        elif mult >= 15:
            base_premium = 0.04
            basis.append(f'超购 {mult:.0f}x，认购适中')
        elif mult >= 5:
            base_premium = 0.0
            basis.append(f'超购 {mult:.0f}x，认购一般')
        else:
            base_premium = -0.05
            basis.append(f'超购仅 {mult:.1f}x，认购冷淡')

        # 基石 → 区间宽度
        cs_pct = signals.get('cornerstone_pct', 0)
        spread_factor = 0.6 if cs_pct >= 0.50 else (0.8 if cs_pct >= 0.30 else 1.0)
        if cs_pct >= 0.50:
            basis.append(f'基石锁仓 {cs_pct*100:.0f}%，波动收窄')

        # 评分
        score = signals.get('total_score', 0.5)
        score_adj = 0.03 if score >= 0.70 else (-0.03 if score < 0.45 else 0.0)

        # 大盘
        bias = signals.get('hstech_bias_5d') or 0.0
        market_adj = 0.02 if bias > 0.02 else (-0.02 if bias < -0.02 else 0.0)

        # 回拨
        clawback = signals.get('clawback_pct', 0)
        clawback_adj = -0.03 if clawback >= 0.50 else 0.0
        if clawback >= 0.50:
            basis.append(f'回拨 {clawback*100:.0f}%，散户筹码多')

        mid_premium = base_premium + score_adj + market_adj + clawback_adj
        base_spread = 0.06 * spread_factor

        basis.append('(规则降级估算，LLM 不可用)')

        info_count = sum([mult > 0, cs_pct > 0, bias != 0,
                          bool(signals.get('stabilizer'))])
        confidence = 'high' if info_count >= 4 else ('medium' if info_count >= 2 else 'low')

        return DarkPriceEstimate(
            low=round(ref_price * (1 + mid_premium - base_spread), 2),
            mid=round(ref_price * (1 + mid_premium), 2),
            high=round(ref_price * (1 + mid_premium + base_spread), 2),
            premium_pct=round(mid_premium * 100, 2),
            confidence=confidence,
            basis=basis,
        )

    # ─── 挂单价计算 ───────────────────────────────────────────

    @staticmethod
    def compute_pricing(
        candidate: IPOCandidate,
        total_score: float,
        dark_estimate: Optional[DarkPriceEstimate] = None,
    ) -> List[PricingStrategy]:
        """
        基于最终定价 + 综合得分 + 暗盘预估生成三档挂单价 + 止损价。

        若有暗盘预估，保守型参考暗盘下限，中性型参考暗盘中位。
        若无暗盘预估或定价未出，回退到纯定价比例法。
        """
        ref_price = candidate.offer_price_final or candidate.offer_price_high
        if ref_price <= 0:
            return []

        stop_loss_pct = -0.05  # 破发 -5% 止损
        stop_loss = round(ref_price * (1 + stop_loss_pct), 2)

        # 有暗盘预估时，用暗盘区间指导挂单价
        if dark_estimate and dark_estimate.mid > 0:
            dk = dark_estimate
            return [
                PricingStrategy(
                    style='conservative',
                    label='保守型',
                    price=round(dk.low * 0.98, 2),
                    reference=f'暗盘预估下限 {dk.low} 再折 2%，确保成交不追高',
                    stop_loss=stop_loss,
                ),
                PricingStrategy(
                    style='neutral',
                    label='中性型',
                    price=round(dk.mid, 2),
                    reference=f'暗盘预估中位 {dk.mid}（溢价 {dk.premium_pct:+.1f}%），博取开盘脉冲',
                    stop_loss=stop_loss,
                ),
                PricingStrategy(
                    style='aggressive',
                    label='进取型',
                    price=round(dk.high * 1.02, 2),
                    reference=f'暗盘预估上限 {dk.high} 上浮 2%，适合高确定性标的',
                    stop_loss=stop_loss,
                ),
            ]

        # 无暗盘预估时，回退到定价比例法
        if total_score >= 0.70:
            conserv_adj, neutral_adj, aggress_adj = -0.02, 0.05, 0.10
        elif total_score >= 0.45:
            conserv_adj, neutral_adj, aggress_adj = -0.03, 0.02, 0.05
        else:
            conserv_adj, neutral_adj, aggress_adj = -0.05, -0.02, 0.01

        return [
            PricingStrategy(
                style='conservative',
                label='保守型',
                price=round(ref_price * (1 + conserv_adj), 2),
                reference=f'参考定价 {ref_price} 折让 {abs(conserv_adj)*100:.0f}%，确保成交不追高',
                stop_loss=stop_loss,
            ),
            PricingStrategy(
                style='neutral',
                label='中性型',
                price=round(ref_price * (1 + neutral_adj), 2),
                reference=f'参考定价 + {neutral_adj*100:.0f}%，博取首日开盘脉冲',
                stop_loss=stop_loss,
            ),
            PricingStrategy(
                style='aggressive',
                label='进取型',
                price=round(ref_price * (1 + aggress_adj), 2),
                reference=f'参考定价 + {aggress_adj*100:.0f}%，适合高确定性标的',
                stop_loss=stop_loss,
            ),
        ]
