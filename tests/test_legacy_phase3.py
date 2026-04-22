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

    def test_futu_stub_rejects_in_paper(self):
        from core.brokers.facade import BrokerFactory, BrokerSecurityError
        factory = BrokerFactory()
        with self.assertRaises(BrokerSecurityError) as ctx:
            from core.brokers.futu import FutuBroker
            FutuBroker()
        self.assertIn('PAPER', str(ctx.exception))
        print(f'\nFutuBroker rejected in PAPER mode OK')

    def test_tiger_stub_rejects_in_paper(self):
        from core.brokers.facade import BrokerFactory, BrokerSecurityError
        with self.assertRaises(BrokerSecurityError):
            from core.brokers.tiger import TigerBroker
            TigerBroker()
        print('\nTigerBroker rejected in PAPER mode OK')

    def test_ibkr_stub_rejects_in_paper(self):
        from core.brokers.facade import BrokerFactory, BrokerSecurityError
        with self.assertRaises(BrokerSecurityError):
            from core.brokers.ibkr import IBBroker
            IBBroker()
        print('\nIBBroker rejected in PAPER mode OK')

    def test_require_live_rejects_without_unlock(self):
        from core.brokers.facade import BrokerFactory, BrokerSecurityError
        factory = BrokerFactory()
        with self.assertRaises(BrokerSecurityError) as ctx:
            factory.require_live()
        self.assertIn('unlock', str(ctx.exception).lower())
        print(f'\nrequire_live() rejected without unlock OK')

    def test_factory_get_broker_returns_paper(self):
        from core.brokers.facade import BrokerFactory
        factory = BrokerFactory()
        broker = factory.get_broker()
        self.assertEqual(broker.name, 'PaperBroker')
        print(f'\nget_broker() returns: {broker.name} OK')

    def test_create_broker_convenience(self):
        from core.brokers.facade import create_broker
        broker = create_broker()
        self.assertEqual(broker.name, 'PaperBroker')
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
