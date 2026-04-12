"""
DailyJournal - 每日运行记录持久化
=================================
目标：让系统具备"记忆"，支持后续分析

设计原则：
1. 每个交易日一个独立目录
2. JSON格式（机器可读） + Markdown摘要（人类可读）
3. 记录：信号、决策、执行结果、持仓快照、市场环境
4. 支持跨日追溯（今日决策参考昨日持仓）

目录结构：
    journal/
        2026-04-11/
            signals.json        # 当日所有信号
            decisions.json     # 决策链（每条决策的reason）
            trades.json        # 实际成交
            positions.json     # 收盘持仓快照
            market.json       # 市场环境（MA200位置、成交量异动）
            summary.md        # 人类可读摘要
            meta.json         # 日期、元信息
        2026-04-10/
            ...
"""

import os
import json
import sys
from datetime import date, timedelta

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader
from backtest import TechnicalIndicators as TI


# ============================================================
# Journal目录管理
# ============================================================

def get_journal_dir(base_name='journal') -> str:
    """获取Journal根目录"""
    return os.path.join(QUANT_DIR, base_name)


def get_day_dir(trading_date: str, base_name='journal') -> str:
    """
    获取某日的Journal目录

    Args:
        trading_date: YYYY-MM-DD格式
    """
    d = os.path.join(get_journal_dir(base_name), trading_date)
    os.makedirs(d, exist_ok=True)
    return d


def list_journal_days(base_name='journal') -> list:
    """列出所有有Journal记录的交易日（倒序）"""
    journal_root = get_journal_dir(base_name)
    if not os.path.exists(journal_root):
        return []
    days = sorted(os.listdir(journal_root), reverse=True)
    return [d for d in days if os.path.isdir(os.path.join(journal_root, d))]


# ============================================================
# 数据结构
# ============================================================

class DayMeta:
    """单日Journal的meta信息"""
    def __init__(self, trading_date: str, weekday: str, n_signals: int,
                 n_trades: int, n_positions: int, total_value: float,
                 cash: float, equity: float):
        self.trading_date = trading_date
        self.weekday = weekday
        self.n_signals = n_signals
        self.n_trades = n_trades
        self.n_positions = n_positions
        self.total_value = total_value
        self.cash = cash
        self.equity = equity

    def to_dict(self) -> dict:
        return {
            'trading_date': self.trading_date,
            'weekday': self.weekday,
            'n_signals': self.n_signals,
            'n_trades': self.n_trades,
            'n_positions': self.n_positions,
            'total_value': round(self.total_value, 2),
            'cash': round(self.cash, 2),
            'equity': round(self.equity, 2),
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


class SignalRecord:
    """单条信号记录"""
    def __init__(self, symbol: str, direction: str, strength: float,
                 reason: str, resonance: bool,
                 price: float, accepted: bool,
                 decision: str, decision_reason: str):
        self.symbol = symbol
        self.direction = direction
        self.strength = strength
        self.reason = reason
        self.resonance = resonance
        self.price = price
        self.accepted = accepted          # 是否接受并执行
        self.decision = decision          # buy/sell/hold
        self.decision_reason = decision_reason  # 接受/拒绝的具体原因

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'direction': self.direction,
            'strength': round(self.strength, 3),
            'reason': self.reason,
            'resonance': self.resonance,
            'price': round(self.price, 3),
            'accepted': self.accepted,
            'decision': self.decision,
            'decision_reason': self.decision_reason,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


class TradeRecord:
    """单笔成交记录"""
    def __init__(self, trade_id: str, symbol: str, direction: str,
                 price: float, shares: int, cost: float,
                 reason: str, execution_type: str,
                 signal_price: float = None,
                 slippage_pct: float = None,
                 pnl: float = None, pnl_pct: float = None):
        self.trade_id = trade_id
        self.symbol = symbol
        self.direction = direction
        self.price = price
        self.shares = shares
        self.cost = cost
        self.reason = reason
        self.execution_type = execution_type  # 'vwap'/'close'/'limit'
        self.signal_price = signal_price
        self.slippage_pct = slippage_pct
        self.pnl = pnl
        self.pnl_pct = pnl_pct

    def to_dict(self) -> dict:
        return {
            'trade_id': self.trade_id,
            'symbol': self.symbol,
            'direction': self.direction,
            'price': round(self.price, 3),
            'shares': self.shares,
            'cost': round(self.cost, 2),
            'reason': self.reason,
            'execution_type': self.execution_type,
            'signal_price': round(self.signal_price, 3) if self.signal_price else None,
            'slippage_pct': round(self.slippage_pct, 4) if self.slippage_pct else None,
            'pnl': round(self.pnl, 2) if self.pnl else None,
            'pnl_pct': round(self.pnl_pct, 4) if self.pnl_pct else None,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


class PositionRecord:
    """单只持仓快照"""
    def __init__(self, symbol: str, shares: int, entry_price: float,
                 current_price: float, market_value: float,
                 cost: float, unrealized_pnl: float,
                 unrealized_pnl_pct: float, hold_days: int):
        self.symbol = symbol
        self.shares = shares
        self.entry_price = entry_price
        self.current_price = current_price
        self.market_value = market_value
        self.cost = cost
        self.unrealized_pnl = unrealized_pnl
        self.unrealized_pnl_pct = unrealized_pnl_pct
        self.hold_days = hold_days

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'shares': self.shares,
            'entry_price': round(self.entry_price, 3),
            'current_price': round(self.current_price, 3),
            'market_value': round(self.market_value, 2),
            'cost': round(self.cost, 2),
            'unrealized_pnl': round(self.unrealized_pnl, 2),
            'unrealized_pnl_pct': round(self.unrealized_pnl_pct, 4),
            'hold_days': self.hold_days,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


class MarketContext:
    """市场环境上下文"""
    def __init__(self, date: str,
                 hs300_price: float, hs300_ma200: float,
                 hs300_pct_above_ma200: float,
                 regime: str,  # bull/neutral/bear
                 volume_ratio: float = None,  # 今日成交量/20日均量
                 atr_pct: float = None):
        self.date = date
        self.hs300_price = hs300_price
        self.hs300_ma200 = hs300_ma200
        self.hs300_pct_above_ma200 = hs300_pct_above_ma200
        self.regime = regime
        self.volume_ratio = volume_ratio
        self.atr_pct = atr_pct

    def to_dict(self) -> dict:
        return {
            'date': self.date,
            'hs300_price': round(self.hs300_price, 3) if self.hs300_price else None,
            'hs300_ma200': round(self.hs300_ma200, 3) if self.hs300_ma200 else None,
            'hs300_pct_above_ma200': round(self.hs300_pct_above_ma200, 3) if self.hs300_pct_above_ma200 else None,
            'regime': self.regime,
            'volume_ratio': round(self.volume_ratio, 3) if self.volume_ratio else None,
            'atr_pct': round(self.atr_pct, 3) if self.atr_pct else None,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


class DecisionRecord:
    """单条决策记录（信号→决策→执行）"""
    def __init__(self, decision_id: str, timestamp: str,
                 symbol: str, signal_reason: str,
                 signal_strength: float, resonance: bool,
                 decision: str,  # buy/sell/hold
                 decision_reason: str,
                 accepted: bool,
                 execution: dict = None):
        self.decision_id = decision_id
        self.timestamp = timestamp
        self.symbol = symbol
        self.signal_reason = signal_reason
        self.signal_strength = signal_strength
        self.resonance = resonance
        self.decision = decision
        self.decision_reason = decision_reason
        self.accepted = accepted
        self.execution = execution  # 如果执行了，记录成交

    def to_dict(self) -> dict:
        return {
            'decision_id': self.decision_id,
            'timestamp': self.timestamp,
            'symbol': self.symbol,
            'signal_reason': self.signal_reason,
            'signal_strength': round(self.signal_strength, 3),
            'resonance': self.resonance,
            'decision': self.decision,
            'decision_reason': self.decision_reason,
            'accepted': self.accepted,
            'execution': self.execution,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


# ============================================================
# JournalWriter - 写入每日记录
# ============================================================

class JournalWriter:
    """
    每日Journal写入器

    在每日运行结束后调用，将当日所有信息写入journal/目录

    Usage:
        writer = JournalWriter('2026-04-11')
        writer.write_signals(signals)
        writer.write_trades(trades)
        writer.write_positions(positions)
        writer.write_market_context(context)
        writer.write_summary()
    """

    WEEKDAY_CN = ['周一','周二','周三','周四','周五','周六','周日']

    def __init__(self, trading_date: str = None, base_name='journal'):
        if trading_date is None:
            trading_date = date.today().strftime('%Y-%m-%d')
        self.trading_date = trading_date
        self.base_name = base_name
        self.day_dir = get_day_dir(trading_date, base_name)

        # 数据收集
        self.signals = []
        self.decisions = []
        self.trades = []
        self.positions = []
        self.market_context = None
        self.meta = None

    def _weekday(self) -> str:
        wd = date.fromisoformat(self.trading_date).weekday()
        days = ['周一','周二','周三','周四','周五','周六','周日']
        return days[wd]

    def write_signals(self, signals: list):
        """写入信号记录"""
        self.signals = [SignalRecord(**s) if isinstance(s, dict) else s for s in signals]
        path = os.path.join(self.day_dir, 'signals.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([s.to_dict() for s in self.signals], f, ensure_ascii=False, indent=2)

    def write_decisions(self, decisions: list):
        """写入决策记录"""
        self.decisions = [DecisionRecord(**d) if isinstance(d, dict) else d for d in decisions]
        path = os.path.join(self.day_dir, 'decisions.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([d.to_dict() for d in self.decisions], f, ensure_ascii=False, indent=2)

    def write_trades(self, trades: list):
        """写入成交记录"""
        self.trades = [TradeRecord(**t) if isinstance(t, dict) else t for t in trades]
        path = os.path.join(self.day_dir, 'trades.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([t.to_dict() for t in self.trades], f, ensure_ascii=False, indent=2)

    def write_positions(self, positions: list):
        """写入持仓快照"""
        self.positions = [PositionRecord(**p) if isinstance(p, dict) else p for p in positions]
        path = os.path.join(self.day_dir, 'positions.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([p.to_dict() for p in self.positions], f, ensure_ascii=False, indent=2)

    def write_market_context(self, context: MarketContext):
        """写入市场环境"""
        self.market_context = context if isinstance(context, MarketContext) else MarketContext(**context)
        path = os.path.join(self.day_dir, 'market.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.market_context.to_dict(), f, ensure_ascii=False, indent=2)

    def write_meta(self, equity: float, cash: float):
        """写入meta信息"""
        self.meta = DayMeta(
            trading_date=self.trading_date,
            weekday=self._weekday(),
            n_signals=len(self.signals),
            n_trades=len(self.trades),
            n_positions=len(self.positions),
            total_value=equity,
            cash=cash,
            equity=equity,
        )
        path = os.path.join(self.day_dir, 'meta.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.meta.to_dict(), f, ensure_ascii=False, indent=2)

    def write_summary_md(self, equity_yesterday: float = None,
                        equity_change_pct: float = None):
        """写入人类可读的Markdown摘要"""
        path = os.path.join(self.day_dir, 'summary.md')

        # 涨跌符号
        def pct_str(p):
            return f"{p:+.2f}%" if p is not None else "N/A"

        lines = [
            f"# {self.trading_date} {self._weekday()} 日报",
            "",
            "## 账户概览",
            f"- 总权益: {self.meta.equity:,.0f}",
            f"- 现金: {self.meta.cash:,.0f}",
            f"- 持仓市值: {self.meta.equity - self.meta.cash:,.0f}",
            "",
            f"- 当日信号: {self.meta.n_signals}条",
            f"- 当日成交: {self.meta.n_trades}笔",
            f"- 收盘持仓: {self.meta.n_positions}只",
            "",
        ]

        # 持仓明细
        if self.positions:
            lines.append("## 持仓明细")
            lines.append(f"| 代码 | 持仓 | 成本 | 现价 | 持天数 | 浮盈 | 盈亏% |")
            lines.append("|------|------|------|------|------|------|------|")
            for p in self.positions:
                if isinstance(p, dict):
                    pnl_str = f"{p['unrealized_pnl']:+,.0f}"
                    pnl_pct_str = f"{p['unrealized_pnl_pct']:+.1%}"
                    lines.append(
                        f"| {p['symbol']} | {p['shares']} | "
                        f"{p['entry_price']:.2f} | {p['current_price']:.2f} | "
                        f"{p['hold_days']} | {pnl_str} | {pnl_pct_str} |"
                    )
            lines.append("")

        # 成交记录
        if self.trades:
            lines.append("## 成交记录")
            for t in self.trades:
                if isinstance(t, dict):
                    direction_cn = {'buy': '买入', 'sell': '卖出'}.get(t['direction'], t['direction'])
                    pnl_str = f"{t['pnl']:+,.0f}({t['pnl_pct']:+.1%})" if t.get('pnl') else ''
                    lines.append(
                        f"- {direction_cn} {t['symbol']} "
                        f"@{t['price']:.2f}x{t['shares']} "
                        f"[{t['reason']}] {pnl_str}"
                    )
            lines.append("")

        # 市场环境
        if self.market_context:
            mc = self.market_context.to_dict() if isinstance(self.market_context, MarketContext) else self.market_context
            regime_cn = {'bull': '多头', 'neutral': '震荡', 'bear': '空头'}.get(mc.get('regime', ''), mc.get('regime', ''))
            pct = mc.get('hs300_pct_above_ma200', 0)
            lines.append("## 市场环境")
            lines.append(f"- 沪深300 MA200状态: {regime_cn} ({pct:+.2f}%)" if pct else "- 市场: N/A")
            lines.append("")

        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

    def finalize(self, equity: float, cash: float,
                 equity_yesterday: float = None):
        """完成Journal写入"""
        self.write_meta(equity, cash)
        self.write_summary_md(equity_yesterday)
        print(f"  [Journal] Written to {self.day_dir}")


# ============================================================
# JournalReader - 读取历史记录
# ============================================================

class JournalReader:
    """
    Journal读取器

    支持：
    - 读取某日记录
    - 读取最近N日
    - 跨日追溯（某只股票的持仓历史）
    - 查询某只股票的信号历史

    Usage:
        reader = JournalReader()
        today = reader.get_day('2026-04-11')
        recent = reader.get_recent_days(5)
        history = reader.get_stock_history('600276.SH', days=30)
    """

    def __init__(self, base_name='journal'):
        self.base_name = base_name
        self.journal_root = get_journal_dir(base_name)

    def _load_json(self, trading_date: str, filename: str) -> list | dict:
        path = os.path.join(self.journal_root, trading_date, filename)
        if not os.path.exists(path):
            return [] if 'signals' in filename or 'trades' in filename else {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get_day(self, trading_date: str) -> dict:
        """读取某日的完整Journal"""
        return {
            'meta': self._load_json(trading_date, 'meta.json'),
            'signals': self._load_json(trading_date, 'signals.json'),
            'decisions': self._load_json(trading_date, 'decisions.json'),
            'trades': self._load_json(trading_date, 'trades.json'),
            'positions': self._load_json(trading_date, 'positions.json'),
            'market': self._load_json(trading_date, 'market.json'),
        }

    def get_recent_days(self, n: int = 5) -> list:
        """读取最近N日的Journal（倒序）"""
        days = list_journal_days(self.base_name)
        return [self.get_day(d) for d in days[:n]]

    def get_stock_history(self, symbol: str, days: int = 30) -> dict:
        """
        获取某只股票最近N天的信号+持仓历史

        用于后续分析："为什么买了/卖了这只股票"
        """
        recent = self.get_recent_days(days)
        history = []
        for day_data in recent:
            day_date = day_data.get('meta', {}).get('trading_date', '')
            # 找该股票的信号
            for sig in day_data.get('signals', []):
                if sig.get('symbol') == symbol:
                    history.append({'date': day_date, 'type': 'signal', **sig})
            # 找该股票的持仓
            for pos in day_data.get('positions', []):
                if pos.get('symbol') == symbol:
                    history.append({'date': day_date, 'type': 'position', **pos})
            # 找该股票的成交
            for trade in day_data.get('trades', []):
                if trade.get('symbol') == symbol:
                    history.append({'date': day_date, 'type': 'trade', **trade})
        return history

    def get_trade_history(self, days: int = 30) -> list:
        """获取最近N天的所有成交记录（展平）"""
        recent = self.get_recent_days(days)
        all_trades = []
        for day_data in recent:
            day_date = day_data.get('meta', {}).get('trading_date', '')
            for trade in day_data.get('trades', []):
                all_trades.append({'date': day_date, **trade})
        return sorted(all_trades, key=lambda x: x.get('date', ''), reverse=True)

    def get_equity_curve(self, days: int = 30) -> list:
        """获取最近N天的权益曲线"""
        recent = self.get_recent_days(days)
        curve = []
        for day_data in recent:
            meta = day_data.get('meta', {})
            if meta:
                curve.append({
                    'date': meta.get('trading_date'),
                    'equity': meta.get('equity'),
                    'cash': meta.get('cash'),
                })
        return curve

    def get_summary_text(self, trading_date: str) -> str:
        """读取某日的Markdown摘要"""
        path = os.path.join(self.journal_root, trading_date, 'summary.md')
        if not os.path.exists(path):
            return f"No summary for {trading_date}"
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def list_days(self) -> list:
        """列出所有有记录的交易日"""
        return list_journal_days(self.base_name)


# ============================================================
# 自动从Engine结果写入Journal
# ============================================================

class EngineToJournal:
    """
    把PortfolioEngine的运行结果转换为Journal格式

    Usage:
        converter = EngineToJournal(trading_date='2026-04-11')
        converter.from_engine_results(
            engine_results=engine.get_results(),
            signals=engine_signals,
            decisions=decisions,
            market_context=context
        )
        writer = converter.get_writer()
        writer.finalize(equity=..., cash=...)
    """

    def __init__(self, trading_date: str = None, base_name='journal'):
        self.writer = JournalWriter(trading_date, base_name)

    def from_engine_results(self, engine_results: dict,
                            signals: list,
                            decisions: list,
                            market_context: MarketContext,
                            cash: float):
        """
        从Engine运行结果转换

        Args:
            engine_results: engine.get_results()
            signals: 每日信号列表
            decisions: 每日决策列表
            market_context: 市场环境
            cash: 当前现金
        """
        results = engine_results
        positions = []

        # 从snapshot还原持仓
        if 'snapshots' in results and results['snapshots']:
            last_snap = results['snapshots'][-1]
            equity = last_snap.get('total_value', 0)
            # 从trades还原当前持仓（已平仓的不算）
            # 这里简化处理，假设positions在engine里
        else:
            equity = 0

        # Positions: 从engine的positions字段
        if 'positions' in results:
            for sym, pos_data in results['positions'].items():
                if isinstance(pos_data, dict) and pos_data.get('shares', 0) > 0:
                    positions.append({
                        'symbol': sym,
                        'shares': pos_data['shares'],
                        'entry_price': pos_data.get('entry_price', 0),
                        'current_price': pos_data.get('current_price', 0),
                        'market_value': pos_data.get('shares', 0) * pos_data.get('current_price', 0),
                        'cost': pos_data.get('shares', 0) * pos_data.get('entry_price', 0),
                        'unrealized_pnl': pos_data.get('unrealized_pnl', 0),
                        'unrealized_pnl_pct': pos_data.get('unrealized_pnl_pct', 0),
                        'hold_days': pos_data.get('hold_days', 0),
                    })

        # Trades: 格式化
        formatted_trades = []
        for t in results.get('trades', []):
            if isinstance(t, dict):
                formatted_trades.append({
                    'trade_id': f"{t.get('date', '')}_{t.get('symbol', '')}_{t.get('direction', '')}",
                    'symbol': t.get('symbol', ''),
                    'direction': t.get('direction', ''),
                    'price': t.get('price', 0),
                    'shares': t.get('shares', 0),
                    'cost': t.get('cost', 0),
                    'reason': t.get('reason', ''),
                    'execution_type': 'close',  # 回测用收盘价
                    'pnl': t.get('pnl'),
                    'pnl_pct': t.get('pnl_pct'),
                })

        self.writer.write_signals(signals)
        self.writer.write_decisions(decisions)
        self.writer.write_trades(formatted_trades)
        self.writer.write_positions(positions)
        self.writer.write_market_context(market_context)

    def get_writer(self) -> JournalWriter:
        return self.writer


# ============================================================
# 使用示例
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("DailyJournal Demo")
    print("=" * 60)

    # Demo: 写入
    writer = JournalWriter('2026-04-11')

    # 模拟信号
    writer.write_signals([
        {'symbol': '600276.SH', 'direction': 'buy', 'strength': 0.9,
         'reason': 'rsi_oversold(28.3)', 'resonance': True,
         'price': 56.50, 'accepted': True,
         'decision': 'buy', 'decision_reason': '共振信号'},
        {'symbol': '600519.SH', 'direction': 'buy', 'strength': 0.7,
         'reason': 'rsi_oversold(32.1)', 'resonance': False,
         'price': 1650.0, 'accepted': False,
         'decision': 'hold', 'decision_reason': '无共振，RSI信号不够强'},
    ])

    # 模拟持仓
    writer.write_positions([
        {'symbol': '600276.SH', 'shares': 8800, 'entry_price': 56.50,
         'current_price': 57.06, 'market_value': 8800*57.06,
         'cost': 8800*56.50, 'unrealized_pnl': 8800*0.56,
         'unrealized_pnl_pct': 0.56/56.50, 'hold_days': 3},
    ])

    # 模拟市场环境
    writer.write_market_context({
        'date': '2026-04-11',
        'hs300_price': 3800.5,
        'hs300_ma200': 3750.0,
        'hs300_pct_above_ma200': 1.35,
        'regime': 'bull',
        'volume_ratio': 1.12,
    })

    writer.finalize(equity=3_100_000, cash=2_300_000)
    print(f"\n  Journal written to: {writer.day_dir}")

    # Demo: 读取
    print("\n" + "=" * 60)
    print("Reading Journal")
    print("=" * 60)

    reader = JournalReader()
    today = reader.get_day('2026-04-11')
    print(f"\n  Meta: {today['meta']}")
    print(f"  Signals: {len(today['signals'])}")
    print(f"  Positions: {len(today['positions'])}")
    print(f"  Market: {today['market']}")

    # 股票历史
    print("\n  600276.SH recent history:")
    hist = reader.get_stock_history('600276.SH', days=5)
    for h in hist:
        print(f"    {h['date']} [{h['type']}]")

    # 摘要
    print("\n  Summary:")
    print(reader.get_summary_text('2026-04-11'))
