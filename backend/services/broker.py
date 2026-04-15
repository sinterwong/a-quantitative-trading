"""
broker.py — Broker abstraction layer
===================================
All order execution goes through this interface.

Phase 1: PaperBroker (no real money, simulates fills)
Phase 2: Swap in FutuBroker / TigerBroker / etc.

Interface contract (BrokerBase):
    connect()        — authenticate / open connection
    disconnect()     — clean shutdown
    get_positions() — current positions
    get_cash()      — available cash
    submit_order(symbol, direction, shares, price_type)
                    — submit order, return OrderResult
    cancel_order(order_id) — cancel a pending order
    is_market_open() — True during A-share trading hours

Order types:
    - market  — 市价单
    - limit  — 限价单

OrderResult fields:
    order_id   — broker's order ID
    status    — submitted | filled | partially_filled | cancelled | rejected
    filled_shares — number of shares actually filled
    avg_price      — average fill price
    submitted_at    — timestamp
    filled_at       — timestamp (if filled)
    reason          — rejection/cancel reason
"""

import os
import sys
import time
import random
import sqlite3
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(THIS_DIR)
PROJ_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, PROJ_DIR)

sys.path.insert(0, os.path.join(PROJ_DIR, 'scripts'))
from services.portfolio import PortfolioService

logger = logging.getLogger('broker')


# ============================================================
# Data classes
# ============================================================

@dataclass
class OrderResult:
    order_id: str
    status: str            # submitted | filled | cancelled | rejected
    symbol: str
    direction: str          # BUY | SELL
    submitted_shares: int
    filled_shares: int = 0
    avg_price: float = 0.0
    signal_price: float = 0.0   # 触发信号时的参考价
    slippage_bps: float = 0.0  # 滑点（基点）：正=贵买/贱卖，负=低价买/高价卖
    submitted_at: str = ''
    filled_at: str = ''
    reason: str = ''


@dataclass
class Position:
    symbol: str
    shares: int
    avg_cost: float
    direction: str = ''   # LONG | SHORT


# ============================================================
# Base interface
# ============================================================

class BrokerBase(ABC):
    """
    Abstract broker interface.
    Subclass this to implement Futu/Tiger/Other brokers.
    """

    def __init__(self, portfolio_service: PortfolioService):
        self.portfolio = portfolio_service

    @abstractmethod
    def connect(self) -> bool:
        """Authenticate and open connection. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self):
        """Clean shutdown."""
        ...

    @abstractmethod
    def is_market_open(self) -> bool:
        """True if A-share market is currently open."""
        ...

    @abstractmethod
    def get_cash(self) -> float:
        """Return available cash."""
        ...

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """Return current positions."""
        ...

    @abstractmethod
    def submit_order(self, symbol: str, direction: str, shares: int,
                     price: float = 0, price_type: str = 'market') -> OrderResult:
        """
        Submit an order.
        price_type: 'market' | 'limit'
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns True if cancelled."""
        ...


# ============================================================
# Paper Broker (Phase 1 implementation)
# ============================================================

class PaperBroker(BrokerBase):
    """
    Paper / simulated broker.
    Orders are filled at realistic VWAP with slippage.
    No real money. No real market access.
    """

    def __init__(self, portfolio_service: PortfolioService,
                 slippage_bps: int = 15):
        """
        slippage_bps: slippage in basis points (15 = 0.15%)
        """
        super().__init__(portfolio_service)
        self.slippage_bps = slippage_bps
        self._connected = False
        self._order_id_counter = 0
        # In-memory order book
        self._orders: List[OrderResult] = []
        # Backup DB path (set by test)
        self._db_backup: str = ''

    def connect(self) -> bool:
        logger.info('PaperBroker: connected (no-op)')
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False
        logger.info('PaperBroker: disconnected')

    def is_market_open(self) -> bool:
        """A-share trading hours: 9:30-11:30, 13:00-15:00 CST Mon-Fri"""
        now = datetime.now()
        wd = now.weekday()
        if wd >= 5:
            return False
        h, m = now.hour, now.minute
        total_min = h * 60 + m
        morning_open = 9 * 60 + 30   # 9:30
        morning_close = 11 * 60 + 30  # 11:30
        afternoon_open = 13 * 60      # 13:00
        afternoon_close = 15 * 60     # 15:00
        return (morning_open <= total_min <= morning_close or
                afternoon_open <= total_min <= afternoon_close)

    def get_cash(self) -> float:
        return self.portfolio.get_cash()

    def get_positions(self) -> List[Position]:
        raw = self.portfolio.get_positions()
        return [
            Position(
                symbol=p['symbol'],
                shares=p['shares'],
                avg_cost=p['entry_price'],
                direction='LONG'
            )
            for p in raw
        ]

    def _next_order_id(self) -> str:
        self._order_id_counter += 1
        return f'PAPER_{int(time.time()*1000)}_{self._order_id_counter}'

    def _simulate_fill(self, symbol: str, direction: str, shares: int,
                       price: float, price_type: str,
                       signal_price: float = 0.0) -> OrderResult:
        """
        Simulate order fill.
        price: market reference price at time of fill.
        signal_price: the signal trigger price (for slippage reference). If 0, use price.
        """
        order_id = self._next_order_id()
        now_str = datetime.now().isoformat()

        # Market reference price (before slippage)
        ref_price = price if price > 0 else self._fetch_market_price(symbol)
        # Use signal_price if provided, else use ref_price
        slip_ref = signal_price if signal_price > 0 else ref_price

        # ── Resolve fill price ──────────────────────────────────
        if price_type == 'market':
            slip = random.uniform(-self.slippage_bps, self.slippage_bps) / 10_000
            fill_price = round(ref_price * (1 + slip), 2)
        else:
            fill_price = ref_price  # limit order fills at ref_price

        # Compute actual slippage in bps (vs signal_price)
        if slip_ref > 0:
            slip_bps = (fill_price - slip_ref) / slip_ref * 10_000
        else:
            slip_bps = 0.0

        time.sleep(0.5)

        order = OrderResult(
            order_id=order_id,
            status='filled',
            symbol=symbol,
            direction=direction,
            submitted_shares=shares,
            filled_shares=shares,
            avg_price=fill_price,
            signal_price=slip_ref,
            slippage_bps=round(slip_bps, 2),
            submitted_at=now_str,
            filled_at=datetime.now().isoformat(),
        )
        self._orders.append(order)
        return order

    def _fetch_market_price(self, symbol: str) -> float:
        """Fetch latest price from Tencent Finance API."""
        try:
            import ssl, urllib.request
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            num, market = symbol.split('.', 1)
            qt = ('sh' if market == 'SH' else 'sz') + num
            url = f'https://qt.gtimg.cn/q={qt}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                raw = resp.read().decode('gbk', errors='replace')
            fields = raw.split('~')
            if len(fields) > 4:
                p = float(fields[3])
                if p > 0:
                    return p
        except Exception:
            pass
        return 0.0

    def submit_order(self, symbol: str, direction: str, shares: int,
                     price: float = 0, price_type: str = 'market') -> OrderResult:
        """
        Submit a paper order.
        Phase 1: immediately fills (market + limit).
        Phase 2: will call parent class for real broker.
        """
        if not self._connected:
            self.connect()

        # --- Risk checks (applied to both paper and real) ---
        cash = self.get_cash()
        cost = shares * (price or 0)

        if direction == 'BUY' and cost > cash:
            return OrderResult(
                order_id=self._next_order_id(),
                status='rejected',
                symbol=symbol,
                direction=direction,
                submitted_shares=shares,
                filled_shares=0,
                reason=f'Insufficient cash: need {cost:.2f}, have {cash:.2f}'
            )

        if direction == 'SELL':
            pos = self.portfolio.get_position(symbol)
            if not pos or pos['shares'] < shares:
                return OrderResult(
                    order_id=self._next_order_id(),
                    status='rejected',
                    symbol=symbol,
                    direction=direction,
                    submitted_shares=shares,
                    filled_shares=0,
                    reason=f'Insufficient shares to sell: have {pos["shares"] if pos else 0}, tried to sell {shares}'
                )

        # --- Execute fill ---
        # price param is the market reference price at submission time
        result = self._simulate_fill(symbol, direction, shares, price, price_type,
                                     signal_price=price)

        # --- Update portfolio ---
        if result.status == 'filled':
            fill_cost = result.filled_shares * result.avg_price
            if direction == 'BUY':
                self.portfolio.set_cash(cash - fill_cost)
                existing = self.portfolio.get_position(symbol)
                old_shares = existing['shares'] if existing else 0
                old_price = existing['entry_price'] if existing else 0
                new_shares = old_shares + shares
                new_avg = (old_shares * old_price + fill_cost) / new_shares
                self.portfolio.upsert_position(symbol, new_shares, round(new_avg, 2))
            else:  # SELL
                self.portfolio.set_cash(cash + fill_cost)
                remaining = (pos['shares'] - shares) if pos else 0
                if remaining <= 0:
                    self.portfolio.close_position(symbol)
                else:
                    self.portfolio.upsert_position(symbol, remaining, pos['entry_price'])

            # Record trade with slippage tracking
            pnl = None
            if direction == 'SELL' and pos:
                pnl = (result.avg_price - pos['entry_price']) * shares
            self.portfolio.record_trade(
                symbol, direction, shares, result.avg_price, pnl,
                slippage_bps=result.slippage_bps)

        logger.info('PaperBroker: order %s %s %d @ %.2f [slippage=%.1fbps] => %s',
                    direction, symbol, shares, result.avg_price, result.slippage_bps, result.status)
        return result

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order (paper broker: reject immediately)."""
        for order in self._orders:
            if order.order_id == order_id and order.status == 'submitted':
                order.status = 'cancelled'
                order.reason = 'Cancelled by user (paper broker)'
                logger.info('PaperBroker: order %s cancelled', order_id)
                return True
        return False

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        for o in self._orders:
            if o.order_id == order_id:
                return o
        return None


# ============================================================
# Stub for real broker (Phase 2)
# ============================================================

class FutuBroker(BrokerBase):
    """
    Phase 2: 富途 (Futu) broker implementation.
    To be implemented.
    """
    def connect(self) -> bool:
        raise NotImplementedError('FutuBroker: not yet implemented')

    def disconnect(self):
        raise NotImplementedError('FutuBroker: not yet implemented')

    def is_market_open(self) -> bool:
        raise NotImplementedError('FutuBroker: not yet implemented')

    def get_cash(self) -> float:
        raise NotImplementedError('FutuBroker: not yet implemented')

    def get_positions(self) -> List[Position]:
        raise NotImplementedError('FutuBroker: not yet implemented')

    def submit_order(self, symbol: str, direction: str, shares: int,
                     price: float = 0, price_type: str = 'market') -> OrderResult:
        raise NotImplementedError('FutuBroker: not yet implemented')

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError('FutuBroker: not yet implemented')
