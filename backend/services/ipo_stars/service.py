"""
service.py — IPO Stars 主服务
=============================
编排数据获取 → 评分 → 报告生成 → 推送的完整流程。
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from .models import IPOCandidate, AnalysisReport
from .scorer import IPOScorer
from .fetcher import IPODataFetcher
from .notifier import IPONotifier
from . import db as ipo_db

logger = logging.getLogger('ipo_stars')


class IPOStarsService:
    """
    港股打新分析主服务。

    用法：
        svc = IPOStarsService()
        candidates = svc.get_candidates(status='upcoming')
        report = svc.analyze('09696', push=True)
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        webhook_url: str = '',
        webhook_type: str = 'feishu',
        cornerstone_whitelist: Optional[List[str]] = None,
    ):
        # 初始化子组件
        self.fetcher = IPODataFetcher()

        # LLM（可选，graceful degradation）
        llm = None
        try:
            from services.llm.factory import create_llm_service
            llm = create_llm_service()
        except Exception as e:
            logger.info('LLM not available, narrative scoring degraded: %s', e)

        self.scorer = IPOScorer(
            weights=weights,
            cornerstone_whitelist=cornerstone_whitelist,
            llm_service=llm,
        )
        self.notifier = IPONotifier(webhook_url, webhook_type) if webhook_url else None

        # 确保表存在
        ipo_db.init_ipo_tables()

    # ─── 候选列表 ─────────────────────────────────────────────

    def get_candidates(
        self,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """
        列出 IPO 候选标的。

        优先从数据库读取。若数据库为空且 fetcher 已实现，
        则尝试从数据源拉取并入库。
        """
        candidates = ipo_db.list_candidates(status=status, limit=limit)
        if candidates:
            return candidates

        # 尝试从数据源拉取
        try:
            raw_list = self.fetcher.fetch_upcoming_ipos()
            for item in raw_list:
                ipo_db.upsert_candidate(item)
            return ipo_db.list_candidates(status=status, limit=limit)
        except NotImplementedError:
            logger.info('Fetcher not implemented, returning empty list')
            return []

    # ─── 深度分析 ─────────────────────────────────────────────

    def analyze(self, code: str, push: bool = False) -> Dict:
        """
        对单只 IPO 进行四维深度分析。

        Args:
            code: 港股代码（如 '09696'）
            push: 是否推送报告到 webhook

        Returns:
            完整分析报告 dict
        """
        # 1. 获取标的数据
        candidate_data = ipo_db.get_candidate(code)
        if not candidate_data:
            # 尝试从数据源拉取
            try:
                prospectus = self.fetcher.fetch_prospectus(code)
                ipo_db.upsert_candidate(prospectus)
                candidate_data = ipo_db.get_candidate(code)
            except NotImplementedError:
                pass

        if not candidate_data:
            return {'error': f'IPO candidate {code} not found'}

        candidate = self._dict_to_candidate(candidate_data)

        # 2. 获取市场环境
        market_ctx = {}
        try:
            market_ctx = self.fetcher.fetch_market_context()
        except NotImplementedError:
            logger.info('Market context fetcher not implemented, using defaults')

        # 3. 运行评分
        scoring_results = self.scorer.score(candidate, market_ctx)
        total_score = sum(r.weighted_score for r in scoring_results)

        # 4. 生成推荐与定价
        recommendation = self.scorer.recommend(total_score)
        heat = self.scorer.heat_level(candidate)
        control = self.scorer.control_level(candidate)
        pricing = self.scorer.compute_pricing(candidate, total_score)

        # 5. 风险提示
        risk_alerts = self._generate_risk_alerts(candidate, scoring_results)

        # 6. 关键因子
        key_factors = self._generate_key_factors(candidate, scoring_results)

        # 7. 组装报告
        now = datetime.now().isoformat()
        report = AnalysisReport(
            code=code,
            name=candidate.name,
            final_score=round(total_score, 4),
            recommendation=recommendation,
            heat_level=heat,
            control_level=control,
            scoring_breakdown=list(scoring_results),
            pricing_strategies=list(pricing),
            risk_alerts=risk_alerts,
            key_factors=key_factors,
            analyzed_at=now,
        )

        # 8. 持久化
        ipo_db.save_analysis({
            'code': code,
            'final_score': report.final_score,
            'recommendation': recommendation,
            'heat_level': heat,
            'control_level': control,
            'sentiment_score': scoring_results[0].score,
            'chips_score': scoring_results[1].score,
            'narrative_score': scoring_results[2].score,
            'valuation_score': scoring_results[3].score,
            'pricing': [ps._asdict() for ps in pricing],
            'risk_alerts': risk_alerts,
            'key_factors': key_factors,
            'analyzed_at': now,
        })

        # 9. 推送
        if push and self.notifier:
            self.notifier.send_report(report)

        return self._report_to_dict(report)

    # ─── 订阅 ─────────────────────────────────────────────────

    def subscribe(self, code: str, strategy: str = 'neutral') -> None:
        """订阅打新提醒。"""
        ipo_db.add_subscription(code, strategy)
        logger.info('Subscribed to %s with %s strategy', code, strategy)

    def get_subscriptions(self) -> List[Dict]:
        """查看已订阅列表。"""
        return ipo_db.list_subscriptions()

    # ─── 批量分析 ─────────────────────────────────────────────

    def batch_analyze(
        self,
        codes: Optional[List[str]] = None,
        push: bool = True,
    ) -> List[Dict]:
        """
        批量分析 + 推送。

        若未指定 codes，则分析所有 upcoming/subscripting 状态的标的。
        """
        if codes is None:
            candidates = ipo_db.list_candidates(status='upcoming', limit=50)
            candidates += ipo_db.list_candidates(status='subscripting', limit=50)
            codes = [c['code'] for c in candidates]

        results = []
        for code in codes:
            try:
                result = self.analyze(code, push=push)
                results.append(result)
            except Exception as e:
                logger.error('Failed to analyze %s: %s', code, e)
                results.append({'code': code, 'error': str(e)})

        return results

    # ─── 内部方法 ─────────────────────────────────────────────

    @staticmethod
    def _dict_to_candidate(d: Dict) -> IPOCandidate:
        """dict → IPOCandidate NamedTuple。"""
        return IPOCandidate(
            code=d.get('code', ''),
            name=d.get('name', ''),
            status=d.get('status', 'upcoming'),
            listing_date=d.get('listing_date', ''),
            offer_price_low=float(d.get('offer_price_low', 0)),
            offer_price_high=float(d.get('offer_price_high', 0)),
            offer_price_final=float(d.get('offer_price_final', 0)),
            issue_size=float(d.get('issue_size', 0)),
            sponsor=d.get('sponsor', ''),
            stabilizer=d.get('stabilizer', ''),
            cornerstone_names=d.get('cornerstone_names', ''),
            cornerstone_pct=float(d.get('cornerstone_pct', 0)),
            public_offer_multiple=float(d.get('public_offer_multiple', 0)),
            clawback_pct=float(d.get('clawback_pct', 0)),
            margin_multiple=float(d.get('margin_multiple', 0)),
            industry=d.get('industry', ''),
            pre_ipo_cost=float(d.get('pre_ipo_cost', 0)),
        )

    @staticmethod
    def _generate_risk_alerts(
        candidate: IPOCandidate,
        scoring_results: list,
    ) -> List[str]:
        """基于分析结果生成风险提示列表。"""
        alerts = []

        # 回拨风险
        if candidate.clawback_pct >= 0.50:
            alerts.append(
                f'预计触发 {candidate.clawback_pct*100:.0f}% 回拨，'
                f'散户筹码多，开盘 15 分钟内波动将极大'
            )

        # 超购过低
        if candidate.public_offer_multiple < 5:
            alerts.append('公开发售认购不足 5 倍，市场热度偏低')

        # Pre-IPO 溢价过高
        offer_mid = (candidate.offer_price_low + candidate.offer_price_high) / 2
        if candidate.pre_ipo_cost > 0 and offer_mid > 0:
            premium = offer_mid / candidate.pre_ipo_cost
            if premium > 3.0:
                alerts.append(
                    f'招股价相对 Pre-IPO 成本溢价 {premium:.1f}x，'
                    f'首日抛压巨大'
                )

        # 无稳价人
        if not candidate.stabilizer:
            alerts.append('无稳价人，破发后缺乏护盘支撑')

        # 基石占比过低
        if candidate.cornerstone_pct < 0.20:
            alerts.append('基石投资者占比不足 20%，锁仓效果有限')

        return alerts

    @staticmethod
    def _generate_key_factors(
        candidate: IPOCandidate,
        scoring_results: list,
    ) -> List[str]:
        """提取关键影响因子。"""
        factors = []

        if candidate.stabilizer:
            factors.append(f'稳价人：{candidate.stabilizer}')

        if candidate.cornerstone_pct > 0:
            factors.append(
                f'基石投资者认购占比 {candidate.cornerstone_pct*100:.0f}%'
            )

        if candidate.public_offer_multiple > 0:
            factors.append(
                f'公开发售超购 {candidate.public_offer_multiple:.1f} 倍'
            )

        if candidate.margin_multiple > 0:
            factors.append(f'综合孖展倍数 {candidate.margin_multiple:.0f}x')

        return factors

    @staticmethod
    def _report_to_dict(report: AnalysisReport) -> Dict:
        """AnalysisReport → 可 JSON 序列化的 dict。"""
        return {
            'code': report.code,
            'name': report.name,
            'final_score': report.final_score,
            'recommendation': report.recommendation,
            'heat_level': report.heat_level,
            'control_level': report.control_level,
            'scoring_breakdown': [sr._asdict() for sr in report.scoring_breakdown],
            'pricing_strategies': [ps._asdict() for ps in report.pricing_strategies],
            'risk_alerts': report.risk_alerts,
            'key_factors': report.key_factors,
            'analyzed_at': report.analyzed_at,
        }
