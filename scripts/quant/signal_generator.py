"""
SignalGenerator - 统一信号生成系统
=====================================
将各类信号（技术面、机构面、市场环境）统一封装，
支持信号叠加、权重共振、过滤链

设计:
1. 每个信号源是一个 SignalSource
2. SignalGenerator 汇总所有源，输出统一信号
3. 支持信号强度权重
4. 支持黑名单过滤（涨停、停牌、流动性不足）
"""

import os
import sys
THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader
from backtest import TechnicalIndicators as TI
import numpy as np
import institutional_live as inst_live


# ============================================================
# 信号级别定义
# ============================================================

class SignalType:
    BUY = 'buy'
    SELL = 'sell'
    HOLD = 'hold'


# ============================================================
# 基础信号源
# ============================================================

class SignalSource:
    """信号源基类"""
    name = 'BaseSource'

    def __init__(self, symbol, params=None):
        self.symbol = symbol
        self.params = params or {}
        self.data = []

    def load(self, data_loader, start, end):
        """加载数据"""
        self.data = data_loader.get_kline(self.symbol, start, end)
        return bool(self.data)

    def evaluate(self, i) -> dict:
        """
        评估当前节点

        Returns:
            dict: {
                'signal': BUY/SELL/HOLD,
                'strength': 0.0-1.0,
                'reason': str,
                'meta': {} (额外数据)
            }
        """
        raise NotImplementedError


# ============================================================
# RSI均值回归信号源
# ============================================================

class RSISignalSource(SignalSource):
    """
    RSI超买超卖信号

    参数:
        period: RSI周期 (default 21)
        oversold: 超卖阈值 (default 35)
        overbought: 超买阈值 (default 65)
        min_hold_days: 最小持仓天数 (default 5)
        stop_loss: 止损比例 (default 0.05)
        take_profit: 止盈比例 (default 0.20)
    """
    name = 'RSI'

    def __init__(self, symbol, params=None):
        super().__init__(symbol, params)
        self.period = self.params.get('period', 21)
        # 支持 walkforward_job 输出的 rsi_buy/rsi_sell 参数名
        self.oversold = self.params.get('rsi_buy', self.params.get('oversold', 35))
        self.overbought = self.params.get('rsi_sell', self.params.get('overbought', 65))
        self.stop_loss = self.params.get('stop_loss', 0.05)
        self.take_profit = self.params.get('take_profit', 0.20)
        self.min_hold_days = self.params.get('min_hold_days', 5)

        self._rsi_vals = None
        self._entry_price = 0
        self._entry_idx = 0
        self._hold_days = 0

    def load(self, data_loader, start, end):
        ok = super().load(data_loader, start, end)
        if ok:
            closes = [d['close'] for d in self.data]
            self._rsi_vals = TI.rsi(closes, self.period)
        return ok

    def evaluate(self, i) -> dict:
        if i < self.period + 1 or not self._rsi_vals:
            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'data_not_ready'}

        rsi_prev = self._rsi_vals[i - self.period - 1]
        rsi = self._rsi_vals[i - self.period]
        price = self.data[i]['close']

        # 有持仓
        if self._entry_price > 0:
            self._hold_days += 1
            pnl = (price - self._entry_price) / self._entry_price

            if pnl <= -self.stop_loss:
                self._entry_price = 0
                self._hold_days = 0
                return {
                    'signal': SignalType.SELL,
                    'strength': 1.0,
                    'reason': f'stop_loss({pnl:.1%})',
                    'meta': {'pnl': pnl}
                }

            if pnl >= self.take_profit:
                self._entry_price = 0
                self._hold_days = 0
                return {
                    'signal': SignalType.SELL,
                    'strength': 1.0,
                    'reason': f'take_profit({pnl:.1%})',
                    'meta': {'pnl': pnl}
                }

            if rsi_prev > self.overbought >= rsi and self._hold_days >= self.min_hold_days:
                self._entry_price = 0
                self._hold_days = 0
                return {
                    'signal': SignalType.SELL,
                    'strength': 0.7,
                    'reason': f'rsi_overbought({rsi:.0f})',
                    'meta': {'rsi': rsi, 'pnl': pnl}
                }

            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'holding'}

        # 无持仓 -> 检查买入
        if rsi_prev < self.oversold <= rsi:
            self._entry_price = price
            self._entry_idx = i
            self._hold_days = 0
            return {
                'signal': SignalType.BUY,
                'strength': 0.9,
                'reason': f'rsi_oversold({rsi:.0f})',
                'meta': {'rsi': rsi, 'rsi_prev': rsi_prev}
            }

        return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'no_signal'}

    def _reinit_from_params(self):
        """重新从 params 初始化阈值（在 load_live_params 后调用）"""
        self.period = self.params.get('period', 21)
        self.oversold = self.params.get('rsi_buy', self.params.get('oversold', 35))
        self.overbought = self.params.get('rsi_sell', self.params.get('overbought', 65))
        self.stop_loss = self.params.get('stop_loss', 0.05)
        self.take_profit = self.params.get('take_profit', 0.20)
        self.min_hold_days = self.params.get('min_hold_days', 5)

    def reset(self):
        self._entry_price = 0
        self._entry_idx = 0
        self._hold_days = 0


# ============================================================
# MACD信号源
# ============================================================

class MACDSignalSource(SignalSource):
    """
    MACD信号

    金叉买入，死叉卖出
    """
    name = 'MACD'

    def __init__(self, symbol, params=None):
        super().__init__(symbol, params)
        self.fast = self.params.get('fast', 12)
        self.slow = self.params.get('slow', 26)
        self.signal = self.params.get('signal', 9)
        self.stop_loss = self.params.get('stop_loss', 0.08)
        self.take_profit = self.params.get('take_profit', 0.25)
        self.min_hold_days = self.params.get('min_hold_days', 10)

        self._macd_vals = None
        self._signal_vals = None
        self._hist_vals = None
        self._entry_price = 0
        self._hold_days = 0

    def _ema(self, data, period):
        k = 2.0 / (period + 1)
        ema = [data[0]]
        for v in data[1:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    def load(self, data_loader, start, end):
        ok = super().load(data_loader, start, end)
        if ok:
            closes = [d['close'] for d in self.data]
            ema12 = self._ema(closes, 12)
            ema26 = self._ema(closes, 26)
            macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
            signal_line = self._ema(macd_line, 9)
            self._macd_vals = macd_line
            self._signal_vals = signal_line
            self._hist_vals = [m - s for m, s in zip(macd_line, signal_line)]
        return ok

    def evaluate(self, i) -> dict:
        if i < self.slow or not self._hist_vals:
            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'data_not_ready'}

        hist_prev = self._hist_vals[i - 1]
        hist = self._hist_vals[i]
        price = self.data[i]['close']

        if self._entry_price > 0:
            self._hold_days += 1
            pnl = (price - self._entry_price) / self._entry_price

            if pnl <= -self.stop_loss:
                self._entry_price = 0
                self._hold_days = 0
                return {'signal': SignalType.SELL, 'strength': 1.0,
                        'reason': f'stop_loss({pnl:.1%})', 'meta': {'pnl': pnl}}

            if pnl >= self.take_profit:
                self._entry_price = 0
                self._hold_days = 0
                return {'signal': SignalType.SELL, 'strength': 1.0,
                        'reason': f'take_profit({pnl:.1%})', 'meta': {'pnl': pnl}}

            if hist_prev > 0 > hist and self._hold_days >= self.min_hold_days:
                self._entry_price = 0
                self._hold_days = 0
                return {'signal': SignalType.SELL, 'strength': 0.8,
                        'reason': 'macd_death_cross', 'meta': {'pnl': pnl}}

            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'holding'}

        # 金叉买入
        if hist_prev < 0 <= hist:
            self._entry_price = price
            self._hold_days = 0
            return {
                'signal': SignalType.BUY,
                'strength': 0.8,
                'reason': f'macd_golden_cross',
                'meta': {'hist': hist}
            }

        return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'no_signal'}

    def _reinit_from_params(self):
        """重新从 params 初始化参数（在 load_live_params 后调用）"""
        self.fast = self.params.get('fast', 12)
        self.slow = self.params.get('slow', 26)
        self.signal = self.params.get('signal', 9)
        self.stop_loss = self.params.get('stop_loss', 0.08)
        self.take_profit = self.params.get('take_profit', 0.25)
        self.min_hold_days = self.params.get('min_hold_days', 10)

    def reset(self):
        self._entry_price = 0
        self._hold_days = 0


# ============================================================
# 机构持仓信号源
# ============================================================

class InstitutionalSignalSource(SignalSource):
    """
    机构持仓信号

    基于基金重仓评分:
    - score > 15: 强烈买入
    - score > 8: 买入
    - score > 3: 中性
    - score <= 3: 减持
    """
    name = 'Institutional'

    def __init__(self, symbol, params=None):
        super().__init__(symbol, params)
        self.quarter = self.params.get('quarter', '20243')
        self.min_score = self.params.get('min_score', 5.0)

    def evaluate(self, i) -> dict:
        if i < 60:
            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'warmup'}

        inst = inst_live.get_etf_institutional_score(self.symbol, self.quarter)
        score = inst.get('total_score', 0)
        signal = inst.get('signal', 'hold')

        if score >= 15:
            return {
                'signal': SignalType.BUY,
                'strength': 0.9,
                'reason': f'inst_strong(score={score:.0f})',
                'meta': {'score': score, 'signal': signal}
            }
        elif score >= 8:
            return {
                'signal': SignalType.BUY,
                'strength': 0.7,
                'reason': f'inst_buy(score={score:.0f})',
                'meta': {'score': score, 'signal': signal}
            }
        elif score < self.min_score:
            return {
                'signal': SignalType.SELL,
                'strength': 0.6,
                'reason': f'inst_low_score(score={score:.0f})',
                'meta': {'score': score}
            }

        return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'inst_neutral'}

    def reset(self):
        pass


# ============================================================
# 市场环境信号源
# ============================================================

class MarketRegimeSource(SignalSource):
    """
    市场环境过滤

    用MA200判断牛熊市:
    - bull: price > MA200 -> 允许做多
    - neutral: MA200方向不明 -> 允许做多但谨慎
    - bear: price < MA200 -> 禁止开新仓（已有持仓可持有或卖出）
    """
    name = 'MarketRegime'

    def __init__(self, symbol, params=None):
        super().__init__(symbol, params)
        self.ma_period = self.params.get('ma_period', 200)

    def load(self, data_loader, start, end):
        ok = super().load(data_loader, start, end)
        return ok

    def evaluate(self, i) -> dict:
        if i < self.ma_period:
            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'warmup'}

        closes = [d['close'] for d in self.data]
        ma = sum(closes[i - self.ma_period + 1:i + 1]) / self.ma_period
        price = closes[i]

        pct_above = (price - ma) / ma * 100

        if pct_above > 2:  # 价格在MA200上方2%+
            regime = 'bull'
            strength = 0.0  # 不提供交易信号，只做过滤
        elif pct_above < -2:  # 价格在MA200下方2%-
            regime = 'bear'
            strength = 0.5
        else:
            regime = 'neutral'
            strength = 0.1

        return {
            'signal': SignalType.HOLD,
            'strength': strength,
            'reason': f'market_{regime}({pct_above:+.1f}%)',
            'meta': {'regime': regime, 'pct_above': pct_above, 'ma': ma}
        }

    def reset(self):
        pass


# ============================================================
# 布林带信号源
# ============================================================

class BollingerBandSource(SignalSource):
    """
    布林带均值回归信号

    价格触及下轨买入（超卖），触及上轨卖出（超买）
    """
    name = 'BollingerBand'

    def __init__(self, symbol, params=None):
        super().__init__(symbol, params)
        self.period = self.params.get('period', 20)
        self.num_std = self.params.get('num_std', 2.0)
        self.stop_loss = self.params.get('stop_loss', 0.06)
        self.take_profit = self.params.get('take_profit', 0.20)
        self.min_hold_days = self.params.get('min_hold_days', 5)

        self._upper = None
        self._middle = None
        self._lower = None
        self._entry_price = 0
        self._hold_days = 0

    def load(self, data_loader, start, end):
        ok = super().load(data_loader, start, end)
        if ok:
            closes = [d['close'] for d in self.data]
            self._middle, self._upper, self._lower = TI.bollinger_bands(closes,
                self.period, self.num_std)
        return ok

    def evaluate(self, i) -> dict:
        if i < self.period or not self._lower:
            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'data_not_ready'}

        price = self.data[i]['close']
        lower = self._lower[i - self.period]
        upper = self._upper[i - self.period]
        middle = self._middle[i - self.period]

        # 布林带位置
        bb_pos = (price - lower) / (upper - lower) if upper != lower else 0.5

        if self._entry_price > 0:
            self._hold_days += 1
            pnl = (price - self._entry_price) / self._entry_price

            if pnl <= -self.stop_loss:
                self._entry_price = 0
                self._hold_days = 0
                return {'signal': SignalType.SELL, 'strength': 1.0,
                        'reason': f'stop_loss({pnl:.1%})', 'meta': {'pnl': pnl}}

            if pnl >= self.take_profit:
                self._entry_price = 0
                self._hold_days = 0
                return {'signal': SignalType.SELL, 'strength': 1.0,
                        'reason': f'take_profit({pnl:.1%})', 'meta': {'pnl': pnl}}

            # 价格触及上轨 + RSI超买
            if price >= upper * 0.98 and self._hold_days >= self.min_hold_days:
                self._entry_price = 0
                self._hold_days = 0
                return {'signal': SignalType.SELL, 'strength': 0.8,
                        'reason': f'bb_upper_hit', 'meta': {'bb_pos': bb_pos}}

            return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'holding'}

        # 触及下轨超卖买入
        if price <= lower * 1.02:
            self._entry_price = price
            self._hold_days = 0
            return {
                'signal': SignalType.BUY,
                'strength': 0.8,
                'reason': f'bb_lower_hit(pos={bb_pos:.2f})',
                'meta': {'bb_pos': bb_pos, 'lower': lower}
            }

        return {'signal': SignalType.HOLD, 'strength': 0.0, 'reason': 'no_signal'}

    def reset(self):
        self._entry_price = 0
        self._hold_days = 0


# ============================================================
# 信号生成器
# ============================================================

class SignalGenerator:
    """
    统一信号生成器

    将多个信号源组合，按规则输出最终信号:
    1. 汇总所有源的信号
    2. 技术信号 + 机构信号共振 -> 强买入
    3. 单一技术信号 -> 弱买入（需通过过滤）
    4. 市场bear环境 -> 禁止开新仓

    使用方式:
        gen = SignalGenerator('600276.SH')
        gen.add_source(RSISignalSource, {'rsi_buy': 35, 'rsi_sell': 70, 'take_profit': 0.30})
        gen.add_source(MACDSignalSource, {})
        gen.add_source(InstitutionalSignalSource, {})
        gen.add_source(MarketRegimeSource, {})
        signal = gen.evaluate(i)  # i = data index
    """

    def __init__(self, symbol):
        self.symbol = symbol
        self.sources = []   # list of (source_instance, weight)
        self._source_instances = {}

    def add_source(self, source_class, params=None, weight=1.0):
        """添加信号源"""
        inst = source_class(self.symbol, params)
        self.sources.append((inst, weight))
        self._source_instances[source_class.name] = inst
        return inst

    def load_live_params(self, live_params: dict):
        """
        用 walkforward 训练出的最新参数覆盖各信号源配置。

        live_params 格式（由 walkforward_job.py 写入 live_params.json）：
            {
              'RSI': {'rsi_buy': 30, 'rsi_sell': 65, 'stop_loss': 0.08, 'take_profit': 0.25},
              'MACD': {'fast': 12, 'slow': 26, 'signal': 9, ...}
            }
        """
        SOURCE_NAME_MAP = {
            'RSISignalSource': 'RSI',
            'MACDSignalSource': 'MACD',
            'BollingerBandSource': 'BollingerBand',
            'InstitutionalSignalSource': 'Institutional',
            'MarketRegimeSource': 'MarketRegime',
        }
        for src, weight in self.sources:
            key = SOURCE_NAME_MAP.get(src.name, src.name)
            if key in live_params:
                trained = live_params[key]
                # 合并：默认参数被 trained 参数覆盖
                src.params = {**src.params, **trained}
                # 重新初始化信号源内部状态（RSI 用 params 初始化指标阈值）
                src._reinit_from_params()

    def load_all(self, data_loader, start, end):
        """加载所有源的数据"""
        for src, _ in self.sources:
            src.load(data_loader, start, end)
        return True

    def get_source(self, name):
        """获取已注册的信号源"""
        return self._source_instances.get(name)

    def evaluate(self, i) -> dict:
        """
        评估所有信号源，返回综合信号

        Returns:
            {
                'signal': BUY/SELL/HOLD,
                'strength': 0.0-1.0,
                'reason': str,
                'sources': [(name, signal, strength), ...],
                'resonance': bool
            }
        """
        results = []
        buy_signals = []
        sell_signals = []

        for src, weight in self.sources:
            eval_result = src.evaluate(i)
            eval_result['weight'] = weight
            eval_result['name'] = src.name
            results.append(eval_result)

            if eval_result['signal'] == SignalType.BUY:
                buy_signals.append((src.name, eval_result['strength'] * weight))
            elif eval_result['signal'] == SignalType.SELL:
                sell_signals.append((src.name, eval_result['strength'] * weight))

        # 检查市场环境
        market_regime = 'bull'  # 默认
        for src, _ in self.sources:
            if src.name == 'MarketRegime':
                eval_result = src.evaluate(i)
                regime = eval_result.get('meta', {}).get('regime', 'bull')
                if regime == 'bear':
                    # bear市场：禁止新开仓，只允许持有或卖出
                    # 如果有BUY信号，降级为HOLD
                    if buy_signals:
                        buy_signals = []  # 清空买入信号

        # 综合买入信号
        final_signal = SignalType.HOLD
        final_strength = 0.0
        resonance = False

        if buy_signals:
            total_buy_strength = sum(s for _, s in buy_signals)
            names = [n for n, _ in buy_signals]

            # 共振：2个以上不同类型的信号同时买入
            if len(buy_signals) >= 2:
                resonance = True
                final_signal = SignalType.BUY
                final_strength = min(total_buy_strength / len(buy_signals) * 1.2, 1.0)
            elif len(buy_signals) == 1:
                # 单一信号：使用原始强度（略降低）
                name, strength = buy_signals[0]
                # 机构信号单独不触发，需要技术信号确认
                if name == 'Institutional' and len([n for n, _ in buy_signals]) == 1:
                    final_signal = SignalType.HOLD
                    final_strength = 0.0
                else:
                    final_signal = SignalType.BUY
                    final_strength = strength * 0.8  # 单信号稍降权

        elif sell_signals:
            total_sell_strength = sum(s for _, s in sell_signals)
            if total_sell_strength >= 0.5:  # 至少0.5强度才触发卖出
                final_signal = SignalType.SELL
                final_strength = min(total_sell_strength / len(sell_signals), 1.0)

        return {
            'signal': final_signal,
            'strength': final_strength,
            'reason': f"{'RESONANCE' if resonance else 'solo'}: {buy_signals or sell_signals or 'no signal'}",
            'sources': [(r['name'], r['signal'], r['strength']) for r in results],
            'resonance': resonance,
            'buy_sources': buy_signals,
            'sell_sources': sell_signals
        }

    def reset_all(self):
        for src, _ in self.sources:
            src.reset()


# ============================================================
# 黑名单过滤
# ============================================================

class BlackListFilter:
    """
    黑名单过滤器

    过滤条件:
    - 涨停/跌停日不可买入
    - 停牌日不可交易
    - 成交量异常低（疑似流动性枯竭）
    """

    def __init__(self, min_volume_ratio=0.001, up_limit_discount=0.90):
        self.min_volume_ratio = min_volume_ratio
        self.up_limit_discount = up_limit_discount  # 涨停折扣（≥此比例视为涨停）

    def can_buy(self, data, i) -> tuple:
        """
        Returns: (allowed: bool, reason: str)
        """
        if i < 1:
            return True, 'warmup'

        d = data[i]
        prev = data[i - 1]

        # 涨停检查（昨日收盘涨停，今日不可追）
        prev_close = prev['close']
        prev_change = (d['close'] - prev_close) / prev_close if 'close' in d and 'close' in prev else 0
        if prev_change > 0.095:  # 昨日涨停
            return False, f'prev_limit_up({prev_change:.1%})'

        # 停牌检查（今日无成交量）
        if d.get('volume', 0) <= 0:
            return False, 'suspended'

        # 流动性检查
        turnover = d.get('turnover', 0)
        if turnover > 0 and d.get('volume', 0) > 0:
            avg_price = turnover / d.get('volume', 1)
            if avg_price > 0:
                daily_value = d.get('volume', 0) * avg_price
                if daily_value < 100000:  # 日成交额小于10万
                    return False, f'low_liquidity({daily_value:,.0f})'

        return True, ''

    def can_sell(self, data, i) -> tuple:
        """卖出限制较少，主要检查停牌"""
        if d.get('volume', 0) <= 0:
            return False, 'suspended'
        return True, ''


# ============================================================
# 使用示例
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("SignalGenerator Test")
    print("=" * 60)

    # 创建信号生成器
    gen = SignalGenerator('300750.SZ')
    gen.add_source(RSISignalSource, {
        'rsi_buy': 35, 'rsi_sell': 70,
        'stop_loss': 0.05, 'take_profit': 0.30
    }, weight=1.0)
    gen.add_source(MACDSignalSource, {
        'stop_loss': 0.08, 'take_profit': 0.25
    }, weight=0.8)
    gen.add_source(InstitutionalSignalSource, {}, weight=1.0)
    gen.add_source(MarketRegimeSource, {}, weight=0.5)

    # 加载数据
    loader = DataLoader()
    gen.load_all(loader, '20200101', '20251231')

    # 评估前50天
    print("\nFirst 50 days signal evaluation:")
    for i in range(60, min(110, len(gen.get_source('RSI').data))):
        src = gen.get_source('RSI')
        date = src.data[i]['date']
        result = gen.evaluate(i)

        if result['signal'] != SignalType.HOLD or result['resonance']:
            print(f"\n  {date}: {result['signal']} (strength={result['strength']:.2f}) "
                  f"{'[RESONANCE]' if result['resonance'] else ''}")
            for name, sig, str2 in result['sources']:
                if sig != SignalType.HOLD:
                    print(f"    {name}: {sig} (strength={str2:.2f})")
