"""
core/brokers/futu.py — 富途证券适配器（STUB）

⚠️  当前为 Stub 实现，所有方法均需在接入真实 futu-api 后实现。

接入步骤：
  1. pip install futu-api
  2. 下载并运行 OpenD 客户端（富途官网）
  3. config/brokers.json 设置 broker=futu, safety_mode=LIVE
  4. 设置 3-step 解锁（见 facade.py BrokerFactory.require_live）
  5. 逐一实现下方方法（参考 futu-api 官方文档）

富途 API 文档：https://openapi.futunn.com/futu-api-doc/

支持市场：港股 / A股（沪深港通）/ 美股
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Set

from core.brokers.base import AccountInfo, BrokerBase, MarketType, QuoteData
from core.oms import Fill, Order, Position

logger = logging.getLogger(__name__)


class FutuBroker(BrokerBase):
    """
    富途证券适配器。

    支持市场：港股（主）/ 沪深A股（沪深港通）/ 美股
    账户类型：港股现货 / 美股现货 / 期权（需额外权限）
    """

    name = 'FutuBroker'

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 11111,
        trade_env: str = 'SIMULATE',  # 'SIMULATE' | 'REAL'
    ) -> None:
        self.host = host
        self.port = port
        self.trade_env = trade_env
        self._connected = False

        # TODO: 初始化 futu-api 连接对象
        # from futu import OpenQuoteContext, OpenSecTradeContext, TrdEnv
        # self._quote_ctx  = None
        # self._trade_ctx  = None
        # self._trd_env    = TrdEnv.SIMULATE if trade_env == 'SIMULATE' else TrdEnv.REAL

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        连接 OpenD 客户端。

        TODO:
            from futu import OpenQuoteContext, OpenSecTradeContext
            try:
                self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
                self._trade_ctx = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.HK,
                    host=self.host, port=self.port,
                    trd_env=self._trd_env,
                )
                self._connected = True
                return True
            except Exception as e:
                logger.error('[FutuBroker] connect failed: %s', e)
                return False
        """
        logger.warning('[FutuBroker] connect() is STUB — OpenD not connected')
        return False

    def disconnect(self) -> None:
        """
        TODO:
            if self._quote_ctx: self._quote_ctx.close()
            if self._trade_ctx: self._trade_ctx.close()
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
            ret, data = self._trade_ctx.accinfo_query(trd_env=self._trd_env)
            if ret == RET_OK:
                row = data.iloc[0]
                return AccountInfo(
                    account_id=str(row['acc_id']),
                    broker_name=self.name,
                    currency=row['currency'],
                    total_assets=float(row['total_assets']),
                    cash=float(row['cash']),
                    market_value=float(row['market_val']),
                    unrealized_pnl=float(row['unrealized_pl']),
                )
        """
        raise NotImplementedError('FutuBroker.get_account() — STUB')

    def get_cash(self) -> float:
        """TODO: return self.get_account().cash"""
        raise NotImplementedError('FutuBroker.get_cash() — STUB')

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        """
        TODO:
            ret, data = self._trade_ctx.position_list_query(trd_env=self._trd_env)
            if ret == RET_OK:
                return [
                    Position(
                        symbol=_futu_to_standard(row['code']),
                        shares=int(row['qty']),
                        avg_price=float(row['cost_price']),
                        current_price=float(row['price']),
                        unrealized_pnl=float(row['pl_val']),
                    )
                    for _, row in data.iterrows() if int(row['qty']) > 0
                ]
            return []
        """
        raise NotImplementedError('FutuBroker.get_positions() — STUB')

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteData:
        """
        TODO:
            futu_code = _standard_to_futu(symbol)  # '600519.SH' → 'SH.600519'
            ret, data = self._quote_ctx.get_market_snapshot([futu_code])
            if ret == RET_OK:
                row = data.iloc[0]
                return QuoteData(
                    symbol=symbol,
                    last=float(row['last_price']),
                    bid=float(row['bid_price']),
                    ask=float(row['ask_price']),
                    prev_close=float(row['prev_close_price']),
                    volume=int(row['volume']),
                    change_pct=float(row['change_rate']),
                )
        """
        raise NotImplementedError('FutuBroker.get_quote() — STUB')

    def is_market_open(self, market: MarketType = MarketType.HK_STOCK) -> bool:
        """
        TODO:
            ret, data = self._quote_ctx.get_global_state()
            # 解析 market_state 字段
        """
        raise NotImplementedError('FutuBroker.is_market_open() — STUB')

    def supported_markets(self) -> Set[MarketType]:
        return {MarketType.HK_STOCK, MarketType.A_SHARE, MarketType.US_STOCK}

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Fill:
        """
        TODO:
            futu_code = _standard_to_futu(order.symbol)
            trd_side  = TrdSide.BUY if order.direction == 'BUY' else TrdSide.SELL
            order_type = OrderType.MARKET if order.order_type == 'MARKET' else OrderType.NORMAL

            ret, data = self._trade_ctx.place_order(
                price=order.price, qty=order.shares,
                code=futu_code, trd_side=trd_side,
                order_type=order_type, trd_env=self._trd_env,
            )
            if ret == RET_OK:
                return Fill(order_id=order.order_id, symbol=order.symbol,
                            direction=order.direction, shares=order.shares,
                            price=order.price)
        """
        raise NotImplementedError('FutuBroker.submit_order() — STUB')

    def cancel_order(self, order_id: str) -> bool:
        """
        TODO:
            ret, _ = self._trade_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id, qty=0, price=0,
                trd_env=self._trd_env,
            )
            return ret == RET_OK
        """
        raise NotImplementedError('FutuBroker.cancel_order() — STUB')

    def get_orders(
        self,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Order]:
        """TODO: ret, data = self._trade_ctx.order_list_query(...)"""
        raise NotImplementedError('FutuBroker.get_orders() — STUB')

    def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
        """TODO: ret, data = self._trade_ctx.deal_list_query(...)"""
        raise NotImplementedError('FutuBroker.get_fills() — STUB')
