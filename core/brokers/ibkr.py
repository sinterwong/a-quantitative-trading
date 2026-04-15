"""
core/brokers/ibkr.py — Interactive Brokers 适配器（STUB）

⚠️  当前为 Stub 实现，禁止在 PAPER 阶段调用。
IBKR 是全球覆盖最广的券商，支持股票/期货/期权/外汇。
真实接入：
  1. pip install ib_insync
  2. config/brokers.json 设置 broker=ibkr
  3. IB Gateway / TWS 地址和端口
  4. 实现下方方法
"""

from core.oms import BrokerAdapter, Order, Fill, Position
from core.brokers.facade import BrokerSecurityError


class IBBroker(BrokerAdapter):
    name = 'IBBroker'

    def __init__(self, host: str = '127.0.0.1', port: int = 4001, client_id: int = 1):
        from core.brokers.facade import BrokerFactory
        factory = BrokerFactory()
        if factory.mode.is_safe():
            raise BrokerSecurityError(
                'IBBroker unavailable in PAPER mode.'
            )
        self.host = host
        self.port = port
        self.client_id = client_id
        self._init_ib()

    def _init_ib(self):
        # — STUB —
        # from ib_insync import IB
        # self.ib = IB()
        # self.ib.connect(self.host, self.port, clientId=self.client_id)
        pass

    def send(self, order: Order) -> Fill:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('IBBroker.send')
        raise NotImplementedError('IBBroker.send() STUB')

    def cancel(self, order_id: str) -> bool:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('IBBroker.cancel')
        raise NotImplementedError('IBBroker.cancel() STUB')

    def quote(self, symbol: str) -> dict:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('IBBroker.quote')
        raise NotImplementedError('IBBroker.quote() STUB')

    def get_positions(self) -> list:
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('IBBroker.get_positions')
        raise NotImplementedError('IBBroker.get_positions() STUB')
