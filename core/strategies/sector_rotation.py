"""
core/strategies/sector_rotation.py — 行业轮动策略

策略逻辑：
  基于 SectorMomentumFactor（已有）对行业 ETF 进行动量排名，
  持有动量最强的前 N 个行业 ETF，每隔 rebalance_days 重新排名换仓。

  信号生成：
    - 对所有行业 ETF 运行 SectorMomentumFactor.evaluate()
    - 取排名前 N 的 ETF → BUY 信号
    - 当前持有但排名落出 top_n+buffer 的 ETF → SELL 信号（避免过度换仓）

  A 股行业 ETF（28 个申万一级行业）：
    510170.SH（医药）、512010.SH（酒）、512660.SH（军工）、
    515000.SH（房地产）、515030.SH（新能源车）、等（可配置）

回测验证：
  from core.strategies.sector_rotation import SectorRotationStrategy
  from core.backtest_engine import BacktestEngine, BacktestConfig

  strategy = SectorRotationStrategy(top_n=3, rebalance_days=21)
  signals = strategy.generate_signals(price_data_dict)
  # price_data_dict: {symbol: OHLCV DataFrame}

目标：WFA OOS Sharpe > 0.4，换手率月均 < 30%
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.factors.base import Signal


# ---------------------------------------------------------------------------
# 默认行业 ETF 列表（申万一级行业代表性 ETF）
# ---------------------------------------------------------------------------

DEFAULT_SECTOR_ETFS: Dict[str, str] = {
    '510170.SH': '医疗器械',
    '512010.SH': '白酒',
    '512660.SH': '军工',
    '512690.SH': '酒饮料',
    '515000.SH': '房地产',
    '515030.SH': '新能源车',
    '516160.SH': '生物医药',
    '516950.SH': '半导体',
    '518880.SH': '黄金',
    '159869.SZ': '消费',
    '159915.SZ': '创业板',
    '159928.SZ': '消费30',
    '159934.SZ': '黄金ETF',
    '510310.SH': '沪深300',
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class RotationSignal:
    """单次轮动调仓信号。"""
    rebalance_date: str
    buy: List[str]              # 建仓标的
    sell: List[str]             # 平仓标的
    hold: List[str]             # 继续持有
    scores: Dict[str, float]    # 各标的动量分数（越高越好）
    top_n: int = 3


@dataclass
class SectorRotationResult:
    """回测/信号生成结果汇总。"""
    signals: List[RotationSignal]
    symbol_universe: List[str]
    total_rebalances: int
    avg_turnover_pct: float     # 平均每次换仓比例（0-1）


class SectorRotationStrategy:
    """
    行业轮动策略。

    基于价格动量（lookback_days 窗口内的收益率）对行业 ETF 排名，
    持有动量最强的前 top_n 个 ETF，每隔 rebalance_days 换仓。

    Parameters
    ----------
    top_n : int
        同时持有的行业数量（默认 3）
    lookback_days : int
        动量计算窗口（交易日，默认 60 天 ≈ 3 个月）
    rebalance_days : int
        换仓频率（交易日，默认 21 天 ≈ 1 个月）
    buffer : int
        换仓缓冲区：排名落出 top_n + buffer 才卖出（减少过度换仓，默认 1）
    momentum_method : str
        动量计算方法：'return'（简单收益率）或 'sharpe'（动量 Sharpe 比率）
    sector_etfs : dict, optional
        {symbol: name} 行业 ETF 字典，默认使用 DEFAULT_SECTOR_ETFS
    """

    def __init__(
        self,
        top_n: int = 3,
        lookback_days: int = 60,
        rebalance_days: int = 21,
        buffer: int = 1,
        momentum_method: str = 'return',
        sector_etfs: Optional[Dict[str, str]] = None,
    ) -> None:
        self.top_n = top_n
        self.lookback_days = lookback_days
        self.rebalance_days = rebalance_days
        self.buffer = buffer
        self.momentum_method = momentum_method
        self.sector_etfs = sector_etfs or DEFAULT_SECTOR_ETFS

    # ------------------------------------------------------------------
    # 核心：计算动量分数
    # ------------------------------------------------------------------

    def _momentum_score(self, price_series: pd.Series) -> float:
        """
        计算单个标的的动量分数。

        Parameters
        ----------
        price_series : pd.Series
            close 价格序列（长度 >= lookback_days）

        Returns
        -------
        float
            动量分数（越高越好）
        """
        tail = price_series.iloc[-self.lookback_days:]
        if len(tail) < max(10, self.lookback_days // 3):
            return 0.0

        if self.momentum_method == 'sharpe':
            rets = tail.pct_change().dropna()
            if len(rets) < 5:
                return 0.0
            std = float(rets.std())
            if std < 1e-10:
                return 0.0
            return float(rets.mean() / std * np.sqrt(252))
        else:
            # 简单总收益率
            start = float(tail.iloc[0])
            end = float(tail.iloc[-1])
            if start <= 0:
                return 0.0
            return (end - start) / start

    def _rank_symbols(
        self,
        price_data: Dict[str, pd.DataFrame],
        as_of_date: pd.Timestamp,
    ) -> Dict[str, float]:
        """
        对所有行业 ETF 计算截至 as_of_date 的动量分数并排名。

        Parameters
        ----------
        price_data : {symbol: OHLCV DataFrame}
        as_of_date : pd.Timestamp

        Returns
        -------
        Dict[str, float]
            {symbol: momentum_score}，按分数降序排列
        """
        scores: Dict[str, float] = {}
        for sym in self.sector_etfs:
            if sym not in price_data:
                continue
            df = price_data[sym]
            df_hist = df[df.index <= as_of_date]
            if 'close' not in df_hist.columns or len(df_hist) < 10:
                continue
            scores[sym] = self._momentum_score(df_hist['close'])

        return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    # ------------------------------------------------------------------
    # 信号生成
    # ------------------------------------------------------------------

    def generate_signals(
        self,
        price_data: Dict[str, pd.DataFrame],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> SectorRotationResult:
        """
        在历史数据上生成完整的轮动信号序列。

        Parameters
        ----------
        price_data : {symbol: OHLCV DataFrame}
            各行业 ETF 的日线 OHLCV 数据（index 为 DatetimeIndex）
        start_date : str, optional
            开始日期（'YYYY-MM-DD'），默认第一个可用日期 + lookback_days
        end_date : str, optional
            结束日期（'YYYY-MM-DD'），默认最后可用日期

        Returns
        -------
        SectorRotationResult
        """
        # 确定公共日期范围
        all_dates: pd.DatetimeIndex = pd.DatetimeIndex([])
        for df in price_data.values():
            if all_dates.empty:
                all_dates = pd.DatetimeIndex(df.index)
            else:
                all_dates = all_dates.union(pd.DatetimeIndex(df.index))
        all_dates = all_dates.sort_values()

        if all_dates.empty:
            return SectorRotationResult([], list(self.sector_etfs.keys()), 0, 0.0)

        if start_date:
            all_dates = all_dates[all_dates >= pd.Timestamp(start_date)]
        if end_date:
            all_dates = all_dates[all_dates <= pd.Timestamp(end_date)]

        # 跳过前 lookback_days 天（无足够历史数据）
        if len(all_dates) <= self.lookback_days:
            return SectorRotationResult([], list(self.sector_etfs.keys()), 0, 0.0)
        all_dates = all_dates[self.lookback_days:]

        # 换仓时间点（每 rebalance_days 触发一次）
        rebalance_dates = all_dates[::self.rebalance_days]

        signals: List[RotationSignal] = []
        current_holdings: List[str] = []
        total_turnover = 0.0

        for rb_date in rebalance_dates:
            scores = self._rank_symbols(price_data, rb_date)
            ranked = list(scores.keys())

            # 确定新持仓
            new_top = ranked[:self.top_n]

            # 卖出：当前持有但排名落出 top_n + buffer
            exit_threshold = self.top_n + self.buffer
            sell = [s for s in current_holdings if s not in ranked[:exit_threshold]]

            # 买入：新 top_n 中尚未持有的
            buy = [s for s in new_top if s not in current_holdings]

            # 继续持有
            hold = [s for s in current_holdings if s not in sell]

            # 换手率（买入 + 卖出 / 总持仓数）
            n_holdings = max(len(current_holdings) + len(buy), 1)
            turnover = (len(buy) + len(sell)) / n_holdings
            total_turnover += turnover

            signals.append(RotationSignal(
                rebalance_date=rb_date.strftime('%Y-%m-%d'),
                buy=buy,
                sell=sell,
                hold=hold,
                scores={k: round(v, 6) for k, v in scores.items()},
                top_n=self.top_n,
            ))

            # 更新持仓
            current_holdings = [s for s in current_holdings if s not in sell] + buy

        avg_turnover = total_turnover / len(signals) if signals else 0.0

        return SectorRotationResult(
            signals=signals,
            symbol_universe=list(self.sector_etfs.keys()),
            total_rebalances=len(signals),
            avg_turnover_pct=round(avg_turnover, 4),
        )

    def latest_signal(
        self,
        price_data: Dict[str, pd.DataFrame],
        current_holdings: Optional[List[str]] = None,
    ) -> RotationSignal:
        """
        只计算最新一次换仓信号（实时使用场景）。

        Parameters
        ----------
        price_data : {symbol: OHLCV DataFrame}
        current_holdings : List[str], optional
            当前持仓列表

        Returns
        -------
        RotationSignal
        """
        if current_holdings is None:
            current_holdings = []

        # 取所有数据的最新日期
        latest_date = max(
            df.index.max() for df in price_data.values()
            if not df.empty
        )
        scores = self._rank_symbols(price_data, latest_date)
        ranked = list(scores.keys())
        new_top = ranked[:self.top_n]
        exit_threshold = self.top_n + self.buffer
        sell = [s for s in current_holdings if s not in ranked[:exit_threshold]]
        buy = [s for s in new_top if s not in current_holdings]
        hold = [s for s in current_holdings if s not in sell]

        return RotationSignal(
            rebalance_date=pd.Timestamp(latest_date).strftime('%Y-%m-%d'),
            buy=buy,
            sell=sell,
            hold=hold,
            scores={k: round(v, 6) for k, v in scores.items()},
            top_n=self.top_n,
        )


# ---------------------------------------------------------------------------
# SectorRotationStrategyV2 — 数据驱动板块轮动 (W3-3)
# ---------------------------------------------------------------------------

@dataclass
class V2RotationSignal:
    """V2 数据驱动轮动信号(按个股而非 ETF)。"""
    timestamp: str
    top_sectors: List[Dict[str, float]]   # [{code, name, score}]
    buy_stocks: List[Dict[str, float]]    # [{symbol, sector_code, change_pct, amount}]
    universe_size: int


class SectorRotationStrategyV2:
    """
    数据驱动行业轮动策略(W3-3)。

    与 V1 的区别:
      - 弃用硬编码 DEFAULT_SECTOR_ETFS,universe 由 gw.sectors() 实时发现
      - 信号粒度从 ETF 切换到"top N 板块的成分股"
      - 板块评分综合 涨幅 + 资金流(net_flow) + 历史动量(SectorFlowStore)

    使用场景:
      - latest_signal() 取当日最新轮动建议
      - 用于 dynamic_selector 等"全市场扫板块挑股"流程

    Parameters
    ----------
    top_sectors_n : 选 top N 板块(默认 5)
    stocks_per_sector : 每个 top 板块取 top N 个成分股(默认 3)
    sector_score_method : 板块打分方式:
      'flow' - 仅 net_flow z-score
      'perf' - 仅当日涨幅
      'combined' - 0.5 * z(net_flow) + 0.5 * change_pct (默认)
    use_history : True 时从 SectorFlowStore 读历史辅助打分,False 仅用快照
    """

    def __init__(
        self,
        top_sectors_n: int = 5,
        stocks_per_sector: int = 3,
        sector_score_method: str = 'combined',
        use_history: bool = True,
    ) -> None:
        self.top_sectors_n = top_sectors_n
        self.stocks_per_sector = stocks_per_sector
        self.sector_score_method = sector_score_method
        self.use_history = use_history

    # ---------------------------------------------------------------- score

    @staticmethod
    def _zscore(values: List[float]) -> List[float]:
        arr = np.asarray(values, dtype=float)
        mu = np.nanmean(arr)
        std = np.nanstd(arr)
        if std == 0 or np.isnan(std):
            return [0.0] * len(values)
        return list((arr - mu) / std)

    def _score_sectors(self, sectors: List) -> List[Dict[str, float]]:
        """对一组 SectorRanking 打分,返回 [{code, name, score}] 降序。"""
        if not sectors:
            return []

        flows = [float(s.net_flow) for s in sectors]
        perfs = [float(s.change_pct) for s in sectors]

        if self.sector_score_method == 'flow':
            scores = self._zscore(flows)
        elif self.sector_score_method == 'perf':
            scores = perfs
        else:  # combined
            z_flow = self._zscore(flows)
            scores = [0.5 * zf + 0.5 * p for zf, p in zip(z_flow, perfs)]

        out = [
            {'code': s.code, 'name': s.name, 'score': float(sc)}
            for s, sc in zip(sectors, scores)
        ]
        return sorted(out, key=lambda x: x['score'], reverse=True)

    # ---------------------------------------------------------------- signal

    def latest_signal(self) -> V2RotationSignal:
        """
        取当日轮动信号:
          1. gw.sectors() → 全市场板块排名
          2. 打分排序 → 选 top N 板块
          3. gw.sector_constituents() → 每个 top 板块取头部成分股(按涨幅)
        """
        try:
            from core.data_gateway import get_gateway
            gw = get_gateway()
            sectors = gw.sectors(limit=100)
        except Exception as exc:
            # 无 universe 时退化为空信号(向后兼容)
            from datetime import datetime as _dt
            return V2RotationSignal(
                timestamp=_dt.now().strftime('%Y-%m-%d'),
                top_sectors=[], buy_stocks=[], universe_size=0,
            )

        ranked = self._score_sectors(sectors)
        top = ranked[:self.top_sectors_n]

        buy_stocks: List[Dict[str, float]] = []
        for s in top:
            try:
                consts = gw.sector_constituents(s['code'], limit=self.stocks_per_sector)
            except Exception:
                continue
            for c in consts:
                buy_stocks.append({
                    'symbol': c.symbol,
                    'sector_code': s['code'],
                    'sector_name': s['name'],
                    'sector_score': s['score'],
                    'change_pct': float(c.change_pct),
                    'amount': float(c.amount),
                })

        from datetime import datetime as _dt
        return V2RotationSignal(
            timestamp=_dt.now().strftime('%Y-%m-%d'),
            top_sectors=top,
            buy_stocks=buy_stocks,
            universe_size=len(sectors),
        )
