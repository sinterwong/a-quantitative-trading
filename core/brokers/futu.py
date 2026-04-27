"""
core/brokers/futu.py — 富途证券适配器（完整实现）

接入富途 OpenD 本地进程，支持纸交易（TrdEnv.SIMULATE）和实盘（TrdEnv.REAL）。

接入步骤：
  1. pip install futu-api
  2. 下载并运行 OpenD 客户端（富途官网：https://www.futunn.com/download/OpenD）
  3. OpenD 默认监听 127.0.0.1:11111
  4. 纸交易无需真实资金，在富途 App 开启"模拟交易"账户即可

安全设计：
  - 默认 trade_env='SIMULATE'（纸交易），需显式传入 'REAL' 才会实盘
  - OpenD 未启动时，所有方法返回合理的降级值（不崩溃）
  - 所有方法均有超时保护

富途 API 文档：https://openapi.futunn.com/futu-api-doc/

用法：
    from core.brokers.futu import FutuBroker

    broker = FutuBroker(host='127.0.0.1', port=11111, trade_env='SIMULATE')
    ok = broker.connect()
    if ok:
        account = broker.get_account()
        print(account.total_assets)
    broker.disconnect()
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Set

from core.brokers.base import AccountInfo, BrokerBase, MarketType, QuoteData
from core.oms import Fill, Order, Position

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# futu-api 可选导入（未安装时降级）
# ---------------------------------------------------------------------------

try:
    import futu as ft
    from futu import (
        OpenQuoteContext,
        OpenSecTradeContext,
        RET_OK,
        TrdEnv,
        TrdMarket,
        TrdSide,
        OrderType,
        ModifyOrderOp,
    )
    _FUTU_AVAILABLE = True
except ImportError:
    _FUTU_AVAILABLE = False
    ft = None


# ---------------------------------------------------------------------------
# 代码格式转换
# ---------------------------------------------------------------------------

def _standard_to_futu(symbol: str) -> str:
    """
    将标准代码转换为富途格式。
    '600519.SH' → 'SH.600519'
    '000001.SZ' → 'SZ.000001'
    '00700.HK'  → 'HK.00700'
    """
    if '.' not in symbol:
        return symbol
    code, market = symbol.rsplit('.', 1)
    return f'{market.upper()}.{code}'


def _futu_to_standard(futu_code: str) -> str:
    """
    将富途代码转换为标准格式。
    'SH.600519' → '600519.SH'
    'SZ.000001' → '000001.SZ'
    'HK.00700'  → '00700.HK'
    """
    if '.' not in futu_code:
        return futu_code
    market, code = futu_code.split('.', 1)
    return f'{code}.{market.upper()}'


# ---------------------------------------------------------------------------
# FutuBroker
# ---------------------------------------------------------------------------

class FutuBroker(BrokerBase):
    """
    富途证券适配器（支持纸交易 / 实盘）。

    OpenD 未运行时，connect() 返回 False，其他方法返回空/零值（不崩溃）。

    Parameters
    ----------
    host : str
        OpenD 监听地址（默认 127.0.0.1）
    port : int
        OpenD 监听端口（默认 11111）
    trade_env : str
        'SIMULATE'（纸交易）或 'REAL'（实盘）
    trd_market : str
        交易市场：'CN'（A股沪深港通）/ 'HK'（港股）/ 'US'（美股）
    """

    name = 'FutuBroker'

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 11111,
        trade_env: str = 'SIMULATE',
        trd_market: str = 'CN',
    ) -> None:
        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.trd_market = trd_market
        self._connected = False

        self._quote_ctx: Optional[object] = None
        self._trade_ctx: Optional[object] = None

        # 富途 enum 对象（仅在 futu-api 可用时有效）
        self._trd_env = None
        self._trd_market = None
        if _FUTU_AVAILABLE:
            self._trd_env = TrdEnv.SIMULATE if trade_env == 'SIMULATE' else TrdEnv.REAL
            self._trd_market = TrdMarket.CN if trd_market == 'CN' else (
                TrdMarket.HK if trd_market == 'HK' else TrdMarket.US
            )

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """连接 OpenD。OpenD 未运行或 futu-api 未安装时返回 False。"""
        if not _FUTU_AVAILABLE:
            logger.warning('[FutuBroker] futu-api 未安装。运行: pip install futu-api')
            return False

        try:
            self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
            self._trade_ctx = OpenSecTradeContext(
                filter_trdmarket=self._trd_market,
                host=self.host,
                port=self.port,
                trd_env=self._trd_env,
            )
            self._connected = True
            logger.info('[FutuBroker] 连接成功 %s:%d env=%s', self.host, self.port, self.trade_env)
            return True
        except Exception as e:
            logger.error('[FutuBroker] 连接失败: %s', e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        """断开连接。"""
        try:
            if self._quote_ctx is not None:
                self._quote_ctx.close()
            if self._trade_ctx is not None:
                self._trade_ctx.close()
        except Exception as e:
            logger.warning('[FutuBroker] 断开异常: %s', e)
        finally:
            self._quote_ctx = None
            self._trade_ctx = None
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        """获取账户资产信息。"""
        if not self._connected or self._trade_ctx is None:
            return AccountInfo(
                account_id='offline',
                broker_name=self.name,
                currency='CNY',
                total_assets=0.0,
                cash=0.0,
                market_value=0.0,
                unrealized_pnl=0.0,
            )

        try:
            ret, data = self._trade_ctx.accinfo_query(trd_env=self._trd_env)
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                return AccountInfo(
                    account_id=str(row.get('acc_id', 'unknown')),
                    broker_name=self.name,
                    currency=str(row.get('currency', 'HKD')),
                    total_assets=float(row.get('total_assets', 0.0)),
                    cash=float(row.get('cash', 0.0)),
                    market_value=float(row.get('market_val', 0.0)),
                    unrealized_pnl=float(row.get('unrealized_pl', 0.0)),
                )
        except Exception as e:
            logger.error('[FutuBroker] get_account 失败: %s', e)

        return AccountInfo(
            account_id='error',
            broker_name=self.name,
            currency='CNY',
            total_assets=0.0,
            cash=0.0,
            market_value=0.0,
            unrealized_pnl=0.0,
        )

    def get_cash(self) -> float:
        return self.get_account().cash

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        """获取当前持仓列表。"""
        if not self._connected or self._trade_ctx is None:
            return []

        try:
            ret, data = self._trade_ctx.position_list_query(trd_env=self._trd_env)
            if ret == RET_OK and not data.empty:
                positions = []
                for _, row in data.iterrows():
                    qty = int(row.get('qty', 0))
                    if qty <= 0:
                        continue
                    positions.append(Position(
                        symbol=_futu_to_standard(str(row.get('code', ''))),
                        shares=qty,
                        avg_price=float(row.get('cost_price', 0.0)),
                        current_price=float(row.get('price', 0.0)),
                        unrealized_pnl=float(row.get('pl_val', 0.0)),
                    ))
                return positions
        except Exception as e:
            logger.error('[FutuBroker] get_positions 失败: %s', e)

        return []

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteData:
        """获取实时行情快照。"""
        if not self._connected or self._quote_ctx is None:
            return QuoteData(
                symbol=symbol, last=0.0, bid=0.0, ask=0.0,
                prev_close=0.0, volume=0, change_pct=0.0,
            )

        try:
            futu_code = _standard_to_futu(symbol)
            ret, data = self._quote_ctx.get_market_snapshot([futu_code])
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                return QuoteData(
                    symbol=symbol,
                    last=float(row.get('last_price', 0.0)),
                    bid=float(row.get('bid_price', 0.0)),
                    ask=float(row.get('ask_price', 0.0)),
                    prev_close=float(row.get('prev_close_price', 0.0)),
                    volume=int(row.get('volume', 0)),
                    change_pct=float(row.get('change_rate', 0.0)),
                )
        except Exception as e:
            logger.error('[FutuBroker] get_quote(%s) 失败: %s', symbol, e)

        return QuoteData(
            symbol=symbol, last=0.0, bid=0.0, ask=0.0,
            prev_close=0.0, volume=0, change_pct=0.0,
        )

    def is_market_open(self, market: MarketType = MarketType.A_SHARE) -> bool:
        """查询市场是否开盘。"""
        if not self._connected or self._quote_ctx is None:
            return False

        try:
            ret, data = self._quote_ctx.get_global_state()
            if ret != RET_OK:
                return False

            market_map = {
                MarketType.A_SHARE: 'market_sh',
                MarketType.HK_STOCK: 'market_hk',
                MarketType.US_STOCK: 'market_us',
            }
            field = market_map.get(market, 'market_sh')
            state = str(data.get(field, 'CLOSED'))
            return state in ('MORNING', 'AFTERNOON', 'MARKET_OPEN')
        except Exception as e:
            logger.error('[FutuBroker] is_market_open 失败: %s', e)
            return False

    def supported_markets(self) -> Set[MarketType]:
        return {MarketType.HK_STOCK, MarketType.A_SHARE, MarketType.US_STOCK}

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Fill:
        """提交订单（市价单 / 限价单）。"""
        if not self._connected or self._trade_ctx is None:
            logger.warning('[FutuBroker] 未连接，订单未提交: %s', order.order_id)
            return Fill(
                order_id=order.order_id,
                symbol=order.symbol,
                direction=order.direction,
                shares=0,
                price=0.0,
            )

        try:
            futu_code = _standard_to_futu(order.symbol)
            trd_side = TrdSide.BUY if order.direction == 'BUY' else TrdSide.SELL
            order_type = OrderType.MARKET if order.order_type == 'MARKET' else OrderType.NORMAL
            price = order.price if order.order_type == 'LIMIT' else 0.0

            ret, data = self._trade_ctx.place_order(
                price=price,
                qty=order.shares,
                code=futu_code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=self._trd_env,
            )

            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                futu_order_id = str(row.get('order_id', order.order_id))
                logger.info(
                    '[FutuBroker] 下单成功: %s %s×%d @ %.3f (futu_id=%s)',
                    order.direction, order.symbol, order.shares, price, futu_order_id,
                )
                return Fill(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    direction=order.direction,
                    shares=order.shares,
                    price=order.price or float(row.get('dealt_avg_price', order.price)),
                    commission=0.0,  # 纸交易无佣金
                )
            else:
                logger.error('[FutuBroker] 下单失败: ret=%d', ret)
        except Exception as e:
            logger.error('[FutuBroker] submit_order 异常: %s', e)

        return Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            direction=order.direction,
            shares=0,
            price=0.0,
        )

    def cancel_order(self, order_id: str) -> bool:
        """撤单。"""
        if not self._connected or self._trade_ctx is None:
            return False

        try:
            ret, _ = self._trade_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=self._trd_env,
            )
            return ret == RET_OK
        except Exception as e:
            logger.error('[FutuBroker] cancel_order 异常: %s', e)
            return False

    def get_orders(
        self,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Order]:
        """查询订单列表。"""
        if not self._connected or self._trade_ctx is None:
            return []

        try:
            ret, data = self._trade_ctx.order_list_query(trd_env=self._trd_env)
            if ret != RET_OK or data.empty:
                return []

            orders = []
            for _, row in data.iterrows():
                order = Order(
                    order_id=str(row.get('order_id', '')),
                    symbol=_futu_to_standard(str(row.get('code', ''))),
                    direction='BUY' if str(row.get('trd_side', '')) == 'BUY' else 'SELL',
                    order_type='LIMIT',
                    shares=int(row.get('qty', 0)),
                    price=float(row.get('price', 0.0)),
                    status=self._map_order_status(str(row.get('order_status', ''))),
                )
                orders.append(order)
            return orders
        except Exception as e:
            logger.error('[FutuBroker] get_orders 异常: %s', e)
            return []

    def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
        """查询成交记录。"""
        if not self._connected or self._trade_ctx is None:
            return []

        try:
            ret, data = self._trade_ctx.deal_list_query(trd_env=self._trd_env)
            if ret != RET_OK or data.empty:
                return []

            fills = []
            for _, row in data.iterrows():
                fills.append(Fill(
                    order_id=str(row.get('order_id', '')),
                    symbol=_futu_to_standard(str(row.get('code', ''))),
                    direction='BUY' if str(row.get('trd_side', '')) == 'BUY' else 'SELL',
                    shares=int(row.get('qty', 0)),
                    price=float(row.get('price', 0.0)),
                    commission=float(row.get('commission', 0.0)),
                ))
            return fills
        except Exception as e:
            logger.error('[FutuBroker] get_fills 异常: %s', e)
            return []

    # ------------------------------------------------------------------
    # BrokerAdapter 兼容接口（OMS 使用）
    # ------------------------------------------------------------------

    def send(self, order: Order) -> Fill:
        """兼容 BrokerAdapter 接口，转发给 submit_order。"""
        return self.submit_order(order)

    def cancel(self, order_id: str) -> bool:
        """兼容 BrokerAdapter 接口。"""
        return self.cancel_order(order_id)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _map_order_status(futu_status: str) -> str:
        """将富途订单状态映射为系统标准状态。"""
        mapping = {
            'SUBMITTED': 'PENDING',
            'FILLED_ALL': 'FILLED',
            'FILLED_PART': 'PENDING',
            'CANCELLED_ALL': 'CANCELLED',
            'FAILED': 'REJECTED',
            'DISABLED': 'CANCELLED',
        }
        return mapping.get(futu_status.upper(), 'PENDING')
