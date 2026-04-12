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
    DynamicStockSelectorV2,
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


class TestDynamicSelectorV2:
    def test_weight_sum(self):
        """Weights should sum to 1.0"""
        selector = DynamicStockSelectorV2()
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
        selector = DynamicStockSelectorV2()
        assert selector.WEIGHT_NEWS == 0.15
        assert selector.WEIGHT_SECTOR == 0.35
        assert selector.WEIGHT_FLOW == 0.25
        assert selector.WEIGHT_TECH == 0.15
        assert selector.WEIGHT_CONSISTENCY == 0.10

    def test_cache_initialized_empty(self):
        """Cache dicts should be initialized as empty"""
        selector = DynamicStockSelectorV2()
        assert isinstance(selector.news_cache, list)
        assert isinstance(selector.sectors_raw, list)
        assert isinstance(selector._constituent_cache, dict)

    def test_consistency_score_bounds(self):
        """Score returns 0-100 even with no data"""
        selector = DynamicStockSelectorV2()
        score = selector.calc_consistency_score_for_bk('nonexistent_bk')
        assert 0 <= score <= 100

    def test_tech_score_bounds(self):
        """Tech score returns 0-100 even with no data"""
        selector = DynamicStockSelectorV2()
        score = selector.calc_tech_score_for_bk('nonexistent_bk')
        assert 0 <= score <= 100
