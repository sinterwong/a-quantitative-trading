"""
core/factors/sector.py — 板块层因子

通过 DataGateway 的板块能力提供两类因子:
  1. SectorFlowFactor    — 个股所属板块的资金流强度(z-score)
  2. SectorBreadthFactor — 板块内涨家占比(后续 W3-2 实现)

设计原则:
  - 显式注入板块数据(sector_flow_data) 时优先使用
  - 未注入时,通过 SectorFlowStore 缓存历次 gw.sectors() 快照
    形成日频时序,因子按 z-score 评估
  - 当前 universe 内任何标的→板块的映射由调用方负责(传 sector_code)
    或显式注入 sector_map dict;若无法识别板块,返回全零(降级)

数据来源:
  - gw.sectors(limit=100):全市场板块涨幅 + 资金流(SectorRanking 列表)
  - 持久化:data/sentiment/sector_flow.parquet(每日累积一行)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal

logger = logging.getLogger('core.factors.sector')


_SECTOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    'data', 'sentiment',
)
os.makedirs(_SECTOR_DIR, exist_ok=True)

_SECTOR_FLOW_PARQUET = os.path.join(_SECTOR_DIR, 'sector_flow.parquet')


class SectorFlowStore:
    """
    板块资金流日频持久化层。

    每次 update() 调用 gw.sectors() 快照,把当日各板块 net_flow 累积写入
    Parquet。read() 返回宽表(index=date, columns=板块代码, value=net_flow)。

    与 MarginDataStore 的区别:这里聚合全市场所有板块,而非单标的。
    """

    def __init__(self, parquet_path: str = _SECTOR_FLOW_PARQUET):
        self._path = parquet_path

    def read(self) -> pd.DataFrame:
        """读取已持久化的宽表;无文件时返回空。"""
        if not os.path.exists(self._path):
            return pd.DataFrame()
        try:
            return pd.read_parquet(self._path)
        except Exception as exc:
            logger.warning('SectorFlowStore read failed: %s', exc)
            return pd.DataFrame()

    def update_today(self, limit: int = 100) -> None:
        """调用 gw.sectors() 取当日快照,合并写入 Parquet。"""
        try:
            from core.data_gateway import get_gateway
            sectors = get_gateway().sectors(limit=limit)
        except Exception as exc:
            logger.warning('SectorFlowStore update_today failed: %s', exc)
            return
        if not sectors:
            return

        today = pd.Timestamp(datetime.now().date())
        # 构造单行 DataFrame(index=今日,columns=板块代码)
        row = {s.code: float(s.net_flow) for s in sectors if s.code}
        if not row:
            return
        new_row = pd.DataFrame(row, index=[today])

        existing = self.read()
        if existing.empty:
            merged = new_row
        else:
            # 合并:今日如已有则覆盖,否则追加
            existing = existing[existing.index != today]
            merged = pd.concat([existing, new_row]).sort_index()

        try:
            merged.to_parquet(self._path, engine='pyarrow', compression='snappy')
        except Exception as exc:
            logger.warning('SectorFlowStore write failed: %s', exc)

    def series_for(self, sector_code: str) -> pd.Series:
        """返回某板块的 net_flow 历史 Series(亿元)。无数据时返回空 Series。"""
        df = self.read()
        if df.empty or sector_code not in df.columns:
            return pd.Series(dtype=float)
        return df[sector_code].dropna()


class SectorFlowFactor(Factor):
    """
    板块资金流因子(个股层因子,通过所属板块的 net_flow 间接打分)。

    因子值 = 滚动均值(板块净流入, window) → z-score。

    解读:
      - z > threshold:所属板块持续资金流入 → BUY(主流板块共振)
      - z < -threshold:所属板块持续资金流出 → 跟随减仓

    Parameters
    ----------
    sector_code : str, optional
        板块代码(如 'BK0716')。若提供,自动从 SectorFlowStore 读取历史。
    sector_flow_data : pd.DataFrame, optional
        显式注入的板块资金流历史(DatetimeIndex,列 net_flow,单位亿元)。
        显式注入时优先于 sector_code 自动读取。
    window : 平滑窗口(默认 5 天)
    symbol : 标的代码(供 Signal 使用)
    """

    name = 'SectorFlow'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sector_code: str = '',
        sector_flow_data: Optional[pd.DataFrame] = None,
        window: int = 5,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sector_code = sector_code
        self.sector_flow_data = sector_flow_data
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def _get_flow_series(self, price_index: pd.Index) -> Optional[pd.Series]:
        """优先显式注入,否则从 SectorFlowStore 读取。"""
        if self.sector_flow_data is not None and 'net_flow' in self.sector_flow_data.columns:
            return self.sector_flow_data['net_flow']
        if self.sector_code:
            try:
                return SectorFlowStore().series_for(self.sector_code)
            except Exception as e:
                logger.warning('SectorFlowFactor read store failed: %s', e)
        return None

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        flow = self._get_flow_series(data.index)
        if flow is None or flow.empty:
            return pd.Series(0.0, index=data.index)

        # 对齐到价格索引(前向填充,缺失补 0)
        aligned = flow.reindex(data.index, method='ffill')
        if aligned.isna().all():
            return pd.Series(0.0, index=data.index)

        smoothed = aligned.fillna(0.0).rolling(self.window, min_periods=1).mean()
        return self.normalize(smoothed)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        threshold: float = 1.0,
    ) -> List[Signal]:
        latest = factor_values.iloc[-1]
        if latest > threshold:
            return [Signal(
                timestamp=datetime.now(), symbol=self.symbol,
                direction='BUY',
                strength=min((latest - threshold) / threshold, 1.0),
                factor_name=self.name, price=price,
                metadata={
                    'sector_code': self.sector_code,
                    'sector_flow_zscore': round(float(latest), 3),
                },
            )]
        if latest < -threshold:
            return [Signal(
                timestamp=datetime.now(), symbol=self.symbol,
                direction='SELL',
                strength=min((abs(latest) - threshold) / threshold, 1.0),
                factor_name=self.name, price=price,
                metadata={
                    'sector_code': self.sector_code,
                    'sector_flow_zscore': round(float(latest), 3),
                },
            )]
        return []


__all__ = ['SectorFlowStore', 'SectorFlowFactor']
