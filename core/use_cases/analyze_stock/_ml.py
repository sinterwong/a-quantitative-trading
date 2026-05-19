"""
analyze_stock._ml — XGBoost 价格方向预测(可选)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger('core.use_cases.analyze_stock')


def try_ml_prediction(symbol: str, df: Any) -> Dict[str, Any]:
    """尝试加载已训练的 XGBoost 模型并预测下一日方向。"""
    try:
        from core.ml.model_registry import ModelRegistry
        registry = ModelRegistry()
        try:
            model, meta = registry.load(symbol, 'xgboost')
        except Exception as exc:
            return {'available': False, 'reason': f'model_not_found: {exc}'}

        from core.ml.price_predictor import MLPredictionFactor
        factor = MLPredictionFactor(symbol=symbol)
        # 直接评估因子(XGBoost 模型存活则返回方向 z-score)
        z = factor.evaluate(df)
        latest_z = float(z.dropna().iloc[-1]) if hasattr(z, 'dropna') and len(z) else 0.0

        return {
            'available': True,
            'model': 'xgboost',
            'latest_score': round(latest_z, 4),
            'direction': 'BUY' if latest_z > 0.3 else ('SELL' if latest_z < -0.3 else 'HOLD'),
            'metrics': meta.get('metrics', {}),
            'trained_at': meta.get('trained_at', ''),
        }
    except Exception as exc:
        logger.debug('_try_ml_prediction failed: %s', exc)
        return {'available': False, 'reason': str(exc)}
