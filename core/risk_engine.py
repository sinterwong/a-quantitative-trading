"""
core/risk_engine.py — 风控引擎

三层风控：
  1. PreTrade: 仓位限制 / 净暴露 / 单标的仓位上限 / 日亏损熔断
  2. InTrade: 实时止损检查（Chandelier Exit / RSI / ATR trailing stop）
  3. PostTrade: 成交记录 / 绩效归因

EventBus 集成：
  - 监听 FillEvent → 更新持仓账本
  - 发射 RiskEvent / AlertEvent
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Literal, Optional, Any, Callable
import threading
import time
import os, sys

import pandas as pd
import numpy as np

# ─── RiskResult ───────────────────────────────────────────────────────────────

@dataclass
class RiskResult:
    """风控检查结果"""
    passed: bool = True
    level: Literal['OK', 'WARN', 'CRITICAL', 'REJECT'] = 'OK'
    reason: str = ''
    details: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls) -> 'RiskResult':
        return cls(passed=True, level='OK')

    @classmethod
    def warn(cls, reason: str, **kwargs) -> 'RiskResult':
        return cls(passed=True, level='WARN', reason=reason, details=kwargs)

    @classmethod
    def reject(cls, reason: str, **kwargs) -> 'RiskResult':
        return cls(passed=False, level='REJECT', reason=reason, details=kwargs)

    @classmethod
    def critical(cls, reason: str, **kwargs) -> 'RiskResult':
        return cls(passed=False, level='CRITICAL', reason=reason, details=kwargs)


# ─── Position Book ────────────────────────────────────────────────────────────

@dataclass
class RiskPosition:
    """风控持仓（扩展信息）"""
    symbol: str
    shares: int = 0
    avg_price: float = 0
    current_price: float = 0
    entry_date: date = field(default_factory=date.today)

    # 风险指标
    entry_high: float = 0           # 买入后最高价（用于 Chandelier Exit）
    atr_14: float = 0               # 14日 ATR
    rsi_14: float = 50              # 当前 RSI

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.shares

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.avg_price == 0 or self.shares == 0:
            return 0
        return (self.current_price - self.avg_price) / self.avg_price

    def update_price(self, price: float, atr: float = 0, rsi: float = 50):
        self.current_price = price
        self.atr_14 = atr
        self.rsi_14 = rsi
        if price > self.entry_high:
            self.entry_high = price


class PositionBook:
    """持仓账本（内存）"""

    def __init__(self):
        self._positions: Dict[str, RiskPosition] = {}
        self._equity: float = 100000
        self._cash: float = 100000
        self._peak_equity: float = 100000
        self._today_pnl: float = 0
        self._peak_trade_date: str = ''
        self._load_from_api()

    def _load_from_api(self):
        try:
            import urllib.request, json
            base = 'http://127.0.0.1:5555'
            with urllib.request.urlopen(f'{base}/portfolio/summary', timeout=5) as r:
                s = json.loads(r.read())
            self._equity = s.get('position_value', 0) + s.get('cash', 0)
            self._cash = s.get('cash', 0)
            self._peak_equity = self._equity
            self._positions.clear()
            with urllib.request.urlopen(f'{base}/positions', timeout=5) as r:
                pos_data = json.loads(r.read())
            for p in pos_data.get('positions', []):
                sym = p['symbol']
                self._positions[sym] = RiskPosition(
                    symbol=sym,
                    shares=int(p.get('shares', 0)),
                    avg_price=float(p.get('avg_price', 0)),
                    current_price=float(p.get('current_price', 0)),
                )
        except Exception as e:
            print(f"[PositionBook] load error: {e}")

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def cash(self) -> float:
        return self._cash

    def position_pct(self, symbol: str) -> float:
        """持仓占总权益比例"""
        if self._equity == 0:
            return 0
        pos = self._positions.get(symbol)
        if not pos:
            return 0
        return (pos.shares * pos.current_price) / self._equity

    def total_exposure(self) -> float:
        """总净暴露比例"""
        if self._equity == 0:
            return 0
        total_mv = sum(p.shares * p.current_price for p in self._positions.values())
        return total_mv / self._equity

    def update_equity(self, equity: float):
        self._equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def update_cash(self, cash: float):
        self._cash = cash

    def update_position_price(self, symbol: str, price: float, atr: float = 0, rsi: float = 50):
        if symbol in self._positions:
            self._positions[symbol].update_price(price, atr, rsi)

    def add_position(self, fill):
        sym = fill.symbol
        if sym in self._positions:
            p = self._positions[sym]
            total = p.shares * p.avg_price + fill.shares * fill.price
            p.shares += fill.shares
            p.avg_price = total / p.shares if p.shares > 0 else 0
            p.current_price = fill.price
            p.entry_high = max(p.entry_high, fill.price)
        else:
            self._positions[sym] = RiskPosition(
                symbol=sym,
                shares=fill.shares,
                avg_price=fill.price,
                current_price=fill.price,
                entry_high=fill.price,
            )

    def reduce_position(self, fill):
        sym = fill.symbol
        if sym in self._positions:
            p = self._positions[sym]
            realized = (fill.price - p.avg_price) * fill.shares
            p.shares -= fill.shares
            if p.shares <= 0:
                del self._positions[sym]

    def get_all(self) -> Dict[str, RiskPosition]:
        return dict(self._positions)

    def get(self, symbol: str) -> Optional[RiskPosition]:
        return self._positions.get(symbol)


# ─── RiskEngine ───────────────────────────────────────────────────────────────

class RiskEngine:
    """
    三层风控引擎。

    PreTrade（下单前）:
      - check_position_limit()  — 单标的 ≤ 25%
      - check_net_exposure()     — 总净暴露 ≤ 90%
      - check_concentration()    — 行业集中度 ≤ 30%
      - check_loss_limit()       — 日亏损熔断

    InTrade（持仓中）:
      - check_atr_stop()        — Chandelier Exit（3×ATR）
      - check_rsi_exit()        — RSI 超买/超卖退出
      - check_take_profit()     — 固定/移动止盈

    PostTrade（成交后）:
      - on_fill()               — 更新账本 + 发射事件
    """

    def __init__(self):
        self.book = PositionBook()
        self.bus = None
        self._stop_check_active = False
        self._stop_thread: Optional[threading.Thread] = None

    def set_bus(self, bus):
        self.bus = bus
        from core.event_bus import FillEvent
        bus.on('FillEvent', self._on_fill)

    # ── PreTrade ─────────────────────────────────────────────────────────────

    def check(self, signal, order=None) -> RiskResult:
        """
        综合 PreTrade 检查。
        返回 RiskResult，passed=False 时 OMS 拒绝下单。
        """
        checks = [
            self.check_position_limit(signal.symbol),
            self.check_loss_limit(),
            self.check_net_exposure(),
        ]
        failed = [c for c in checks if not c.passed]
        if failed:
            return failed[0]
        return RiskResult.ok()

    def check_position_limit(self, symbol: str, limit: float = 0.25) -> RiskResult:
        """单标的持仓 ≤ limit"""
        pct = self.book.position_pct(symbol)
        if pct > limit:
            return RiskResult.reject(
                f'Position {symbol} {pct*100:.1f}% > {limit*100:.0f}% limit',
                position_pct=pct, limit=limit
            )
        if pct > limit * 0.8:
            return RiskResult.warn(
                f'Position {symbol} {pct*100:.1f}% near limit',
                position_pct=pct, limit=limit
            )
        return RiskResult.ok()

    def check_net_exposure(self, limit: float = 0.90) -> RiskResult:
        """总净暴露 ≤ limit"""
        exp = self.book.total_exposure()
        if exp > limit:
            return RiskResult.reject(
                f'Total exposure {exp*100:.1f}% > {limit*100:.0f}%',
                exposure=exp
            )
        return RiskResult.ok()

    def check_concentration(self, symbol: str, limit: float = 0.30) -> RiskResult:
        """行业集中度 ≤ limit（预留）"""
        # 未来接入 sector_map.json
        return RiskResult.ok()

    def check_loss_limit(self, daily_limit: float = 0.02) -> RiskResult:
        """日亏损熔断：当日亏损超 2% → 禁止开新仓"""
        today = date.today().isoformat()
        if self.book._peak_trade_date != today:
            self.book._today_pnl = 0
            self.book._peak_trade_date = today

        if self.book._equity < self.book._peak_equity * (1 - daily_limit):
            return RiskResult.reject(
                f'Daily loss {daily_limit*100:.0f}% exceeded',
                equity=self.book._equity,
                peak=self.book._peak_equity,
                loss_pct=(1 - self.book._equity / self.book._peak_equity)
            )
        return RiskResult.ok()

    # ── InTrade ──────────────────────────────────────────────────────────────

    def check_atr_stop(self, symbol: str, multiplier: float = 3.0) -> Optional[RiskResult]:
        """
        Chandelier Exit：最高价 - multiplier × ATR
        持仓从最高价回撤超过 multiplier×ATR → 触发止损
        """
        pos = self.book.get(symbol)
        if not pos or pos.shares == 0:
            return None

        if pos.entry_high == 0 or pos.atr_14 == 0:
            return None

        stop_price = pos.entry_high - multiplier * pos.atr_14
        current = pos.current_price

        if current <= stop_price:
            return RiskResult.reject(
                f'ATR stop triggered: {current:.2f} <= {stop_price:.2f} (high={pos.entry_high}, atr={pos.atr_14})',
                symbol=symbol,
                stop_price=stop_price,
                current_price=current,
                atr=pos.atr_14,
                entry_high=pos.entry_high,
            )
        return None

    def check_rsi_exit(self, symbol: str, rsi_sell: float = 65) -> Optional[RiskResult]:
        """RSI 超买 → 平仓信号"""
        pos = self.book.get(symbol)
        if not pos or pos.shares == 0:
            return None
        if pos.rsi_14 > rsi_sell:
            return RiskResult.warn(
                f'RSI {pos.rsi_14:.1f} > {rsi_sell} overbought',
                symbol=symbol,
                rsi=pos.rsi_14,
            )
        return None

    def check_take_profit(
        self,
        symbol: str,
        fixed_pct: float = 0.20,
        trailing: bool = True,
    ) -> Optional[RiskResult]:
        """
        止盈检查。
        - fixed_pct: 固定止盈（20%）
        - trailing: 移动止盈（从最高价回撤 > 10% 则止盈）
        """
        pos = self.book.get(symbol)
        if not pos or pos.shares == 0:
            return None

        # 固定止盈
        if pos.unrealized_pnl_pct >= fixed_pct:
            # 触发移动止盈：最高价回撤 > 10%
            if trailing:
                if pos.entry_high > 0:
                    drawdown = (pos.entry_high - pos.current_price) / pos.entry_high
                    if drawdown > 0.10:
                        return RiskResult.warn(
                            f'Trailing stop triggered: +{pos.unrealized_pnl_pct*100:.1f}% profit, {drawdown*100:.1f}% drawdown',
                            symbol=symbol,
                            pnl_pct=pos.unrealized_pnl_pct,
                            drawdown=drawdown,
                        )

        return None

    def check_all_exits(self, symbol: str) -> List[RiskResult]:
        """检查所有退出条件，返回所有触发项"""
        results = []
        r = self.check_atr_stop(symbol)
        if r:
            results.append(r)
        r = self.check_rsi_exit(symbol)
        if r and r.level == 'WARN':
            results.append(r)
        r = self.check_take_profit(symbol)
        if r:
            results.append(r)
        return results

    # ── InTrade Monitoring Loop ─────────────────────────────────────────────

    def start_monitoring(self, interval: int = 60, symbols: List[str] = None):
        """启动持仓监控线程（每 interval 秒检查一次）"""
        if self._stop_check_active:
            return
        self._stop_check_active = True
        self._symbols = symbols or []

        def loop():
            while self._stop_check_active:
                try:
                    self._monitor_cycle()
                except Exception as e:
                    print(f"[RiskEngine] monitor error: {e}")
                time.sleep(interval)

        self._stop_thread = threading.Thread(target=loop, daemon=True)
        self._stop_thread.start()

    def _monitor_cycle(self):
        """一次监控循环：更新价格 + 检查退出"""
        for sym, pos in list(self.book.get_all().items()):
            if pos.shares == 0:
                continue
            try:
                price, atr, rsi = self._fetch_risk_data(sym)
                self.book.update_position_price(sym, price, atr, rsi)

                exits = self.check_all_exits(sym)
                for exit_r in exits:
                    if not exit_r.passed or exit_r.level == 'WARN':
                        self._emit_risk_event(exit_r, sym)
            except Exception as e:
                print(f"[RiskEngine] cycle error for {sym}: {e}")

    def _fetch_risk_data(self, symbol: str) -> tuple:
        """获取当前价格 + ATR + RSI"""
        try:
            import urllib.request
            sym = symbol.replace('.SH', '').replace('.SZ', '')
            url = f'https://qt.gtimg.cn/q={sym}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode('gbk')
            fields = raw.split('~')
            price = float(fields[3]) if len(fields) > 3 else 0
            # ATR/RSI 需历史数据估算（简化：使用 price 波动率）
            atr = price * 0.015  # 估算 1.5% ATR
            rsi = 50.0  # 简化，未接入
            return price, atr, rsi
        except Exception:
            return 0, 0, 50

    def _emit_risk_event(self, result: RiskResult, symbol: str):
        if self.bus:
            from core.event_bus import RiskEvent, AlertEvent
            self.bus.emit(RiskEvent(
                level=result.level,
                symbol=symbol,
                reason=result.reason,
                detail=result.details,
            ))
            if result.level in ('WARN', 'CRITICAL'):
                self.bus.emit(AlertEvent(
                    level='WARN',
                    title=f'风控预警 {symbol}',
                    message=f'{result.reason}',
                    channel='feishu',
                ))

    def stop_monitoring(self):
        self._stop_check_active = False

    # ── PostTrade ───────────────────────────────────────────────────────────

    def _on_fill(self, event):
        """成交回报 → 更新账本"""
        fill = event
        self.book.update_equity(
            self.book._equity + (fill.price * fill.shares if fill.direction == 'BUY' else 0)
        )
        if fill.direction == 'BUY':
            self.book.add_position(fill)
        else:
            self.book.reduce_position(fill)
