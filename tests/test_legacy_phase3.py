"""
Phase 3 test: BrokerFactory + SafetyMode + Real Broker Stubs
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest


class TestBrokerFactory(unittest.TestCase):

    def setUp(self):
        from core.brokers import facade
        facade.BrokerFactory._instance = None

    def tearDown(self):
        from core.brokers import facade
        facade.BrokerFactory._instance = None

    def test_default_mode_is_paper(self):
        from core.brokers.facade import BrokerFactory, SafetyMode
        factory = BrokerFactory()
        self.assertEqual(factory.mode, SafetyMode.PAPER)
        print(f'\nDefault mode: {factory.mode}')

    def test_paper_broker_works(self):
        from core.brokers.paper import PaperBroker
        broker = PaperBroker()
        self.assertEqual(broker.name, 'PaperBroker')
        print(f'\nPaperBroker name: {broker.name} OK')

    def test_futu_stub_rejects_submit_order(self):
        """FutuBroker stub 可实例化，但 submit_order 应抛 NotImplementedError。"""
        from core.brokers.futu import FutuBroker
        from core.oms import Order
        b = FutuBroker()
        with self.assertRaises(NotImplementedError):
            b.submit_order(Order(symbol='00700.HK', direction='BUY',
                                 order_type='MARKET', shares=100))
        print('\nFutuBroker submit_order raises NotImplementedError OK')

    def test_tiger_stub_rejects_submit_order(self):
        from core.brokers.tiger import TigerBroker
        from core.oms import Order
        b = TigerBroker()
        with self.assertRaises(NotImplementedError):
            b.submit_order(Order(symbol='AAPL', direction='BUY',
                                 order_type='MARKET', shares=100))
        print('\nTigerBroker submit_order raises NotImplementedError OK')

    def test_ibkr_stub_rejects_submit_order(self):
        from core.brokers.ibkr import IBBroker
        from core.oms import Order
        b = IBBroker()
        with self.assertRaises(NotImplementedError):
            b.submit_order(Order(symbol='AAPL', direction='BUY',
                                 order_type='MARKET', shares=100))
        print('\nIBBroker submit_order raises NotImplementedError OK')

    def test_require_live_rejects_without_unlock(self):
        from core.brokers.facade import BrokerFactory, BrokerSecurityError
        factory = BrokerFactory()
        with self.assertRaises(BrokerSecurityError) as ctx:
            factory.require_live()
        self.assertIn('unlock', str(ctx.exception).lower())
        print(f'\nrequire_live() rejected without unlock OK')

    def test_factory_get_broker_returns_simulated(self):
        """PAPER 模式下 factory 返回 SimulatedBroker（已取代旧 PaperBroker）。"""
        from core.brokers.facade import BrokerFactory
        from core.brokers.simulated import SimulatedBroker
        factory = BrokerFactory()
        broker = factory.get_broker()
        self.assertIsInstance(broker, SimulatedBroker)
        print(f'\nget_broker() returns: {broker.name} OK')

    def test_create_broker_convenience(self):
        from core.brokers.facade import create_broker
        from core.brokers.simulated import SimulatedBroker
        broker = create_broker()
        self.assertIsInstance(broker, SimulatedBroker)
        print(f'\ncreate_broker() returns: {broker.name} OK')


class TestSafetyMode(unittest.TestCase):

    def test_paper_is_safe(self):
        from core.brokers.facade import SafetyMode
        self.assertTrue(SafetyMode.PAPER.is_safe())
        self.assertTrue(SafetyMode.SIMULATED.is_safe())
        self.assertFalse(SafetyMode.LIVE.is_safe())

    def test_mode_label_ascii_only(self):
        from core.brokers.facade import BrokerFactory
        factory = BrokerFactory()
        label = factory.mode_label
        # Must not contain non-ASCII characters
        label.encode('ascii')
        print(f'\nMode label (ASCII): {label}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
