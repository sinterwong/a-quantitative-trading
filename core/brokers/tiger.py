"""
core/brokers/tiger.py — 老虎证券适配器（STUB）

⚠️  当前为 Stub 实现，禁止在 PAPER 阶段调用。
真实接入：
  1. pip install tigeropen
  2. config/brokers.json 设置 broker=tiger
  3. 填入 tiger_id / tiger_account
  4. 实现下方方法
"""

from core.oms import BrokerAdapter, Order, Fill, Position
from core.brokers.facade import BrokerSecurityError


class TigerBroker(BrokerAdapter):
    name = 'TigerBroker'

    def __init__(self, tiger_id: str = '', account: str = ''):
        from core.brokers.facade import BrokerFactory
        factory = BrokerFactory()
        if factory.mode.is_safe():
            raise BrokerSecurityError(
                'TigerBroker unavailable in PAPER mode.'
            )
        self.tiger_id = tiger_id
        self.account = account
        self._init_tiger()

    def _init_tiger(self):
        # — STUB —
        # import tigeropen
        # self.client = tigeropen.get_trade_client(self.tiger_id, self.account)
        pass

    def send(self, order: Order) -> Fill:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('TigerBroker.send')
        raise NotImplementedError('TigerBroker.send() STUB')

    def cancel(self, order_id: str) -> bool:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('TigerBroker.cancel')
        raise NotImplementedError('TigerBroker.cancel() STUB')

    def quote(self, symbol: str) -> dict:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('TigerBroker.quote')
        raise NotImplementedError('TigerBroker.quote() STUB')

    def get_positions(self) -> list:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('TigerBroker.get_positions')
        raise NotImplementedError('TigerBroker.get_positions() STUB')
