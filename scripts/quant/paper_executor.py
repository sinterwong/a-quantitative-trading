"""
PaperExecutor - Simulated Execution Engine
"""
import os
import sys

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)


class FillType:
    VWAP = "vwap"
    CLOSE = "close"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    PENDING = "pending"
    REJECTED = "rejected"


class SlippageModel:
    def __init__(self, base_rate=0.0005, volume_sensitivity=0.3,
                 limit_up_penalty=0.003, limit_down_bonus=-0.001):
        self.base_rate = base_rate
        self.volume_sensitivity = volume_sensitivity
        self.limit_up_penalty = limit_up_penalty
        self.limit_down_bonus = limit_down_bonus

    def calc(self, direction, price, shares, daily_volume, turnover, limit_pct):
        trade_value = shares * price
        volume_ratio = min(trade_value / turnover, 1.0) if turnover > 0 else 1.0
        volume_factor = self.volume_sensitivity * volume_ratio
        if limit_pct > 0.095:
            limit_adj = self.limit_up_penalty
        elif limit_pct < -0.095:
            limit_adj = self.limit_down_bonus
        else:
            limit_adj = 0.0
        slip = self.base_rate * (1.0 + volume_factor) + limit_adj
        return max(slip, 0.0) if direction == "buy" else min(slip, 0.0)


class MarketSnapshot:
    def __init__(self, date, symbol, open_p, high, low, close, prev_close, volume, turnover):
        self.date = date
        self.symbol = symbol
        self.open = open_p
        self.high = high
        self.low = low
        self.close = close
        self.prev_close = prev_close
        self.change_pct = (close - prev_close) / prev_close if prev_close else 0.0
        self.is_limit_up = self.change_pct > 0.095
        self.is_limit_down = self.change_pct < -0.095
        self.is_suspended = volume == 0
        self.vwap = turnover / volume if volume > 0 else close
        self.volume = volume
        self.turnover = turnover


class SignalOrder:
    def __init__(self, order_id, symbol, direction, price,
                 shares_requested, reason, signal_strength=0.0, resonance=False):
        self.order_id = order_id
        self.symbol = symbol
        self.direction = direction
        self.price = price
        self.shares_requested = shares_requested
        self.shares_filled = 0
        self.shares_pending = shares_requested
        self.reason = reason
        self.signal_strength = signal_strength
        self.resonance = resonance
        self.signal_price = price
        self.exec_price = None
        self.exec_type = None
        self.slippage_pct = None
        self.cost = None
        self.pnl = None
        self.pnl_pct = None
        self.status = "pending"
        self.date = None


class PaperExecutor:
    def __init__(self, initial_capital=3000000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.slippage_model = SlippageModel()
        self.commission_rate = 0.0003
        self.stamp_tax_rate = 0.001
        self.pending_orders = []
        self.filled_orders = []
        self.rejected_orders = []
        self.order_counter = 0
        self.current_date = None

    def inject_signals(self, signals):
        for sig in signals:
            self.order_counter += 1
            order = SignalOrder(
                order_id="O%d" % self.order_counter,
                symbol=sig["symbol"],
                direction=sig["direction"],
                price=sig["price"],
                shares_requested=sig.get("shares", 0),
                reason=sig.get("reason", ""),
                signal_strength=sig.get("signal_strength", 0.0),
                resonance=sig.get("resonance", False)
            )
            self.pending_orders.append(order)

    def inject_market_snapshots(self, snapshots):
        self.market_snapshots = snapshots

    def process_pending(self, date):
        self.current_date = date
        self._process_all()

    def get_fills(self):
        return self.filled_orders

    def get_pending(self):
        return [o for o in self.pending_orders if o.status == "pending"]

    def get_rejected(self):
        return self.rejected_orders

    def get_trade_log(self, date):
        logs = []
        all_orders = self.filled_orders + self.rejected_orders
        for o in all_orders:
            if o.status in ("filled", "rejected"):
                log = {
                    "trade_id": o.order_id,
                    "symbol": o.symbol,
                    "direction": o.direction,
                    "price": round(o.exec_price, 3) if o.exec_price else None,
                    "shares": o.shares_filled,
                    "cost": round(o.cost, 2) if o.cost else None,
                    "reason": o.reason,
                    "execution_type": o.exec_type,
                    "signal_price": round(o.signal_price, 3) if o.signal_price else None,
                    "slippage_pct": round(o.slippage_pct, 4) if o.slippage_pct else None,
                    "pnl": round(o.pnl, 2) if o.pnl else None,
                    "pnl_pct": round(o.pnl_pct, 4) if o.pnl_pct else None,
                    "status": o.status,
                }
                logs.append(log)
        return logs

    def get_position_value(self):
        total = 0.0
        for sym, pos in self.positions.items():
            if pos["shares"] > 0 and sym in self.market_snapshots:
                total += pos["shares"] * self.market_snapshots[sym].close
        return total

    def get_account_status(self):
        pv = self.get_position_value()
        return {
            "cash": round(self.cash, 2),
            "position_value": round(pv, 2),
            "total_value": round(self.cash + pv, 2),
            "positions": {sym: dict(pos) for sym, pos in self.positions.items() if pos["shares"] > 0}
        }

    def _process_all(self):
        to_remove = []
        for order in self.pending_orders:
            if order.status != "pending":
                continue
            snap = self.market_snapshots.get(order.symbol)
            if snap is None:
                self._do_reject(order, "no_market_data")
                to_remove.append(order)
                continue
            if snap.is_suspended:
                self._do_reject(order, "suspended")
                to_remove.append(order)
                continue
            if order.direction == "buy" and snap.is_limit_up:
                exec_price = snap.vwap * 1.003
                shares = self._calc_buyable(order, snap, exec_price)
                if shares == 0:
                    self._do_reject(order, "limit_up")
                    to_remove.append(order)
                    continue
                self._do_exec_buy(order, snap, exec_price, shares)
                to_remove.append(order)
                continue
            if order.direction == "sell" and snap.is_limit_down:
                self._do_reject(order, "limit_down")
                to_remove.append(order)
                continue
            if order.direction == "buy":
                slip = self.slippage_model.calc("buy", snap.vwap,
                    order.shares_requested, snap.volume, snap.turnover, snap.change_pct)
                exec_price = snap.vwap * (1.0 + slip)
                shares = self._calc_buyable(order, snap, exec_price)
                if shares == 0:
                    self._do_reject(order, "insufficient_cash")
                    to_remove.append(order)
                    continue
                self._do_exec_buy(order, snap, exec_price, shares)
                to_remove.append(order)
            elif order.direction == "sell":
                slip = self.slippage_model.calc("sell", snap.vwap,
                    order.shares_requested, snap.volume, snap.turnover, snap.change_pct)
                exec_price = snap.vwap * (1.0 + slip)
                pos = self.positions.get(order.symbol)
                avail = pos["shares"] if pos else 0
                shares = min(order.shares_requested, avail)
                if shares == 0:
                    self._do_reject(order, "no_position")
                    to_remove.append(order)
                    continue
                self._do_exec_sell(order, snap, exec_price, shares)
                to_remove.append(order)
        for o in to_remove:
            if o in self.pending_orders:
                self.pending_orders.remove(o)

    def _calc_buyable(self, order, snap, exec_price):
        max_by_cash = int(self.cash * 0.97 / (exec_price * (1.0 + self.commission_rate)))
        max_by_pos = int(self.initial_capital * 0.30 / exec_price)
        return min(order.shares_requested, max_by_cash, max_by_pos)

    def _do_exec_buy(self, order, snap, exec_price, shares):
        gross = shares * exec_price
        commission = gross * self.commission_rate
        total_cost = gross + commission
        slip = (exec_price - snap.vwap) / snap.vwap
        if shares > 0:
            if order.symbol not in self.positions:
                self.positions[order.symbol] = {"shares": 0, "avg_cost": 0.0}
            pos = self.positions[order.symbol]
            old_val = pos["shares"] * pos["avg_cost"]
            new_val = shares * exec_price
            total_shares = pos["shares"] + shares
            pos["shares"] = total_shares
            pos["avg_cost"] = (old_val + new_val) / total_shares if total_shares > 0 else 0.0
            self.cash -= total_cost
        order.shares_filled = shares
        order.shares_pending = order.shares_requested - shares
        order.exec_price = exec_price
        order.exec_type = FillType.VWAP
        order.slippage_pct = slip
        order.cost = total_cost
        order.status = "filled"
        order.date = self.current_date
        self.filled_orders.append(order)

    def _do_exec_sell(self, order, snap, exec_price, shares):
        gross = shares * exec_price
        commission = gross * self.commission_rate
        stamp = gross * self.stamp_tax_rate
        net = gross - commission - stamp
        slip = (snap.vwap - exec_price) / snap.vwap
        pos = self.positions.get(order.symbol)
        avg_cost = pos["avg_cost"] if pos else 0.0
        pnl = (exec_price - avg_cost) * shares if avg_cost > 0 else 0.0
        pnl_pct = (exec_price - avg_cost) / avg_cost if avg_cost > 0 else 0.0
        if order.symbol in self.positions:
            self.positions[order.symbol]["shares"] -= shares
        self.cash += net
        order.shares_filled = shares
        order.shares_pending = order.shares_requested - shares
        order.exec_price = exec_price
        order.exec_type = FillType.VWAP
        order.slippage_pct = slip
        order.cost = commission + stamp
        order.pnl = pnl
        order.pnl_pct = pnl_pct
        order.status = "filled"
        order.date = self.current_date
        self.filled_orders.append(order)

    def _do_reject(self, order, reason):
        order.status = "rejected"
        order.exec_type = FillType.REJECTED
        order.date = self.current_date
        self.rejected_orders.append(order)


if __name__ == "__main__":
    print("=" * 60)
    print("PaperExecutor Test")
    print("=" * 60)

    executor = PaperExecutor(3000000.0)

    snapshots = {
        "600276.SH": MarketSnapshot(
            "2026-04-11", "600276.SH",
            55.0, 58.0, 54.5, 57.06, 55.50,
            88000000, 4960000000.0),
        "600519.SH": MarketSnapshot(
            "2026-04-11", "600519.SH",
            1600.0, 1650.0, 1590.0, 1453.96, 1620.0,
            30000000, 45000000000.0),
        "300750.SZ": MarketSnapshot(
            "2026-04-11", "300750.SZ",
            415.0, 420.0, 410.0, 416.0, 410.0,
            50000000, 20700000000.0),
    }
    executor.inject_market_snapshots(snapshots)

    signals = [
        {"symbol": "600276.SH", "direction": "buy",
         "price": 56.50, "shares": 8000,
         "reason": "rsi_oversold", "signal_strength": 0.9, "resonance": True},
        {"symbol": "600519.SH", "direction": "sell",
         "price": 1453.96, "shares": 300,
         "reason": "stop_loss", "signal_strength": 1.0, "resonance": False},
        {"symbol": "300750.SZ", "direction": "buy",
         "price": 415.0, "shares": 5000,
         "reason": "rsi_oversold", "signal_strength": 0.8, "resonance": False},
    ]
    executor.inject_signals(signals)
    executor.process_pending("2026-04-11")

    print("\n[Trade Log]")
    for t in executor.get_trade_log("2026-04-11"):
        dc = {"buy": "BUY", "sell": "SELL"}.get(t["direction"], t["direction"])
        slip_str = "slip=%.2f%%" % (t["slippage_pct"] * 100) if t.get("slippage_pct") else ""
        pnl_str = "pnl=%.0f(%.1f%%)" % (t["pnl"], t["pnl_pct"] * 100) if t.get("pnl") else ""
        print("  %-10s %-4s %s @%.2fx%d [%s] %s %s" % (
            t["execution_type"], dc, t["symbol"],
            t["price"], t["shares"], t["reason"], slip_str, pnl_str
        ))

    print("\n[Rejected]")
    for t in executor.get_rejected():
        print("  %s %s @%.2f [%s]" % (t.direction, t.symbol, t.price, t.status))

    status = executor.get_account_status()
    print("\n[Account]")
    print("  Cash:   %.0f" % status["cash"])
    print("  PosVal: %.0f" % status["position_value"])
    print("  Total:  %.0f" % status["total_value"])
    for sym, pos in status["positions"].items():
        print("  %s: %d shares, avg_cost=%.2f" % (sym, pos["shares"], pos["avg_cost"]))
