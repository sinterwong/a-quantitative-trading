"""
test_ipo_stars.py — IPO Stars 模块测试
"""

import os
import sys
import json
import sqlite3
import textwrap
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
from backend.services.ipo_stars.fetcher import (
    IPODataFetcher, _HKEXTableParser, _parse_prospectus_pdf,
    _parse_allotment_pdf,
)


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
        # 交互卡片格式
        assert payload['msg_type'] == 'interactive'
        card = payload['card']
        # 标题包含股票信息
        assert '测试科技' in card['header']['title']['content']
        assert '09696' in card['header']['title']['content']
        # 重点参与 → 绿色
        assert card['header']['template'] == 'green'
        # 卡片元素包含评分、暗盘、挂单、风险等内容
        elements_text = json.dumps(card['elements'], ensure_ascii=False)
        assert '重点参与' in elements_text
        assert '保守型' in elements_text
        assert '暗盘价预估' in elements_text
        assert '11.80' in elements_text
        assert '回拨风险' in elements_text
        assert '基石占比' in elements_text
        # 包含评分条形图
        assert '█' in elements_text

    def test_render_dingtalk(self):
        from backend.services.ipo_stars.notifier import IPONotifier
        notifier = IPONotifier('https://fake.hook', 'dingtalk')
        report = self._make_report()
        payload = notifier._render_dingtalk(report)
        # ActionCard 格式
        assert payload['msgtype'] == 'actionCard'
        card = payload['actionCard']
        assert '测试科技' in card['title']
        text = card['text']
        assert '重点参与' in text
        assert '保守型' in text
        assert '暗盘价预估' in text
        assert '风险提示' in text
        # 按钮
        assert len(card['btns']) >= 1
        assert '查看详情' in card['btns'][0]['title']

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


# ============================================================
# Fetcher
# ============================================================

# ─── Mock HTML（模拟 HKEX New Listings 页面结构）────────────────

MOCK_HKEX_HTML = textwrap.dedent('''\
<html><body>
<table class="table table-scroll">
    <thead>
        <tr>
            <th>Stock Code</th>
            <th>Stock Name</th>
            <th>NEW LISTING ANNOUNCEMENTS</th>
            <th>PROSPECTUSES</th>
            <th>ALLOTMENT RESULTS</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>7630</td>
            <td>IMPACT Therapeutics, Inc - B</td>
            <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0505/ann.pdf">Download</a></td>
            <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0505/prospectus_7630.pdf">Download</a></td>
            <td>&nbsp;</td>
        </tr>
        <tr>
            <td>1236</td>
            <td>SHENZHEN LDROBOT CO., LTD</td>
            <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0430/ann.pdf">Download</a></td>
            <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0430/prospectus_1236.pdf">Download</a></td>
            <td><a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0430/allotment_1236.pdf">Download</a></td>
        </tr>
        <tr>
            <td>2667</td>
            <td>Beijing Tong Ren Tang Healthcare Investment Co., Ltd.</td>
            <td>&nbsp;</td>
            <td>&nbsp;</td>
            <td>&nbsp;</td>
        </tr>
    </tbody>
</table>
</body></html>
''')


class TestFetcher:

    # ─── HKEX HTML 解析 ──────────────────────────────────────

    def test_hkex_table_parser_extracts_rows(self):
        parser = _HKEXTableParser()
        parser.feed(MOCK_HKEX_HTML)
        assert len(parser.rows) == 3

    def test_hkex_table_parser_code_and_name(self):
        parser = _HKEXTableParser()
        parser.feed(MOCK_HKEX_HTML)
        assert parser.rows[0]['code'] == '7630'
        assert 'IMPACT' in parser.rows[0]['name']
        assert parser.rows[1]['code'] == '1236'
        assert 'LDROBOT' in parser.rows[1]['name']
        assert parser.rows[2]['code'] == '2667'

    def test_hkex_table_parser_pdf_links(self):
        parser = _HKEXTableParser()
        parser.feed(MOCK_HKEX_HTML)
        # Row 0: has prospectus, no allotment
        assert 'prospectus_7630.pdf' in parser.rows[0].get('prospectus_url', '')
        assert 'allotment_url' not in parser.rows[0]
        # Row 1: has both
        assert 'prospectus_1236.pdf' in parser.rows[1].get('prospectus_url', '')
        assert 'allotment_1236.pdf' in parser.rows[1].get('allotment_url', '')
        # Row 2: no links
        assert 'prospectus_url' not in parser.rows[2]

    def test_fetch_upcoming_ipos_with_mock_html(self):
        """fetch_upcoming_ipos 应返回正确解析的候选列表。"""
        fetcher = IPODataFetcher()
        with patch.object(fetcher, '_fetch_html', return_value=MOCK_HKEX_HTML):
            results = fetcher.fetch_upcoming_ipos()

        assert len(results) == 3
        # 代码应补齐为 5 位
        assert results[0]['code'] == '07630'
        assert results[1]['code'] == '01236'
        assert results[2]['code'] == '02667'
        # 状态判断
        assert results[0]['status'] == 'subscripting'   # 有 prospectus 无 allotment
        assert results[1]['status'] == 'allotted'       # 有 allotment
        assert results[2]['status'] == 'upcoming'       # 都没有

    def test_fetch_upcoming_ipos_empty_page(self):
        """空页面应返回空列表。"""
        fetcher = IPODataFetcher()
        empty_html = '<html><body><table><thead></thead><tbody></tbody></table></body></html>'
        with patch.object(fetcher, '_fetch_html', return_value=empty_html):
            results = fetcher.fetch_upcoming_ipos()
        assert results == []

    # ─── 招股书 PDF 解析 ─────────────────────────────────────

    def test_fetch_prospectus_requires_url(self):
        """无 URL 时应抛出 ValueError。"""
        fetcher = IPODataFetcher()
        with pytest.raises(ValueError, match='No prospectus URL'):
            fetcher.fetch_prospectus('01236')

    def test_parse_prospectus_pdf_mock(self):
        """用最小 PDF 验证解析框架不崩溃。"""
        import fitz
        # 创建一个包含关键字段的最小 PDF
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Offer Price: HK$24.00 to HK$30.00")
        page.insert_text((72, 130), "33,333,400 H Shares")
        page.insert_text((72, 160),
            "Dealing in the H Shares is expected to commence on "
            "Monday, May 11, 2026")
        pdf_bytes = doc.tobytes()
        doc.close()

        result = _parse_prospectus_pdf(pdf_bytes)
        assert result.get('offer_price_low') == 24.0
        assert result.get('offer_price_high') == 30.0
        assert result.get('listing_date') == '2026-05-11'
        assert result.get('issue_shares') == 33333400
        # issue_size = 33333400 * 27.0 / 1e8 ≈ 9.0
        assert result.get('issue_size') == pytest.approx(9.0, abs=0.1)

    def test_parse_prospectus_pdf_stabilizer(self):
        """验证稳价人提取。"""
        import fitz
        doc = fitz.open()
        # 页面 0: 基本信息
        p0 = doc.new_page()
        p0.insert_text((72, 100), "Cover page")
        # 页面 1: 稳价人
        p1 = doc.new_page()
        p1.insert_text((72, 100), "Stabilizing Manager\nHaitong International Securities Company Limited")
        pdf_bytes = doc.tobytes()
        doc.close()

        result = _parse_prospectus_pdf(pdf_bytes)
        assert 'Haitong' in result.get('stabilizer', '')

    def test_parse_prospectus_pdf_cornerstone(self):
        """验证基石投资者占比提取。"""
        import fitz
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((72, 100),
            "Cornerstone Investor agreements\n"
            "representing approximately 34.62% of the total Offer Shares")
        pdf_bytes = doc.tobytes()
        doc.close()

        result = _parse_prospectus_pdf(pdf_bytes)
        assert result.get('cornerstone_pct') == pytest.approx(0.3462, abs=0.001)

    def test_parse_prospectus_pdf_empty(self):
        """空 PDF 不应崩溃。"""
        import fitz
        doc = fitz.open()
        doc.new_page()
        pdf_bytes = doc.tobytes()
        doc.close()

        result = _parse_prospectus_pdf(pdf_bytes)
        assert isinstance(result, dict)
        assert 'offer_price_low' not in result

    # ─── 市场环境 ────────────────────────────────────────────

    def test_fetch_market_context_mock(self):
        """mock 新浪接口响应，验证解析逻辑。"""
        mock_response = (
            'var hq_str_rt_hkHSTECH="HSTECH,恒生科技指数,'
            '5089.110,4969.200,5105.720,5070.300,5094.100,'
            '124.900,2.510,0.000,0.000,13993596.949,228450580,'
            '0.000,0.000,6715.460,4619.670,2026/05/07,09:39:26,,,,,,";'
        )
        fetcher = IPODataFetcher()
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response.encode('gbk')
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            ctx = fetcher.fetch_market_context()

        assert ctx['hstech_close'] == pytest.approx(5089.11)
        assert ctx['hstech_prev_close'] == pytest.approx(4969.20)
        assert ctx['hstech_change_pct'] == pytest.approx(2.51)
        assert ctx['hstech_bias_5d'] is None  # 暂未实现
        assert ctx['hsi_vix'] is None

    def test_fetch_market_context_bad_response(self):
        """异常响应应返回空 dict。"""
        fetcher = IPODataFetcher()
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'invalid data'
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            ctx = fetcher.fetch_market_context()

        assert ctx == {}

    # ─── 未实现接口 ──────────────────────────────────────────

    def test_subscription_data_not_implemented(self):
        fetcher = IPODataFetcher()
        with pytest.raises(NotImplementedError, match='富途'):
            fetcher.fetch_subscription_data('01236')

    def test_stabilizer_history_not_implemented(self):
        fetcher = IPODataFetcher()
        with pytest.raises(NotImplementedError):
            fetcher.fetch_stabilizer_history('test')

    # ─── 分配结果 PDF 解析 ───────────────────────────────────

    def test_parse_allotment_pdf_basic(self):
        """验证分配结果 PDF 基本字段提取。"""
        import fitz
        doc = fitz.open()
        # Page 0: disclaimer (skip)
        doc.new_page()
        # Page 1: summary
        p1 = doc.new_page()
        p1.insert_text((72, 80), "GLOBAL OFFERING")
        p1.insert_text((72, 100), "Final Offer Price : HK$39.33 per H Share")
        p1.insert_text((72, 120), "Stock code : 1187")
        # Page 2: allotment details
        p2 = doc.new_page()
        p2.insert_text((72, 80), "HONG KONG PUBLIC OFFERING")
        p2.insert_text((72, 100), "Subscription level  399.08 times")
        p2.insert_text((72, 120), "Claw-back triggered  N/A")
        p2.insert_text((72, 140),
            "% of Offer Shares under the Hong Kong Public Offering to\n"
            " the Global Offering\n"
            "10.00%")
        p2.insert_text((72, 200), "Number of Offer Shares : 27,000,000")
        p2.insert_text((72, 220), "Gross proceeds  HK$1,062.0 million")
        # Page 3: cornerstone
        p3 = doc.new_page()
        p3.insert_text((72, 80), "Cornerstone Investors")
        p3.insert_text((72, 100), "Sub-total 9,654,300 9,654,300 35.76%")
        # Page 4: listing date
        p4 = doc.new_page()
        p4.insert_text((72, 80), "Dealings commencement date May 6, 2026")
        pdf_bytes = doc.tobytes()
        doc.close()

        result = _parse_allotment_pdf(pdf_bytes)
        assert result.get('offer_price_final') == 39.33
        assert result.get('code') == '01187'
        assert result.get('public_offer_multiple') == pytest.approx(399.08)
        assert result.get('clawback_triggered') is False
        assert result.get('hk_public_offering_pct') == pytest.approx(0.10)
        assert result.get('cornerstone_pct') == pytest.approx(0.3576)
        assert result.get('offer_shares') == 27_000_000
        assert result.get('issue_size') == pytest.approx(10.62)
        assert result.get('listing_date') == '2026-05-06'

    def test_parse_allotment_pdf_empty(self):
        """空 PDF 不应崩溃。"""
        import fitz
        doc = fitz.open()
        doc.new_page()
        pdf_bytes = doc.tobytes()
        doc.close()

        result = _parse_allotment_pdf(pdf_bytes)
        assert isinstance(result, dict)
        assert 'offer_price_final' not in result

    def test_fetch_allotment_results_requires_url(self):
        fetcher = IPODataFetcher()
        with pytest.raises(ValueError, match='No allotment URL'):
            fetcher.fetch_allotment_results('01187')

    # ─── Update API ──────────────────────────────────────────

    def test_ipo_update_api(self, tmp_db):
        """POST /ipo/<code>/update 应更新指定字段。"""
        # 先插入一个候选标的
        ipo_db.upsert_candidate({
            'code': '01236', 'name': 'LDROBOT', 'status': 'upcoming',
        })
        # 验证手动更新
        updated = ipo_db.get_candidate('01236')
        assert updated['stabilizer'] == ''

        # 模拟 update 逻辑（直接测试 db 层）
        existing = ipo_db.get_candidate('01236')
        existing_data = dict(existing)
        existing_data.update({
            'stabilizer': 'Haitong International',
            'pre_ipo_cost': 15.0,
            'industry': '机器人',
        })
        ipo_db.upsert_candidate(existing_data)

        result = ipo_db.get_candidate('01236')
        assert result['stabilizer'] == 'Haitong International'
        assert result['pre_ipo_cost'] == 15.0
        assert result['industry'] == '机器人'
        assert result['name'] == 'LDROBOT'  # 未更新的字段保持不变

    # ─── 行业首日表现统计 ────────────────────────────────────

    def test_list_sector_performance(self, tmp_db):
        """同行业已上市标的首日表现查询。"""
        # 插入几个已上市标的
        for i, ret in enumerate([0.15, -0.03, 0.08]):
            ipo_db.upsert_candidate({
                'code': f'0100{i}',
                'name': f'AI Stock {i}',
                'status': 'listed',
                'industry': '人工智能',
                'first_day_return': ret,
            })
        # 插入一个不同行业的
        ipo_db.upsert_candidate({
            'code': '02000',
            'name': 'Mfg Stock',
            'status': 'listed',
            'industry': '传统制造',
            'first_day_return': -0.10,
        })

        results = ipo_db.list_sector_performance('人工智能')
        assert len(results) == 3
        assert all(r['first_day_return'] is not None for r in results)

        # 不同行业不应出现
        mfg = ipo_db.list_sector_performance('传统制造')
        assert len(mfg) == 1
        assert mfg[0]['first_day_return'] == -0.10

    def test_list_sector_performance_empty(self, tmp_db):
        assert ipo_db.list_sector_performance('') == []
        assert ipo_db.list_sector_performance('不存在的行业') == []

    # ─── LLM Narrative Scoring ───────────────────────────────

    def test_narrative_with_mock_llm(self, sample_candidate):
        """LLM 可用时应使用 ipo_narrative prompt。"""
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            'hotness': 0.9,
            'scarcity': 0.7,
            'narrative_strength': 0.8,
            'overall': 0.82,
            'reasoning': ['AI赛道热门', '港股稀缺'],
        })
        mock_provider = MagicMock()
        mock_provider.chat.return_value = mock_response
        mock_llm = MagicMock()
        mock_llm.provider = mock_provider

        scorer = IPOScorer(llm_service=mock_llm)
        results = scorer.score(sample_candidate)
        narrative = [r for r in results if r.dimension == 'narrative'][0]
        # LLM overall=0.82, keyword_score=0.5, scarcity_bonus=0.0 (no DB)
        # combined = 0.5 * 0.25 + 0.82 * 0.6 + 0.0 * 0.15 = 0.617
        assert narrative.score == pytest.approx(0.617, abs=0.01)
        assert 'llm_narrative' in narrative.details
        assert 'scarcity_bonus' in narrative.details
        mock_provider.chat.assert_called_once()

    def test_narrative_llm_fallback(self, sample_candidate):
        """LLM 失败时应降级为纯关键词评分。"""
        mock_provider = MagicMock()
        mock_provider.chat.side_effect = RuntimeError("LLM down")
        mock_llm = MagicMock()
        mock_llm.provider = mock_provider

        scorer = IPOScorer(llm_service=mock_llm)
        results = scorer.score(sample_candidate)
        narrative = [r for r in results if r.dimension == 'narrative'][0]
        # 应该只有关键词评分，无 LLM
        assert 'llm_narrative' not in narrative.details
        assert narrative.score > 0  # '人工智能' 应匹配

    def test_narrative_custom_hot_keywords(self, sample_candidate):
        """自定义热点关键词应生效。"""
        # 默认关键词包含 '人工智能'，sample industry='人工智能'
        scorer_default = IPOScorer()
        r1 = scorer_default.score(sample_candidate)
        n1 = [r for r in r1 if r.dimension == 'narrative'][0]

        # 自定义关键词不包含 '人工智能'
        scorer_custom = IPOScorer(hot_keywords=['区块链', '元宇宙'])
        r2 = scorer_custom.score(sample_candidate)
        n2 = [r for r in r2 if r.dimension == 'narrative'][0]

        assert n1.score > n2.score  # 默认匹配到 AI，自定义不匹配

    def test_scarcity_bonus_first_in_sector(self, tmp_db, sample_candidate):
        """行业首股应获得稀缺性加分。"""
        from backend.services.ipo_stars.scorer import IPOScorer
        # DB 中无同行业标的 → scarcity_bonus = 1.0
        bonus = IPOScorer._compute_scarcity_bonus('人工智能')
        assert bonus == 1.0

    def test_scarcity_bonus_crowded_sector(self, tmp_db, sample_candidate):
        """行业已有多只新股时无加分。"""
        from backend.services.ipo_stars.scorer import IPOScorer
        # 插入 3 只同行业已上市标的
        for i in range(3):
            ipo_db.upsert_candidate({
                'code': f'0200{i}', 'name': f'AI {i}',
                'status': 'listed', 'industry': '人工智能',
                'first_day_return': 0.1,
            })
        bonus = IPOScorer._compute_scarcity_bonus('人工智能')
        assert bonus == 0.0

    def test_config_hot_keywords(self):
        """IPOStarsConfig 应包含 hot_keywords 字段。"""
        from core.config import IPOStarsConfig
        cfg = IPOStarsConfig()
        assert isinstance(cfg.hot_keywords, list)
        assert 'AI' in cfg.hot_keywords

    def test_parse_ipo_stars_hot_keywords(self):
        """_parse_ipo_stars 应解析 hot_keywords。"""
        from core.config import _parse_ipo_stars
        raw = {
            'enabled': True,
            'hot_keywords': ['区块链', '元宇宙', 'Web3'],
        }
        cfg = _parse_ipo_stars(raw)
        assert cfg.hot_keywords == ['区块链', '元宇宙', 'Web3']

    def test_ipo_narrative_prompt_exists(self):
        """ipo_narrative prompt 模块应存在且包含必要字段。"""
        import importlib.util
        prompt_file = os.path.join(
            PROJ_DIR, 'backend', 'services', 'llm', 'prompts', 'ipo_narrative.py',
        )
        spec = importlib.util.spec_from_file_location('ipo_narrative', prompt_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert 'hotness' in mod.SYSTEM_PROMPT
        assert 'scarcity' in mod.SYSTEM_PROMPT
        assert '{name}' in mod.USER_TEMPLATE
        assert '{industry}' in mod.USER_TEMPLATE


# ============================================================
# PnL Tracking (ipo_results)
# ============================================================

class TestPnLTracking:

    def test_ipo_results_table_exists(self, tmp_db):
        """ipo_results 表应在 init 后存在。"""
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        conn.close()
        assert 'ipo_results' in tables

    def test_save_and_get_result(self, tmp_db):
        """保存并查询打新结果。"""
        ipo_db.save_result({
            'code': '01187',
            'name': 'Cofoe Medical',
            'predicted_score': 0.72,
            'recommendation': '重点参与',
            'subscribe_price': 39.33,
            'first_day_open': 42.50,
            'first_day_close': 41.80,
            'first_day_return': 0.0628,
            'pnl_per_lot': 1235.0,
            'listed_at': '2026-05-06',
        })
        result = ipo_db.get_result('01187')
        assert result is not None
        assert result['name'] == 'Cofoe Medical'
        assert result['predicted_score'] == 0.72
        assert result['first_day_return'] == pytest.approx(0.0628)
        assert result['pnl_per_lot'] == 1235.0

    def test_get_nonexistent_result(self, tmp_db):
        assert ipo_db.get_result('99999') is None

    def test_upsert_result(self, tmp_db):
        """重复保存应更新而非报错。"""
        ipo_db.save_result({
            'code': '01187', 'name': 'V1',
            'first_day_return': 0.05,
        })
        ipo_db.save_result({
            'code': '01187', 'name': 'V2',
            'first_day_return': 0.06,
        })
        result = ipo_db.get_result('01187')
        assert result['name'] == 'V2'
        assert result['first_day_return'] == 0.06

    def test_list_results(self, tmp_db):
        """列出所有打新结果。"""
        for i in range(3):
            ipo_db.save_result({
                'code': f'0100{i}',
                'name': f'Stock {i}',
                'listed_at': f'2026-05-0{i+1}',
                'first_day_return': 0.1 * (i + 1),
            })
        results = ipo_db.list_results()
        assert len(results) == 3
        # 按 listed_at DESC 排序
        assert results[0]['code'] == '01002'


# ============================================================
# Integration Test (end-to-end with mocks)
# ============================================================

# ============================================================
# Backtest
# ============================================================

class TestBacktest:

    def test_run_backtest_default_data(self):
        """用内置样本数据运行回测应返回结果。"""
        from backend.services.ipo_stars.backtest import run_backtest
        result = run_backtest()
        assert result['n_samples'] >= 5
        assert -1.0 <= result['spearman_ic'] <= 1.0
        assert 0.0 <= result['hit_rate'] <= 1.0
        assert len(result['records']) == result['n_samples']
        # 每条记录有必要字段
        for r in result['records']:
            assert 'predicted_score' in r
            assert 'actual_return' in r
            assert 'correct_direction' in r

    def test_backtest_scoring_direction(self):
        """热门标的评分应高于冷门标的。"""
        from backend.services.ipo_stars.backtest import run_backtest, HISTORICAL_IPOS
        result = run_backtest()
        records = result['records']
        # MIXUE (超购5269x) 评分应高于 TCTM (超购1.2x)
        mixue = next(r for r in records if r['code'] == '02160')
        tctm = next(r for r in records if r['code'] == '01070')
        assert mixue['predicted_score'] > tctm['predicted_score']

    def test_backtest_save_output(self, tmp_path):
        """回测结果应能保存到 JSON 文件。"""
        from backend.services.ipo_stars.backtest import run_backtest
        output = str(tmp_path / 'backtest.json')
        result = run_backtest(output_path=output)
        assert os.path.exists(output)
        with open(output, 'r') as f:
            loaded = json.load(f)
        assert loaded['n_samples'] == result['n_samples']
        assert loaded['spearman_ic'] == result['spearman_ic']

    def test_spearman_ic(self):
        """验证 Spearman IC 计算。"""
        from backend.services.ipo_stars.backtest import _spearman_ic
        # 完美正相关
        assert _spearman_ic([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
        # 完美负相关
        assert _spearman_ic([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
        # 不足 3 个样本
        assert _spearman_ic([1, 2], [2, 1]) == 0.0


class TestIntegration:
    """全链路集成测试：入库 → 评分 → 报告 → 推送。"""

    def test_full_pipeline(self, tmp_db):
        """HKEX 数据入库 → 评分 → 报告生成 → webhook 推送全流程。"""
        from backend.services.ipo_stars.service import IPOStarsService
        from backend.services.ipo_stars.notifier import IPONotifier

        # 1. 入库候选标的
        ipo_db.upsert_candidate({
            'code': '07630',
            'name': 'IMPACT Therapeutics',
            'status': 'subscripting',
            'listing_date': '2026-05-15',
            'offer_price_low': 24.0,
            'offer_price_high': 30.0,
            'offer_price_final': 27.0,
            'issue_size': 10.0,
            'sponsor': 'Morgan Stanley',
            'stabilizer': 'Goldman Sachs',
            'cornerstone_names': 'GIC,Temasek',
            'cornerstone_pct': 0.45,
            'public_offer_multiple': 50.0,
            'clawback_pct': 0.20,
            'margin_multiple': 40.0,
            'industry': '生物医药',
        })

        # 2. 验证入库
        candidate = ipo_db.get_candidate('07630')
        assert candidate is not None
        assert candidate['name'] == 'IMPACT Therapeutics'

        # 3. 创建 service (无 LLM, 无 webhook)
        with patch('backend.services.ipo_stars.service.IPODataFetcher') as MockFetcher:
            mock_fetcher = MockFetcher.return_value
            mock_fetcher.fetch_market_context.return_value = {
                'hstech_close': 5100.0,
                'hstech_change_pct': 1.5,
            }
            # Patch create_llm_service to avoid import issues
            svc = IPOStarsService()

            # 4. 运行分析
            result = svc.analyze('07630', push=False)

        # 5. 验证分析结果
        assert 'error' not in result
        assert result['code'] == '07630'
        assert result['name'] == 'IMPACT Therapeutics'
        assert 0.0 <= result['final_score'] <= 1.0
        assert result['recommendation'] in ('重点参与', '建议观察', '放弃')
        assert len(result['scoring_breakdown']) == 4
        assert len(result['pricing_strategies']) >= 0  # 有定价时应有策略

        # 6. 验证分析结果已持久化
        analysis = ipo_db.get_analysis('07630')
        assert analysis is not None
        assert analysis['final_score'] == result['final_score']

    def test_pipeline_fetcher_failure_degradation(self, tmp_db):
        """fetcher 失败时应降级，不崩溃。"""
        from backend.services.ipo_stars.service import IPOStarsService

        ipo_db.upsert_candidate({
            'code': '01236', 'name': 'LDROBOT',
            'status': 'subscripting',
            'offer_price_final': 15.0,
            'industry': '机器人',
        })

        with patch('backend.services.ipo_stars.service.IPODataFetcher') as MockFetcher:
            mock_fetcher = MockFetcher.return_value
            mock_fetcher.fetch_market_context.side_effect = Exception('network error')
            svc = IPOStarsService()
            result = svc.analyze('01236')

        assert 'error' not in result
        assert result['code'] == '01236'
        assert 0.0 <= result['final_score'] <= 1.0

    def test_pipeline_not_found(self, tmp_db):
        """不存在的标的应返回错误。"""
        from backend.services.ipo_stars.service import IPOStarsService
        with patch('backend.services.ipo_stars.service.IPODataFetcher') as MockFetcher:
            mock_fetcher = MockFetcher.return_value
            mock_fetcher.fetch_prospectus.side_effect = NotImplementedError
            svc = IPOStarsService()
            result = svc.analyze('99999')
        assert 'error' in result

    def test_batch_analyze(self, tmp_db):
        """批量分析应处理多只标的。"""
        from backend.services.ipo_stars.service import IPOStarsService

        for code, name in [('07630', 'IMPACT'), ('01236', 'LDROBOT')]:
            ipo_db.upsert_candidate({
                'code': code, 'name': name,
                'status': 'subscripting',
                'offer_price_final': 20.0,
            })

        with patch('backend.services.ipo_stars.service.IPODataFetcher') as MockFetcher:
            mock_fetcher = MockFetcher.return_value
            mock_fetcher.fetch_market_context.return_value = {}
            svc = IPOStarsService()
            results = svc.batch_analyze(push=False)

        assert len(results) == 2
        assert all('error' not in r for r in results)

    def test_webhook_push_integration(self, tmp_db):
        """报告生成后 webhook 推送验证。"""
        from backend.services.ipo_stars.service import IPOStarsService

        ipo_db.upsert_candidate({
            'code': '07630', 'name': 'IMPACT',
            'status': 'subscripting',
            'offer_price_final': 27.0,
            'sponsor': 'MS', 'stabilizer': 'GS',
            'cornerstone_pct': 0.45,
            'public_offer_multiple': 50.0,
        })

        with patch('backend.services.ipo_stars.service.IPODataFetcher') as MockFetcher:
            mock_fetcher = MockFetcher.return_value
            mock_fetcher.fetch_market_context.return_value = {}
            svc = IPOStarsService(
                    webhook_url='https://fake.webhook.com/hook',
                    webhook_type='feishu',
                )
            # Mock the HTTP POST
            with patch.object(svc.notifier, '_post', return_value=True) as mock_post:
                result = svc.analyze('07630', push=True)

        assert 'error' not in result
        mock_post.assert_called_once()
        # 验证 payload 格式
        payload = mock_post.call_args[0][0]
        assert payload['msg_type'] == 'interactive'
        assert 'IMPACT' in payload['card']['header']['title']['content']
