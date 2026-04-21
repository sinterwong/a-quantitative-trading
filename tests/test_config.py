"""
Phase 5 Tests — Unified Config

运行方式：
    python tests/test_config.py
    pytest tests/test_config.py -v
"""

from __future__ import annotations
import sys
import os
import tempfile
import textwrap

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, PROJ_DIR)

from core.config import (
    load_config, load_from_json,
    TradingConfig, PortfolioConfig, RiskConfig,
    StrategyConfig, FactorConfig, RunnerConfig,
    DataConfig, AlertsConfig, LiveSymbolConfig,
    _deep_merge,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_YAML = textwrap.dedent("""\
    portfolio:
      capital: 50000
      max_positions: 3
    risk:
      max_drawdown: 0.10
    runner:
      dry_run: true
      interval: 60
    strategies:
      RSI:
        symbol: "510310.SH"
        factors:
          - name: RSI
            weight: 0.6
            params:
              period: 14
        signal_threshold: 0.4
        stop_loss: 0.05
        take_profit: 0.20
    live_symbols:
      - symbol: "510310.SH"
        strategy: RSI
""")

ENV_OVERRIDE_YAML = textwrap.dedent("""\
    portfolio:
      capital: 20000
    runner:
      dry_run: true
      interval: 300
    ---
    _env_overrides:
      live:
        portfolio:
          capital: 100000
        runner:
          dry_run: false
          interval: 600
      dev:
        runner:
          interval: 30
""")


def _write_tmp(content: str, suffix: str = '.yaml') -> str:
    """写临时文件，返回路径。"""
    with tempfile.NamedTemporaryFile(
        mode='w', suffix=suffix, delete=False, encoding='utf-8'
    ) as f:
        f.write(content)
        return f.name


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def _check(cond: bool, msg: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {msg}")
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def _section(name: str) -> None:
    print(f"\n=== {name} ===")


# ===========================================================================
# Test classes
# ===========================================================================

class TestDeepMerge:

    def test_simple_override(self):
        base = {'a': 1, 'b': 2}
        over = {'b': 99}
        r = _deep_merge(base, over)
        assert r == {'a': 1, 'b': 99}

    def test_nested_merge(self):
        base = {'a': {'x': 1, 'y': 2}}
        over = {'a': {'y': 99}}
        r = _deep_merge(base, over)
        assert r['a'] == {'x': 1, 'y': 99}

    def test_deep_nested(self):
        base = {'a': {'b': {'c': 1, 'd': 2}}}
        over = {'a': {'b': {'d': 99}}}
        r = _deep_merge(base, over)
        assert r['a']['b']['c'] == 1
        assert r['a']['b']['d'] == 99

    def test_base_not_mutated(self):
        base = {'a': 1}
        over = {'a': 2}
        r = _deep_merge(base, over)
        assert base['a'] == 1   # 原始未变

    def test_override_adds_new_key(self):
        base = {'a': 1}
        over = {'b': 2}
        r = _deep_merge(base, over)
        assert r == {'a': 1, 'b': 2}

    def test_empty_override(self):
        base = {'a': 1, 'b': 2}
        r = _deep_merge(base, {})
        assert r == {'a': 1, 'b': 2}

    def test_empty_base(self):
        over = {'a': 1}
        r = _deep_merge({}, over)
        assert r == {'a': 1}


class TestLoadConfig:

    def test_load_returns_trading_config(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert isinstance(cfg, TradingConfig)
        finally:
            os.unlink(path)

    def test_portfolio_parsed(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert cfg.portfolio.capital == 50_000
            assert cfg.portfolio.max_positions == 3
        finally:
            os.unlink(path)

    def test_risk_parsed(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert abs(cfg.risk.max_drawdown - 0.10) < 1e-9
        finally:
            os.unlink(path)

    def test_runner_parsed(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert cfg.runner.dry_run is True
            assert cfg.runner.interval == 60
        finally:
            os.unlink(path)

    def test_strategy_parsed(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert 'RSI' in cfg.strategies
            s = cfg.strategies['RSI']
            assert s.symbol == '510310.SH'
            assert abs(s.signal_threshold - 0.4) < 1e-9
        finally:
            os.unlink(path)

    def test_strategy_factors(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            s = cfg.strategies['RSI']
            assert len(s.factors) == 1
            f = s.factors[0]
            assert f.name == 'RSI'
            assert f.params['period'] == 14
        finally:
            os.unlink(path)

    def test_live_symbols_parsed(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert len(cfg.live_symbols) == 1
            assert cfg.live_symbols[0].symbol == '510310.SH'
        finally:
            os.unlink(path)

    def test_live_symbol_list(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert '510310.SH' in cfg.live_symbol_list()
        finally:
            os.unlink(path)

    def test_missing_file_returns_defaults(self):
        cfg = load_config(path='/tmp/nonexistent_xyz.yaml', env='dev')
        assert isinstance(cfg, TradingConfig)
        assert cfg.portfolio.capital == 20_000  # 默认值

    def test_env_stored(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path, env='live')
            assert cfg.env == 'live'
        finally:
            os.unlink(path)

    def test_strategy_method(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path)
            s = cfg.strategy('RSI')
            assert s is not None
            assert s.symbol == '510310.SH'
        finally:
            os.unlink(path)

    def test_strategy_method_missing(self):
        path = _write_tmp(MINIMAL_YAML)
        try:
            cfg = load_config(path=path)
            assert cfg.strategy('NonExistent') is None
        finally:
            os.unlink(path)


class TestEnvOverrides:

    def test_live_overrides_applied(self):
        path = _write_tmp(ENV_OVERRIDE_YAML)
        try:
            cfg = load_config(path=path, env='live')
            assert cfg.portfolio.capital == 100_000
            assert cfg.runner.dry_run is False
            assert cfg.runner.interval == 600
        finally:
            os.unlink(path)

    def test_dev_overrides_applied(self):
        path = _write_tmp(ENV_OVERRIDE_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert cfg.portfolio.capital == 20_000   # 未被 dev 覆盖
            assert cfg.runner.interval == 30
        finally:
            os.unlink(path)

    def test_live_dry_run_false(self):
        path = _write_tmp(ENV_OVERRIDE_YAML)
        try:
            cfg = load_config(path=path, env='live')
            assert cfg.runner.dry_run is False
        finally:
            os.unlink(path)

    def test_dev_dry_run_true(self):
        path = _write_tmp(ENV_OVERRIDE_YAML)
        try:
            cfg = load_config(path=path, env='dev')
            assert cfg.runner.dry_run is True
        finally:
            os.unlink(path)

    def test_unknown_env_uses_base(self):
        path = _write_tmp(ENV_OVERRIDE_YAML)
        try:
            cfg = load_config(path=path, env='staging')
            assert cfg.portfolio.capital == 20_000   # 无 staging override，用 base


        finally:
            os.unlink(path)


class TestLoadFromJson:

    def _write_params(self) -> str:
        import json
        params = {
            '_comment': 'test',
            'portfolio': {
                'capital': 30000,
                'max_positions': 4,
                'max_layers_per_stock': 2,
                'layer_size': 3000,
            },
            'risk': {
                'max_position_pct': 0.20,
                'max_drawdown_limit': 0.12,
                'commission': 0.0003,
                'stamp_tax': 0.001,
                'slippage': 0.0005,
            },
            'strategies': {
                'RSI': {
                    'symbol': '510310.SH',
                    'params': {
                        'rsi_buy': 25,
                        'stop_loss': 0.05,
                        'take_profit': 0.20,
                        'min_hold_days': 3,
                        'atr_threshold': 0.85,
                    }
                }
            }
        }
        path = _write_tmp(json.dumps(params), suffix='.json')
        return path

    def _write_live_params(self) -> str:
        import json
        live = {
            '510310.SH_RSI': {
                'symbol': '510310.SH',
                'strategy': 'RSI',
                'note': 'test',
            }
        }
        return _write_tmp(json.dumps(live), suffix='.json')

    def test_load_from_json_basic(self):
        params_path = self._write_params()
        try:
            cfg = load_from_json(params_path)
            assert isinstance(cfg, TradingConfig)
            assert cfg.portfolio.capital == 30_000
        finally:
            os.unlink(params_path)

    def test_load_from_json_with_live(self):
        params_path = self._write_params()
        live_path = self._write_live_params()
        try:
            cfg = load_from_json(params_path, live_params_path=live_path)
            assert len(cfg.live_symbols) == 1
            assert cfg.live_symbols[0].symbol == '510310.SH'
        finally:
            os.unlink(params_path)
            os.unlink(live_path)

    def test_load_from_json_strategies(self):
        params_path = self._write_params()
        try:
            cfg = load_from_json(params_path)
            assert 'RSI' in cfg.strategies
            assert cfg.strategies['RSI'].symbol == '510310.SH'
        finally:
            os.unlink(params_path)

    def test_load_from_json_risk(self):
        params_path = self._write_params()
        try:
            cfg = load_from_json(params_path)
            assert abs(cfg.risk.max_drawdown - 0.12) < 1e-9
        finally:
            os.unlink(params_path)


class TestDefaultConfig:

    def test_default_portfolio(self):
        cfg = PortfolioConfig()
        assert cfg.capital == 20_000
        assert cfg.max_positions == 5
        assert cfg.max_position_pct == 0.25

    def test_default_risk(self):
        cfg = RiskConfig()
        assert cfg.max_drawdown == 0.15
        assert cfg.max_net_exposure == 0.90
        assert cfg.commission_rate == 0.0003

    def test_default_runner(self):
        cfg = RunnerConfig()
        assert cfg.dry_run is True
        assert cfg.interval == 300

    def test_default_data(self):
        cfg = DataConfig()
        assert cfg.primary_source == 'tencent'
        assert cfg.bar_ttl == 3600

    def test_real_yaml_loads(self):
        """加载项目实际 trading.yaml，不报错。"""
        real_path = os.path.join(PROJ_DIR, 'config', 'trading.yaml')
        if not os.path.exists(real_path):
            return  # 文件不存在则跳过
        cfg = load_config(path=real_path, env='dev')
        assert isinstance(cfg, TradingConfig)
        assert len(cfg.strategies) > 0
        assert len(cfg.live_symbols) > 0


# ===========================================================================
# Plain-Python runner
# ===========================================================================

def _run_class(cls):
    global _passed, _failed
    obj = cls()
    for attr in sorted(dir(cls)):
        if not attr.startswith('test_'):
            continue
        method = getattr(obj, attr)
        if not callable(method):
            continue
        ok = False
        try:
            method()
            ok = True
        except AssertionError as e:
            _failed += 1
            print(f"  FAIL: {cls.__name__}.{attr} — {e}")
        except Exception as e:
            _failed += 1
            print(f"  FAIL: {cls.__name__}.{attr} — EXCEPTION: {e}")
        if ok:
            _passed += 1
            print(f"  PASS: {cls.__name__}.{attr}")


if __name__ == '__main__':
    _section('DeepMerge')
    _run_class(TestDeepMerge)

    _section('LoadConfig')
    _run_class(TestLoadConfig)

    _section('EnvOverrides')
    _run_class(TestEnvOverrides)

    _section('LoadFromJson')
    _run_class(TestLoadFromJson)

    _section('DefaultConfig')
    _run_class(TestDefaultConfig)

    print('\n' + '=' * 60)
    if _failed > 0:
        print(f'FAIL: {_failed} test(s) failed')
        sys.exit(1)
    else:
        print(f'Phase 5 Config: {_passed} passed, 0 failed')
