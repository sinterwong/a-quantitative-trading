"""
core/brokers/futu.py — 富途证券适配器（STUB）

⚠️  当前为 Stub 实现，禁止在 PAPER 阶段调用。
真实接入时请：
  1. 安装 futu-api: pip install futu-api
  2. 修改 config/brokers.json 设置 broker=futu
  3. 设置富途 OpenD 地址和端口
  4. 取消下方 # — STUB 注释，实现真实方法
"""

from core.oms import BrokerAdapter, Order, Fill, Position
from core.brokers.facade import BrokerSecurityError


class FutuBroker(BrokerAdapter):
    """
    富途证券适配器（STUB）。
    所有方法均抛出 BrokerSecurityError，确保在 PAPER 阶段绝对安全。
    """

    name = 'FutuBroker'

    def __init__(self, host: str = '127.0.0.1', port: int = 11111):
        # 在 PAPER 模式下，拒绝初始化
        from core.brokers.facade import BrokerFactory
        factory = BrokerFactory()
        if factory.mode.is_safe():
            raise BrokerSecurityError(
                'FutuBroker unavailable in PAPER mode. '
                'System is in simulated trading phase.'
            )
        self.host = host
        self.port = port
        self._init_futu()

    def _init_futu(self):
        # — STUB —
        # from futu import OpenQuoteContext, OpenSecTradeContext
        # self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
        # self.trade_ctx = OpenSecTradeContext(host=self.host, port=self.port)
        pass

    def send(self, order: Order) -> Fill:
        """发送订单（STUB）"""
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('FutuBroker.send')
        # — STUB 实现 —
        raise NotImplementedError('FutuBroker.send() is a STUB')

    def cancel(self, order_id: str) -> bool:
        """取消订单（STUB）"""
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('FutuBroker.cancel')
        raise NotImplementedError('FutuBroker.cancel() STUB')

    def quote(self, symbol: str) -> dict:
        """获取报价（STUB）"""
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('FutuBroker.quote')
        raise NotImplementedError('FutuBroker.quote() STUB')

    def get_positions(self) -> list:
        """获取持仓（STUB）"""
        from core.brokers.facade import BrokerFactory
        BrokerFactory().assert_safe('FutuBroker.get_positions')
        raise NotImplementedError('FutuBroker.get_positions() STUB')
