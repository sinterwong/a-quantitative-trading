"""
core/brokers/ibkr.py — Interactive Brokers 适配器（STUB）

⚠️  当前为 Stub 实现，所有方法均需在接入真实 ib_insync 后实现。

接入步骤：
  1. pip install ib_insync
  2. 安装并运行 IB Gateway 或 TWS（Interactive Brokers 官方客户端）
  3. config/brokers.json 设置 broker=ibkr, safety_mode=LIVE
  4. 设置 3-step 解锁（见 facade.py BrokerFactory.require_live）
  5. 逐一实现下方方法（参考 ib_insync 文档）

IBKR / ib_insync 文档：https://ib-insync.readthedocs.io/

支持市场：美股 / 港股 / 欧股 / 期货 / 期权 / 外汇 / 债券
注意：IBKR 最小下单单位因市场和品种而异
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Set

from core.brokers.base import AccountInfo, BrokerBase, MarketType, QuoteData
from core.oms import Fill, Order, Position

logger = logging.getLogger(__name__)


class IBBroker(BrokerBase):
    """
    Interactive Brokers 适配器。

    支持市场：美股 / 港股 / 欧股 / 期货 / 期权 / 外汇 / 债券
    注意：ib_insync 基于 asyncio，与 AsyncStrategyRunner 天然兼容
    """

    name = 'IBBroker'

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 4001,          # TWS: 7497/7496, IB Gateway: 4001/4002
        client_id: int = 1,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self._connected = False

        # TODO: 初始化 ib_insync 连接
        # from ib_insync import IB
        # self._ib = IB()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        TODO:
            from ib_insync import IB
            try:
                self._ib = IB()
                self._ib.connect(self.host, self.port, clientId=self.client_id)
                self._connected = self._ib.isConnected()
                return self._connected
            except Exception as e:
                logger.error('[IBBroker] connect failed: %s', e)
                return False
        """
        logger.warning('[IBBroker] connect() is STUB — IB Gateway not connected')
        return False

    def disconnect(self) -> None:
        """
        TODO:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
            self._connected = False
        """
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        """
        TODO:
            vals = self._ib.accountValues()
            summary = {v.tag: v.value for v in vals if v.currency == 'BASE'}
            return AccountInfo(
                account_id=self._ib.managedAccounts()[0],
                broker_name=self.name,
                currency='USD',
                total_assets=float(summary.get('NetLiquidation', 0)),
                cash=float(summary.get('AvailableFunds', 0)),
                market_value=float(summary.get('GrossPositionValue', 0)),
                unrealized_pnl=float(summary.get('UnrealizedPnL', 0)),
            )
        """
        raise NotImplementedError('IBBroker.get_account() — STUB')

    def get_cash(self) -> float:
        """TODO: return self.get_account().cash"""
        raise NotImplementedError('IBBroker.get_cash() — STUB')

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        """
        TODO:
            positions = self._ib.positions()
            return [
                Position(
                    symbol=_ib_to_standard(p.contract),
                    shares=int(p.position),
                    avg_price=float(p.avgCost / p.contract.multiplier),
                )
                for p in positions if p.position != 0
            ]
        """
        raise NotImplementedError('IBBroker.get_positions() — STUB')

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteData:
        """
        TODO:
            from ib_insync import Stock
            contract = _make_ib_contract(symbol)
            self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract)
            self._ib.sleep(1)
            return QuoteData(
                symbol=symbol,
                last=float(ticker.last or ticker.close),
                bid=float(ticker.bid),
                ask=float(ticker.ask),
                volume=int(ticker.volume or 0),
            )
        """
        raise NotImplementedError('IBBroker.get_quote() — STUB')

    def is_market_open(self, market: MarketType = MarketType.US_STOCK) -> bool:
        """
        TODO:
            from ib_insync import Index
            spx = Index('SPX', 'CBOE')
            self._ib.qualifyContracts(spx)
            details = self._ib.reqContractDetails(spx)[0]
            # 解析 liquidHours / tradingHours 字段
        """
        raise NotImplementedError('IBBroker.is_market_open() — STUB')

    def supported_markets(self) -> Set[MarketType]:
        return {
            MarketType.US_STOCK, MarketType.HK_STOCK,
            MarketType.FUTURES, MarketType.OPTIONS, MarketType.FOREX,
        }

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Fill:
        """
        TODO:
            from ib_insync import MarketOrder, LimitOrder, Trade
            contract = _make_ib_contract(order.symbol)
            self._ib.qualifyContracts(contract)
            action = 'BUY' if order.direction == 'BUY' else 'SELL'
            if order.order_type == 'MARKET':
                ib_order = MarketOrder(action, order.shares)
            else:
                ib_order = LimitOrder(action, order.shares, order.price)

            trade: Trade = self._ib.placeOrder(contract, ib_order)
            self._ib.sleep(2)  # 等待部分成交
            fill_price = trade.orderStatus.avgFillPrice or order.price
            return Fill(
                order_id=order.order_id,
                symbol=order.symbol,
                direction=order.direction,
                shares=int(trade.orderStatus.filled),
                price=fill_price,
            )
        """
        raise NotImplementedError('IBBroker.submit_order() — STUB')

    def cancel_order(self, order_id: str) -> bool:
        """
        TODO:
            trade = next((t for t in self._ib.trades() if str(t.order.orderId) == order_id), None)
            if trade:
                self._ib.cancelOrder(trade.order)
                return True
            return False
        """
        raise NotImplementedError('IBBroker.cancel_order() — STUB')

    def get_orders(
        self,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Order]:
        """TODO: self._ib.trades() → filter by status/since"""
        raise NotImplementedError('IBBroker.get_orders() — STUB')

    def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
        """TODO: self._ib.fills() → filter by since"""
        raise NotImplementedError('IBBroker.get_fills() — STUB')
