"""
core/data_quality.py — 数据质量检验模块（Phase 1-C）

功能：
  1. 缺口检测（非交易日 → 仅报告跳空日）
  2. 异常涨跌检测（±20% 以上，A 股涨跌停为 ±10%，极端情况取 ±20%）
  3. 成交量为 0 日检测（停牌或数据缺失）
  4. 整体质量报告
  5. 自动标记/剔除异常 bar

设计原则：
  - 不修改原始数据，返回带标记列的副本
  - 所有检测结果汇总为 DataQualityReport，方便日志和测试
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("core.data_quality")


# ─── 数据类 ──────────────────────────────────────────────────────────────────

@dataclass
class AnomalyRecord:
    """单个异常记录"""
    date: pd.Timestamp
    anomaly_type: str      # 'gap' | 'abnormal_move' | 'zero_volume'
    detail: str            # 详细描述
    value: float = 0.0     # 异常数值（涨跌幅 or 缺失天数）


@dataclass
class DataQualityReport:
    """数据质量报告"""
    symbol: str
    total_bars: int
    anomalies: List[AnomalyRecord] = field(default_factory=list)
    n_gaps: int = 0
    n_abnormal_moves: int = 0
    n_zero_volume: int = 0
    completeness_pct: float = 100.0    # 有效 bar / 总 bar（%）

    @property
    def is_clean(self) -> bool:
        """无任何异常"""
        return len(self.anomalies) == 0

    @property
    def quality_score(self) -> float:
        """0~100 质量评分（问题越多分越低）"""
        if self.total_bars == 0:
            return 0.0
        penalty = (
            self.n_zero_volume * 2     # 停牌扣 2 分
            + self.n_abnormal_moves * 5  # 异常涨跌扣 5 分
            + self.n_gaps * 3           # 跳空扣 3 分
        )
        # 归一化到每百根 bar
        penalty_per_100 = penalty / self.total_bars * 100
        return max(0.0, min(100.0, 100.0 - penalty_per_100))

    def summary(self) -> str:
        lines = [
            f"数据质量报告 — {self.symbol}",
            f"  总 bar 数:    {self.total_bars}",
            f"  完整性:       {self.completeness_pct:.1f}%",
            f"  跳空日:       {self.n_gaps} 个",
            f"  异常涨跌:     {self.n_abnormal_moves} 个",
            f"  零成交量:     {self.n_zero_volume} 个",
            f"  质量评分:     {self.quality_score:.1f}/100",
        ]
        if not self.is_clean:
            lines.append("  异常记录（前5条）:")
            for a in self.anomalies[:5]:
                lines.append(f"    [{a.anomaly_type}] {a.date.date()} — {a.detail}")
        return "\n".join(lines)


# ─── DataQualityChecker ───────────────────────────────────────────────────────

class DataQualityChecker:
    """
    OHLCV 数据质量检验器。

    用法：
        checker = DataQualityChecker(symbol='510300')
        df_clean = checker.check_and_mark(df)   # 添加质量标记列
        report = checker.report                  # 访问报告
        df_filtered = checker.drop_anomalies(df_clean)  # 剔除异常行
    """

    def __init__(
        self,
        symbol: str = '',
        max_gap_days: int = 7,           # 超过 N 个日历日视为跳空（排除周末+法定假日约 3-5 天）
        abnormal_move_pct: float = 20.0,  # 涨跌幅超过此阈值视为异常（%）
    ):
        self.symbol = symbol
        self.max_gap_days = max_gap_days
        self.abnormal_move_pct = abnormal_move_pct
        self.report: Optional[DataQualityReport] = None

    def check_and_mark(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        对 OHLCV DataFrame 做质量检验，返回带标记列的副本。

        新增列：
          is_zero_volume    : bool — 成交量为 0
          is_abnormal_move  : bool — 涨跌幅超过阈值
          is_gap            : bool — 与前根 bar 间隔超过阈值
          quality_flag      : str  — 'ok' | 'zero_volume' | 'abnormal_move' | 'gap' | 'multi'
        """
        if df is None or df.empty:
            self.report = DataQualityReport(symbol=self.symbol, total_bars=0)
            return df

        out = df.copy()
        anomalies: List[AnomalyRecord] = []

        # ── 确保时间索引 ──────────────────────────────────────────────────────
        if 'date' in out.columns and out.index.name != 'date':
            if not pd.api.types.is_datetime64_any_dtype(out['date']):
                out['date'] = pd.to_datetime(out['date'])
            out = out.set_index('date')

        if not pd.api.types.is_datetime64_any_dtype(out.index):
            out.index = pd.to_datetime(out.index)

        out = out.sort_index()

        # ── 1. 零成交量检测 ────────────────────────────────────────────────────
        zero_vol = out['volume'] == 0
        out['is_zero_volume'] = zero_vol
        for dt in out.index[zero_vol]:
            vol = out.loc[dt, 'volume']
            anomalies.append(AnomalyRecord(
                date=dt, anomaly_type='zero_volume',
                detail=f"成交量=0（停牌或数据缺失）", value=float(vol)
            ))

        # ── 2. 异常涨跌检测 ────────────────────────────────────────────────────
        pct_chg = out['close'].pct_change() * 100  # %
        abnormal = pct_chg.abs() > self.abnormal_move_pct
        out['is_abnormal_move'] = abnormal
        for dt in out.index[abnormal]:
            pct = pct_chg.loc[dt]
            anomalies.append(AnomalyRecord(
                date=dt, anomaly_type='abnormal_move',
                detail=f"涨跌幅={pct:+.2f}%（阈值±{self.abnormal_move_pct}%）",
                value=float(pct)
            ))

        # ── 3. 跳空检测（日历天数间隔）────────────────────────────────────────
        if len(out) > 1:
            date_gaps = out.index.to_series().diff().dt.days
            gap_mask = date_gaps > self.max_gap_days
            out['is_gap'] = gap_mask
            for dt in out.index[gap_mask]:
                g = int(date_gaps.loc[dt])
                anomalies.append(AnomalyRecord(
                    date=dt, anomaly_type='gap',
                    detail=f"距离前根 bar 间隔 {g} 天（阈值 {self.max_gap_days} 天）",
                    value=float(g)
                ))
        else:
            out['is_gap'] = False

        # ── 4. 综合质量标记 ────────────────────────────────────────────────────
        flags = []
        for i in range(len(out)):
            row_flags = []
            if out['is_zero_volume'].iloc[i]:
                row_flags.append('zero_volume')
            if out['is_abnormal_move'].iloc[i]:
                row_flags.append('abnormal_move')
            if out['is_gap'].iloc[i]:
                row_flags.append('gap')
            if len(row_flags) == 0:
                flags.append('ok')
            elif len(row_flags) == 1:
                flags.append(row_flags[0])
            else:
                flags.append('multi')
        out['quality_flag'] = flags

        # ── 报告 ──────────────────────────────────────────────────────────────
        n_zero_vol = int(zero_vol.sum())
        n_abnormal = int(abnormal.sum())
        n_gaps = int(out['is_gap'].sum())
        n_bad = sum(1 for f in flags if f != 'ok')
        completeness = (len(out) - n_bad) / len(out) * 100 if len(out) > 0 else 100.0

        self.report = DataQualityReport(
            symbol=self.symbol,
            total_bars=len(out),
            anomalies=anomalies,
            n_gaps=n_gaps,
            n_abnormal_moves=n_abnormal,
            n_zero_volume=n_zero_vol,
            completeness_pct=completeness,
        )

        logger.info(
            "数据质量检验: %s — %d bars, gap=%d, abnormal=%d, zero_vol=%d, score=%.1f",
            self.symbol, len(out), n_gaps, n_abnormal, n_zero_vol,
            self.report.quality_score,
        )

        return out

    def drop_anomalies(
        self,
        df: pd.DataFrame,
        drop_zero_volume: bool = True,
        drop_abnormal_move: bool = True,
        drop_gaps: bool = False,       # 跳空通常保留（不删除数据）
    ) -> pd.DataFrame:
        """
        剔除已标记的异常 bar，返回清洗后的 DataFrame。
        须先调用 check_and_mark()。
        """
        if df is None or df.empty:
            return df
        mask = pd.Series([True] * len(df), index=df.index)
        if drop_zero_volume and 'is_zero_volume' in df.columns:
            mask &= ~df['is_zero_volume']
        if drop_abnormal_move and 'is_abnormal_move' in df.columns:
            mask &= ~df['is_abnormal_move']
        if drop_gaps and 'is_gap' in df.columns:
            mask &= ~df['is_gap']
        return df[mask]


# ─── 便捷函数 ─────────────────────────────────────────────────────────────────

def check_data_quality(
    df: pd.DataFrame,
    symbol: str = '',
    max_gap_days: int = 7,
    abnormal_move_pct: float = 20.0,
) -> DataQualityReport:
    """
    一键检验数据质量，返回报告。不修改原始数据。

    Example:
        report = check_data_quality(df, '510300')
        print(report.summary())
        print(f"质量评分: {report.quality_score:.1f}")
    """
    checker = DataQualityChecker(symbol, max_gap_days, abnormal_move_pct)
    checker.check_and_mark(df)
    return checker.report
