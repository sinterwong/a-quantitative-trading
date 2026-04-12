"""
PortfolioEngine v3 - SignalGenerator集成版
============================================
核心变化:
1. SignalGeneratorStrategy: 把SignalGenerator适配成PortfolioEngine的策略接口
2. BlackListFilter: 黑名单过滤（涨停、停牌、流动性）
3. 统一信号格式

使用:
    from signal_generator import SignalGenerator, RSISignalSource, MACDSignalSource

    engine = PortfolioEngineV3(3000000)
    engine.add_strategy('300750.SZ', 'RSI+MACD', {
        'sources': [
            ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'take_profit': 0.30}, 1.0),
            ('MACDSignalSource', {'stop_loss': 0.08, 'take_profit': 0.25}, 0.8),
        ]
    })
    engine.run('20200101', '20251231')
    engine.print_summary()
"""

import os
import sys

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader
from backtest import TechnicalIndicators as TI
from signal_generator import (
    SignalGenerator, RSISignalSource, MACDSignalSource,
    BollingerBandSource, InstitutionalSignalSource,
    MarketRegimeSource, SignalType, BlackListFilter
)
from data_provider import DataProvider, HistoricalDataProvider, LiveDataProvider
import numpy as np


# ============================================================
# SignalGenerator策略适配器
# ============================================================

class SignalGeneratorStrategy:
    """
    把SignalGenerator适配成PortfolioEngine的策略接口

    持仓状态完全由SignalGenerator内部管理
    （通过_source_instances访问各信号源的状态）
    """

    def __init__(self, symbol, config):
        """
        config = {
            'sources': [
                ('RSISignalSource', {params}, weight),
                ('MACDSignalSource', {params}, weight),
                ...
            ],
            'blacklist_enabled': True,
        }
        """
        self.symbol = symbol
        self.config = config
        self.name = f"SignalGen({symbol})"

        # 创建SignalGenerator
        self.gen = SignalGenerator(symbol)
        for src_name, params, weight in config.get('sources', []):
            from signal_generator import (
                RSISignalSource, MACDSignalSource, BollingerBandSource,
                InstitutionalSignalSource, MarketRegimeSource
            )
            SOURCE_MAP = {
                'RSISignalSource': RSISignalSource,
                'MACDSignalSource': MACDSignalSource,
                'BollingerBandSource': BollingerBandSource,
                'InstitutionalSignalSource': InstitutionalSignalSource,
                'MarketRegimeSource': MarketRegimeSource,
            }
            src_class = SOURCE_MAP.get(src_name)
            if src_class:
                self.gen.add_source(src_class, params or {}, weight)

        self.blacklist = BlackListFilter() if config.get('blacklist_enabled', True) else None
        self.data = []
        self._loaded = False

    def load_data(self, data_loader, start, end):
        self.gen.load_all(data_loader, start, end)
        # 复用第一个源的数据
        first_src = self.gen.sources[0][0] if self.gen.sources else None
        self.data = first_src.data if first_src else []
        self._loaded = bool(self.data)
        return self._loaded


    def load_data_with_provider(self, provider, start, end):
        class _LoaderAdapter:
            def __init__(self, prov, sym):
                self._prov = prov
                self._sym = sym
            def get_kline(self, sym, s, e):
                return self._prov.get_kline(sym, s, e)
        adapter = _LoaderAdapter(provider, self.symbol)
        for src, _ in self.gen.sources:
            src.load(adapter, start, end)
        first = self.gen.sources[0][0] if self.gen.sources else None
        self.data = first.data if first else []
        self._loaded = bool(self.data)
        return self._loaded



    def reset(self):
        self.gen.reset_all()

    def generate_signal(self, i) -> dict:
        """
        返回适配后的信号格式:
        {
            'direction': 'buy'/'sell'/'hold',
            'strength': 0.0-1.0,
            'reason': str,
            'data': raw_signal_generator_result
        }
        """
        if not self._loaded or i < 0:
            return {'direction': 'hold', 'strength': 0.0, 'reason': 'not_loaded'}

        # 黑名单过滤
        if self.blacklist and i >= 1:
            allowed, reason = self.blacklist.can_buy(self.data, i)
            if not allowed:
                return {'direction': 'hold', 'strength': 0.0, 'reason': f'blacklist:{reason}'}

        # SignalGenerator评估
        raw = self.gen.evaluate(i)
        direction = raw['signal']  # already 'buy'/'sell'/'hold'
        strength = raw['strength']
        reason = raw['reason']

        # 强制过滤: bear市场不开新仓
        for src, _ in self.gen.sources:
            if hasattr(src, 'name') and src.name == 'MarketRegime':
                regime_result = src.evaluate(i)
                regime = regime_result.get('meta', {}).get('regime', 'bull')
                if regime == 'bear' and direction == 'buy':
                    return {'direction': 'hold', 'strength': 0.0,
                            'reason': f'market_bear({regime_result.get("meta", {}).get("pct_above", 0):+.1f}%)'}

        return {
            'direction': direction,
            'strength': strength,
            'reason': reason,
            'data': raw,
            'resonance': raw.get('resonance', False)
        }


# ============================================================
# Position
# ============================================================

class Position:
    def __init__(self, symbol, shares, entry_price, entry_date):
        self.symbol = symbol
        self.shares = shares
        self.entry_price = entry_price
        self.entry_date = entry_date
        self.current_price = entry_price

    def update(self, price):
        self.current_price = price

    def market_value(self):
        return self.shares * self.current_price


# ============================================================
# PortfolioEngine v3
# ============================================================

class PortfolioEngineV3:
    """
    多标的组合回测引擎 v3

    支持:
    - SignalGeneratorStrategy (多信号共振)
    - 黑名单过滤
    - 前置风控
    - 显式现金追踪
    """

    def __init__(self, initial_capital=3000000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.strategies = {}        # symbol -> {config, instance}
        self.positions = {}          # symbol -> Position
        self.snapshots = []         # list of dict
        self.trades = []            # list of dict
        self._data_cache = {}       # symbol -> kline data
        self._current_date = None
        self._peak_value = initial_capital
        self._current_drawdown = 0.0
        self._circuit_broken = False
        self.start = '20200101'
        self.end = '20251231'
        self.data_provider = None  # Injected by DailyEngine

        # 风控参数
        self.max_position_pct = 0.30
        self.max_total_exposure = 1.0
        self.max_trades_per_day = 5
        self.max_drawdown_limit = 0.50
        self.commission = 0.0003
        self.stamp_tax = 0.001
        self.slippage = 0.0005

    def add_strategy(self, symbol, strategy_type='RSI+Inst', config=None):
        """
        注册策略

        strategy_type: 'RSI', 'RSI+MACD', 'RSI+Inst', 'MultiSignal'
        config: dict with 'sources' list
        """
        if config is None:
            # 根据类型构建默认配置
            if strategy_type == 'RSI':
                config = {
                    'sources': [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 65, 'stop_loss': 0.05, 'take_profit': 0.20}, 1.0)
                    ]
                }
            elif strategy_type == 'RSI+Inst':
                config = {
                    'sources': [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'stop_loss': 0.05, 'take_profit': 0.30}, 1.0),
                        ('InstitutionalSignalSource', {}, 0.8),
                    ]
                }
            elif strategy_type == 'RSI+MACD':
                config = {
                    'sources': [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 65, 'stop_loss': 0.05, 'take_profit': 0.25}, 1.0),
                        ('MACDSignalSource', {'stop_loss': 0.08, 'take_profit': 0.25}, 0.7),
                    ]
                }
            elif strategy_type == 'MultiSignal':
                config = {
                    'sources': [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'stop_loss': 0.05, 'take_profit': 0.30}, 1.0),
                        ('MACDSignalSource', {'stop_loss': 0.08, 'take_profit': 0.25}, 0.7),
                        ('InstitutionalSignalSource', {}, 0.8),
                        ('MarketRegimeSource', {}, 0.3),
                    ],
                    'blacklist_enabled': True
                }

        self.strategies[symbol] = {
            'type': strategy_type,
            'config': config,
            'instance': None
        }
        print(f"  [Registered] {symbol} -> {strategy_type}")

    def _load_all_data(self, start, end, data_provider=None):
        if data_provider is None:
            data_provider = getattr(self, "data_provider", None)
        if data_provider is None:
            from data_provider import HistoricalDataProvider
            data_provider = HistoricalDataProvider()
        print(f"\nLoading data for {len(self.strategies)} symbols...")
        for symbol in self.strategies:
            info = self.strategies[symbol]
            # Create instance if not yet created
            if info['instance'] is None:
                inst = SignalGeneratorStrategy(symbol, info['config'])
                info['instance'] = inst
            else:
                inst = info['instance']
            ok = inst.load_data_with_provider(data_provider, start, end)
            if ok:
                info['instance'] = inst
                self._data_cache[symbol] = inst.data
                print(f"  [OK] {symbol}: {len(inst.data)} records")
            else:
                print(f"  [FAIL] {symbol}: No data")

    def _get_common_dates(self):
        if not self._data_cache:
            return []
        date_sets = []
        for data in self._data_cache.values():
            dates = set(d['date'] for d in data)
            date_sets.append(dates)
        common = date_sets[0]
        for ds in date_sets[1:]:
            common &= ds

        def to_date(s):
            if len(s) == 10 and s[4] == '-':
                from datetime import date
                return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
            else:
                from datetime import date
                return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))

        sd = to_date(self.start)
        ed = to_date(self.end)
        return sorted([d for d in common if sd <= to_date(d) <= ed])

    # 行业板块映射（用于同行业仓位检查）
    _SECTOR_MAP = {
        '600900.SH': '公用事业',
        '300750.SZ': '新能源',
        '600276.SH': '创新药',
        '688981.SH': '半导体',
        '600519.SH': '高端消费',
        '601318.SH': '金融',
        '000858.SZ': '消费',
    }

    def _check_sector_exposure(self, symbol, price, shares, total_equity):
        """检查同行业仓位是否超限（个股上限30%时，同行业合计也不超过30%）"""
        sector = self._SECTOR_MAP.get(symbol)
        if not sector:
            return True, ""
        pos_value = shares * price
        # 统计同行业已有仓位
        sector_existing = 0.0
        for sym, pos in self.positions.items():
            if sym != symbol and pos.shares > 0 and self._SECTOR_MAP.get(sym) == sector:
                sector_existing += pos.market_value()
        combined = sector_existing + pos_value
        max_sector = total_equity * self.max_position_pct
        if combined > max_sector:
            return False, f"sector_limit({sector} {combined/total_equity:.1%}>30%)"
        return True, ""

    def _check_risk(self, symbol, action, price, shares=0):
        # 熔断后只禁止开新仓，不禁止平仓
        if self._circuit_broken:
            if action == 'buy':
                return False, "circuit_broken"
            return True, ""

        equity = self.cash + sum(p.market_value() for p in self.positions.values() if p.shares > 0)
        if equity > self._peak_value:
            self._peak_value = equity
        self._current_drawdown = (self._peak_value - equity) / self._peak_value
        if self._current_drawdown > self.max_drawdown_limit:
            self._circuit_broken = True
            return False, f"max_drawdown({self._current_drawdown:.1%})"

        if action == 'buy':
            if self.positions.get(symbol) and self.positions[symbol].shares > 0:
                return False, "already_holding"
            total = self.cash + sum(p.market_value() for p in self.positions.values() if p.shares > 0)
            max_pos = total * self.max_position_pct
            if shares * price > max_pos:
                return False, "position_limit"
            current_pos = sum(p.market_value() for p in self.positions.values() if p.shares > 0)
            if current_pos + shares * price > total * self.max_total_exposure:
                return False, "total_exposure"

            # 同行业仓位检查
            allowed, sector_msg = self._check_sector_exposure(symbol, price, shares, total)
            if not allowed:
                return False, sector_msg

        elif action == 'sell':
            pos = self.positions.get(symbol)
            if not pos or pos.shares <= 0:
                return False, "no_position"

        return True, ""

    def _execute_trade(self, symbol, direction, price, shares, reason='', meta=None):
        if direction == 'sell':
            pos = self.positions.get(symbol)
            if not pos or pos.shares <= 0:
                return
            allowed, reason2 = self._check_risk(symbol, 'sell', price, 0)
            if not allowed:
                return
            shares = pos.shares
            gross = shares * price
            cost = gross * (self.commission + self.stamp_tax + self.slippage)
            net = gross - cost
            pnl = (price - pos.entry_price) / pos.entry_price
            self.cash += net
            self.positions[symbol].shares = 0
            self.trades.append({
                'date': self._current_date, 'symbol': symbol,
                'direction': 'sell', 'price': price, 'shares': shares,
                'reason': reason, 'meta': meta,
                'gross': gross, 'cost': cost, 'net': net, 'pnl': pnl
            })

        elif direction == 'buy':
            allowed, reason2 = self._check_risk(symbol, 'buy', price, 0)
            if not allowed:
                return
            total = self.cash + sum(p.market_value() for p in self.positions.values() if p.shares > 0)
            max_shares_by_pos = int(total * self.max_position_pct / (price * (1 + self.commission + self.slippage)))
            max_shares_by_cash = int(self.cash * 0.97 / (price * (1 + self.commission + self.slippage)))
            shares = min(max_shares_by_pos, max_shares_by_cash)
            if shares <= 0:
                return
            allowed, reason2 = self._check_risk(symbol, 'buy', price, shares)
            if not allowed:
                return
            cost = shares * price * (1 + self.commission + self.slippage)
            self.cash -= cost
            self.positions[symbol] = Position(symbol, shares, price, self._current_date)
            self.trades.append({
                'date': self._current_date, 'symbol': symbol,
                'direction': 'buy', 'price': price, 'shares': shares,
                'reason': reason, 'meta': meta, 'cost': cost
            })

    def _snapshot(self):
        pos_value = sum(p.market_value() for p in self.positions.values() if p.shares > 0)
        total = self.cash + pos_value
        self.snapshots.append({
            'date': self._current_date,
            'cash': self.cash,
            'position_value': pos_value,
            'total_value': total
        })

    def run(self, start=None, end=None, data_provider=None):
        if start is None: start = self.start
        if end is None: end = self.end
        self.start = start
        self.end = end
        if data_provider is not None:
            self.data_provider = data_provider
        self._load_all_data(start, end, data_provider=self.data_provider)
        dates = self._get_common_dates()
        print("=" * 60)
        print("PORTFOLIO ENGINE v3 - SignalGenerator Integrated")
        print("=" * 60)
        print(f"  Capital: {self.initial_capital:,.0f}")
        print(f"  Period: {start} -> {end}")
        print(f"  Strategies: {len(self.strategies)}")

        self._load_all_data(start, end, data_provider=self.data_provider)
        dates = self._get_common_dates()
        print(f"\n  Trading days: {len(dates)}")

        # Reset
        for info in self.strategies.values():
            if info['instance']:
                info['instance'].reset()
        self.positions = {}
        self.snapshots = []
        self.trades = []
        self.cash = self.initial_capital
        self._peak_value = self.initial_capital
        self._circuit_broken = False
        self._current_date = dates[0] if dates else start
        self._snapshot()

        print(f"\n  Running backtest...")
        for date_idx, date in enumerate(dates):
            self._current_date = date

            # 更新持仓价格
            for sym, pos in self.positions.items():
                if pos.shares > 0 and sym in self._data_cache:
                    for d in self._data_cache[sym]:
                        if d['date'] == date:
                            pos.update(d['close'])
                            break

            # 收集所有信号
            daily_signals = []
            for symbol, info in self.strategies.items():
                inst = info['instance']
                if not inst:
                    continue
                idx = None
                for i, d in enumerate(inst.data):
                    if d['date'] == date:
                        idx = i
                        break
                if idx is None:
                    continue

                sig = inst.generate_signal(idx)
                if sig['direction'] != 'hold' and sig['strength'] > 0:
                    sig['symbol'] = symbol
                    sig['price'] = inst.data[idx]['close']
                    daily_signals.append(sig)

            # 按强度排序，限制交易数
            daily_signals.sort(key=lambda x: x['strength'], reverse=True)
            trades_today = 0
            for sig in daily_signals:
                if trades_today >= self.max_trades_per_day:
                    break
                sym = sig['symbol']
                direction = sig['direction']
                price = sig['price']
                reason = sig['reason']
                meta = sig.get('data')

                old_cash = self.cash
                self._execute_trade(sym, direction, price,
                                  shares=0, reason=reason, meta=meta)
                if direction == 'buy' and self.cash < old_cash:
                    trades_today += 1

            self._snapshot()

            if (date_idx + 1) % 500 == 0:
                eq = self.snapshots[-1]['total_value']
                print(f"  {date_idx+1}/{len(dates)} | "
                      f"Trades={len(self.trades)} | "
                      f"Equity={eq:,.0f} | "
                      f"Cash={self.cash:,.0f} | "
                      f"DD={self._current_drawdown:.1%}")

        self._final_closeout()
        self._compute_stats()
        print(f"\n  Done! Trades={len(self.trades)}, "
              f"Final equity={self.snapshots[-1]['total_value']:,.0f}")
        if self._circuit_broken:
            print(f"  [Circuit breaker was triggered at {self._current_drawdown:.1%}]")

    def _final_closeout(self):
        last_date = self.snapshots[-1]['date'] if self.snapshots else self._current_date
        for sym, pos in list(self.positions.items()):
            if pos.shares > 0 and sym in self._data_cache:
                last = self._data_cache[sym][-1]
                lp = last['close']
                ld = last['date']
                gross = pos.shares * lp
                cost = gross * (self.commission + self.stamp_tax + self.slippage)
                net = gross - cost
                pnl = (lp - pos.entry_price) / pos.entry_price
                self.cash += net
                self.trades.append({
                    'date': ld, 'symbol': sym,
                    'direction': 'sell', 'price': lp, 'shares': pos.shares,
                    'reason': 'final_closeout', 'meta': None,
                    'gross': gross, 'cost': cost, 'net': net, 'pnl': pnl
                })
                pos.shares = 0
        self._current_date = last_date
        self._snapshot()

    def _compute_stats(self):
        if not self.snapshots:
            return
        vals = [s['total_value'] for s in self.snapshots]
        rets = [(vals[i] - vals[i-1]) / vals[i-1] for i in range(1, len(vals))]
        self.total_return = (vals[-1] - self.initial_capital) / self.initial_capital * 100
        self.annual_ret = np.mean(rets) * 252 * 100 if rets else 0
        self.annual_vol = np.std(rets) * np.sqrt(252) * 100 if rets else 0
        self.sharpe = (self.annual_ret/100 - 0.03) / (self.annual_vol/100) if self.annual_vol > 0 else 0
        peak = np.maximum.accumulate(np.array(vals))
        dd = (np.array(vals) - peak) / peak * 100
        self.max_drawdown = np.min(dd)
        sells = [t for t in self.trades if t['direction'] == 'sell']
        wins = sum(1 for t in sells if t.get('pnl', 0) > 0)
        self.n_trades = len(sells)
        self.win_rate = wins / max(len(sells), 1) * 100
        self.stats = {
            'total_return': self.total_return,
            'annual_ret': self.annual_ret,
            'annual_vol': self.annual_vol,
            'sharpe': self.sharpe,
            'max_drawdown': self.max_drawdown,
            'n_trades': self.n_trades,
            'win_rate': self.win_rate
        }

    def get_results(self):
        return {'stats': self.stats, 'trades': self.trades, 'snapshots': self.snapshots}

    def print_summary(self):
        s = self.stats
        print("\n" + "=" * 60)
        print("RESULTS v3")
        print("=" * 60)
        print(f"  Return:    {s['total_return']:+.1f}%")
        print(f"  Annual:    {s['annual_ret']:+.1f}%")
        print(f"  Sharpe:   {s['sharpe']:.2f}")
        print(f"  MaxDD:    {s['max_drawdown']:.1f}%")
        print(f"  Vol:       {s['annual_vol']:.1f}%")
        print(f"  Win Rate:  {s['win_rate']:.0f}%")
        print(f"  Trades:    {s['n_trades']}")
        print(f"\n  Final Cash:   {self.snapshots[-1]['cash']:,.0f}")
        print(f"  Final Equity: {self.snapshots[-1]['total_value']:,.0f}")
        print("\n  Last 10 trades:")
        for t in self.trades[-10:]:
            pnl_str = f"{t.get('pnl', 0)*100:+.1f}%" if t['direction'] == 'sell' else ''
            resonance = '[RES]' if t.get('meta') and t['meta'].get('resonance') else ''
            print(f"    {t['date']} {t['direction']:4} {t['symbol']} "
                  f"@{t['price']:.2f} x{t['shares']} {pnl_str} {resonance}")


# ============================================================
# 测试不同策略类型
# ============================================================

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--type', default='RSI+Inst',
                       choices=['RSI', 'RSI+Inst', 'RSI+MACD', 'MultiSignal', 'all'],
                       help='Strategy type')
    args = parser.parse_args()

    if args.type == 'all':
        strategy_types = ['RSI', 'RSI+Inst', 'RSI+MACD', 'MultiSignal']
    else:
        strategy_types = [args.type]

    for stype in strategy_types:
        print(f"\n{'='*60}")
        print(f"TEST: {stype}")
        print("=" * 60)

        engine = PortfolioEngineV3(3000000)
        engine.max_drawdown_limit = 0.30

        if stype == 'RSI':
            engine.add_strategy('600276.SH', 'RSI', {
                'sources': [('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'stop_loss': 0.10, 'take_profit': 0.15}, 1.0)]
            })
            engine.add_strategy('600519.SH', 'RSI', {
                'sources': [('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 75, 'stop_loss': 0.08, 'take_profit': 0.30}, 1.0)]
            })
            engine.add_strategy('300750.SZ', 'RSI', {
                'sources': [('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 65, 'stop_loss': 0.05, 'take_profit': 0.30}, 1.0)]
            })
        elif stype == 'RSI+Inst':
            engine.add_strategy('600276.SH', 'RSI+Inst', None)
            engine.add_strategy('600519.SH', 'RSI+Inst', None)
            engine.add_strategy('300750.SZ', 'RSI+Inst', None)
            engine.add_strategy('000858.SZ', 'RSI+Inst', None)
        elif stype == 'RSI+MACD':
            engine.add_strategy('600276.SH', 'RSI+MACD', None)
            engine.add_strategy('600519.SH', 'RSI+MACD', None)
            engine.add_strategy('300750.SZ', 'RSI+MACD', None)
        elif stype == 'MultiSignal':
            engine.add_strategy('600276.SH', 'MultiSignal', None)
            engine.add_strategy('600519.SH', 'MultiSignal', None)
            engine.add_strategy('300750.SZ', 'MultiSignal', None)

        engine.run('20200101', '20251231')
        engine.print_summary()
