"""
tests/test_single_stock_analysis.py — 单股票综合分析模块测试

覆盖：
  - 符号识别 detect_market / normalize_*_symbol
  - AnalysisRequest.from_body 校验
  - _compute_risk_metrics 数值边界
  - _make_recommendation 决策逻辑（regime / fundamentals / 风险约束）
  - _safe_json_extract LLM 输出解析
  - analyze_a_share / analyze_hk_share 端到端（mock 数据层 + 因子）
  - HTTP /analysis/stock/a 与 /analysis/stock/hk 路由
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import pandas as pd  # noqa: E402
import numpy as np   # noqa: E402


# ---------------------------------------------------------------------------
# 符号识别
# ---------------------------------------------------------------------------

class TestDetectMarket(unittest.TestCase):

    def test_a_share_sh(self):
        from backend.services.single_stock_analysis import detect_market
        self.assertEqual(detect_market('603369.SH'), 'A')
        self.assertEqual(detect_market('600519.SH'), 'A')

    def test_a_share_sz(self):
        from backend.services.single_stock_analysis import detect_market
        self.assertEqual(detect_market('000858.SZ'), 'A')

    def test_hk_share_formats(self):
        from backend.services.single_stock_analysis import detect_market
        self.assertEqual(detect_market('HK:00700'), 'HK')
        self.assertEqual(detect_market('00700.HK'), 'HK')
        self.assertEqual(detect_market('hk00700'), 'HK')
        self.assertEqual(detect_market('HK:9988'), 'HK')   # 4 位

    def test_unknown_format(self):
        from backend.services.single_stock_analysis import detect_market
        self.assertEqual(detect_market('AAPL'), 'unknown')
        self.assertEqual(detect_market(''), 'unknown')


class TestNormalizers(unittest.TestCase):

    def test_normalize_a_share_uppercase(self):
        from backend.services.single_stock_analysis import normalize_a_share_symbol
        self.assertEqual(normalize_a_share_symbol('603369.sh'), '603369.SH')

    def test_normalize_a_share_invalid_raises(self):
        from backend.services.single_stock_analysis import normalize_a_share_symbol
        with self.assertRaises(ValueError):
            normalize_a_share_symbol('00700.HK')

    def test_normalize_hk_pads_to_5(self):
        from backend.services.single_stock_analysis import normalize_hk_symbol
        self.assertEqual(normalize_hk_symbol('HK:700'), 'hk00700')   # 3 位 -> 不匹配 (需 4-5 位)
        # 4 位补 5
        self.assertEqual(normalize_hk_symbol('HK:9988'), 'hk09988')
        self.assertEqual(normalize_hk_symbol('00700.HK'), 'hk00700')


# ---------------------------------------------------------------------------
# AnalysisRequest 解析
# ---------------------------------------------------------------------------

class TestAnalysisRequest(unittest.TestCase):

    def test_minimal_body(self):
        from backend.services.single_stock_analysis import AnalysisRequest
        req = AnalysisRequest.from_body({'symbol': '603369.SH'})
        self.assertEqual(req.symbol, '603369.SH')
        self.assertEqual(req.lookback_days, 250)
        self.assertFalse(req.include_llm)
        self.assertTrue(req.include_regime)

    def test_missing_symbol_raises(self):
        from backend.services.single_stock_analysis import AnalysisRequest
        with self.assertRaises(ValueError):
            AnalysisRequest.from_body({})

    def test_full_body(self):
        from backend.services.single_stock_analysis import AnalysisRequest
        req = AnalysisRequest.from_body({
            'symbol': '600519.SH', 'lookback_days': 60,
            'include_llm': True, 'include_news': True, 'include_ml': True,
        })
        self.assertEqual(req.lookback_days, 60)
        self.assertTrue(req.include_llm)
        self.assertTrue(req.include_news)
        self.assertTrue(req.include_ml)


# ---------------------------------------------------------------------------
# 风险指标计算
# ---------------------------------------------------------------------------

class TestRiskMetrics(unittest.TestCase):

    def _fake_df(self, n=120, seed=42):
        rng = np.random.default_rng(seed)
        rets = rng.normal(0.0005, 0.015, n)
        prices = 50 * np.exp(np.cumsum(rets))
        return pd.DataFrame({
            'open': prices * 0.998,
            'high': prices * 1.005,
            'low':  prices * 0.995,
            'close': prices,
            'volume': rng.integers(1e5, 1e7, n),
        })

    def test_compute_risk_basic_fields(self):
        from backend.services.single_stock_analysis import _compute_risk_metrics
        df = self._fake_df()
        last = float(df['close'].iloc[-1])
        m = _compute_risk_metrics(df, last)
        for k in ('atr_14', 'atr_pct', 'var_95_1d',
                  'annualized_vol', 'max_drawdown_window',
                  'suggested_stop_loss', 'suggested_take_profit'):
            self.assertIn(k, m, f'missing {k}')

    def test_compute_risk_insufficient(self):
        from backend.services.single_stock_analysis import _compute_risk_metrics
        df = self._fake_df(n=10)
        m = _compute_risk_metrics(df, 50.0)
        self.assertEqual(m.get('error'), 'insufficient_bars')

    def test_compute_risk_stop_below_close(self):
        from backend.services.single_stock_analysis import _compute_risk_metrics
        df = self._fake_df()
        last = float(df['close'].iloc[-1])
        m = _compute_risk_metrics(df, last)
        self.assertLess(m['suggested_stop_loss'], last)
        self.assertGreater(m['suggested_take_profit'], last)


# ---------------------------------------------------------------------------
# 推荐决策
# ---------------------------------------------------------------------------

class TestRecommendation(unittest.TestCase):

    def test_strong_buy_score_triggers_buy(self):
        from backend.services.single_stock_analysis import _make_recommendation
        rec = _make_recommendation(
            combined_score=1.5, dominant='BUY',
            regime={'regime': 'BULL', 'signal_threshold_multiplier': 1.0,
                    'allow_new_buys': True},
            fundamentals={'roe_ttm': 15.0, 'revenue_yoy': 0.20},
            risk={'annualized_vol': 0.25},
        )
        self.assertEqual(rec['action'], 'BUY')
        self.assertGreater(rec['confidence'], 0.8)

    def test_bear_blocks_new_buys(self):
        from backend.services.single_stock_analysis import _make_recommendation
        rec = _make_recommendation(
            combined_score=2.0, dominant='BUY',
            regime={'regime': 'BEAR', 'signal_threshold_multiplier': 1.4,
                    'allow_new_buys': False},
            fundamentals={}, risk={},
        )
        self.assertEqual(rec['action'], 'HOLD')
        self.assertIn('BEAR', rec['reasoning'])

    def test_negative_roe_penalty(self):
        from backend.services.single_stock_analysis import _make_recommendation
        rec_with_neg_roe = _make_recommendation(
            combined_score=0.50, dominant='HOLD',
            regime=None,
            fundamentals={'roe_ttm': -5.0, 'revenue_yoy': -0.30},
            risk={},
        )
        # 0.5 - 0.15(roe) - 0.10(rev) = 0.25 < threshold 0.5 → HOLD
        self.assertEqual(rec_with_neg_roe['action'], 'HOLD')
        self.assertIn('ROE', rec_with_neg_roe['reasoning'])

    def test_high_vol_discount(self):
        from backend.services.single_stock_analysis import _make_recommendation
        # 高波动应折损置信度
        rec = _make_recommendation(
            combined_score=0.6, dominant='BUY', regime=None,
            fundamentals={}, risk={'annualized_vol': 0.80},
        )
        # 0.6 * 0.85 = 0.51 ≥ 0.5 → BUY 但 confidence 略低
        self.assertEqual(rec['action'], 'BUY')


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------

class TestSafeJsonExtract(unittest.TestCase):

    def test_pure_json(self):
        from backend.services.single_stock_analysis import _safe_json_extract
        self.assertEqual(_safe_json_extract('{"a": 1}'), {'a': 1})

    def test_fenced_codeblock(self):
        from backend.services.single_stock_analysis import _safe_json_extract
        text = '前置说明\n```json\n{"action": "BUY"}\n```\n后续'
        self.assertEqual(_safe_json_extract(text), {'action': 'BUY'})

    def test_text_with_braces(self):
        from backend.services.single_stock_analysis import _safe_json_extract
        text = '分析：\n{"x": 2, "y": [1,2,3]}\n以上为输出。'
        self.assertEqual(_safe_json_extract(text), {'x': 2, 'y': [1, 2, 3]})

    def test_invalid_returns_none(self):
        from backend.services.single_stock_analysis import _safe_json_extract
        self.assertIsNone(_safe_json_extract('not json'))
        self.assertIsNone(_safe_json_extract(''))


# ---------------------------------------------------------------------------
# analyze_a_share 端到端（mock 数据层 + 因子流水线）
# ---------------------------------------------------------------------------

class TestAnalyzeAShare(unittest.TestCase):

    def _fake_df(self, n=120, seed=7):
        rng = np.random.default_rng(seed)
        rets = rng.normal(0.0008, 0.012, n)
        prices = 30 * np.exp(np.cumsum(rets))
        return pd.DataFrame({
            'date': pd.date_range('2025-01-01', periods=n, freq='B'),
            'open': prices * 0.999,
            'high': prices * 1.006,
            'low':  prices * 0.994,
            'close': prices,
            'volume': rng.integers(1e6, 1e8, n),
        })

    def test_unknown_format_raises(self):
        from backend.services.single_stock_analysis import analyze_a_share, AnalysisRequest
        req = AnalysisRequest(symbol='HK:00700')
        with self.assertRaises(ValueError):
            analyze_a_share(req)

    def test_returns_minimal_when_data_empty(self):
        """数据层完全失败 → 返回最小化报告而非崩溃。"""
        from backend.services import single_stock_analysis as ssa
        from backend.services.single_stock_analysis import AnalysisRequest

        fake_dl = MagicMock()
        fake_dl.get_bars = MagicMock(return_value=pd.DataFrame())
        fake_dl.get_realtime = MagicMock(return_value=None)

        with patch('core.data_layer.get_data_layer', return_value=fake_dl):
            req = AnalysisRequest(symbol='603369.SH', include_regime=False)
            report = ssa.analyze_a_share(req)

        self.assertEqual(report.market, 'A')
        self.assertEqual(report.symbol, '603369.SH')
        self.assertIn('bars_unavailable', ' '.join(report.warnings))
        self.assertEqual(report.recommendation['action'], 'HOLD')

    def test_full_path_returns_structure(self):
        """完整路径：mock 数据层 + 因子，验证报告字段齐全。"""
        from backend.services import single_stock_analysis as ssa
        from backend.services.single_stock_analysis import AnalysisRequest

        df = self._fake_df()
        fake_dl = MagicMock()
        fake_dl.get_bars = MagicMock(return_value=df)
        fake_dl.get_realtime = MagicMock(return_value=None)

        # FundamentalDataManager 返回空 DataFrame（避免真调 akshare）
        fake_fm = MagicMock()
        fake_fm.get_fundamentals = MagicMock(return_value=pd.DataFrame())

        # Regime mock
        fake_regime = MagicMock()
        fake_regime.regime = 'BULL'
        fake_regime.date_str = '2026-05-09'
        fake_regime.close = 3500.0
        fake_regime.ma20 = 3450.0
        fake_regime.ma60 = 3300.0
        fake_regime.atr_ratio = 0.012
        fake_regime.atr_threshold_dynamic = 0.015
        fake_regime.ma60_slope = 0.001
        fake_regime.position_cap = 1.0
        fake_regime.signal_threshold_multiplier = 1.0
        fake_regime.allow_new_buys = True
        fake_regime.should_reduce_positions = False
        fake_regime.reason = 'close>MA20>MA60'
        fake_regime.source = 'akshare'

        with patch('core.data_layer.get_data_layer', return_value=fake_dl), \
             patch('core.fundamental_data.FundamentalDataManager',
                   return_value=fake_fm), \
             patch('core.regime.get_regime', return_value=fake_regime):
            req = AnalysisRequest(symbol='603369.SH', include_regime=True)
            report = ssa.analyze_a_share(req)

        d = report.to_dict()
        # 必有字段
        for k in ('symbol', 'market', 'as_of', 'snapshot',
                  'factor_pipeline', 'risk', 'recommendation', 'data_quality'):
            self.assertIn(k, d)

        self.assertEqual(d['market'], 'A')
        self.assertEqual(d['regime']['regime'], 'BULL')
        # 风险至少包含 atr_14
        self.assertIn('atr_14', d['risk'])
        # 推荐 action 是合法值
        self.assertIn(d['recommendation']['action'], ('BUY', 'SELL', 'HOLD'))


# ---------------------------------------------------------------------------
# analyze_hk_share
# ---------------------------------------------------------------------------

class TestAnalyzeHkShare(unittest.TestCase):

    def _make_snap(self):
        from core.hk_data_source import HKStockSnapshot
        from datetime import datetime
        return HKStockSnapshot(
            timestamp=datetime.now(), symbol='hk00700', name='腾讯控股',
            last=350.0, open=348.0, high=352.0, low=346.0, prev_close=347.0,
            change=3.0, change_pct=0.86, volume=1_000_000, amount=350_000_000.0,
            high_52w=400.0, low_52w=280.0, mkt_cap=3_300_000_000_000.0,
        )

    def test_normalizes_symbol(self):
        from backend.services import single_stock_analysis as ssa
        from backend.services.single_stock_analysis import AnalysisRequest

        # mock 网络访问
        fake_ds = MagicMock()
        fake_ds.fetch_latest = MagicMock(return_value=self._make_snap())
        fake_ds.fetch_history = MagicMock(return_value=pd.DataFrame())

        with patch('core.hk_data_source.HKStockDataSource', return_value=fake_ds):
            req = AnalysisRequest(symbol='HK:00700', include_regime=True)
            report = ssa.analyze_hk_share(req)

        self.assertEqual(report.symbol, 'hk00700')
        self.assertEqual(report.market, 'HK')
        self.assertIn('腾讯', report.snapshot['name'])
        # 港股 regime 返回 N/A 而非错误
        self.assertEqual(report.regime['regime'], 'N/A')

    def test_no_history_falls_back_to_52w_risk(self):
        from backend.services import single_stock_analysis as ssa
        from backend.services.single_stock_analysis import AnalysisRequest

        snap = self._make_snap()
        fake_ds = MagicMock()
        fake_ds.fetch_latest = MagicMock(return_value=snap)
        fake_ds.fetch_history = MagicMock(return_value=pd.DataFrame())

        with patch('core.hk_data_source.HKStockDataSource', return_value=fake_ds):
            req = AnalysisRequest(symbol='HK:00700')
            report = ssa.analyze_hk_share(req)

        self.assertEqual(report.risk.get('note'), 'estimated_from_52w_range')
        self.assertGreater(report.risk['range_52w_pct'], 0)


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------

class TestEndpoints(unittest.TestCase):

    def setUp(self):
        os.environ.pop('TRADING_API_KEY', None)
        os.environ.pop('TRADING_RL_PER_MIN', None)
        os.environ.pop('TRADING_API_REQUIRE_LOCALHOST', None)
        import backend.api as api_mod
        from services.portfolio import PortfolioService
        import tempfile
        self.tmp = tempfile.mkdtemp()
        api_mod._svc = PortfolioService(db_path=os.path.join(self.tmp, 'p.db'))
        api_mod._GLOBAL_RATE_LIMIT.clear()
        api_mod.app.config['TESTING'] = True
        self.client = api_mod.app.test_client()

    def tearDown(self):
        import backend.api as api_mod
        api_mod._svc = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_endpoint_rejects_hk_symbol(self):
        r = self.client.post('/analysis/stock/a',
                              data=json.dumps({'symbol': 'HK:00700'}),
                              content_type='application/json')
        self.assertEqual(r.status_code, 422)
        body = r.get_json()
        self.assertIn('A 股', body['error'])

    def test_hk_endpoint_rejects_a_symbol(self):
        r = self.client.post('/analysis/stock/hk',
                              data=json.dumps({'symbol': '603369.SH'}),
                              content_type='application/json')
        self.assertEqual(r.status_code, 422)

    def test_a_endpoint_missing_symbol_returns_422(self):
        r = self.client.post('/analysis/stock/a',
                              data=json.dumps({}),
                              content_type='application/json')
        self.assertEqual(r.status_code, 422)
        self.assertIn('symbol', r.get_json()['error'])

    def test_a_endpoint_returns_structured_report(self):
        """端到端：mock 全部下游，验证端点返回 200 + 报告结构。"""
        df = pd.DataFrame({
            'date': pd.date_range('2025-01-01', periods=120, freq='B'),
            'open':  np.linspace(30, 35, 120),
            'high':  np.linspace(30, 35, 120) * 1.01,
            'low':   np.linspace(30, 35, 120) * 0.99,
            'close': np.linspace(30, 35, 120),
            'volume': np.full(120, 1_000_000),
        })

        fake_dl = MagicMock()
        fake_dl.get_bars = MagicMock(return_value=df)
        fake_dl.get_realtime = MagicMock(return_value=None)

        fake_fm = MagicMock()
        fake_fm.get_fundamentals = MagicMock(return_value=pd.DataFrame())

        with patch('core.data_layer.get_data_layer', return_value=fake_dl), \
             patch('core.fundamental_data.FundamentalDataManager',
                   return_value=fake_fm):
            r = self.client.post('/analysis/stock/a',
                                  data=json.dumps({
                                      'symbol': '603369.SH',
                                      'include_regime': False,
                                  }),
                                  content_type='application/json')

        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['market'], 'A')
        self.assertEqual(body['symbol'], '603369.SH')
        self.assertIn('snapshot', body)
        self.assertIn('factor_pipeline', body)
        self.assertIn('risk', body)
        self.assertIn('recommendation', body)


if __name__ == '__main__':
    unittest.main()
