"""
Tests for dynamic_selector.py
Run with: python -m pytest tests/ -v
"""

import pytest
import sys
import os

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from dynamic_selector import (
    DynamicStockSelector,
    safe_float,
    safe_int,
    _read_file_cache,
    _write_file_cache,
    SECTOR_NEWS_KEYWORDS,
)


class TestSafeFloat:
    def test_valid_number(self):
        assert safe_float('1.23') == 1.23
        assert safe_float('0') == 0.0
        assert safe_float('-3.5') == -3.5

    def test_invalid_returns_default(self):
        assert safe_float('abc') == 0.0
        assert safe_float('') == 0.0
        assert safe_float(None) == 0.0

    def test_custom_default(self):
        assert safe_float('abc', -1.0) == -1.0


class TestSafeInt:
    def test_valid_integer(self):
        assert safe_int('42') == 42
        assert safe_int('0') == 0
        assert safe_int('-7') == -7

    def test_invalid_returns_default(self):
        assert safe_int('abc') == 0
        assert safe_int('') == 0
        assert safe_int(None) == 0


class TestSectorKeywords:
    def test_keywords_not_empty(self):
        assert len(SECTOR_NEWS_KEYWORDS) > 0

    def test_each_sector_has_keywords(self):
        for sector, keywords in SECTOR_NEWS_KEYWORDS.items():
            assert len(keywords) > 0, f"{sector} has no keywords"
            assert all(isinstance(kw, str) for kw in keywords)


class TestDynamicSelector:
    def test_weight_sum(self):
        """Weights should sum to 1.0"""
        selector = DynamicStockSelector()
        total = (
            selector.WEIGHT_NEWS
            + selector.WEIGHT_SECTOR
            + selector.WEIGHT_FLOW
            + selector.WEIGHT_TECH
            + selector.WEIGHT_CONSISTENCY
        )
        assert abs(total - 1.0) < 0.001, f"Weights sum to {total}, expected 1.0"

    def test_default_weights_known(self):
        """Verify known weight values"""
        selector = DynamicStockSelector()
        assert selector.WEIGHT_NEWS == 0.15
        assert selector.WEIGHT_SECTOR == 0.35
        assert selector.WEIGHT_FLOW == 0.25
        assert selector.WEIGHT_TECH == 0.15
        assert selector.WEIGHT_CONSISTENCY == 0.10

    def test_cache_initialized_empty(self):
        """Cache dicts should be initialized as empty"""
        selector = DynamicStockSelector()
        assert isinstance(selector.news_cache, list)
        assert isinstance(selector.sectors_raw, list)
        assert isinstance(selector._constituent_cache, dict)

    def test_consistency_score_bounds(self):
        """Score returns 0-100 even with no data"""
        selector = DynamicStockSelector()
        score = selector.calc_consistency_score_for_bk('nonexistent_bk')
        assert 0 <= score <= 100

    def test_tech_score_bounds(self):
        """Tech score returns 0-100 even with no data"""
        selector = DynamicStockSelector()
        score = selector.calc_tech_score_for_bk('nonexistent_bk')
        assert 0 <= score <= 100

    # ── W3-4: 板块资金流动量评分 ──────────────────────────────────────────

    def test_flow_momentum_score_no_history(self):
        """无 SectorFlowStore 数据 → 返回 0。"""
        selector = DynamicStockSelector()
        score = selector.calc_flow_momentum_score(
            'BK_NO_HIST', today_net_flow=5e9,
        )
        assert score == 0.0

    def test_flow_momentum_score_high_inflow(self):
        """今日流入显著高于历史均值 → 正值。"""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from core.factors.sector import SectorFlowStore

        hist = pd.Series([0.0, 1e8, 0.5e8, -0.5e8, 0.2e8],
                         index=pd.bdate_range('2024-01-01', periods=5))
        with patch.object(SectorFlowStore, 'series_for', return_value=hist):
            selector = DynamicStockSelector()
            score = selector.calc_flow_momentum_score('BK1', today_net_flow=1e10)
        assert score > 0.5

    def test_flow_momentum_score_high_outflow(self):
        """今日流出显著高于历史均值 → 负值。"""
        import pandas as pd
        from unittest.mock import patch
        from core.factors.sector import SectorFlowStore

        hist = pd.Series([2e8, 1e8, 1.5e8, 1.2e8, 1.8e8],
                         index=pd.bdate_range('2024-01-01', periods=5))
        with patch.object(SectorFlowStore, 'series_for', return_value=hist):
            selector = DynamicStockSelector()
            score = selector.calc_flow_momentum_score('BK1', today_net_flow=-5e9)
        assert score < -0.5

    def test_flow_momentum_score_zero_std(self):
        """历史无波动(std=0)→ 返回 0,不抛除零异常。"""
        import pandas as pd
        from unittest.mock import patch
        from core.factors.sector import SectorFlowStore

        hist = pd.Series([1e8] * 5,
                         index=pd.bdate_range('2024-01-01', periods=5))
        with patch.object(SectorFlowStore, 'series_for', return_value=hist):
            selector = DynamicStockSelector()
            score = selector.calc_flow_momentum_score('BK1', today_net_flow=5e9)
        assert score == 0.0
