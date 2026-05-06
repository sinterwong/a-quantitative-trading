"""
test_ipo_stars.py — IPO Stars 模块测试
"""

import os
import sys
import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

# ─── Path setup ───────────────────────────────────────────────
PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ_DIR)
sys.path.insert(0, os.path.join(PROJ_DIR, 'backend'))

from backend.services.ipo_stars.models import (
    IPOCandidate, ScoringResult, PricingStrategy, AnalysisReport,
    DarkPriceEstimate,
)
from backend.services.ipo_stars.scorer import IPOScorer, DEFAULT_WEIGHTS
from backend.services.ipo_stars import db as ipo_db


# ─── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def sample_candidate():
    """创建示例 IPO 候选标的。"""
    return IPOCandidate(
        code='09696',
        name='测试科技',
        status='subscripting',
        listing_date='2026-05-15',
        offer_price_low=10.0,
        offer_price_high=12.0,
        offer_price_final=11.5,
        issue_size=15.0,
        sponsor='中金公司',
        stabilizer='摩根士丹利',
        cornerstone_names='GIC,Temasek,高瓴',
        cornerstone_pct=0.55,
        public_offer_multiple=80.0,
        clawback_pct=0.30,
        margin_multiple=60.0,
        industry='人工智能',
        pre_ipo_cost=8.0,
    )


@pytest.fixture
def cold_candidate():
    """冷门 IPO 标的。"""
    return IPOCandidate(
        code='01234',
        name='冷门实业',
        status='upcoming',
        listing_date='2026-06-01',
        offer_price_low=5.0,
        offer_price_high=8.0,
        offer_price_final=0,
        issue_size=2.0,
        sponsor='小券商',
        stabilizer='',
        cornerstone_names='',
        cornerstone_pct=0.05,
        public_offer_multiple=2.0,
        clawback_pct=0.10,
        margin_multiple=5.0,
        industry='传统制造',
        pre_ipo_cost=0,
    )


@pytest.fixture
def market_ctx():
    """示例市场环境。"""
    return {
        'hstech_close': 4500.0,
        'hstech_bias_5d': 0.02,
        'hsi_vix': 18.5,
        'sector_ipo_performance': [
            {'code': '09695', 'name': 'AAA', 'first_day_return': 0.15},
            {'code': '09694', 'name': 'BBB', 'first_day_return': 0.08},
            {'code': '09693', 'name': 'CCC', 'first_day_return': -0.03},
        ],
    }


@pytest.fixture
def scorer():
    return IPOScorer()


# ─── 临时数据库 ───────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """使用临时数据库文件。"""
    db_path = str(tmp_path / 'test_ipo.db')

    def patched_get_db():
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # Patch both module paths (backend.services.portfolio and services.portfolio)
    import backend.services.portfolio as portfolio_mod
    monkeypatch.setattr(portfolio_mod, 'get_db', patched_get_db)

    # Also patch in sys.modules if loaded via services.portfolio
    import sys as _sys
    if 'services.portfolio' in _sys.modules:
        monkeypatch.setattr(_sys.modules['services.portfolio'], 'get_db', patched_get_db)

    ipo_db.init_ipo_tables()
    yield db_path


# ============================================================
# Models
# ============================================================

class TestModels:

    def test_ipo_candidate_creation(self, sample_candidate):
        assert sample_candidate.code == '09696'
        assert sample_candidate.name == '测试科技'
        assert sample_candidate.cornerstone_pct == 0.55

    def test_scoring_result_creation(self):
        sr = ScoringResult(
            dimension='sentiment',
            score=0.75,
            weight=0.45,
            weighted_score=0.3375,
            details={'test': True},
        )
        assert sr.weighted_score == pytest.approx(0.3375)

    def test_pricing_strategy_creation(self):
        ps = PricingStrategy(
            style='conservative',
            label='保守型',
            price=11.27,
            reference='test ref',
            stop_loss=10.93,
        )
        assert ps.style == 'conservative'
        assert ps.price == 11.27


# ============================================================
# Scorer
# ============================================================

class TestScorer:

    def test_weights_sum_to_one(self, scorer):
        total = sum(scorer.weights.values())
        assert total == pytest.approx(1.0)

    def test_score_returns_four_results(self, scorer, sample_candidate, market_ctx):
        results = scorer.score(sample_candidate, market_ctx)
        assert len(results) == 4
        dimensions = {r.dimension for r in results}
        assert dimensions == {'market_sentiment', 'chips_structure', 'narrative', 'valuation'}

    def test_scores_in_range(self, scorer, sample_candidate, market_ctx):
        results = scorer.score(sample_candidate, market_ctx)
        for r in results:
            assert 0.0 <= r.score <= 1.0, f"{r.dimension} score out of range: {r.score}"
            assert r.weighted_score == pytest.approx(r.score * r.weight, abs=0.001)

    def test_total_score_in_range(self, scorer, sample_candidate, market_ctx):
        results = scorer.score(sample_candidate, market_ctx)
        total = sum(r.weighted_score for r in results)
        assert 0.0 <= total <= 1.0

    def test_hot_candidate_scores_higher(self, scorer, sample_candidate, cold_candidate, market_ctx):
        hot_results = scorer.score(sample_candidate, market_ctx)
        cold_results = scorer.score(cold_candidate, market_ctx)
        hot_total = sum(r.weighted_score for r in hot_results)
        cold_total = sum(r.weighted_score for r in cold_results)
        assert hot_total > cold_total

    def test_recommend_levels(self):
        assert IPOScorer.recommend(0.80) == '重点参与'
        assert IPOScorer.recommend(0.70) == '重点参与'
        assert IPOScorer.recommend(0.55) == '建议观察'
        assert IPOScorer.recommend(0.45) == '建议观察'
        assert IPOScorer.recommend(0.30) == '放弃'
        assert IPOScorer.recommend(0.00) == '放弃'

    def test_heat_level(self, sample_candidate, cold_candidate):
        assert IPOScorer.heat_level(sample_candidate) == '较热'
        assert IPOScorer.heat_level(cold_candidate) == '冷淡'

    def test_control_level(self, sample_candidate, cold_candidate):
        assert IPOScorer.control_level(sample_candidate) == '高'
        assert IPOScorer.control_level(cold_candidate) == '低'

    def test_compute_pricing_with_final_price(self, sample_candidate):
        pricing = IPOScorer.compute_pricing(sample_candidate, total_score=0.75)
        assert len(pricing) == 3
        styles = [p.style for p in pricing]
        assert styles == ['conservative', 'neutral', 'aggressive']
        # 保守 < 中性 < 进取
        assert pricing[0].price <= pricing[1].price <= pricing[2].price
        # 止损价都低于定价
        for p in pricing:
            assert p.stop_loss < sample_candidate.offer_price_final

    def test_compute_pricing_no_price(self):
        no_price = IPOCandidate(
            code='00000', name='X', status='upcoming',
            listing_date='', offer_price_low=0, offer_price_high=0,
            offer_price_final=0, issue_size=0, sponsor='', stabilizer='',
            cornerstone_names='', cornerstone_pct=0,
            public_offer_multiple=0, clawback_pct=0, margin_multiple=0,
            industry='', pre_ipo_cost=0,
        )
        pricing = IPOScorer.compute_pricing(no_price, 0.5)
        assert pricing == []

    # ─── 暗盘价预估 ──────────────────────────────────────────

    def test_dark_estimate_hot_candidate(self, scorer, sample_candidate, market_ctx):
        """热门标的暗盘预估应为正溢价（规则降级模式）。"""
        est = scorer.estimate_dark_price_range(sample_candidate, 0.75, market_ctx)
        assert est is not None
        assert est.mid > sample_candidate.offer_price_final  # 正溢价
        assert est.low <= est.mid <= est.high                # 区间有序
        assert est.premium_pct > 0
        assert est.confidence in ('high', 'medium', 'low')
        assert len(est.basis) > 0

    def test_dark_estimate_cold_candidate(self, scorer, cold_candidate):
        """冷门标的暗盘预估应为负溢价或低溢价。"""
        est = scorer.estimate_dark_price_range(cold_candidate, 0.30)
        assert est is not None
        offer_high = cold_candidate.offer_price_high
        assert est.mid <= offer_high  # 冷门不应大幅溢价
        assert est.premium_pct <= 0

    def test_dark_estimate_no_price(self, scorer):
        """无定价时应返回 None。"""
        no_price = IPOCandidate(
            code='00000', name='X', status='upcoming',
            listing_date='', offer_price_low=0, offer_price_high=0,
            offer_price_final=0, issue_size=0, sponsor='', stabilizer='',
            cornerstone_names='', cornerstone_pct=0,
            public_offer_multiple=0, clawback_pct=0, margin_multiple=0,
            industry='', pre_ipo_cost=0,
        )
        assert scorer.estimate_dark_price_range(no_price, 0.5) is None

    def test_dark_estimate_high_lockup_narrows_range(self, scorer, sample_candidate, market_ctx):
        """高基石锁仓应收窄暗盘预估区间（规则降级模式）。"""
        est = scorer.estimate_dark_price_range(sample_candidate, 0.6, market_ctx)
        spread = est.high - est.low
        low_lock = sample_candidate._replace(cornerstone_pct=0.10, cornerstone_names='')
        est_low = scorer.estimate_dark_price_range(low_lock, 0.6, market_ctx)
        spread_low = est_low.high - est_low.low
        assert spread < spread_low  # 高锁仓区间更窄

    def test_dark_estimate_clawback_pressure(self, scorer, sample_candidate, market_ctx):
        """高回拨应压低暗盘预估。"""
        normal = scorer.estimate_dark_price_range(sample_candidate, 0.6, market_ctx)
        high_cb = sample_candidate._replace(clawback_pct=0.50)
        pressed = scorer.estimate_dark_price_range(high_cb, 0.6, market_ctx)
        assert pressed.mid < normal.mid  # 高回拨 → 暗盘承压

    def test_dark_estimate_with_mock_llm(self, sample_candidate, market_ctx):
        """LLM 可用时应调用 LLM 返回结果。"""
        mock_response = MagicMock()
        mock_response.content = '{"low": 12.0, "mid": 12.5, "high": 13.0, "confidence": "high", "reasoning": ["超购80x热度高", "基石55%锁仓"]}'
        mock_provider = MagicMock()
        mock_provider.chat.return_value = mock_response
        mock_llm = MagicMock()
        mock_llm.provider = mock_provider

        scorer = IPOScorer(llm_service=mock_llm)
        est = scorer.estimate_dark_price_range(sample_candidate, 0.75, market_ctx)
        assert est is not None
        assert est.low == 12.0
        assert est.mid == 12.5
        assert est.high == 13.0
        assert est.confidence == 'high'
        assert len(est.basis) == 2
        mock_provider.chat.assert_called_once()

    def test_dark_estimate_llm_fallback(self, sample_candidate, market_ctx):
        """LLM 调用失败时应降级为规则估算。"""
        mock_provider = MagicMock()
        mock_provider.chat.side_effect = RuntimeError("LLM unavailable")
        mock_llm = MagicMock()
        mock_llm.provider = mock_provider

        scorer = IPOScorer(llm_service=mock_llm)
        est = scorer.estimate_dark_price_range(sample_candidate, 0.75, market_ctx)
        assert est is not None
        assert '规则降级' in ' '.join(est.basis)

    def test_pricing_with_dark_estimate(self, scorer, sample_candidate, market_ctx):
        """有暗盘预估时，挂单价应基于暗盘区间。"""
        est = scorer.estimate_dark_price_range(sample_candidate, 0.75, market_ctx)
        pricing = IPOScorer.compute_pricing(sample_candidate, 0.75, dark_estimate=est)
        assert len(pricing) == 3
        # 保守型价格应基于暗盘下限
        assert pricing[0].price < est.mid
        assert '暗盘预估' in pricing[0].reference
        assert '暗盘预估' in pricing[1].reference

    def test_pricing_without_dark_estimate(self, sample_candidate):
        """无暗盘预估时应回退到定价比例法。"""
        pricing = IPOScorer.compute_pricing(sample_candidate, 0.75, dark_estimate=None)
        assert len(pricing) == 3
        assert '参考定价' in pricing[0].reference

    def test_sentiment_scoring_without_market_ctx(self, scorer, sample_candidate):
        results = scorer.score(sample_candidate, market_ctx=None)
        assert len(results) == 4

    def test_custom_weights(self, sample_candidate, market_ctx):
        custom_weights = {
            'market_sentiment': 0.30,
            'chips_structure': 0.30,
            'narrative': 0.30,
            'valuation': 0.10,
        }
        scorer = IPOScorer(weights=custom_weights)
        results = scorer.score(sample_candidate, market_ctx)
        for r in results:
            assert r.weight == custom_weights[r.dimension]


# ============================================================
# Database
# ============================================================

class TestDatabase:

    def test_init_tables(self, tmp_db):
        """Tables should exist after init."""
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        conn.close()
        assert 'ipo_candidates' in tables
        assert 'ipo_analyses' in tables
        assert 'ipo_subscriptions' in tables

    def test_upsert_and_get_candidate(self, tmp_db):
        ipo_db.upsert_candidate({
            'code': '09696',
            'name': '测试科技',
            'status': 'subscripting',
            'offer_price_low': 10.0,
            'offer_price_high': 12.0,
            'industry': 'AI',
        })
        result = ipo_db.get_candidate('09696')
        assert result is not None
        assert result['name'] == '测试科技'
        assert result['offer_price_low'] == 10.0

    def test_upsert_updates_existing(self, tmp_db):
        ipo_db.upsert_candidate({'code': '09696', 'name': 'V1', 'status': 'upcoming'})
        ipo_db.upsert_candidate({'code': '09696', 'name': 'V2', 'status': 'subscripting'})
        result = ipo_db.get_candidate('09696')
        assert result['name'] == 'V2'
        assert result['status'] == 'subscripting'

    def test_list_candidates(self, tmp_db):
        for i in range(5):
            ipo_db.upsert_candidate({
                'code': f'0000{i}',
                'name': f'Stock {i}',
                'status': 'upcoming' if i < 3 else 'closed',
            })
        all_list = ipo_db.list_candidates()
        assert len(all_list) == 5

        upcoming = ipo_db.list_candidates(status='upcoming')
        assert len(upcoming) == 3

    def test_get_nonexistent_candidate(self, tmp_db):
        assert ipo_db.get_candidate('99999') is None

    def test_save_and_get_analysis(self, tmp_db):
        ipo_db.save_analysis({
            'code': '09696',
            'final_score': 0.72,
            'recommendation': '重点参与',
            'heat_level': '较热',
            'control_level': '高',
            'sentiment_score': 0.8,
            'chips_score': 0.6,
            'narrative_score': 0.7,
            'valuation_score': 0.5,
            'pricing': [{'style': 'neutral', 'price': 12.0}],
            'risk_alerts': ['测试风险'],
            'key_factors': ['基石占比 55%'],
        })
        result = ipo_db.get_analysis('09696')
        assert result is not None
        assert result['final_score'] == 0.72
        assert result['recommendation'] == '重点参与'
        assert isinstance(result['pricing'], list)
        assert result['risk_alerts'] == ['测试风险']

    def test_subscriptions(self, tmp_db):
        ipo_db.add_subscription('09696', 'neutral')
        ipo_db.add_subscription('09697', 'aggressive')
        subs = ipo_db.list_subscriptions()
        assert len(subs) == 2

        removed = ipo_db.remove_subscription('09696', 'neutral')
        assert removed is True
        subs = ipo_db.list_subscriptions()
        assert len(subs) == 1

    def test_duplicate_subscription_ignored(self, tmp_db):
        ipo_db.add_subscription('09696', 'neutral')
        ipo_db.add_subscription('09696', 'neutral')  # should not raise
        subs = ipo_db.list_subscriptions()
        assert len(subs) == 1


# ============================================================
# Config
# ============================================================

class TestConfig:

    def test_ipo_stars_config_defaults(self):
        from core.config import IPOStarsConfig
        cfg = IPOStarsConfig()
        assert cfg.enabled is False
        assert cfg.webhook_type == 'feishu'
        assert sum(cfg.scoring_weights.values()) == pytest.approx(1.0)

    def test_ipo_stars_in_trading_config(self):
        from core.config import TradingConfig
        tc = TradingConfig()
        assert hasattr(tc, 'ipo_stars')
        assert tc.ipo_stars.enabled is False

    def test_parse_ipo_stars_from_yaml(self):
        from core.config import _parse_ipo_stars
        raw = {
            'enabled': True,
            'webhook_url': 'https://example.com/hook',
            'webhook_type': 'dingtalk',
            'scoring_weights': {
                'market_sentiment': 0.40,
                'chips_structure': 0.30,
                'narrative': 0.20,
                'valuation': 0.10,
            },
        }
        cfg = _parse_ipo_stars(raw)
        assert cfg.enabled is True
        assert cfg.webhook_url == 'https://example.com/hook'
        assert cfg.webhook_type == 'dingtalk'
        assert cfg.scoring_weights['market_sentiment'] == 0.40


# ============================================================
# Notifier (unit test — no actual HTTP call)
# ============================================================

class TestNotifier:

    def _make_report(self):
        return AnalysisReport(
            code='09696',
            name='测试科技',
            final_score=0.72,
            recommendation='重点参与',
            heat_level='较热',
            control_level='高',
            scoring_breakdown=[
                ScoringResult('market_sentiment', 0.8, 0.45, 0.36, {}),
                ScoringResult('chips_structure', 0.6, 0.25, 0.15, {}),
                ScoringResult('narrative', 0.7, 0.20, 0.14, {}),
                ScoringResult('valuation', 0.5, 0.10, 0.05, {}),
            ],
            pricing_strategies=[
                PricingStrategy('conservative', '保守型', 11.27, 'ref', 10.93),
                PricingStrategy('neutral', '中性型', 12.08, 'ref', 10.93),
                PricingStrategy('aggressive', '进取型', 12.65, 'ref', 10.93),
            ],
            dark_price_estimate=DarkPriceEstimate(
                low=11.80, mid=12.30, high=12.80,
                premium_pct=7.0, confidence='high',
                basis=['超购 80x，散户追捧', '基石锁仓 55%'],
            ),
            risk_alerts=['回拨风险', '无稳价人'],
            key_factors=['基石占比 55%', '稳价人：摩根士丹利'],
            analyzed_at='2026-05-10T14:30:00',
        )

    def test_render_feishu(self):
        from backend.services.ipo_stars.notifier import IPONotifier
        notifier = IPONotifier('https://fake.hook', 'feishu')
        report = self._make_report()
        payload = notifier._render_feishu(report)
        assert payload['msg_type'] == 'text'
        text = payload['content']['text']
        assert '测试科技' in text
        assert '重点参与' in text
        assert '保守型' in text
        assert '暗盘价预估' in text
        assert '11.80' in text  # dark low price

    def test_render_dingtalk(self):
        from backend.services.ipo_stars.notifier import IPONotifier
        notifier = IPONotifier('https://fake.hook', 'dingtalk')
        report = self._make_report()
        payload = notifier._render_dingtalk(report)
        assert payload['msgtype'] == 'markdown'
        text = payload['markdown']['text']
        assert '测试科技' in text
        assert '重点参与' in text

    def test_send_report_no_url(self):
        from backend.services.ipo_stars.notifier import IPONotifier
        notifier = IPONotifier('', 'feishu')
        report = self._make_report()
        assert notifier.send_report(report) is False


# ============================================================
# Service (integration with mock fetcher)
# ============================================================

class TestService:

    def test_risk_alerts_generation(self, sample_candidate):
        from backend.services.ipo_stars.service import IPOStarsService
        alerts = IPOStarsService._generate_risk_alerts(sample_candidate, [])
        # sample_candidate has clawback_pct=0.30 (no 50% alert)
        # but has stabilizer, cornerstone_pct=0.55 (no low-cornerstone alert)
        assert isinstance(alerts, list)

    def test_risk_alerts_cold_candidate(self, cold_candidate):
        from backend.services.ipo_stars.service import IPOStarsService
        alerts = IPOStarsService._generate_risk_alerts(cold_candidate, [])
        # cold: no stabilizer, low public_offer_multiple, low cornerstone
        alert_text = ' '.join(alerts)
        assert '无稳价人' in alert_text
        assert '认购不足' in alert_text
        assert '不足 20%' in alert_text

    def test_key_factors_generation(self, sample_candidate):
        from backend.services.ipo_stars.service import IPOStarsService
        factors = IPOStarsService._generate_key_factors(sample_candidate, [])
        assert any('摩根士丹利' in f for f in factors)
        assert any('55%' in f for f in factors)

    def test_dict_to_candidate(self):
        from backend.services.ipo_stars.service import IPOStarsService
        d = {
            'code': '09696', 'name': 'Test', 'status': 'upcoming',
            'offer_price_low': 10.0, 'offer_price_high': 12.0,
            'cornerstone_pct': 0.5,
        }
        c = IPOStarsService._dict_to_candidate(d)
        assert isinstance(c, IPOCandidate)
        assert c.code == '09696'
        assert c.cornerstone_pct == 0.5
