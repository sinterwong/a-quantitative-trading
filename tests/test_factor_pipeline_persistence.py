"""
test_factor_pipeline_persistence.py — DynamicWeightPipeline IC 状态落库测试

验证:
  1. save_pipeline_state / load_pipeline_state roundtrip
  2. DynamicWeightPipeline 默认 persist=True,_update_weights 后会落库
  3. persist=False 时不读不写
  4. 新进程(新 pipeline 实例) 能从 state.db 恢复 IC 历史和衰减状态
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from core.factor_pipeline import DynamicWeightPipeline
from core.factor_pipeline_persistence import (
    save_pipeline_state, load_pipeline_state,
)
from core.factors.price_momentum import RSIFactor, ATRFactor


def _make_data(n: int = 100) -> pd.DataFrame:
    rng = pd.date_range('2025-01-01', periods=n, freq='D')
    rs = np.random.default_rng(42).normal(0, 1, n).cumsum() + 100
    return pd.DataFrame({
        'open': rs, 'high': rs + 1, 'low': rs - 1, 'close': rs,
        'volume': np.full(n, 10_000),
    }, index=rng)


class _IsolatedStateDB:
    """临时把 QUANT_STATE_DB 指到一个临时文件,测试结束清理。"""

    def __enter__(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        self._prev = os.environ.get('QUANT_STATE_DB')
        os.environ['QUANT_STATE_DB'] = self._tmp.name
        return self._tmp.name

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop('QUANT_STATE_DB', None)
        else:
            os.environ['QUANT_STATE_DB'] = self._prev
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass


class TestPersistenceHelpers(unittest.TestCase):

    def test_save_and_load_roundtrip(self):
        with _IsolatedStateDB():
            ok = save_pipeline_state(
                ic_history={'RSI': [0.1, -0.05, 0.2], 'ATR': [0.0]},
                dynamic_weights={'RSI': 0.7, 'ATR': 0.3},
                decay_disabled={'RSI': False, 'ATR': True},
                bars_since_update=5,
            )
            self.assertTrue(ok)

            ic_h, weights, disabled, bars = load_pipeline_state()
            self.assertEqual(ic_h['RSI'], [0.1, -0.05, 0.2])
            self.assertAlmostEqual(weights['RSI'], 0.7)
            self.assertTrue(disabled['ATR'])
            self.assertEqual(bars, 5)

    def test_load_empty_when_no_db(self):
        # 不写入,直接读 → 全空
        with _IsolatedStateDB():
            ic_h, w, d, b = load_pipeline_state()
            self.assertEqual(ic_h, {})
            self.assertEqual(w, {})
            self.assertEqual(d, {})
            self.assertEqual(b, 0)


class TestDynamicWeightPipelinePersist(unittest.TestCase):

    def test_update_weights_writes_to_db(self):
        with _IsolatedStateDB() as db_path:
            pipe = DynamicWeightPipeline(
                ic_window_days=30, update_freq_days=5, min_bars=20,
            )
            pipe.add(RSIFactor, weight=0.5, params={})
            pipe.add(ATRFactor, weight=0.5, params={})

            data = _make_data(80)
            pipe.run('TEST', data)
            pipe.run('TEST', data)

            # 触发了 _update_weights → 应有落库
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    'SELECT factor_name, current_weight FROM factor_pipeline_state',
                ).fetchall()
            finally:
                conn.close()
            names = {r[0] for r in rows}
            self.assertIn('RSI', names)
            self.assertIn('ATR', names)

    def test_new_pipeline_restores_state(self):
        with _IsolatedStateDB():
            save_pipeline_state(
                ic_history={'RSI': [-0.1, -0.2, -0.3]},
                dynamic_weights={'RSI': 0.0, 'ATR': 1.0},
                decay_disabled={'RSI': True, 'ATR': False},
                bars_since_update=12,
            )
            pipe = DynamicWeightPipeline()
            pipe.add(RSIFactor, weight=0.5, params={})
            pipe.add(ATRFactor, weight=0.5, params={})
            self.assertEqual(pipe._ic_history['RSI'], [-0.1, -0.2, -0.3])
            self.assertTrue(pipe._decay_disabled['RSI'])
            self.assertEqual(pipe._bars_since_update, 12)

    def test_persist_false_skips_db(self):
        with _IsolatedStateDB() as db_path:
            save_pipeline_state(
                ic_history={'RSI': [0.5]}, dynamic_weights={'RSI': 1.0},
                decay_disabled={}, bars_since_update=3,
            )
            pipe = DynamicWeightPipeline(persist=False)
            # init 不应加载已有状态
            self.assertEqual(pipe._ic_history, {})
            self.assertEqual(pipe._bars_since_update, 0)


if __name__ == '__main__':
    unittest.main()
