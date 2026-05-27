# -*- coding: utf-8 -*-
"""
refresh_prices 单元测试 — 验证腾讯行情 v_ 前缀解析 bug（fix/refresh-prices-tencent-prefix）。

Bug: 腾讯 qt.gtimg.cn 返回的 key 带 v_ 前缀（如 v_hk00700），但 qt_to_sym
字典的 key 不含 v_ 前缀（如 hk00700），导致 HK 股票价格永远无法匹配，
refresh_prices 对港股失效。

Fix: qt_to_sym 同时注册带 v_ 和不带 v_ 的 key；解析时也对 qt_prefix 做 lstrip('v_')。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pytest

from services.portfolio import PortfolioService


def _make_tencent_response(*symbols_prices) -> str:
    """
    构造腾讯 qt.gtimg.cn 格式的响应。

    symbols_prices: list of (qt_symbol, price)
    例如: ('hk00700', 435.2), ('sh600519', 1234.5)
    """
    parts = []
    for qt_sym, price in symbols_prices:
        f = ['1', 'Name', qt_sym.lstrip('v_')] + ['0'] * 30
        f[3] = str(price)
        f[31] = '0.0'
        parts.append(f'v_{qt_sym}="{chr(126).join(f)}"')
    return ';'.join(parts) + ';'


class TestRefreshPricesTencentVPrefix:
    """验证 v_ 前缀解析对所有品种类型有效。"""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """每个测试用独立 temp DB，通过 patch services.portfolio.get_db 实现隔离。"""
        import os, tempfile, shutil, sqlite3
        self._test_dir = tempfile.mkdtemp(prefix='refresh_prices_test_')
        self.db_path = os.path.join(self._test_dir, 'test.db')
        # Patch module-level get_db so PortfolioService uses our temp path
        import services.portfolio as pf_mod
        _orig_get_db = pf_mod.get_db
        def _patched_get_db():
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
            return conn
        pf_mod.get_db = _patched_get_db
        self.svc = PortfolioService(db_path=self.db_path)
        yield
        pf_mod.get_db = _orig_get_db
        shutil.rmtree(self._test_dir, ignore_errors=True)

    def _mock_urlopen(self, raw_response: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_response
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=mock_resp)
        m.__exit__ = MagicMock(return_value=False)
        return MagicMock(return_value=m)

    # ── 港股（核心 bug 场景）───────────────────────────────────────────────

    def test_hk_stock_price_updated_with_v_prefix(self):
        """港股 0700.HK：腾讯返回 v_hk00700，修复后能正确匹配并刷新价格。"""
        svc = self.svc
        svc.upsert_position('0700.HK', shares=10000, entry_price=0.53, latest_price=0.53)
        fake = _make_tencent_response(('hk00700', 435.2))
        with patch('urllib.request.urlopen', self._mock_urlopen(fake.encode('gbk'))):
            result = svc.refresh_prices()
        assert '0700.HK' in result, f'0700.HK not in {result}'
        assert abs(result['0700.HK'] - 435.2) < 0.01
        pos = svc.get_position('0700.HK')
        assert abs(pos['latest_price'] - 435.2) < 0.01

    def test_hk_stock_price_updated_multiple_hk(self):
        """多只港股同时刷新：0700.HK + 1810.HK 都能正确匹配。"""
        svc = self.svc
        svc.upsert_position('0700.HK', shares=10000, entry_price=0.53, latest_price=0.53)
        svc.upsert_position('1810.HK', shares=5000, entry_price=0.35, latest_price=0.35)
        fake = _make_tencent_response(('hk00700', 435.2), ('hk01810', 12.8))
        with patch('urllib.request.urlopen', self._mock_urlopen(fake.encode('gbk'))):
            result = svc.refresh_prices()
        assert '0700.HK' in result
        assert '1810.HK' in result
        assert abs(result['0700.HK'] - 435.2) < 0.01
        assert abs(result['1810.HK'] - 12.8) < 0.01

    def test_hk_stock_partial_update(self):
        """部分港股有数据，部分无数据：无数据的保持原价不被覆盖。"""
        svc = self.svc
        svc.upsert_position('0700.HK', shares=10000, entry_price=0.53, latest_price=430.0)
        svc.upsert_position('1810.HK', shares=5000, entry_price=0.35, latest_price=10.0)
        fake = _make_tencent_response(('hk00700', 435.2))  # 只有 0700
        with patch('urllib.request.urlopen', self._mock_urlopen(fake.encode('gbk'))):
            result = svc.refresh_prices()
        assert '0700.HK' in result
        assert '1810.HK' not in result
        assert abs(svc.get_position('0700.HK')['latest_price'] - 435.2) < 0.01
        assert abs(svc.get_position('1810.HK')['latest_price'] - 10.0) < 0.01

    # ── A 股（回归测试）───────────────────────────────────────────────────

    def test_a_share_price_updated_with_v_prefix(self):
        """A 股 sh600519：验证 v_sh600519 格式也正确匹配。"""
        svc = self.svc
        svc.upsert_position('SH600519', shares=1000, entry_price=1200.0, latest_price=1200.0)
        fake = _make_tencent_response(('sh600519', 1234.5))
        with patch('urllib.request.urlopen', self._mock_urlopen(fake.encode('gbk'))):
            result = svc.refresh_prices()
        assert 'SH600519' in result
        assert abs(result['SH600519'] - 1234.5) < 0.01

    def test_mixed_a_hk_refresh(self):
        """A 股 + 港股混合：两类都能正确刷新。"""
        svc = self.svc
        svc.upsert_position('SH600519', shares=1000, entry_price=1200.0, latest_price=1200.0)
        svc.upsert_position('0700.HK', shares=10000, entry_price=0.53, latest_price=0.53)
        fake = _make_tencent_response(('sh600519', 1234.5), ('hk00700', 435.2))
        with patch('urllib.request.urlopen', self._mock_urlopen(fake.encode('gbk'))):
            result = svc.refresh_prices()
        assert abs(result.get('SH600519', 0) - 1234.5) < 0.01
        assert abs(result.get('0700.HK', 0) - 435.2) < 0.01

    # ── 边界情况 ─────────────────────────────────────────────────────────

    def test_no_positions_returns_empty(self):
        """无持仓时 refresh_prices 返回空字典。"""
        svc = self.svc
        assert svc.get_positions() == [], f"expected empty, got {svc.get_positions()}"
        result = svc.refresh_prices()
        assert result == {}

    def test_empty_response_does_not_crash(self):
        """腾讯返回空字符串时应安全处理，不抛异常。"""
        svc = self.svc
        svc.upsert_position('0700.HK', shares=10000, entry_price=0.53, latest_price=430.0)
        with patch('urllib.request.urlopen', self._mock_urlopen(b'')):
            result = svc.refresh_prices()
        assert result == {}
        assert svc.get_position('0700.HK')['latest_price'] == 430.0

    def test_invalid_price_field_ignored(self):
        """price 为 0 或 '-' 时应被忽略，不更新。"""
        svc = self.svc
        svc.upsert_position('0700.HK', shares=10000, entry_price=0.53, latest_price=430.0)
        fake = 'v_hk00700="1~腾讯~00700~0.00~0.00~0.00~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~~~~~~~~~~~~~~~~~~~~~~~";'
        with patch('urllib.request.urlopen', self._mock_urlopen(fake.encode('gbk'))):
            result = svc.refresh_prices()
        assert '0700.HK' not in result
        assert svc.get_position('0700.HK')['latest_price'] == 430.0
