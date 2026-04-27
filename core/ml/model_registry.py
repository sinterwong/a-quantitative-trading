"""
core/ml/model_registry.py — ML 模型版本管理

职责：
  - 保存 / 加载训练好的模型（joblib 序列化）
  - 维护版本元数据（训练时间、特征名、性能指标）
  - 支持按 symbol + model_type 索引

存储路径：
  data/ml_models/{symbol}/{model_type}/{version}.joblib
  data/ml_models/{symbol}/{model_type}/meta.json

用法：
    from core.ml.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.save(model, symbol='000001.SZ', model_type='xgboost',
             feature_names=feature_names, metrics={'auc': 0.62})

    model, meta = reg.load(symbol='000001.SZ', model_type='xgboost')
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_BASE_DIR = Path('data/ml_models')


class ModelRegistry:
    """
    本地 ML 模型版本管理器。

    Parameters
    ----------
    base_dir : Path
        模型根目录（默认 data/ml_models）
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = base_dir or _BASE_DIR

    # ------------------------------------------------------------------
    # Save / Load
    # ------------------------------------------------------------------

    def save(
        self,
        model: Any,
        *,
        symbol: str,
        model_type: str,
        feature_names: Optional[List[str]] = None,
        metrics: Optional[Dict[str, float]] = None,
        version: Optional[str] = None,
    ) -> Path:
        """
        序列化模型并保存元数据。

        Parameters
        ----------
        model : Any
            已训练的 sklearn/XGBoost/LightGBM 模型对象
        symbol : str
            标的代码（如 '000001.SZ'）
        model_type : str
            模型类型标识（如 'xgboost', 'lgbm'）
        feature_names : List[str]
            训练时使用的特征名（用于预测时对齐）
        metrics : dict
            训练验证指标（如 {'auc': 0.62, 'acc': 0.58}）
        version : str
            版本号（默认使用当前时间戳）

        Returns
        -------
        保存的模型文件路径
        """
        import joblib

        version = version or datetime.now().strftime('%Y%m%d_%H%M%S')
        model_dir = self._model_dir(symbol, model_type)
        model_dir.mkdir(parents=True, exist_ok=True)

        model_path = model_dir / f'{version}.joblib'
        joblib.dump(model, model_path)

        # 保存元数据
        meta = {
            'version': version,
            'symbol': symbol,
            'model_type': model_type,
            'trained_at': datetime.now().isoformat(),
            'feature_names': feature_names or [],
            'metrics': metrics or {},
            'model_path': str(model_path),
        }
        meta_path = model_dir / 'meta.json'
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return model_path

    def load(
        self,
        symbol: str,
        model_type: str,
        version: Optional[str] = None,
    ) -> Tuple[Any, Dict]:
        """
        加载模型和元数据。

        Parameters
        ----------
        symbol : str
            标的代码
        model_type : str
            模型类型标识
        version : str
            版本号（默认加载最新版本）

        Returns
        -------
        (model, meta_dict) 元组
        """
        import joblib

        model_dir = self._model_dir(symbol, model_type)
        if not model_dir.exists():
            raise FileNotFoundError(
                f"No model found for symbol={symbol}, model_type={model_type}"
            )

        if version is None:
            # 按文件名排序，取最新版本
            models = sorted(model_dir.glob('*.joblib'))
            if not models:
                raise FileNotFoundError(
                    f"No .joblib files in {model_dir}"
                )
            model_path = models[-1]
        else:
            model_path = model_dir / f'{version}.joblib'
            if not model_path.exists():
                raise FileNotFoundError(f"Model version {version} not found: {model_path}")

        model = joblib.load(model_path)

        meta_path = model_dir / 'meta.json'
        meta: Dict = {}
        if meta_path.exists():
            with open(meta_path, encoding='utf-8') as f:
                meta = json.load(f)

        return model, meta

    def exists(self, symbol: str, model_type: str) -> bool:
        """检查是否存在已保存的模型。"""
        model_dir = self._model_dir(symbol, model_type)
        return model_dir.exists() and any(model_dir.glob('*.joblib'))

    def list_versions(self, symbol: str, model_type: str) -> List[str]:
        """返回该 symbol + model_type 下的所有版本号（按时间升序）。"""
        model_dir = self._model_dir(symbol, model_type)
        if not model_dir.exists():
            return []
        return sorted(p.stem for p in model_dir.glob('*.joblib'))

    def get_meta(self, symbol: str, model_type: str) -> Optional[Dict]:
        """返回最新元数据，不存在则返回 None。"""
        meta_path = self._model_dir(symbol, model_type) / 'meta.json'
        if not meta_path.exists():
            return None
        with open(meta_path, encoding='utf-8') as f:
            return json.load(f)

    def delete(self, symbol: str, model_type: str, version: Optional[str] = None) -> None:
        """
        删除模型文件。若 version=None，删除所有版本。
        主要用于测试清理。
        """
        import shutil
        model_dir = self._model_dir(symbol, model_type)
        if not model_dir.exists():
            return
        if version is None:
            shutil.rmtree(model_dir)
        else:
            model_path = model_dir / f'{version}.joblib'
            if model_path.exists():
                os.remove(model_path)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _model_dir(self, symbol: str, model_type: str) -> Path:
        safe_symbol = symbol.replace('.', '_').replace('/', '_')
        return self.base_dir / safe_symbol / model_type
