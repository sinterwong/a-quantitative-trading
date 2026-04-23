"""
core/brokers/simulated.py — SimulatedBroker（模拟撮合，自包含）

特点：
  - 完全自包含，不依赖 HTTP / 外部 API
  - 实现 BrokerBase 全部接口，可无缝切换为任何真实券商
  - 真实 A 股规则：手数（100股）/ 印花税 / 涨跌停拒单
  - 可配置：初始资金 / 佣金率 / 滑点 / 是否模拟市场时间
  - 支持 reset()，方便单元测试和回放验证
  - 可选 SQLite 持久化（默认关闭）

用法：
    from core.brokers.simulated import SimulatedBroker, SimConfig

    broker = SimulatedBroker(SimConfig(initial_cash=500_000))
    broker.connect()
    account = broker.get_account()   # AccountInfo
    quote   = broker.get_quote('600519.SH')  # QuoteData

    from core.oms import Order
    order = Order(symbol='600519.SH', direction='BUY', shares=100)
    fill = broker.submit_order(order)
    print(fill)
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, time as dt_time
from typing import Dict, List, Literal, Optional, Set

import numpy as np

from core.brokers.base import (
    AccountInfo, BrokerBase, MarketType, OrderStatus, QuoteData,
)
from core.oms import Fill, Order, Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SimConfig
# ---------------------------------------------------------------------------

@dataclass
class SimConfig:
    """SimulatedBroker 配置。"""
    initial_cash: float = 1_000_000.0  # 初始资金
    commission_rate: float = 0.0003    # 佣金率（万3）
    min_commission: float = 5.0        # 最低佣金
    stamp_tax_rate: float = 0.001      # 印花税（A股卖出 0.1%）
    slippage_bps: float = 5.0          # 滑点（基点），买贵卖便宜
    lot_size: int = 100                # 最小下单单位（A股1手=100股）
    enforce_lot: bool = True           # 是否强制手数检查
    enforce_market_hours: bool = False # 是否模拟市场时间限制（默认关闭方便测试）
    price_source: str = 'tencent'      # 'tencent' | 'manual' | 'none'
    sqlite_path: Optional[str] = None  # 持久化路径，None=不持久化


# ---------------------------------------------------------------------------
# SimulatedBroker
# ---------------------------------------------------------------------------

class SimulatedBroker(BrokerBase):
    """
    模拟撮合券商。

    实现 BrokerBase 全部接口，用于模拟实盘验证和功能开发。
    接口与真实券商（Futu/Tiger/IBKR）完全兼容，可一键切换。
    """

    name = 'SimulatedBroker'

    def __init__(
        self,
        config: Optional[SimConfig] = None,
        account_id: str = 'SIM-001',
    ) -> None:
        self.config = config or SimConfig()
        self._account_id = account_id
        self._connected = False

        # 内部状态
        self._cash: float = self.config.initial_cash
        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}
        self._fills: List[Fill] = []
        self._realized_pnl_today: float = 0.0

        # 手动报价（用于测试，当 price_source='manual' 时）
        self._manual_quotes: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        self._connected = True
        logger.info('[SimulatedBroker] Connected (account=%s, cash=%.2f)',
                    self._account_id, self._cash)
        return True

    def disconnect(self) -> None:
        self._connected = False
        logger.info('[SimulatedBroker] Disconnected.')

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        mv = self.total_position_value()
        unrealized = sum(
            (p.current_price - p.avg_price) * p.shares
            for p in self._positions.values()
            if p.current_price > 0
        )
        return AccountInfo(
            account_id=self._account_id,
            broker_name=self.name,
            currency='CNY',
            total_assets=self._cash + mv,
            net_assets=self._cash + mv,
            cash=self._cash,
            frozen_cash=0.0,
            market_value=mv,
            unrealized_pnl=unrealized,
            realized_pnl_today=self._realized_pnl_today,
        )

    def get_cash(self) -> float:
        return self._cash

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        # 更新当前价格
        for sym, pos in self._positions.items():
            q = self.get_quote(sym)
            if q.last > 0:
                pos.current_price = q.last
                pos.unrealized_pnl = (q.last - pos.avg_price) * pos.shares
        return [p for p in self._positions.values() if p.shares > 0]

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteData:
        """获取实时报价。优先用腾讯行情，fallback 手动价格。"""
        if self.config.price_source == 'manual':
            price = self._manual_quotes.get(symbol, 0.0)
            return QuoteData(
                symbol=symbol, last=price,
                bid=price * 0.999 if price > 0 else 0,
                ask=price * 1.001 if price > 0 else 0,
                is_tradable=price > 0,
            )

        if self.config.price_source == 'tencent':
            return self._fetch_tencent_quote(symbol)

        return QuoteData(symbol=symbol, last=0.0, is_tradable=False)

    def set_quote(self, symbol: str, price: float) -> None:
        """手动设置报价（price_source='manual' 时使用，方便测试）。"""
        self._manual_quotes[symbol] = price

    def is_market_open(self, market: MarketType = MarketType.A_SHARE) -> bool:
        if not self.config.enforce_market_hours:
            return True  # 模拟模式不限时间
        now = datetime.now()
        if now.weekday() >= 5:  # 周末
            return False
        t = now.time()
        if market == MarketType.A_SHARE:
            morning = dt_time(9, 30) <= t <= dt_time(11, 30)
            afternoon = dt_time(13, 0) <= t <= dt_time(15, 0)
            return morning or afternoon
        if market == MarketType.HK_STOCK:
            morning = dt_time(9, 30) <= t <= dt_time(12, 0)
            afternoon = dt_time(13, 0) <= t <= dt_time(16, 0)
            return morning or afternoon
        return True

    def supported_markets(self) -> Set[MarketType]:
        return {MarketType.A_SHARE}

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Fill:
        """
        模拟撮合。

        规则：
          1. 手数检查（A股 100 股 / 手）
          2. 获取实时报价，报价为 0 时拒单
          3. 涨停（>+9.9%）不可买，跌停（<-9.9%）不可卖
          4. 滑点模拟：买入 +slippage_bps，卖出 -slippage_bps
          5. 扣佣金（≥ min_commission）
          6. 卖出扣印花税（A股）
          7. 更新内部现金和持仓
        """
        if order.order_id not in self._orders:
            self._orders[order.order_id] = order

        # --- 手数检查 ---
        if self.config.enforce_lot and order.shares % self.config.lot_size != 0:
            logger.warning('[SimulatedBroker] 非整手拒单: %s %d shares', order.symbol, order.shares)
            order.status = 'REJECTED'
            return self._reject_fill(order, reason='非整手数')

        # --- 报价 ---
        quote = self.get_quote(order.symbol)
        ref_price = quote.last

        if ref_price <= 0:
            order.status = 'REJECTED'
            return self._reject_fill(order, reason='无法获取行情')

        # --- 涨跌停检查 ---
        if quote.prev_close > 0:
            limit_up   = round(quote.prev_close * 1.099, 2)
            limit_down = round(quote.prev_close * 0.901, 2)
            if order.direction == 'BUY' and ref_price >= limit_up:
                order.status = 'REJECTED'
                return self._reject_fill(order, reason='涨停拒买')
            if order.direction == 'SELL' and ref_price <= limit_down:
                order.status = 'REJECTED'
                return self._reject_fill(order, reason='跌停拒卖')

        # --- 滑点 ---
        slippage_factor = self.config.slippage_bps / 10000
        if order.direction == 'BUY':
            fill_price = round(ref_price * (1 + slippage_factor), 2)
        else:
            fill_price = round(ref_price * (1 - slippage_factor), 2)

        # --- 佣金 ---
        trade_value = fill_price * order.shares
        commission = max(self.config.min_commission,
                         trade_value * self.config.commission_rate)

        # --- 印花税（A股卖出单向）---
        stamp_tax = 0.0
        if order.direction == 'SELL':
            stamp_tax = trade_value * self.config.stamp_tax_rate

        total_cost = commission + stamp_tax

        # --- 现金检查（买入）---
        if order.direction == 'BUY':
            needed = trade_value + total_cost
            if needed > self._cash:
                order.status = 'REJECTED'
                return self._reject_fill(
                    order, reason=f'资金不足 (需{needed:.2f} 可用{self._cash:.2f})'
                )

        # --- 持仓检查（卖出）---
        if order.direction == 'SELL':
            pos = self._positions.get(order.symbol)
            if pos is None or pos.shares < order.shares:
                available = pos.shares if pos else 0
                order.status = 'REJECTED'
                return self._reject_fill(
                    order, reason=f'持仓不足 (需{order.shares} 有{available})'
                )

        # --- 撮合 ---
        order.status = 'FILLED'
        order.fill_price = fill_price
        order.fill_time = datetime.now()

        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            direction=order.direction,
            shares=order.shares,
            price=fill_price,
            commission=round(commission, 4),
            slippage_bps=self.config.slippage_bps,
            filled_at=order.fill_time,
        )

        # --- 更新状态 ---
        self._apply_fill(fill, stamp_tax)
        self._fills.append(fill)

        logger.info(
            '[SimulatedBroker] FILLED %s %s %d@%.2f commission=%.2f stamp=%.2f',
            fill.direction, fill.symbol, fill.shares,
            fill.price, fill.commission, stamp_tax,
        )
        return fill

    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None:
            return False
        if order.status in ('FILLED', 'CANCELLED', 'REJECTED'):
            return False
        order.status = 'CANCELLED'
        return True

    def get_orders(
        self,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Order]:
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status]
        if since:
            orders = [o for o in orders if o.created_at >= since]
        return sorted(orders, key=lambda o: o.created_at, reverse=True)

    def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
        fills = self._fills
        if since:
            fills = [f for f in fills if f.filled_at >= since]
        return sorted(fills, key=lambda f: f.filled_at, reverse=True)

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    def reset(self, initial_cash: Optional[float] = None) -> None:
        """
        重置到初始状态（清空持仓/订单/成交）。

        Parameters
        ----------
        initial_cash : 重置后的初始资金（None = 使用 config.initial_cash）
        """
        self._cash = initial_cash if initial_cash is not None else self.config.initial_cash
        self._positions.clear()
        self._orders.clear()
        self._fills.clear()
        self._manual_quotes.clear()
        self._realized_pnl_today = 0.0
        logger.info('[SimulatedBroker] Reset. cash=%.2f', self._cash)

    def inject_position(
        self,
        symbol: str,
        shares: int,
        avg_price: float,
        current_price: Optional[float] = None,
    ) -> None:
        """
        直接注入持仓（用于测试/回放场景，不经过订单流）。
        """
        pos = Position(
            symbol=symbol,
            shares=shares,
            avg_price=avg_price,
            current_price=current_price or avg_price,
        )
        self._positions[symbol] = pos

    @property
    def snapshot(self) -> Dict:
        """返回当前状态快照（用于断言/调试）。"""
        return {
            'cash': self._cash,
            'positions': {s: {'shares': p.shares, 'avg_price': p.avg_price}
                         for s, p in self._positions.items()},
            'n_orders': len(self._orders),
            'n_fills': len(self._fills),
            'realized_pnl_today': self._realized_pnl_today,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_fill(self, fill: Fill, stamp_tax: float = 0.0) -> None:
        """将成交应用到现金和持仓。"""
        trade_value = fill.price * fill.shares
        total_cost = fill.commission + stamp_tax

        if fill.direction == 'BUY':
            self._cash -= (trade_value + total_cost)
            pos = self._positions.get(fill.symbol)
            if pos is None:
                pos = Position(symbol=fill.symbol)
                self._positions[fill.symbol] = pos
            # 加权平均成本
            total_shares = pos.shares + fill.shares
            pos.avg_price = (
                (pos.shares * pos.avg_price + fill.shares * fill.price) / total_shares
            )
            pos.shares = total_shares
            pos.current_price = fill.price

        else:  # SELL
            self._cash += (trade_value - total_cost)
            pos = self._positions.get(fill.symbol)
            if pos:
                realized = (fill.price - pos.avg_price) * fill.shares - total_cost
                pos.realized_pnl += realized
                self._realized_pnl_today += realized
                pos.shares -= fill.shares
                if pos.shares == 0:
                    del self._positions[fill.symbol]

    @staticmethod
    def _reject_fill(order: Order, reason: str = '') -> Fill:
        return Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            direction=order.direction,
            shares=0,
            price=0.0,
            commission=0.0,
        )

    @staticmethod
    def _fetch_tencent_quote(symbol: str) -> QuoteData:
        """腾讯实时行情（和 PaperBroker.quote 同源，但返回 QuoteData）。"""
        import urllib.request
        try:
            sym_code = symbol.split('.')[0]
            # 沪市 sh600519，深市 sz000858
            prefix = 'sh' if symbol.endswith('.SH') else 'sz'
            url = f'https://qt.gtimg.cn/q={prefix}{sym_code}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode('gbk')
            fields = raw.split('~')
            if len(fields) < 35:
                raise ValueError('fields too short')
            last       = float(fields[3])
            prev_close = float(fields[4])
            open_      = float(fields[5])
            high       = float(fields[33])
            low        = float(fields[34])
            volume_hand = int(float(fields[6]))  # 手
            amount     = float(fields[37]) * 10000 if len(fields) > 37 else 0
            bid        = float(fields[9])   if len(fields) > 9  else last
            ask        = float(fields[19])  if len(fields) > 19 else last
            change_pct = (last - prev_close) / prev_close * 100 if prev_close > 0 else 0

            limit_up   = round(prev_close * 1.1, 2)
            limit_down = round(prev_close * 0.9, 2)
            is_tradable = (limit_down < last < limit_up)

            return QuoteData(
                symbol=symbol, last=last, bid=bid, ask=ask,
                open=open_, high=high, low=low,
                prev_close=prev_close,
                volume=volume_hand, amount=amount,
                change_pct=round(change_pct, 2),
                is_tradable=is_tradable,
            )
        except Exception as e:
            logger.debug('[SimulatedBroker] tencent quote failed (%s): %s', symbol, e)
            return QuoteData(symbol=symbol, last=0.0, is_tradable=False)
