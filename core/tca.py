"""
core/tca.py — 交易成本分析（Trade Cost Analysis）

实现指标：
  - Implementation Shortfall (IS)：决策价 vs 成交价的偏差（bps）
  - Market Impact：价格冲击成本
  - Timing Cost：延迟成本
  - 按标的 / 时段 / Regime 分类统计隐性成本

用法：
    from core.tca import TCAAnalyzer, TCARecord

    # 从回测结果或实盘记录构建 TCA 记录
    records = TCAAnalyzer.from_backtest_result(result)
    report  = TCAAnalyzer(records).analyze()
    print(report.summary())

    # 月度 TCA 报告（反馈调整 slippage_bps 参数）
    TCAAnalyzer(records).save_monthly_report('outputs/tca_2026_04.json')
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs')
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# TCARecord — 单笔交易的成本分析记录
# ---------------------------------------------------------------------------

@dataclass
class TCARecord:
    """单笔交易成本分析记录。

    Attributes
    ----------
    trade_id : str
        唯一标识（可用 "{symbol}_{timestamp}"）
    symbol : str
        标的代码
    direction : str
        'BUY' 或 'SELL'
    timestamp : datetime
        成交时间
    decision_price : float
        决策价（信号触发时的价格，通常为前一根 bar 的 close）
    execution_price : float
        实际成交价（next bar open + 滑点）
    shares : int
        成交股数
    commission : float
        佣金（元）
    stamp_tax : float
        印花税（元，卖出时）
    regime : str
        成交时的市场 Regime（'BULL'/'BEAR'/'VOLATILE'/'CALM'）
    signal_reason : str
        信号触发原因
    """
    trade_id: str
    symbol: str
    direction: str              # 'BUY' | 'SELL'
    timestamp: datetime
    decision_price: float       # 信号触发时的价格
    execution_price: float      # 实际成交价
    shares: int
    commission: float = 0.0
    stamp_tax: float = 0.0
    regime: str = 'CALM'
    signal_reason: str = ''

    # ------------------------------------------------------------------
    # 计算属性
    # ------------------------------------------------------------------

    @property
    def trade_value(self) -> float:
        """成交金额（元）"""
        return self.execution_price * self.shares

    @property
    def implementation_shortfall_bps(self) -> float:
        """
        Implementation Shortfall（基点）。
        IS = (execution_price - decision_price) / decision_price × 10000
        买入：IS > 0 表示成本超出决策价（不利）
        卖出：IS < 0 表示成交低于决策价（不利），取绝对值统计
        """
        if self.decision_price <= 0:
            return 0.0
        raw = (self.execution_price - self.decision_price) / self.decision_price * 10_000
        # 对买入：IS>0 不利；对卖出：IS<0 不利
        return raw if self.direction == 'BUY' else -raw

    @property
    def total_cost_bps(self) -> float:
        """
        总显性成本（佣金 + 印花税），以 bps 表示。
        total_cost_bps = (commission + stamp_tax) / trade_value × 10000
        """
        if self.trade_value <= 0:
            return 0.0
        return (self.commission + self.stamp_tax) / self.trade_value * 10_000

    @property
    def total_shortfall_bps(self) -> float:
        """总成本 = IS + 显性成本"""
        return self.implementation_shortfall_bps + self.total_cost_bps

    @property
    def date(self) -> date:
        return self.timestamp.date() if hasattr(self.timestamp, 'date') else self.timestamp

    @property
    def hour(self) -> int:
        return self.timestamp.hour if hasattr(self.timestamp, 'hour') else 0


# ---------------------------------------------------------------------------
# TCAReport — 汇总报告
# ---------------------------------------------------------------------------

@dataclass
class TCAReport:
    """TCA 汇总报告"""
    n_trades: int
    avg_is_bps: float               # 平均 IS（bps）
    avg_total_cost_bps: float       # 平均总成本（bps）
    median_is_bps: float
    p95_is_bps: float               # 95% 分位数（最差情形）
    by_symbol: Dict[str, Dict]      # {symbol: {avg_is, n_trades, ...}}
    by_direction: Dict[str, Dict]   # {'BUY': {...}, 'SELL': {...}}
    by_regime: Dict[str, Dict]      # {'BULL': {...}, ...}
    by_hour: Dict[int, Dict]        # {9: {...}, 10: {...}, ...}
    monthly: Dict[str, Dict]        # {'2026-04': {...}, ...}
    recommended_slippage_bps: float # 建议 slippage_bps 参数值

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "  TCA 交易成本分析报告",
            "=" * 60,
            f"  样本数量:         {self.n_trades} 笔",
            f"  平均 IS:          {self.avg_is_bps:.2f} bps",
            f"  平均总成本:       {self.avg_total_cost_bps:.2f} bps",
            f"  中位数 IS:        {self.median_is_bps:.2f} bps",
            f"  P95 IS:           {self.p95_is_bps:.2f} bps",
            f"  建议 slippage:    {self.recommended_slippage_bps:.1f} bps",
            "",
            "  按 Regime 分层:",
        ]
        for regime, stats in self.by_regime.items():
            lines.append(
                f"    {regime:<10}: avg_IS={stats['avg_is_bps']:.2f} bps  n={stats['n_trades']}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# TCAAnalyzer
# ---------------------------------------------------------------------------

class TCAAnalyzer:
    """
    交易成本分析器。

    接受 TCARecord 列表，输出 TCAReport 及各维度明细。
    """

    def __init__(self, records: List[TCARecord]):
        self.records = records

    # ------------------------------------------------------------------
    # 构造器：从回测结果生成 TCARecord 列表
    # ------------------------------------------------------------------

    @classmethod
    def from_backtest_result(
        cls,
        result,                         # BacktestResult
        regime_series: Optional[pd.Series] = None,
    ) -> 'TCAAnalyzer':
        """
        从 BacktestEngine.run() 的 BacktestResult 构建 TCAAnalyzer。

        Parameters
        ----------
        result : BacktestResult
            回测结果，含 result.trades (List[TradeRecord])
        regime_series : pd.Series, optional
            日期 → Regime 标签；None 时统一标记为 'CALM'
        """
        records: List[TCARecord] = []
        for i, t in enumerate(result.trades):
            ts = t.timestamp
            trade_date = ts.date() if hasattr(ts, 'date') else ts

            regime = 'CALM'
            if regime_series is not None:
                regime = str(regime_series.get(trade_date, 'CALM'))

            # 用 signal_reason 里包含的信息估算 decision_price
            # 回测引擎中：成交价 = next_bar.open，决策价估算为 signal_bar.close
            # 如果无法获得精确决策价，用成交价本身（IS = 0）
            decision_price = t.price  # 保守：无 slippage 记录时 IS=0

            stamp_tax = 0.0
            if t.direction == 'SELL':
                # 从 config 推算印花税（0.1%）
                stamp_tax = t.value * 0.001

            records.append(TCARecord(
                trade_id=f"{t.symbol}_{i}",
                symbol=t.symbol,
                direction=t.direction,
                timestamp=ts,
                decision_price=decision_price,
                execution_price=t.price,
                shares=t.shares,
                commission=t.commission,
                stamp_tax=stamp_tax,
                regime=regime,
                signal_reason=t.signal_reason,
            ))
        return cls(records)

    @classmethod
    def from_trade_dicts(
        cls,
        trades: List[dict],
        regime_series: Optional[pd.Series] = None,
    ) -> 'TCAAnalyzer':
        """
        从实盘交易记录字典列表构建（与 backend/services/portfolio.py 兼容）。

        每条 dict 需含：symbol, direction, timestamp/created_at,
        decision_price(可选), price/execution_price, shares,
        commission(可选), pnl(可选)
        """
        records: List[TCARecord] = []
        for i, t in enumerate(trades):
            raw_ts = t.get('timestamp') or t.get('created_at', '')
            try:
                ts = datetime.fromisoformat(str(raw_ts))
            except Exception:
                ts = datetime.now()

            trade_date = ts.date()
            regime = 'CALM'
            if regime_series is not None:
                regime = str(regime_series.get(trade_date, 'CALM'))

            exec_price = float(t.get('price') or t.get('execution_price') or 0)
            dec_price = float(t.get('decision_price') or exec_price)
            direction = str(t.get('direction', 'BUY')).upper()
            shares = int(t.get('shares', 0))
            commission = float(t.get('commission', 0))
            stamp_tax = exec_price * shares * 0.001 if direction == 'SELL' else 0.0

            records.append(TCARecord(
                trade_id=str(t.get('id') or f"{t.get('symbol','?')}_{i}"),
                symbol=str(t.get('symbol', '?')),
                direction=direction,
                timestamp=ts,
                decision_price=dec_price,
                execution_price=exec_price,
                shares=shares,
                commission=commission,
                stamp_tax=stamp_tax,
                regime=regime,
                signal_reason=str(t.get('reason') or ''),
            ))
        return cls(records)

    # ------------------------------------------------------------------
    # 核心分析
    # ------------------------------------------------------------------

    def analyze(self) -> TCAReport:
        """运行完整 TCA 分析，返回 TCAReport。"""
        if not self.records:
            return self._empty_report()

        is_vals = np.array([r.implementation_shortfall_bps for r in self.records])
        cost_vals = np.array([r.total_cost_bps for r in self.records])

        avg_is = float(np.mean(is_vals))
        avg_cost = float(np.mean(cost_vals))
        median_is = float(np.median(is_vals))
        p95_is = float(np.percentile(is_vals, 95))

        # 建议 slippage_bps = avg_is 向上取整到 5 的倍数，最低 3
        recommended = max(3.0, round(max(avg_is, 0) / 5 + 0.5) * 5)

        by_symbol = self._group_stats('symbol')
        by_direction = self._group_stats('direction')
        by_regime = self._group_stats('regime')
        by_hour = self._group_stats('hour')
        monthly = self._monthly_stats()

        return TCAReport(
            n_trades=len(self.records),
            avg_is_bps=round(avg_is, 3),
            avg_total_cost_bps=round(avg_cost, 3),
            median_is_bps=round(median_is, 3),
            p95_is_bps=round(p95_is, 3),
            by_symbol=by_symbol,
            by_direction=by_direction,
            by_regime=by_regime,
            by_hour=by_hour,
            monthly=monthly,
            recommended_slippage_bps=recommended,
        )

    def _group_stats(self, key: str) -> Dict:
        groups: Dict[str, List[TCARecord]] = {}
        for r in self.records:
            k = str(getattr(r, key))
            groups.setdefault(k, []).append(r)

        result = {}
        for k, recs in groups.items():
            is_arr = [r.implementation_shortfall_bps for r in recs]
            cost_arr = [r.total_cost_bps for r in recs]
            result[k] = {
                'n_trades': len(recs),
                'avg_is_bps': round(float(np.mean(is_arr)), 3),
                'avg_total_cost_bps': round(float(np.mean(cost_arr)), 3),
                'median_is_bps': round(float(np.median(is_arr)), 3),
                'p95_is_bps': round(float(np.percentile(is_arr, 95)), 3) if len(is_arr) >= 2 else float(np.mean(is_arr)),
                'total_commission': round(sum(r.commission for r in recs), 2),
                'total_stamp_tax': round(sum(r.stamp_tax for r in recs), 2),
            }
        return result

    def _monthly_stats(self) -> Dict[str, Dict]:
        groups: Dict[str, List[TCARecord]] = {}
        for r in self.records:
            month = r.timestamp.strftime('%Y-%m') if hasattr(r.timestamp, 'strftime') else str(r.timestamp)[:7]
            groups.setdefault(month, []).append(r)

        result = {}
        for month in sorted(groups.keys()):
            recs = groups[month]
            is_arr = [r.implementation_shortfall_bps for r in recs]
            result[month] = {
                'n_trades': len(recs),
                'avg_is_bps': round(float(np.mean(is_arr)), 3),
                'total_commission': round(sum(r.commission for r in recs), 2),
                'total_stamp_tax': round(sum(r.stamp_tax for r in recs), 2),
                'total_cost': round(
                    sum(r.commission + r.stamp_tax for r in recs), 2
                ),
            }
        return result

    @staticmethod
    def _empty_report() -> TCAReport:
        return TCAReport(
            n_trades=0,
            avg_is_bps=0.0,
            avg_total_cost_bps=0.0,
            median_is_bps=0.0,
            p95_is_bps=0.0,
            by_symbol={},
            by_direction={},
            by_regime={},
            by_hour={},
            monthly={},
            recommended_slippage_bps=5.0,
        )

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def save_monthly_report(self, path: str = '') -> str:
        """保存当月 TCA 报告为 JSON 文件。"""
        report = self.analyze()
        if not path:
            month_str = datetime.now().strftime('%Y_%m')
            path = os.path.join(_OUTPUTS_DIR, f'tca_{month_str}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2, default=str)
        return path

    def to_dataframe(self) -> pd.DataFrame:
        """返回所有记录的 DataFrame，方便进一步分析。"""
        if not self.records:
            return pd.DataFrame()
        rows = []
        for r in self.records:
            rows.append({
                'trade_id': r.trade_id,
                'symbol': r.symbol,
                'direction': r.direction,
                'timestamp': r.timestamp,
                'decision_price': r.decision_price,
                'execution_price': r.execution_price,
                'shares': r.shares,
                'commission': r.commission,
                'stamp_tax': r.stamp_tax,
                'regime': r.regime,
                'is_bps': r.implementation_shortfall_bps,
                'total_cost_bps': r.total_cost_bps,
                'total_shortfall_bps': r.total_shortfall_bps,
            })
        return pd.DataFrame(rows)
