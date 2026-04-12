"""
strategies/__init__.py — 策略插件注册中心
==========================================
所有策略插件在此注册，支持热插拔。

用法：
    from strategies import STRATEGY_REGISTRY, load_strategy

    # 加载指定策略
    plugin = load_strategy('RSI', {'rsi_buy': 30, 'rsi_sell': 65}, symbol='600519.SH')

    # 列出所有可用策略
    from strategies import list_strategies
    print(list_strategies())
"""

import os
import sys
from typing import Dict, Type, Optional

# ─── 确保策略包顶层可导入 ────────────────────────────────────
# 将 quant_repo 根目录加入 sys.path（如果尚未加入）
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # strategies/ -> quant_repo/
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# ─── 内置策略注册表 ─────────────────────────────────────────

STRATEGY_REGISTRY: Dict[str, Type['BaseStrategy']] = {}


def register_strategy(name: str):
    """装饰器：将策略类注册到全局注册表"""
    def decorator(cls: Type['BaseStrategy']):
        STRATEGY_REGISTRY[name] = cls
        return cls
    return decorator


def load_strategy(name: str,
                   params: Optional[dict] = None,
                   symbol: str = '') -> Optional['BaseStrategy']:
    """
    根据名称加载策略插件。

    Args:
        name:  策略名称（'RSI', 'MACD', 'BollingerBand'）
        params: 策略参数 dict
        symbol: 股票代码
    Returns:
        策略实例，或 None（策略不存在）
    """
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        return None
    return cls(symbol=symbol, params=params or {})


def list_strategies() -> Dict[str, str]:
    """返回所有已注册策略的名称和描述"""
    return {
        name: (cls.__doc__.strip().split('\n')[0] if cls.__doc__ else '')
        for name, cls in STRATEGY_REGISTRY.items()
    }


# ─── 显式导入并注册所有内置策略 ──────────────────────────────
# 必须在这里显式导入，不能用 auto-discovery（避免 ImportError 静默失败）

try:
    from strategies.rsi_strategy import RSIStrategy
    STRATEGY_REGISTRY['RSI'] = RSIStrategy
except ImportError as e:
    pass

try:
    from strategies.macd_strategy import MACDStrategy
    STRATEGY_REGISTRY['MACD'] = MACDStrategy
except ImportError as e:
    pass

try:
    from strategies.bollinger_strategy import BollingerBandStrategy
    STRATEGY_REGISTRY['BollingerBand'] = BollingerBandStrategy
except ImportError as e:
    pass


# ─── 策略插件基类（可直接继承）───────────────────────────────

class BaseStrategy:
    """
    所有策略插件的基类。

    子类需要实现：
      - evaluate(self, data: list, i: int) -> dict
      - reset()
    """

    name    = 'BaseStrategy'
    version = '1.0'

    def __init__(self, symbol: str, params: Optional[dict] = None):
        self.symbol = symbol
        self.params = params or {}
        self._data: list = []
        self._state: dict = {}

    def evaluate(self, data: list, i: int) -> dict:
        raise NotImplementedError

    def reset(self):
        self._state = {}

    def closes(self) -> list:
        return [d['close'] for d in self._data]

    @staticmethod
    def compute_rsi(closes: list, period: int = 14) -> list:
        if len(closes) < period + 1:
            return []
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return [100.0] * len(closes)
        rs = avg_gain / avg_loss
        return [round(100 - (100 / (1 + rs)), 4)] * len(closes)

    @staticmethod
    def compute_ema(data: list, period: int) -> list:
        if len(data) < period:
            return []
        k = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for v in data[period:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    def __repr__(self):
        return f"<{self.name}({self.symbol})>"
