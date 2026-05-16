"""
analyze_stock._risk_metrics — ATR / VaR / 波动 / 回撤 / 止损止盈 估算。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict

logger = logging.getLogger('core.use_cases.analyze_stock')


def compute_risk_metrics(df, current_price: float) -> Dict[str, Any]:
    """从日 K 计算 ATR / VaR-95 / 年化波动率 / 最大回撤 / 建议止损止盈。"""
    try:
        import numpy as np
        import pandas as pd

        if df is None or len(df) < 20:
            return {'error': 'insufficient_bars'}

        df = df.copy()
        # ATR(14) — Wilder
        high = df['high'].astype(float)
        low = df['low'].astype(float)
        close = df['close'].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_14 = float(tr.tail(14).mean())

        # 收益率序列 → VaR / 波动 / 回撤
        rets = close.pct_change().dropna()
        if len(rets) < 20:
            return {
                'atr_14': round(atr_14, 4),
                'atr_pct': round(atr_14 / current_price * 100, 3) if current_price > 0 else None,
                'error': 'insufficient_returns',
            }

        var_95 = float(np.percentile(rets, 5))    # 1 日 VaR(负值表示亏损)
        ann_vol = float(rets.std() * math.sqrt(252))

        # 滚动峰值最大回撤(以闭盘价口径)
        equity_curve = (1 + rets).cumprod()
        peak = equity_curve.cummax()
        dd = (equity_curve / peak - 1)
        max_dd = float(dd.min())

        suggested_stop = round(current_price - 3.0 * atr_14, 4) if current_price > 0 else None
        # 1.5R 止盈:3R 风险下浮 → 4.5×ATR 上浮(保留可调)
        suggested_tp = round(current_price + 4.5 * atr_14, 4) if current_price > 0 else None

        return {
            'atr_14': round(atr_14, 4),
            'atr_pct': round(atr_14 / current_price * 100, 3) if current_price > 0 else None,
            'var_95_1d': round(var_95, 6),
            'annualized_vol': round(ann_vol, 4),
            'max_drawdown_window': round(max_dd, 4),
            'suggested_stop_loss': suggested_stop,
            'suggested_take_profit': suggested_tp,
            'returns_window_days': int(len(rets)),
        }
    except Exception as exc:
        logger.warning('_compute_risk_metrics failed: %s', exc)
        return {'error': str(exc)}
