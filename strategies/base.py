"""
strategies/base.py — 策略插件基类
=================================
定义 BaseStrategy 抽象基类，所有策略插件继承此类。

子类必须实现的方法：
  - evaluate(self, data, i) -> dict
  - reset()

子类可选覆盖：
  - __init__(...) — 接收 symbol + params
"""

from typing import Dict, List, Optional


class BaseStrategy:
    """
    策略插件基类。

    Properties:
      name       — 策略名称
      version    — 版本号
      symbol     — 标的代码
      params     — 配置参数字典

    Methods:
      evaluate(data, i) — 核心评估逻辑，返回信号 dict
      reset()           — 重置状态
    """

    name    = 'BaseStrategy'
    version = '1.0'

    def __init__(self, symbol: str, params: Optional[dict] = None):
        self.symbol = symbol
        self.params = params or {}
        self._data: List[dict] = []
        self._state: dict = {}   # 插件私有状态

    # ── 子类必须实现 ────────────────────────────────────────

    def evaluate(self, data: List[dict], i: int) -> Dict:
        """
        评估信号。

        Args:
            data: K线数据 [{date, open, high, low, close, volume}, ...]
            i: 当前评估点在 data 中的索引

        Returns:
            {
                'signal':  'buy' | 'sell' | 'hold',
                'strength': 0.0 ~ 1.0,
                'reason':  str,
                'meta':    dict (可选)
            }
        """
        raise NotImplementedError(f"{self.name} must implement evaluate()")

    def reset(self):
        """重置内部状态（标的切换时调用）"""
        self._state = {}

    # ── 工具方法 ───────────────────────────────────────────

    def closes(self) -> List[float]:
        return [d['close'] for d in self._data]

    def highs(self) -> List[float]:
        return [d.get('high', d['close']) for d in self._data]

    def lows(self) -> List[float]:
        return [d.get('low', d['close']) for d in self._data]

    def volumes(self) -> List[float]:
        return [d.get('volume', 0) for d in self._data]

    @staticmethod
    def compute_rsi(closes: List[float], period: int = 14) -> List[float]:
        """计算 RSI 序列"""
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
    def compute_ema(data: List[float], period: int) -> List[float]:
        """计算 EMA 序列"""
        if len(data) < period:
            return []
        k = 2.0 / (period + 1)
        ema = [sum(data[:period]) / period]
        for v in data[period:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    def __repr__(self):
        return f"<{self.name}({self.symbol})>"
