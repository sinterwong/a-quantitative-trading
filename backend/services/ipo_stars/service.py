"""
service.py — IPO Stars 主服务
=============================
编排数据获取 → 评分 → 报告生成 → 推送的完整流程。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

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
        hot_keywords: Optional[List[str]] = None,
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
            hot_keywords=hot_keywords,
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

    def analyze(
        self,
        code: str,
        push: bool = False,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """
        对单只 IPO 进行四维深度分析。

        Args:
            code: 港股代码（如 '09696'）
            push: 是否推送报告到 webhook
            overrides: 字段覆盖字典，用于补充/覆盖数据库中的字段
                       支持的字段：
                       - public_offer_multiple (float): 公开发售认购倍数
                       - industry (str): 所属行业板块
                       - pre_ipo_cost (float): pre-IPO 估值
                       - name (str): 公司名称（覆盖显示名）

        Returns:
            完整分析报告 dict
        """
        overrides = overrides or {}

        # 1. 获取标的数据 — 若 DB 缺失或关键字段为空，自动从 HKEX 拉取
        candidate_data = ipo_db.get_candidate(code)
        needs_fetch = (
            not candidate_data
            or not candidate_data.get('offer_price_low')
            or not candidate_data.get('listing_date')
        )
        if needs_fetch:
            fetched = self._auto_fetch(code)
            # 至少要拿到 name 才算找到（否则可能是 HKEX 没收录或网络失败）
            if fetched and fetched.get('name'):
                # 已有数据 + 新抓数据合并（DB 字段优先于网络抓取，避免覆盖手动录入）
                merged = {**fetched, **(candidate_data or {})}
                # 但允许 fetched 填补 DB 中为空的字段
                for key, val in fetched.items():
                    if val and not merged.get(key):
                        merged[key] = val
                ipo_db.upsert_candidate(merged)
                candidate_data = ipo_db.get_candidate(code)

        if not candidate_data:
            return {'error': f'IPO candidate {code} not found'}

        # 应用字段覆盖
        candidate = self._dict_to_candidate({**candidate_data, **overrides})

        # 2. 获取市场环境
        market_ctx = {}
        try:
            market_ctx = self.fetcher.fetch_market_context()
        except (NotImplementedError, Exception) as e:
            logger.info('Market context fetch failed, using defaults: %s', e)

        # 2b. 补充同行业新股首日表现（来自本地 DB）
        if candidate.industry and 'sector_ipo_performance' not in market_ctx:
            sector_perf = ipo_db.list_sector_performance(candidate.industry)
            if sector_perf:
                market_ctx['sector_ipo_performance'] = sector_perf

        # 3. 运行评分
        scoring_results = self.scorer.score(candidate, market_ctx)
        total_score = sum(r.weighted_score for r in scoring_results)

        # 4. 暗盘价预估 + 推荐与定价
        recommendation = self.scorer.recommend(total_score)
        heat = self.scorer.heat_level(candidate)
        control = self.scorer.control_level(candidate)
        dark_estimate = self.scorer.estimate_dark_price_range(
            candidate, total_score, market_ctx,
        )
        pricing = self.scorer.compute_pricing(candidate, total_score, dark_estimate)

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
            dark_price_estimate=dark_estimate,
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

    def _auto_fetch(self, code: str) -> Dict:
        """
        从 HKEX 自动拉取该 code 的元数据：
            1. fetch_upcoming_ipos() → 找到对应行，拿到 prospectus_url / allotment_url
            2. 若有 allotment_url（已分配）→ fetch_allotment_results() 拿最终定价 + 超购倍数
            3. 若有 prospectus_url → fetch_prospectus()（含 LLM 兜底）

        所有异常都吞掉，返回尽可能多的字段（包括空 dict）。
        """
        merged: Dict = {'code': code}
        try:
            ipos = self.fetcher.fetch_upcoming_ipos()
        except Exception as e:
            logger.info('fetch_upcoming_ipos failed: %s', e)
            return {}

        # 找到对应 code 的行（4 位 / 5 位 都兼容）
        norm_code = code.zfill(5)
        try:
            target = next(
                (x for x in ipos if isinstance(x, dict) and (
                    x.get('code') == norm_code
                    or x.get('code', '').lstrip('0') == code.lstrip('0')
                )),
                None,
            )
        except (TypeError, AttributeError):
            target = None
        if not target:
            logger.info('Code %s not found in HKEX New Listings', code)
            return {}

        merged['name'] = target.get('name', '')
        merged['status'] = target.get('status', 'upcoming')

        # 若已分配，先解析 allotment（拿最终定价/超购倍数最准）
        if target.get('allotment_url'):
            try:
                allot = self.fetcher.fetch_allotment_results(
                    code, target['allotment_url'],
                )
                merged.update({k: v for k, v in allot.items() if v})
            except Exception as e:
                logger.info('fetch_allotment_results failed for %s: %s', code, e)

        # 解析招股书（含 LLM 兜底）
        if target.get('prospectus_url'):
            try:
                prospectus = self.fetcher.fetch_prospectus(
                    code, target['prospectus_url'],
                )
                # 招股书字段不覆盖 allotment 已拿到的（如最终定价）
                for k, v in prospectus.items():
                    if v and not merged.get(k):
                        merged[k] = v
            except Exception as e:
                logger.info('fetch_prospectus failed for %s: %s', code, e)

        return merged

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
            first_day_return=float(d.get('first_day_return') or 0),
            lot_size=int(d.get('lot_size') or 0),
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
        d = {
            'code': report.code,
            'name': report.name,
            'final_score': report.final_score,
            'recommendation': report.recommendation,
            'heat_level': report.heat_level,
            'control_level': report.control_level,
            'scoring_breakdown': [sr._asdict() for sr in report.scoring_breakdown],
            'pricing_strategies': [ps._asdict() for ps in report.pricing_strategies],
            'dark_price_estimate': report.dark_price_estimate._asdict() if report.dark_price_estimate else None,
            'risk_alerts': report.risk_alerts,
            'key_factors': report.key_factors,
            'analyzed_at': report.analyzed_at,
        }
        return d
