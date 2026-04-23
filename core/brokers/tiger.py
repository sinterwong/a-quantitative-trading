"""
core/brokers/tiger.py — 老虎证券适配器（STUB）

⚠️  当前为 Stub 实现，所有方法均需在接入真实 tigeropen 后实现。

接入步骤：
  1. pip install tigeropen
  2. 在老虎开放平台申请 API 权限，获取 tiger_id / 私钥文件
  3. config/brokers.json 设置 broker=tiger, safety_mode=LIVE
  4. 设置 3-step 解锁（见 facade.py BrokerFactory.require_live）
  5. 逐一实现下方方法（参考 tigeropen 官方文档）

老虎 API 文档：https://tigeropen.github.io/

支持市场：港股 / 美股 / A股（沪深港通）/ 期货 / 期权
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Set

from core.brokers.base import AccountInfo, BrokerBase, MarketType, QuoteData
from core.oms import Fill, Order, Position

logger = logging.getLogger(__name__)


class TigerBroker(BrokerBase):
    """
    老虎证券适配器。

    支持市场：港股 / 美股 / A股（沪深港通）/ 期货 / 期权
    账户类型：综合账户（可同时持有多市场资产）
    """

    name = 'TigerBroker'

    def __init__(
        self,
        tiger_id: str = '',
        account: str = '',
        private_key_path: str = '',
    ) -> None:
        self.tiger_id = tiger_id
        self.account = account
        self.private_key_path = private_key_path
        self._connected = False

        # TODO: 初始化 tigeropen 客户端
        # from tigeropen.tiger_open_config import TigerOpenClientConfig
        # from tigeropen.trade.trade_client import TradeClient
        # from tigeropen.quote.quote_client import QuoteClient
        # self._cfg         = None
        # self._trade_client = None
        # self._quote_client = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        TODO:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
            from tigeropen.quote.quote_client import QuoteClient
            try:
                self._cfg = TigerOpenClientConfig()
                self._cfg.tiger_id = self.tiger_id
                self._cfg.account = self.account
                self._cfg.private_key = open(self.private_key_path).read()
                self._trade_client = TradeClient(self._cfg)
                self._quote_client = QuoteClient(self._cfg)
                self._connected = True
                return True
            except Exception as e:
                logger.error('[TigerBroker] connect failed: %s', e)
                return False
        """
        logger.warning('[TigerBroker] connect() is STUB')
        return False

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        """
        TODO:
            assets = self._trade_client.get_assets(account=self.account)
            segment = assets[0].segments[0]  # 取第一个账户段
            return AccountInfo(
                account_id=self.account,
                broker_name=self.name,
                currency=segment.currency,
                total_assets=float(segment.net_liquidation),
                cash=float(segment.cash_balance),
                market_value=float(segment.gross_position_value),
                unrealized_pnl=float(segment.unrealized_pnl),
            )
        """
        raise NotImplementedError('TigerBroker.get_account() — STUB')

    def get_cash(self) -> float:
        """TODO: return self.get_account().cash"""
        raise NotImplementedError('TigerBroker.get_cash() — STUB')

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        """
        TODO:
            positions = self._trade_client.get_positions(account=self.account)
            return [
                Position(
                    symbol=_tiger_to_standard(p.contract.symbol, p.contract.currency),
                    shares=int(p.quantity),
                    avg_price=float(p.average_cost),
                    current_price=float(p.market_price),
                    unrealized_pnl=float(p.unrealized_pnl),
                )
                for p in positions if p.quantity > 0
            ]
        """
        raise NotImplementedError('TigerBroker.get_positions() — STUB')

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteData:
        """
        TODO:
            from tigeropen.common.consts import Market
            bars = self._quote_client.get_quote_real_time([symbol])
            if bars:
                b = bars[0]
                return QuoteData(
                    symbol=symbol,
                    last=float(b.latest_price),
                    bid=float(b.bid_price),
                    ask=float(b.ask_price),
                    prev_close=float(b.pre_close),
                    volume=int(b.volume),
                    change_pct=float(b.rate_of_change),
                )
        """
        raise NotImplementedError('TigerBroker.get_quote() — STUB')

    def is_market_open(self, market: MarketType = MarketType.HK_STOCK) -> bool:
        """TODO: 查询 Tiger 交易时段 API"""
        raise NotImplementedError('TigerBroker.is_market_open() — STUB')

    def supported_markets(self) -> Set[MarketType]:
        return {
            MarketType.HK_STOCK, MarketType.US_STOCK,
            MarketType.A_SHARE, MarketType.FUTURES, MarketType.OPTIONS,
        }

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Fill:
        """
        TODO:
            from tigeropen.trade.domain.order import MarketOrder, LimitOrder
            from tigeropen.common.consts import ActionType
            action = ActionType.BUY if order.direction == 'BUY' else ActionType.SELL
            if order.order_type == 'MARKET':
                tiger_order = MarketOrder(account=self.account, contract=_make_contract(order.symbol),
                                          action=action, quantity=order.shares)
            else:
                tiger_order = LimitOrder(account=self.account, contract=_make_contract(order.symbol),
                                         action=action, quantity=order.shares, limit_price=order.price)
            self._trade_client.place_order(tiger_order)
            return Fill(order_id=order.order_id, symbol=order.symbol,
                        direction=order.direction, shares=order.shares, price=order.price)
        """
        raise NotImplementedError('TigerBroker.submit_order() — STUB')

    def cancel_order(self, order_id: str) -> bool:
        """TODO: self._trade_client.cancel_order(account=self.account, id=int(order_id))"""
        raise NotImplementedError('TigerBroker.cancel_order() — STUB')

    def get_orders(
        self,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Order]:
        """TODO: self._trade_client.get_orders(account=self.account, ...)"""
        raise NotImplementedError('TigerBroker.get_orders() — STUB')

    def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
        """TODO: self._trade_client.get_transactions(account=self.account, ...)"""
        raise NotImplementedError('TigerBroker.get_fills() — STUB')
