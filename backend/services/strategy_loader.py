"""
strategy_loader.py — 策略插件加载器
=====================================
从 params.json 读取策略配置，动态加载策略插件。

params.json 格式：
{
  "strategies": {
    "RSI": {
      "symbol": "600519.SH",
      "params": {"rsi_buy": 30, "rsi_sell": 65, "stop_loss": 0.08}
    },
    "MACD": {
      "symbol": "600900.SH",
      "params": {"fast_period": 8, "slow_period": 26}
    }
  }
}

用法：
    loader = StrategyLoader(params_file='params.json')
    strategy = loader.load('RSI')
    result = strategy.evaluate(kline_data, i=-1)
"""

import os
import sys
import json
from typing import Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/ -> quant_repo/
PARAMS_FILE = os.path.join(BASE_DIR, 'params.json')


def get_strategy_path() -> str:
    """获取 params.json 路径"""
    if os.path.exists(PARAMS_FILE):
        return PARAMS_FILE
    fallback = os.path.join(BASE_DIR, 'scripts', 'params.json')
    return fallback if os.path.exists(fallback) else PARAMS_FILE


def load_strategy_config() -> Dict:
    """从 params.json 读取策略配置"""
    path = get_strategy_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get('strategies', {})
    except Exception:
        return {}


class StrategyLoader:
    """
    策略插件加载器。

    使用示例：
        loader = StrategyLoader()
        strat = loader.load('RSI')
        if strat:
            result = strat.evaluate(data, i=-1)
    """

    def __init__(self, config: Optional[Dict] = None):
        # 将 quant_repo 加入 sys.path（确保 strategies 可导入）
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)

        # 同时支持 strategies 和 defaults 两种 key（defaults 是旧格式）
        raw = config or load_strategy_config()
        self._config = {}
        for key, val in raw.items():
            if key == 'strategies':
                self._config.update(val)
            elif key == 'defaults':
                # defaults 里每个 key 本身就是策略名
                self._config.update(val)

        # 延迟导入避免循环依赖
        from strategies import STRATEGY_REGISTRY
        self._registry = STRATEGY_REGISTRY

    def load(self, name: str, symbol: Optional[str] = None,
             params: Optional[Dict] = None) -> Optional[object]:
        """
        加载策略实例。

        优先级：显式传入参数 > params.json 配置 > 默认参数
        """
        if name not in self._registry:
            return None

        # 合并参数：params.json 配置 + 显式传入
        cfg = self._config.get(name, {})
        merged_params = {**cfg.get('params', {}), **(params or {})}
        sym = symbol or cfg.get('symbol', '')

        cls = self._registry[name]
        return cls(symbol=sym, params=merged_params)

    def load_all(self) -> Dict[str, object]:
        """加载所有配置过的策略"""
        result = {}
        for name in self._config:
            strat = self.load(name)
            if strat:
                result[name] = strat
        return result

    def list_available(self) -> Dict[str, str]:
        """列出所有可用策略及描述"""
        from strategies import list_strategies
        return list_strategies()
