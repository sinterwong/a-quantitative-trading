"""
FactorRegistry — 因子注册表

职责：
- 维护 name → Factor 类的映射
- 支持按名称实例化因子（传入 params）
- 开箱即用：自动注册所有内置因子
- 支持外部插件通过 register() 扩展

用法：
    from core.factor_registry import registry

    factor = registry.create('RSI', period=21, symbol='600519.SH')
    all_names = registry.list_factors()
"""

from __future__ import annotations
from typing import Dict, Type, Any, List, Optional
from core.factors.base import Factor


class FactorRegistry:
    """因子注册表（单例）。"""

    def __init__(self) -> None:
        self._factors: Dict[str, Type[Factor]] = {}
        self._defaults: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        cls: Type[Factor],
        *,
        name: Optional[str] = None,
        default_params: Optional[Dict[str, Any]] = None,
    ) -> Type[Factor]:
        """
        注册因子类。

        Parameters
        ----------
        cls:
            Factor 的子类
        name:
            注册名（默认使用 cls.name）
        default_params:
            实例化时的默认参数（可被 create() 覆盖）

        Returns
        -------
        cls（方便作为装饰器使用）
        """
        if not (isinstance(cls, type) and issubclass(cls, Factor)):
            raise TypeError(f"{cls} must be a subclass of Factor")
        key = name or cls.name
        self._factors[key] = cls
        self._defaults[key] = default_params or {}
        return cls

    def unregister(self, name: str) -> None:
        """移除已注册的因子（测试用）。"""
        self._factors.pop(name, None)
        self._defaults.pop(name, None)

    # ------------------------------------------------------------------
    # Lookup & instantiation
    # ------------------------------------------------------------------

    def get_class(self, name: str) -> Type[Factor]:
        """根据名称返回因子类；未找到则抛出 KeyError。"""
        try:
            return self._factors[name]
        except KeyError:
            raise KeyError(
                f"Factor '{name}' not registered. "
                f"Available: {self.list_factors()}"
            )

    def create(self, name: str, **kwargs: Any) -> Factor:
        """
        按名称实例化因子。

        kwargs 会覆盖 default_params 中同名参数。
        """
        cls = self.get_class(name)
        params = {**self._defaults.get(name, {}), **kwargs}
        return cls(**params)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_factors(self) -> List[str]:
        """返回所有已注册因子名（字母序）。"""
        return sorted(self._factors.keys())

    def get_default_params(self, name: str) -> Dict[str, Any]:
        """返回因子默认参数副本。"""
        return dict(self._defaults.get(name, {}))

    def __contains__(self, name: str) -> bool:
        return name in self._factors

    def __len__(self) -> int:
        return len(self._factors)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

registry = FactorRegistry()


def _auto_register() -> None:
    """自动注册所有内置因子。"""
    from core.factors.price_momentum import (
        RSIFactor, BollingerFactor, MACDFactor, ATRFactor, OrderImbalanceFactor,
    )

    registry.register(RSIFactor, default_params={'period': 14})
    registry.register(BollingerFactor, default_params={'period': 20, 'nb_std': 2.0})
    registry.register(MACDFactor, default_params={'fast': 12, 'slow': 26, 'signal': 9})
    registry.register(ATRFactor, default_params={'period': 14, 'lookback': 20})
    registry.register(OrderImbalanceFactor, default_params={'window': 10})


_auto_register()
