#!/usr/bin/env python3
"""
Test runner for the quant trading system.
Run: python tests/run_tests.py
No external dependencies beyond stdlib.
"""
import sys
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, PROJ_DIR)

passed = 0
failed = 0

def section(name):
    print('\n=== ' + name + ' ===')

def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print('  PASS: ' + msg)
    else:
        failed += 1
        print('  FAIL: ' + msg)

# ============================================================
# Section 1: dynamic_selector tests
# ============================================================

sys.path.insert(0, os.path.join(PROJ_DIR, 'scripts'))
from dynamic_selector import (
    DynamicStockSelectorV2,
    safe_float,
    safe_int,
    SECTOR_NEWS_KEYWORDS,
)

section('SafeFloat')
check(safe_float('1.23') == 1.23, 'valid number')
check(safe_float('0') == 0.0, 'zero')
check(safe_float('-3.5') == -3.5, 'negative')
check(safe_float('abc') == 0.0, 'invalid -> default 0')
check(safe_float('') == 0.0, 'empty string -> default 0')
check(safe_float(None) == 0.0, 'None -> default 0')
check(safe_float('abc', -1.0) == -1.0, 'custom default works')

section('SafeInt')
check(safe_int('42') == 42, 'valid integer')
check(safe_int('0') == 0, 'zero')
check(safe_int('-7') == -7, 'negative')
check(safe_int('abc') == 0, 'invalid -> default 0')
check(safe_int('') == 0, 'empty -> default 0')
check(safe_int(None) == 0, 'None -> default 0')

section('SectorKeywords')
check(len(SECTOR_NEWS_KEYWORDS) > 0, 'keywords dict not empty')
for sector, keywords in SECTOR_NEWS_KEYWORDS.items():
    check(len(keywords) > 0, sector + ' has keywords')
    check(all(isinstance(kw, str) for kw in keywords), sector + ' keywords are strings')

section('DynamicStockSelectorV2')
s = DynamicStockSelectorV2()
total = (s.WEIGHT_NEWS + s.WEIGHT_SECTOR + s.WEIGHT_FLOW
         + s.WEIGHT_TECH + s.WEIGHT_CONSISTENCY)
check(abs(total - 1.0) < 0.001, 'weights sum to 1.0 (got ' + str(total) + ')')
check(s.WEIGHT_NEWS == 0.15, 'WEIGHT_NEWS=0.15')
check(s.WEIGHT_SECTOR == 0.35, 'WEIGHT_SECTOR=0.35')
check(s.WEIGHT_FLOW == 0.25, 'WEIGHT_FLOW=0.25')
check(s.WEIGHT_TECH == 0.15, 'WEIGHT_TECH=0.15')
check(s.WEIGHT_CONSISTENCY == 0.10, 'WEIGHT_CONSISTENCY=0.10')
check(isinstance(s.news_cache, list), 'news_cache is list')
check(isinstance(s.sectors_raw, list), 'sectors_raw is list')
check(isinstance(s._constituent_cache, dict), '_constituent_cache is dict')
check(0 <= s.calc_consistency_score_for_bk('nonexistent') <= 100,
       'consistency score bounds [0,100]')
check(0 <= s.calc_tech_score_for_bk('nonexistent') <= 100,
       'tech score bounds [0,100]')

# ============================================================
# Section 2: signal_generator tests
# ============================================================

sys.path.insert(0, os.path.join(PROJ_DIR, 'scripts', 'quant'))
import random

from signal_generator import (
    SignalType, SignalSource, RSISignalSource,
    MarketRegimeSource, SignalGenerator, BlackListFilter,
)

def make_fake_data(n=60, seed=42):
    """Create n bars of synthetic OHLCV data."""
    random.seed(seed)
    d = []
    p = 10.0
    for i in range(n):
        p = p * (1 + random.uniform(-0.02, 0.025))
        d.append({
            'date': '2024-01-%02d' % (i + 1),
            'open':  round(p * 0.99, 2),
            'high':  round(p * 1.01, 2),
            'low':   round(p * 0.98, 2),
            'close': round(p, 2),
            'volume': int(random.uniform(1e6, 5e6)),
        })
    return d

section('SignalType')
check(SignalType.BUY == 'buy', 'BUY is buy')
check(SignalType.SELL == 'sell', 'SELL is sell')
check(SignalType.HOLD == 'hold', 'HOLD is hold')
check(len({SignalType.BUY, SignalType.SELL, SignalType.HOLD}) == 3,
       'signals are distinct')

section('RSISignalSource defaults')
rsi = RSISignalSource('TEST')
check(rsi.period == 21, 'default period=21')
check(rsi.oversold == 35, 'default oversold=35')
check(rsi.overbought == 65, 'default overbought=65')
check(rsi.stop_loss == 0.05, 'default stop_loss=0.05')
check(rsi.take_profit == 0.20, 'default take_profit=0.20')

section('RSISignalSource custom params')
rsi2 = RSISignalSource('TEST', {'period': 14, 'oversold': 30, 'overbought': 70})
check(rsi2.period == 14, 'custom period=14')
check(rsi2.oversold == 30, 'custom oversold=30')
check(rsi2.overbought == 70, 'custom overbought=70')

section('RSISignalSource data loading')
rsi3 = RSISignalSource('TEST', {'period': 14})
loader_data = make_fake_data()
ok = rsi3.load(type('L', (), {'get_kline': lambda s,x,y,z: loader_data})(), '20240101', '20241231')
check(ok == True, 'load returns True')
check(len(rsi3.data) == 60, 'loaded 60 bars')
check(rsi3._rsi_vals is not None, 'RSI values computed')

section('RSISignalSource evaluate')
result_early = rsi3.evaluate(5)
check(result_early['signal'] == SignalType.HOLD, 'early evaluate -> HOLD')
check(result_early['reason'] == 'data_not_ready', 'reason = data_not_ready')
result_ok = rsi3.evaluate(30)
check(result_ok['signal'] in (SignalType.BUY, SignalType.SELL, SignalType.HOLD),
       'evaluate returns valid signal')
check(0.0 <= result_ok['strength'] <= 1.0, 'strength in [0,1]')

section('RSISignalSource reset')
rsi3.reset()
check(rsi3._entry_price == 0, 'entry_price reset to 0')
check(rsi3._hold_days == 0, 'hold_days reset to 0')

section('MarketRegimeSource')
mr = MarketRegimeSource('TEST')
check(mr.ma_period == 200, 'default ma_period=200')
ok = mr.load(type('L', (), {'get_kline': lambda s,x,y,z: loader_data})(), '20240101', '20241231')
check(ok == True, 'MarketRegime loads data')
result_mr = mr.evaluate(59)
check(result_mr['signal'] in (SignalType.BUY, SignalType.SELL, SignalType.HOLD),
       'evaluate returns valid signal')

section('SignalGenerator')
gen = SignalGenerator('TEST')
check(len(gen.sources) == 0, 'empty initially')
gen.add_source(RSISignalSource, params={'period': 14}, weight=1.0)
gen.add_source(MarketRegimeSource, params={}, weight=0.5)
check(len(gen.sources) == 2, 'added 2 sources')
weights = [w for _, w in gen.sources]
check(weights == [1.0, 0.5], 'weights set correctly')
gen.load_all(type('L', (), {'get_kline': lambda s,x,y,z: loader_data})(), '20240101', '20241231')
src = gen.get_source('RSI')
check(isinstance(src, RSISignalSource), 'get_source returns RSISignalSource')
gen_result = gen.evaluate(30)
check(gen_result['signal'] in (SignalType.BUY, SignalType.SELL, SignalType.HOLD),
       'generator evaluate returns valid signal')

section('BlackListFilter')
filt = BlackListFilter()
# prev day limit up (>9.5%) should block
up_data = [{'close': 10.0}, {'close': 11.0, 'volume': 1e7}]
allowed_up, reason_up = filt.can_buy(up_data, 1)
check(allowed_up == False, 'prev limit up blocks')
check('limit_up' in reason_up, 'reason mentions limit_up')
# suspended (volume=0) should block
suspended_data = [{'close': 10.0}, {'close': 10.2, 'volume': 0}]
allowed_sus, reason_sus = filt.can_buy(suspended_data, 1)
check(allowed_sus == False, 'suspended stock blocks')
# normal passes
ok_data = [{'close': 10.0}, {'close': 10.2, 'volume': 1e7}]
allowed_ok, reason_ok = filt.can_buy(ok_data, 1)
check(allowed_ok == True, 'normal stock passes')

# ============================================================
# Section 3: DataLayer tests (Phase 1)
# ============================================================

section('DataLayer (Phase 1)')

import importlib, subprocess, sys as _sys

def _run_test_module(module_name, label):
    """运行独立测试模块，捕获结果写入全局计数。"""
    global passed, failed
    result = subprocess.run(
        [_sys.executable, os.path.join(THIS_DIR, module_name)],
        capture_output=True, text=True, encoding='utf-8'
    )
    output = result.stdout + result.stderr
    # 解析最后一行摘要
    for line in reversed(output.splitlines()):
        line = line.strip()
        if 'passed' in line and 'failed' in line:
            # 格式: "Phase X Label: N passed, M failed"
            try:
                parts = line.split(':')[-1].strip().split(',')
                p = int(parts[0].split()[0])
                f = int(parts[1].split()[0])
                passed += p
                failed += f
                status = 'PASS' if f == 0 else 'FAIL'
                print(f'  {status}: {label} — {p} passed, {f} failed')
                return
            except Exception:
                pass
        elif line.startswith('ALL') and 'TESTS PASSED' in line:
            try:
                p = int(line.split()[1])
                passed += p
                print(f'  PASS: {label} — {p} passed')
                return
            except Exception:
                pass
    # 找不到摘要行 → 按退出码判断
    if result.returncode == 0:
        print(f'  PASS: {label} (no summary line)')
    else:
        failed += 1
        print(f'  FAIL: {label} (exit code {result.returncode})')
        if result.stderr:
            for ln in result.stderr.splitlines()[:5]:
                print('    ' + ln)

_run_test_module('test_data_layer.py',      'DataLayer')
_run_test_module('test_factor_pipeline.py', 'FactorRegistry+Pipeline')
_run_test_module('test_strategy_runner.py', 'StrategyRunner')
_run_test_module('test_portfolio_risk.py',  'PortfolioRisk')
_run_test_module('test_config.py',          'Config')
# Phase 1 新增测试
_run_test_module('test_backtest_engine.py', 'BacktestEngine(P1-A)')
_run_test_module('test_walkforward.py',     'WalkForward(P1-B)')
_run_test_module('test_data_quality.py',    'DataQuality(P1-C)')

# ============================================================
# Summary
# ============================================================

print('\n' + '=' * 50)
if failed > 0:
    print('FAIL: ' + str(failed) + ' test(s) failed')
    sys.exit(1)
else:
    print('ALL ' + str(passed) + ' TESTS PASSED')
