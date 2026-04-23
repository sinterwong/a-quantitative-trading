"""tests/test_broker_base.py — BrokerBase / SimulatedBroker 单元测试"""

from __future__ import annotations

import unittest
from datetime import datetime

from core.brokers.base import AccountInfo, BrokerBase, MarketType, QuoteData
from core.brokers.simulated import SimConfig, SimulatedBroker
from core.oms import Fill, Order, Position


class TestSimulatedBrokerLifecycle(unittest.TestCase):

    def setUp(self):
        self.broker = SimulatedBroker(SimConfig(
            initial_cash=500_000,
            price_source='manual',
            enforce_lot=True,
            enforce_market_hours=False,
        ))
        self.broker.connect()
        self.broker.set_quote('600519.SH', 1800.0)

    def test_connect_returns_true(self):
        b = SimulatedBroker()
        self.assertTrue(b.connect())
        self.assertTrue(b.is_connected())

    def test_disconnect(self):
        self.broker.disconnect()
        self.assertFalse(self.broker.is_connected())

    def test_get_account_initial(self):
        acc = self.broker.get_account()
        self.assertIsInstance(acc, AccountInfo)
        self.assertAlmostEqual(acc.cash, 500_000)
        self.assertEqual(acc.broker_name, 'SimulatedBroker')

    def test_get_cash(self):
        self.assertAlmostEqual(self.broker.get_cash(), 500_000)

    def test_get_quote_manual(self):
        q = self.broker.get_quote('600519.SH')
        self.assertIsInstance(q, QuoteData)
        self.assertAlmostEqual(q.last, 1800.0)
        self.assertTrue(q.is_tradable)

    def test_get_quote_unknown_symbol(self):
        q = self.broker.get_quote('UNKNOWN.SH')
        self.assertEqual(q.last, 0.0)
        self.assertFalse(q.is_tradable)

    def test_is_market_open_no_hours(self):
        # enforce_market_hours=False → always open
        self.assertTrue(self.broker.is_market_open(MarketType.A_SHARE))

    def test_supported_markets(self):
        markets = self.broker.supported_markets()
        self.assertIn(MarketType.A_SHARE, markets)

    def test_repr(self):
        r = repr(self.broker)
        self.assertIn('SimulatedBroker', r)
        self.assertIn('connected', r)


class TestSimulatedBrokerOrders(unittest.TestCase):

    def setUp(self):
        self.broker = SimulatedBroker(SimConfig(
            initial_cash=1_000_000,
            price_source='manual',
            enforce_lot=True,
            slippage_bps=5.0,
            commission_rate=0.0003,
            min_commission=5.0,
            stamp_tax_rate=0.001,
        ))
        self.broker.connect()
        self.broker.set_quote('600519.SH', 1800.0)

    def _buy_order(self, shares=100):
        return Order(symbol='600519.SH', direction='BUY',
                     order_type='MARKET', shares=shares)

    def _sell_order(self, shares=100):
        return Order(symbol='600519.SH', direction='SELL',
                     order_type='MARKET', shares=shares)

    def test_buy_fill_success(self):
        fill = self.broker.submit_order(self._buy_order())
        self.assertEqual(fill.shares, 100)
        self.assertGreater(fill.price, 0)
        self.assertGreater(fill.commission, 0)

    def test_buy_slippage_direction(self):
        """买入成交价应 > 报价。"""
        fill = self.broker.submit_order(self._buy_order())
        self.assertGreater(fill.price, 1800.0)

    def test_sell_slippage_direction(self):
        """卖出成交价应 < 报价。"""
        self.broker.inject_position('600519.SH', 200, 1700.0)
        fill = self.broker.submit_order(self._sell_order())
        self.assertEqual(fill.shares, 100)
        self.assertLess(fill.price, 1800.0)

    def test_cash_decreases_after_buy(self):
        before = self.broker.get_cash()
        fill = self.broker.submit_order(self._buy_order())
        after = self.broker.get_cash()
        expected_cost = fill.price * 100 + fill.commission
        self.assertAlmostEqual(before - after, expected_cost, places=0)

    def test_cash_increases_after_sell(self):
        self.broker.inject_position('600519.SH', 200, 1700.0)
        before = self.broker.get_cash()
        fill = self.broker.submit_order(self._sell_order())
        after = self.broker.get_cash()
        # 卖出收入 - 佣金 - 印花税
        self.assertGreater(after, before)

    def test_position_created_after_buy(self):
        self.broker.submit_order(self._buy_order())
        positions = self.broker.get_positions()
        syms = [p.symbol for p in positions]
        self.assertIn('600519.SH', syms)

    def test_position_removed_after_full_sell(self):
        self.broker.inject_position('600519.SH', 100, 1700.0)
        self.broker.submit_order(self._sell_order(shares=100))
        positions = self.broker.get_positions()
        syms = [p.symbol for p in positions]
        self.assertNotIn('600519.SH', syms)

    def test_reject_non_lot(self):
        order = Order(symbol='600519.SH', direction='BUY',
                      order_type='MARKET', shares=50)  # 非整手
        fill = self.broker.submit_order(order)
        self.assertEqual(fill.shares, 0)

    def test_reject_insufficient_cash(self):
        self.broker._cash = 100.0  # 强制设置极低现金
        fill = self.broker.submit_order(self._buy_order())
        self.assertEqual(fill.shares, 0)

    def test_reject_insufficient_position(self):
        # 无持仓时卖出
        fill = self.broker.submit_order(self._sell_order())
        self.assertEqual(fill.shares, 0)

    def test_cancel_pending(self):
        order = Order(symbol='600519.SH', direction='BUY',
                      order_type='MARKET', shares=100)
        order.status = 'PENDING'
        self.broker._orders[order.order_id] = order
        result = self.broker.cancel_order(order.order_id)
        self.assertTrue(result)
        self.assertEqual(order.status, 'CANCELLED')

    def test_cancel_nonexistent(self):
        result = self.broker.cancel_order('nonexistent')
        self.assertFalse(result)

    def test_get_orders_all(self):
        self.broker.submit_order(self._buy_order())
        orders = self.broker.get_orders()
        self.assertGreater(len(orders), 0)

    def test_get_orders_by_status(self):
        self.broker.submit_order(self._buy_order())
        filled = self.broker.get_orders(status='FILLED')
        self.assertTrue(all(o.status == 'FILLED' for o in filled))

    def test_get_fills(self):
        self.broker.submit_order(self._buy_order())
        fills = self.broker.get_fills()
        self.assertGreater(len(fills), 0)
        self.assertIsInstance(fills[0], Fill)

    def test_stamp_tax_on_sell(self):
        """印花税只在卖出时扣除，买入不扣。"""
        self.broker.inject_position('600519.SH', 200, 1700.0)
        cash_before = self.broker.get_cash()
        fill = self.broker.submit_order(self._sell_order())
        cash_after = self.broker.get_cash()
        # 实际收入 = 成交价 * 手数 - 佣金 - 印花税
        stamp_tax = fill.price * 100 * 0.001
        commission = fill.commission
        expected_income = fill.price * 100 - commission - stamp_tax
        self.assertAlmostEqual(cash_after - cash_before, expected_income, places=0)


class TestSimulatedBrokerReset(unittest.TestCase):

    def test_reset_clears_state(self):
        broker = SimulatedBroker(SimConfig(
            initial_cash=100_000, price_source='manual'
        ))
        broker.connect()
        broker.set_quote('600519.SH', 1800.0)
        broker.submit_order(Order(symbol='600519.SH', direction='BUY',
                                  order_type='MARKET', shares=100))
        self.assertGreater(len(broker.get_orders()), 0)

        broker.reset()
        self.assertEqual(len(broker.get_orders()), 0)
        self.assertEqual(len(broker.get_positions()), 0)
        self.assertAlmostEqual(broker.get_cash(), 100_000)

    def test_reset_custom_cash(self):
        broker = SimulatedBroker()
        broker.connect()
        broker.reset(initial_cash=500_000)
        self.assertAlmostEqual(broker.get_cash(), 500_000)

    def test_inject_position(self):
        broker = SimulatedBroker(SimConfig(price_source='manual'))
        broker.connect()
        broker.set_quote('600519.SH', 1800.0)
        broker.inject_position('600519.SH', 200, 1700.0)
        positions = broker.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].shares, 200)
        self.assertAlmostEqual(positions[0].avg_price, 1700.0)

    def test_snapshot(self):
        broker = SimulatedBroker(SimConfig(
            initial_cash=200_000, price_source='manual'
        ))
        broker.connect()
        snap = broker.snapshot
        self.assertEqual(snap['cash'], 200_000)
        self.assertEqual(snap['n_fills'], 0)


class TestBrokerBaseCompatibility(unittest.TestCase):
    """验证 BrokerBase 与旧 BrokerAdapter 接口兼容。"""

    def test_send_delegates_to_submit_order(self):
        """send() 应委托给 submit_order()，不应崩溃。"""
        broker = SimulatedBroker(SimConfig(price_source='manual', initial_cash=500_000))
        broker.connect()
        broker.set_quote('000858.SZ', 100.0)
        order = Order(symbol='000858.SZ', direction='BUY',
                      order_type='MARKET', shares=100)
        fill = broker.send(order)
        self.assertIsInstance(fill, Fill)

    def test_cancel_delegates_to_cancel_order(self):
        broker = SimulatedBroker(SimConfig(price_source='manual'))
        broker.connect()
        order = Order(symbol='000858.SZ', direction='BUY',
                      order_type='MARKET', shares=100)
        order.status = 'PENDING'
        broker._orders[order.order_id] = order
        result = broker.cancel(order.order_id)
        self.assertTrue(result)

    def test_quote_returns_dict(self):
        broker = SimulatedBroker(SimConfig(price_source='manual'))
        broker.connect()
        broker.set_quote('000858.SZ', 150.0)
        q = broker.quote('000858.SZ')
        self.assertIsInstance(q, dict)
        self.assertIn('last', q)
        self.assertAlmostEqual(q['last'], 150.0)

    def test_is_instance_of_broker_adapter(self):
        from core.oms import BrokerAdapter
        broker = SimulatedBroker()
        self.assertIsInstance(broker, BrokerAdapter)
        self.assertIsInstance(broker, BrokerBase)


class TestStubBrokers(unittest.TestCase):
    """验证 stub 券商正确继承 BrokerBase 且方法抛出 NotImplementedError。"""

    def test_futu_is_broker_base(self):
        from core.brokers.futu import FutuBroker
        from core.brokers.base import BrokerBase
        b = FutuBroker()
        self.assertIsInstance(b, BrokerBase)
        self.assertFalse(b.is_connected())

    def test_futu_connect_returns_false(self):
        from core.brokers.futu import FutuBroker
        b = FutuBroker()
        self.assertFalse(b.connect())

    def test_futu_submit_order_raises(self):
        from core.brokers.futu import FutuBroker
        b = FutuBroker()
        with self.assertRaises(NotImplementedError):
            b.submit_order(Order(symbol='00700.HK', direction='BUY',
                                 order_type='MARKET', shares=100))

    def test_tiger_is_broker_base(self):
        from core.brokers.tiger import TigerBroker
        b = TigerBroker()
        self.assertIsInstance(b, BrokerBase)

    def test_ibkr_is_broker_base(self):
        from core.brokers.ibkr import IBBroker
        b = IBBroker()
        self.assertIsInstance(b, BrokerBase)

    def test_futu_supported_markets(self):
        from core.brokers.futu import FutuBroker
        markets = FutuBroker().supported_markets()
        self.assertIn(MarketType.HK_STOCK, markets)

    def test_ibkr_supported_markets(self):
        from core.brokers.ibkr import IBBroker
        markets = IBBroker().supported_markets()
        self.assertIn(MarketType.US_STOCK, markets)


if __name__ == '__main__':
    unittest.main()
