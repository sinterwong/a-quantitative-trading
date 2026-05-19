"""Backend HTTP layer dependencies.

Owns the broker / risk-engine / idempotency-store getters used by route
handlers. Crucially, this module has **no dependency on Flask or
backend.api**, so Blueprint modules can ``from backend.api_deps import
_get_or_build_broker, _get_risk_engine`` directly — without going through
backend.api (which would force the Blueprint to coexist with the helpers
during a partial module load and require the ``sys.modules`` reflection
trick that the R2-4 review flagged).

Why this matters:
    - Test isolation: ``patch.object(backend.api_deps,
      '_get_or_build_broker', ...)`` works regardless of how the Blueprint
      was loaded.
    - No more circular-import-by-importlib gotchas when tests load route
      modules in isolation.

PortfolioService getter is *not* moved here: it lives in ``backend.api``
where the Flask app object also lives, and route modules already import
it via ``from backend.api import get_svc``. Moving it here would create a
new circular: api → api_deps → api.

Test pattern (matches what tests already do for backend.api):

    import backend.api_deps as api_deps
    with patch.object(api_deps, '_get_or_build_broker', return_value=fake):
        ...
"""

from __future__ import annotations

from typing import Any, Optional

from core.singleton import LockedSingleton


# ─── Idempotency store ─────────────────────────────────────────────────────
# R0-1: same Idempotency-Key inside 24h returns the original response.

def _make_idempotency_store() -> Any:
    from core.idempotency import IdempotencyStore
    return IdempotencyStore()


_idempotency_store_singleton: LockedSingleton = LockedSingleton(
    _make_idempotency_store, name="api.idempotency_store",
)


# ─── Risk engine ────────────────────────────────────────────────────────────
# Flask 多线程 WSGI 下两个请求并发进入懒建分支会创建两份 RiskEngine,
# 其 __init__ 有副作用(打开 sqlite 句柄、注册回调),所以必须加锁。

def _make_risk_engine() -> Any:
    from core.risk_engine import RiskEngine
    return RiskEngine()


_risk_engine_singleton: LockedSingleton = LockedSingleton(
    _make_risk_engine, name="api.risk_engine",
)


def _get_risk_engine() -> Optional[Any]:
    """共享 RiskEngine:优先复用 StrategyRunner 的实例,否则懒建本地 singleton。

    返回 None 表示 RiskEngine 不可用(初始化失败 / 配置缺失);调用方决定如何处理。
    """
    try:
        from quant_app.main import get_monitor
        m = get_monitor()
        if m is not None and getattr(m, '_strategy_runner', None) is not None:
            re = getattr(m._strategy_runner, 'risk_engine', None)
            if re is not None:
                return re
    except Exception:
        # quant_app 未启动 / monitor 还没构造完 — 都走 fallback。
        pass
    try:
        return _risk_engine_singleton.get()
    except Exception:
        return None


# ─── Broker ─────────────────────────────────────────────────────────────────


def _get_or_build_broker() -> Any:
    """生产 broker:优先复用 quant_app.main.get_broker() 的共享实例;
    测试 / 无 monitor 场景回退到新建本地 PaperBroker。"""
    try:
        from quant_app.main import get_broker
        b = get_broker()
        if b is not None:
            return b
    except Exception:
        # quant_app 未启动 — 进 fallback。
        pass
    # Fallback: 本地 PaperBroker(走 backend.api.get_svc() 拿同一个
    # PortfolioService 单例)。
    from backend.api import get_svc
    from services.broker import PaperBroker
    b = PaperBroker(portfolio_service=get_svc())
    b.connect()
    return b
