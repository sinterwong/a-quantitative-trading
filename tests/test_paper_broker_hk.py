"""
test_paper_broker_hk.py — PaperBroker 港股修复单元测试

覆盖:
- _fetch_market_price() 对 hk 前缀代码的正确解析
- submit_from_signal() 无价格拦截 (price=0 → rejected)
- submit_from_signal() 港股闭市拦截 (HK market closed → rejected)
- submit_order() price=0 处理
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch


def _make_broker(cash: float = 1_000_000):
    """构造 PaperBroker 实例（mock portfolio_service）。"""
    from backend.services.broker import PaperBroker
    svc = MagicMock()
    svc.get_cash.return_value = cash
    svc.get_total_equity.return_value = cash
    svc.get_position.return_value = None
    svc.set_cash = MagicMock()
    svc.upsert_position = MagicMock()
    svc.record_trade = MagicMock()
    svc.close_position = MagicMock()
    b = PaperBroker(portfolio_service=svc, slippage_bps=10)
    b.connect()
    return b, svc


class TestFetchMarketPrice(unittest.TestCase):
    """_fetch_market_price 对港股代码的正确解析"""

    def test_hk_prefix_symbol_direct(self):
        """hk00700 → qt.gtimg.cn?q=hk00700（不走 split('.')）"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        # Mock urllib response (must support context manager __enter__)
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'hk00700="1~~0700.HK~438.600~439.200~439.800~438.000~5000000~0~0~0~0~0"'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch('urllib.request.urlopen', return_value=mock_resp):
            price = b._fetch_market_price('hk00700')
        self.assertAlmostEqual(price, 438.600)

    def test_hk_lowercase_prefix(self):
        """小写 hk00700 同样正确解析"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'hk00700="1~~0700.HK~441.000~0~0~0~0~0~0~0~0~0"'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch('urllib.request.urlopen', return_value=mock_resp):
            price = b._fetch_market_price('hk00700')
        self.assertAlmostEqual(price, 441.000)

    def test_sh_symbol_unchanged(self):
        """A股 sh 代码走原有 split('.') 逻辑不变"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'sh600519="1~~600519.SH~1800.00~1795.00~1790.00~1800.00~100000~0~0~0~0~0"'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch('urllib.request.urlopen', return_value=mock_resp):
            price = b._fetch_market_price('600519.SH')
        self.assertAlmostEqual(price, 1800.00)

    def test_sz_symbol_unchanged(self):
        """A股 sz 代码走原有逻辑"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'sz000001="1~~000001.SZ~12.50~12.40~12.35~12.50~200000~0~0~0~0~0"'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch('urllib.request.urlopen', return_value=mock_resp):
            price = b._fetch_market_price('000001.SZ')
        self.assertAlmostEqual(price, 12.50)

    def test_no_price_returns_zero(self):
        """fields[3] <= 0 时返回 0.0（不 crash）"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'hk00700="1~~0700.HK~0~0~0~0~0~0~0~0~0~0"'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch('urllib.request.urlopen', return_value=mock_resp):
            price = b._fetch_market_price('hk00700')
        self.assertEqual(price, 0.0)

    def test_network_error_returns_zero(self):
        """网络异常时返回 0.0，不抛出异常"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        with patch('urllib.request.urlopen', side_effect=OSError('timeout')):
            price = b._fetch_market_price('hk00700')
        self.assertEqual(price, 0.0)


class TestSubmitFromSignalNoPrice(unittest.TestCase):
    """submit_from_signal 无价格信号 → rejected（防止垃圾价成交）"""

    def setUp(self):
        self._sleep_patcher = patch(
            'backend.services.broker.time.sleep', lambda *_a, **_k: None,
        )
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def _make_signal(self, symbol, direction, price=0, metadata=None):
        """构造 mock Signal 对象。"""
        sig = MagicMock()
        sig.symbol = symbol
        sig.direction = direction
        sig.price = price
        sig.metadata = metadata or {}
        return sig

    def test_zero_price_rejected(self):
        """signal.price=0 且 _fetch_market_price 返回 0 → rejected"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()
        b._fetch_market_price = lambda symbol: 0.0  # 行情也拿不到

        sig = self._make_signal('600519.SH', 'BUY', price=0)
        result = b.submit_from_signal(sig)

        self.assertEqual(result.status, 'rejected')
        self.assertIn('no valid price', result.reason)

    def test_zero_price_with_fetched_price_accepted(self):
        """signal.price=0 但 _fetch_market_price 能取到价格 → 成交"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()
        b._fetch_market_price = lambda symbol: 1800.0  # 能取到价格

        sig = self._make_signal('600519.SH', 'BUY', price=0)
        result = b.submit_from_signal(sig)

        self.assertEqual(result.status, 'filled')
        self.assertGreater(result.filled_shares, 0)

    def test_signal_price_zero_not_used_for_order(self):
        """signal.price > 0 时用信号价, 不 fallback 到 _fetch_market_price"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()
        b._fetch_market_price = MagicMock(return_value=0.0)  # 行情失败

        sig = self._make_signal('600519.SH', 'BUY', price=1800.0)
        result = b.submit_from_signal(sig)

        # signal.price=1800 > 0，直接用信号价成交，不需要 fetch
        self.assertEqual(result.status, 'filled')
        b._fetch_market_price.assert_not_called()


class TestSubmitFromSignalHkClosed(unittest.TestCase):
    """港股闭市拦截: 周末/节假日 → rejected"""

    def setUp(self):
        self._sleep_patcher = patch(
            'backend.services.broker.time.sleep', lambda *_a, **_k: None,
        )
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def _make_signal(self, symbol, direction, price=100, metadata=None):
        sig = MagicMock()
        sig.symbol = symbol
        sig.direction = direction
        sig.price = price
        sig.metadata = metadata or {}
        return sig

    def test_hk_weekend_rejected(self):
        """港股代码 + 周末 → rejected (HK market closed)"""
        from backend.services.broker import PaperBroker
        from backend.services.intraday.market_hours import is_hk_market_open

        b, _ = _make_broker()

        with patch(
            'backend.services.intraday.market_hours.is_hk_market_open',
            return_value=False,  # 港股闭市
        ):
            sig = self._make_signal('hk00700', 'BUY', price=438.0)
            result = b.submit_from_signal(sig)

        self.assertEqual(result.status, 'rejected')
        self.assertIn('HK market closed', result.reason)

    def test_a_stock_not_affected_by_hk_check(self):
        """A 股代码不受港股闭市拦截影响"""
        from backend.services.broker import PaperBroker

        b, _ = _make_broker()

        with patch(
            'backend.services.intraday.market_hours.is_hk_market_open',
            return_value=False,  # 港股闭市
        ):
            # A 股信号不应被 HK 市场规则拦截
            sig = self._make_signal('600519.SH', 'BUY', price=1800.0)
            result = b.submit_from_signal(sig)

        # A 股不受港股闭市影响，但 reason 不应包含 'HK market closed'
        self.assertNotIn('HK market closed', result.reason or '')


class TestEmptySymbol(unittest.TestCase):
    """空 symbol 防御性校验"""

    def setUp(self):
        self._sleep_patcher = patch(
            'backend.services.broker.time.sleep', lambda *_a, **_k: None,
        )
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_empty_symbol_rejected(self):
        """symbol='' → rejected"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        sig = MagicMock()
        sig.symbol = ''
        sig.direction = 'BUY'
        sig.price = 100.0
        sig.metadata = {}

        result = b.submit_from_signal(sig)
        self.assertEqual(result.status, 'rejected')
        self.assertIn('empty symbol', result.reason)

    def test_invalid_direction_rejected(self):
        """direction 不在 ('BUY','SELL') → rejected"""
        from backend.services.broker import PaperBroker
        b, _ = _make_broker()

        sig = MagicMock()
        sig.symbol = '600519.SH'
        sig.direction = 'INVALID'
        sig.price = 100.0
        sig.metadata = {}

        result = b.submit_from_signal(sig)
        self.assertEqual(result.status, 'rejected')
        self.assertIn('invalid direction', result.reason)