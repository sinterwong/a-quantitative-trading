"""
DailyEngine - 每日运行引擎
================================
包装PortfolioEngineV3 + DataProvider注入层

核心能力:
1. 支持历史回测 (HistoricalDataProvider) 和实时实盘 (LiveDataProvider)
2. 完全解耦：Engine不感知数据来源
3. 每日运行上下文管理

使用:
    # 回测模式
    engine = DailyEngine(capital=3000000, mode='backtest')
    engine.set_portfolio([('600276.SH', 'RSI+Inst', None), ...])
    engine.run(start='20200101', end='20251231')
    engine.print_summary()

    # 实盘模式
    engine = DailyEngine(capital=3000000, mode='live')
    engine.set_portfolio([('600276.SH', 'RSI+Inst', None), ...])
    engine.run_today()  # 用LiveDataProvider
    engine.print_today_report()
"""

import os
import sys

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)

from data_provider import DataProvider, HistoricalDataProvider, LiveDataProvider
from portfolio_engine_v3 import PortfolioEngineV3
import numpy as np


class DailyEngine:
    """
    每日运行引擎

    两种运行模式:
    - 'backtest': 使用HistoricalDataProvider（全量历史数据）
    - 'live': 使用LiveDataProvider（实时行情）

    三种策略类型:
    - 'RSI': 纯RSI均值回归
    - 'RSI+Inst': RSI + 机构信号共振
    - 'RSI+MACD': RSI + MACD共振
    - 'MultiSignal': 全信号共振
    """

    def __init__(self, capital=3000000, mode='backtest'):
        self.capital = capital
        self.mode = mode
        self.portfolio = []  # [(symbol, strategy_type, custom_config), ...]
        self.engine = None
        self.provider = None
        self.results = {}
        self._snapshots = []
        self._trades = []

    def set_portfolio(self, portfolio):
        """
        设置持仓组合

        Args:
            portfolio: list of (symbol, strategy_type, config_override)
                     config_override=None使用默认配置
        """
        self.portfolio = portfolio
        print(f"  Portfolio set: {len(portfolio)} stocks")
        for sym, stype, cfg in portfolio:
            print(f"    - {sym}: {stype}")

    def _build_provider(self, start=None, end=None):
        """根据mode构建DataProvider"""
        if self.mode == 'backtest':
            self.provider = HistoricalDataProvider()
            print(f"  [Provider] HistoricalDataProvider")
        elif self.mode == 'live':
            self.provider = LiveDataProvider()
            print(f"  [Provider] LiveDataProvider")
        else:
            raise ValueError(f"Unknown mode: {self.mode}")

    def _build_engine(self):
        """构建Engine实例"""
        self.engine = PortfolioEngineV3(self.capital)
        self.engine.data_provider = self.provider  # inject

        for symbol, strategy_type, config_override in self.portfolio:
            self.engine.add_strategy(symbol, strategy_type, config_override)

    def run(self, start=None, end=None):
        """
        运行回测/模拟

        Args:
            start: 开始日期 (YYYYMMDD或YYYY-MM-DD)
            end: 结束日期
        """
        self._build_provider()
        self._build_engine()

        print(f"\n{'='*60}")
        print(f"DailyEngine [{self.mode.upper()}]")
        print(f"{'='*60}")

        self.engine.run(start=start, end=end, data_provider=self.provider)
        self._snapshots = self.engine.snapshots
        self._trades = self.engine.trades
        self.results = self.engine.get_results()

        return self.results

    def run_today(self):
        """
        运行今日分析（实盘模式）

        使用LiveDataProvider获取今日实时数据，
        结合历史数据计算信号，
        输出今日决策建议（不实际下单）
        """
        if self.mode != 'live':
            print("[Warning] run_today() is for live mode. Switching to live.")
            self.mode = 'live'

        self._build_provider()
        self._build_engine()

        from datetime import date
        today = date.today().strftime('%Y%m%d')
        yesterday = (date.today() - __import__('datetime').timedelta(days=1)).strftime('%Y%m%d')

        print(f"\n{'='*60}")
        print(f"DailyEngine [LIVE] - {today}")
        print(f"{'='*60}")

        # 用今日数据运行引擎（无历史，仅今日）
        self.engine.run(start=yesterday, end=today, data_provider=self.provider)
        self._snapshots = self.engine.snapshots
        self._trades = self.engine.trades
        self.results = self.engine.get_results()

        return self.results

    def print_summary(self):
        if self.engine:
            self.engine.print_summary()

    def get_portfolio_status(self) -> dict:
        """
        获取当前持仓状态

        Returns:
            dict: {
                'cash': float,
                'equity': float,
                'positions': [{symbol, shares, cost, current_price, pnl, pnl_pct}, ...],
                'total_value': float,
                'date': str
            }
        """
        if not self.engine or not self.engine.snapshots:
            return {}

        last_snap = self.engine.snapshots[-1]
        positions = []
        for sym, pos in self.engine.positions.items():
            if pos.shares > 0:
                pnl = (pos.current_price - pos.entry_price) * pos.shares
                pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price
                positions.append({
                    'symbol': sym,
                    'shares': pos.shares,
                    'entry_price': pos.entry_price,
                    'current_price': pos.current_price,
                    'market_value': pos.market_value(),
                    'cost': pos.entry_price * pos.shares,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                })

        return {
            'date': last_snap.get('date', ''),
            'cash': last_snap.get('cash', 0),
            'position_value': last_snap.get('position_value', 0),
            'total_value': last_snap.get('total_value', 0),
            'positions': positions,
        }

    def get_today_signals(self) -> list:
        """
        获取今日触发的信号

        Returns:
            list of {symbol, signal, strength, reason, decision}
        """
        signals = []
        if not self.engine:
            return signals

        for sym, pos_info in self.engine.strategies.items():
            inst = pos_info.get('instance')
            if not inst or not inst.data:
                continue
            # 最后一个数据点
            idx = len(inst.data) - 1
            sig = inst.generate_signal(idx)
            if sig and sig['direction'] != 'hold':
                signals.append({
                    'symbol': sym,
                    'direction': sig['direction'],
                    'strength': sig['strength'],
                    'reason': sig['reason'],
                    'resonance': sig.get('resonance', False),
                })

        return signals

    def print_today_report(self):
        """打印今日报告"""
        from datetime import date
        today = date.today().strftime('%Y-%m-%d')

        print(f"\n{'='*60}")
        print(f"今日报告 {today}")
        print(f"{'='*60}")

        # 持仓状态
        status = self.get_portfolio_status()
        print(f"\n【持仓状态】")
        print(f"  总权益: {status.get('total_value', 0):,.0f}")
        print(f"  现金:   {status.get('cash', 0):,.0f}")
        print(f"  持仓市值: {status.get('position_value', 0):,.0f}")

        positions = status.get('positions', [])
        if positions:
            print(f"\n  {'代码':<12} {'持仓':>8} {'成本':>10} {'当前价':>10} {'市值':>12} {'盈亏':>10} {'盈亏%':>8}")
            print("  " + "-" * 78)
            for p in positions:
                print(f"  {p['symbol']:<12} {p['shares']:>8} "
                      f"{p['entry_price']:>10.2f} {p['current_price']:>10.2f} "
                      f"{p['market_value']:>12,.0f} {p['pnl']:>+10,.0f} {p['pnl_pct']:>+7.1%}")
        else:
            print(f"\n  空仓")

        # 今日信号
        signals = self.get_today_signals()
        print(f"\n【今日信号】")
        if signals:
            for sig in signals:
                resonance = " [共振]" if sig.get('resonance') else ""
                print(f"  {sig['direction'].upper():<6} {sig['symbol']:<12} "
                      f"强度={sig['strength']:.2f}{resonance}  {sig['reason']}")
        else:
            print(f"  无信号")

        # 未执行交易（信号产生但未成交）
        pending = [t for t in self._trades if t.get('reason', '').startswith('signal_')]
        if pending:
            print(f"\n【待成交】")
            for t in pending:
                print(f"  {t['direction']} {t['symbol']} @{t['price']:.2f}")


# ============================================================
# 股票注册表 - 外部股票配置接口
# ============================================================

class StockRegistry:
    """
    外部股票注册表

    用户通过这个接口配置要管理的股票列表
    每个股票可覆盖默认策略参数

    使用方式:
        registry = StockRegistry()
        registry.register('600276.SH', strategy='RSI+Inst',
                        weight=0.25,    # 目标权重
                        params={'rsi_buy': 30, 'rsi_sell': 70})  # 参数覆盖
        registry.register('300750.SZ', strategy='RSI', weight=0.25)

        # 构建组合
        portfolio = registry.build_portfolio()
        engine = DailyEngine()
        engine.set_portfolio(portfolio)
    """

    def __init__(self):
        self._stocks = {}  # symbol -> {strategy, weight, params, enabled}

    def register(self, symbol, strategy='RSI+Inst', weight=None, params=None, enabled=True):
        """
        注册一只股票

        Args:
            symbol: 股票代码
            strategy: 策略类型 ('RSI', 'RSI+Inst', 'RSI+MACD', 'MultiSignal')
            weight: 目标权重 (默认等权)
            params: 参数覆盖 dict，传入SignalGenerator的config
            enabled: 是否启用
        """
        # 构建config
        config = {'sources': []}
        if params:
            config.update(params)

        self._stocks[symbol] = {
            'strategy': strategy,
            'weight': weight,
            'config': config,
            'enabled': enabled,
        }

    def build_portfolio(self, total_weight=1.0) -> list:
        """
        构建组合列表

        Returns:
            [(symbol, strategy_type, config_override), ...]
        """
        enabled = [(s, d) for s, d in self._stocks.items() if d['enabled']]
        if not enabled:
            return []

        # 等权分配
        n = len(enabled)
        weight_per = total_weight / n

        result = []
        for symbol, data in enabled:
            # 构建config
            strategy = data['strategy']
            params = data['config']

            # 如果没有显式sources，构建默认配置
            if not params.get('sources'):
                if strategy == 'RSI':
                    params['sources'] = [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 65, 'stop_loss': 0.05, 'take_profit': 0.20}, 1.0)
                    ]
                elif strategy == 'RSI+Inst':
                    params['sources'] = [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'stop_loss': 0.05, 'take_profit': 0.30}, 1.0),
                        ('InstitutionalSignalSource', {}, 0.8),
                    ]
                elif strategy == 'RSI+MACD':
                    params['sources'] = [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 65, 'stop_loss': 0.05, 'take_profit': 0.25}, 1.0),
                        ('MACDSignalSource', {'stop_loss': 0.08, 'take_profit': 0.25}, 0.7),
                    ]
                elif strategy == 'MultiSignal':
                    params['sources'] = [
                        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'stop_loss': 0.05, 'take_profit': 0.30}, 1.0),
                        ('MACDSignalSource', {'stop_loss': 0.08, 'take_profit': 0.25}, 0.7),
                        ('InstitutionalSignalSource', {}, 0.8),
                        ('MarketRegimeSource', {}, 0.3),
                    ]

            result.append((symbol, strategy, params))

        return result

    def get_weight(self, symbol) -> float:
        d = self._stocks.get(symbol, {})
        return d.get('weight') or 0.0

    def list_stocks(self) -> dict:
        return dict(self._stocks)


# ============================================================
# 使用示例
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("DailyEngine Test")
    print("=" * 60)

    # 方式1：直接注册
    engine = DailyEngine(capital=3000000, mode='backtest')
    engine.set_portfolio([
        ('600276.SH', 'RSI+Inst', None),
        ('600519.SH', 'RSI+Inst', None),
        ('300750.SZ', 'RSI+Inst', None),
        ('000858.SZ', 'RSI+Inst', None),
    ])
    engine.run('20200101', '20251231')
    engine.print_summary()

    print("\n" + "=" * 60)
    print("StockRegistry Test")
    print("=" * 60)

    # 方式2：通过StockRegistry注册（更灵活）
    registry = StockRegistry()
    registry.register('600276.SH', strategy='RSI+Inst', params={'rsi_buy': 35, 'rsi_sell': 70})
    registry.register('600519.SH', strategy='RSI+Inst', params={'rsi_buy': 35, 'rsi_sell': 75, 'take_profit': 0.30})
    registry.register('300750.SZ', strategy='RSI+Inst', params={'rsi_buy': 35, 'rsi_sell': 65, 'take_profit': 0.30})
    registry.register('000858.SZ', strategy='RSI+Inst')

    portfolio = registry.build_portfolio()
    print(f"\nBuilt portfolio: {len(portfolio)} stocks")
    for sym, stype, cfg in portfolio:
        print(f"  {sym}: {stype}, sources={len(cfg.get('sources', []))}")
