"""
core/config.py — 统一配置加载器

读取 config/trading.yaml，支持环境覆盖（live/dev）。

用法：
    from core.config import load_config, TradingConfig

    cfg = load_config()              # 使用环境变量 TRADING_ENV（默认 dev）
    cfg = load_config(env='live')    # 强制 live

    # 访问
    cfg.portfolio.capital            # 20000
    cfg.risk.max_drawdown            # 0.15
    cfg.runner.dry_run               # True/False
    cfg.strategy('RSI')              # StrategyConfig(symbol=..., factors=...)
    cfg.live_symbols                 # ['510310.SH', ...]

设计：
    - 纯 Python dataclass，无运行时魔法
    - deep_merge 确保 env 覆盖只改需要改的字段
    - 向后兼容：可从 params.json + live_params.json 迁移
    - TRADING_ENV 环境变量控制 dev/live 切换
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# pyyaml 是项目已有依赖
import yaml


# ---------------------------------------------------------------------------
# 默认配置路径
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "trading.yaml"


# ---------------------------------------------------------------------------
# Dataclasses（typed config nodes）
# ---------------------------------------------------------------------------

@dataclass
class PortfolioConfig:
    capital: float = 20_000
    max_positions: int = 5
    max_position_pct: float = 0.25
    layer_size: float = 2_000
    max_layers: int = 2


@dataclass
class RiskConfig:
    # PreTrade
    max_net_exposure: float = 0.90
    max_daily_loss: float = 0.02
    # InTrade
    atr_stop_multiplier: float = 3.0
    take_profit_pct: float = 0.20
    trailing_drawdown: float = 0.10
    # Portfolio-level
    max_drawdown: float = 0.15
    max_sector_weight: float = 0.30
    var_limit: float = 0.03
    max_correlation: float = 0.85
    # Cost
    commission_rate: float = 0.0003
    stamp_tax: float = 0.001
    slippage_bps: float = 5.0


@dataclass
class FactorConfig:
    name: str = ''
    weight: float = 1.0
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyConfig:
    symbol: str = ''
    factors: List[FactorConfig] = field(default_factory=list)
    signal_threshold: float = 0.5
    bars_lookback: int = 120
    stop_loss: float = 0.05
    take_profit: float = 0.20
    atr_threshold: float = 0.85
    min_hold_days: int = 3
    note: str = ''


@dataclass
class LiveSymbolConfig:
    symbol: str = ''
    strategy: str = ''
    note: str = ''


@dataclass
class RunnerConfig:
    interval: int = 300
    dry_run: bool = True
    min_bars: int = 30
    bars_lookback: int = 120
    log_level: str = 'INFO'


@dataclass
class DataConfig:
    bar_ttl: int = 3600
    quote_ttl: int = 30
    primary_source: str = 'tencent'
    fallback_source: str = 'sina'
    request_timeout: int = 10


@dataclass
class AlertsConfig:
    feishu_webhook: str = ''
    log_only: bool = True


@dataclass
class TradingConfig:
    """
    顶层配置对象。
    通过 load_config() 获取，不要直接实例化。
    """
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    data: DataConfig = field(default_factory=DataConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    strategies: Dict[str, StrategyConfig] = field(default_factory=dict)
    live_symbols: List[LiveSymbolConfig] = field(default_factory=list)
    env: str = 'dev'
    _raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def strategy(self, name: str) -> Optional[StrategyConfig]:
        """按名称查找策略配置（未找到则 None）。"""
        return self.strategies.get(name)

    def live_symbol_list(self) -> List[str]:
        """返回实盘标的的 symbol 列表。"""
        return [s.symbol for s in self.live_symbols]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(
    path: Optional[str] = None,
    env: Optional[str] = None,
) -> TradingConfig:
    """
    加载并解析 trading.yaml。

    Parameters
    ----------
    path:
        配置文件路径（默认 config/trading.yaml）
    env:
        环境名称 'live' | 'dev'（默认读 TRADING_ENV 环境变量，再默认 'dev'）

    Returns
    -------
    TradingConfig
    """
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    if not config_path.exists():
        # 没有 YAML 文件时返回默认配置（保证测试不依赖文件存在）
        return TradingConfig(env=env or _resolve_env())

    with open(config_path, encoding='utf-8') as f:
        raw_docs = list(yaml.safe_load_all(f))

    # safe_load_all 可能返回多个文档（因为有 --- 分隔符）
    base = raw_docs[0] if raw_docs else {}
    # 第二个文档存放 _env_overrides
    env_overrides_doc = raw_docs[1] if len(raw_docs) > 1 else {}
    env_overrides = (env_overrides_doc or {}).get('_env_overrides', {})

    resolved_env = env or _resolve_env()
    override = env_overrides.get(resolved_env, {})

    merged = _deep_merge(base, override)

    return _parse(merged, env=resolved_env)


def _resolve_env() -> str:
    return os.environ.get('TRADING_ENV', 'dev')


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse(raw: Dict[str, Any], env: str) -> TradingConfig:
    portfolio = _parse_portfolio(raw.get('portfolio', {}))
    risk = _parse_risk(raw.get('risk', {}))
    runner = _parse_runner(raw.get('runner', {}))
    data = _parse_data(raw.get('data', {}))
    alerts = _parse_alerts(raw.get('alerts', {}))
    strategies = _parse_strategies(raw.get('strategies', {}))
    live_symbols = _parse_live_symbols(raw.get('live_symbols', []))

    return TradingConfig(
        portfolio=portfolio,
        risk=risk,
        runner=runner,
        data=data,
        alerts=alerts,
        strategies=strategies,
        live_symbols=live_symbols,
        env=env,
        _raw=raw,
    )


def _parse_portfolio(d: Dict) -> PortfolioConfig:
    return PortfolioConfig(
        capital=float(d.get('capital', 20_000)),
        max_positions=int(d.get('max_positions', 5)),
        max_position_pct=float(d.get('max_position_pct', 0.25)),
        layer_size=float(d.get('layer_size', 2_000)),
        max_layers=int(d.get('max_layers', 2)),
    )


def _parse_risk(d: Dict) -> RiskConfig:
    return RiskConfig(
        max_net_exposure=float(d.get('max_net_exposure', 0.90)),
        max_daily_loss=float(d.get('max_daily_loss', 0.02)),
        atr_stop_multiplier=float(d.get('atr_stop_multiplier', 3.0)),
        take_profit_pct=float(d.get('take_profit_pct', 0.20)),
        trailing_drawdown=float(d.get('trailing_drawdown', 0.10)),
        max_drawdown=float(d.get('max_drawdown', 0.15)),
        max_sector_weight=float(d.get('max_sector_weight', 0.30)),
        var_limit=float(d.get('var_limit', 0.03)),
        max_correlation=float(d.get('max_correlation', 0.85)),
        commission_rate=float(d.get('commission_rate', 0.0003)),
        stamp_tax=float(d.get('stamp_tax', 0.001)),
        slippage_bps=float(d.get('slippage_bps', 5.0)),
    )


def _parse_runner(d: Dict) -> RunnerConfig:
    return RunnerConfig(
        interval=int(d.get('interval', 300)),
        dry_run=bool(d.get('dry_run', True)),
        min_bars=int(d.get('min_bars', 30)),
        bars_lookback=int(d.get('bars_lookback', 120)),
        log_level=str(d.get('log_level', 'INFO')),
    )


def _parse_data(d: Dict) -> DataConfig:
    return DataConfig(
        bar_ttl=int(d.get('bar_ttl', 3600)),
        quote_ttl=int(d.get('quote_ttl', 30)),
        primary_source=str(d.get('primary_source', 'tencent')),
        fallback_source=str(d.get('fallback_source', 'sina')),
        request_timeout=int(d.get('request_timeout', 10)),
    )


def _parse_alerts(d: Dict) -> AlertsConfig:
    return AlertsConfig(
        feishu_webhook=str(d.get('feishu_webhook', '')),
        log_only=bool(d.get('log_only', True)),
    )


def _parse_strategies(d: Dict) -> Dict[str, StrategyConfig]:
    result: Dict[str, StrategyConfig] = {}
    for name, raw in d.items():
        if not isinstance(raw, dict):
            continue
        factors = [
            FactorConfig(
                name=f.get('name', ''),
                weight=float(f.get('weight', 1.0)),
                params=dict(f.get('params', {})),
            )
            for f in raw.get('factors', [])
        ]
        result[name] = StrategyConfig(
            symbol=str(raw.get('symbol', '')),
            factors=factors,
            signal_threshold=float(raw.get('signal_threshold', 0.5)),
            bars_lookback=int(raw.get('bars_lookback', 120)),
            stop_loss=float(raw.get('stop_loss', 0.05)),
            take_profit=float(raw.get('take_profit', 0.20)),
            atr_threshold=float(raw.get('atr_threshold', 0.85)),
            min_hold_days=int(raw.get('min_hold_days', 3)),
            note=str(raw.get('_note', raw.get('note', ''))),
        )
    return result


def _parse_live_symbols(lst: list) -> List[LiveSymbolConfig]:
    result: List[LiveSymbolConfig] = []
    for item in lst:
        if not isinstance(item, dict):
            continue
        result.append(LiveSymbolConfig(
            symbol=str(item.get('symbol', '')),
            strategy=str(item.get('strategy', '')),
            note=str(item.get('note', '')),
        ))
    return result


# ---------------------------------------------------------------------------
# Deep merge utility
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict, override: Dict) -> Dict:
    """
    递归合并两个字典。override 中的值覆盖 base 中的同键值。
    对于嵌套 dict，递归合并而非直接替换。
    """
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


# ---------------------------------------------------------------------------
# Legacy migration helpers
# ---------------------------------------------------------------------------

def load_from_json(
    params_path: str,
    live_params_path: Optional[str] = None,
) -> TradingConfig:
    """
    从旧版 params.json / live_params.json 加载配置（迁移过渡期使用）。
    """
    with open(params_path, encoding='utf-8') as f:
        params = json.load(f)

    cfg = TradingConfig()

    # portfolio
    p = params.get('portfolio', {})
    cfg.portfolio = PortfolioConfig(
        capital=float(p.get('capital', 20_000)),
        max_positions=int(p.get('max_positions', 5)),
        max_position_pct=float(params.get('risk', {}).get('max_position_pct', 0.25)),
        layer_size=float(p.get('layer_size', 2_000)),
        max_layers=int(p.get('max_layers_per_stock', 2)),
    )

    # risk
    r = params.get('risk', {})
    cfg.risk = RiskConfig(
        commission_rate=float(r.get('commission', 0.0003)),
        stamp_tax=float(r.get('stamp_tax', 0.001)),
        slippage_bps=float(r.get('slippage', 0.0005)) * 10_000,
        max_drawdown=float(r.get('max_drawdown_limit', 0.15)),
        max_net_exposure=0.90,
    )

    # strategies
    for name, strat in params.get('strategies', {}).items():
        sp = strat.get('params', {})
        cfg.strategies[name] = StrategyConfig(
            symbol=strat.get('symbol', ''),
            stop_loss=float(sp.get('stop_loss', 0.05)),
            take_profit=float(sp.get('take_profit', 0.20)),
            atr_threshold=float(sp.get('atr_threshold', 0.85)),
            min_hold_days=int(sp.get('min_hold_days', 3)),
        )

    # live_params
    if live_params_path:
        with open(live_params_path, encoding='utf-8') as f:
            live_params = json.load(f)
        seen: set = set()
        for key, item in live_params.items():
            sym = item.get('symbol', '')
            if sym and sym not in seen:
                seen.add(sym)
                cfg.live_symbols.append(LiveSymbolConfig(
                    symbol=sym,
                    strategy=item.get('strategy', ''),
                    note=item.get('note', ''),
                ))

    return cfg
