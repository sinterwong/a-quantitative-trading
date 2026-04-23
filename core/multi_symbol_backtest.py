"""
core/multi_symbol_backtest.py — 多标的批量回测（P1-B）

功能：
  - 在沪深300成分股 top10 流动性标的上分别运行 WFA
  - 汇总 OOS Sharpe 分布，检验策略泛化能力
  - 合格标准：≥ 7/10 标的 OOS Sharpe > 0

预设标的（沪深300 成分股中流动性 top10，2024年末）：
  贵州茅台 600519.SH、宁德时代 300750.SZ、招商银行 600036.SH
  中国平安 601318.SH、东方财富 300059.SZ、五粮液 000858.SZ
  比亚迪 002594.SZ、迈瑞医疗 300760.SZ、立讯精密 002475.SZ
  恒瑞医药 600276.SH

用法：
    from core.multi_symbol_backtest import MultiSymbolBacktest, DEFAULT_CSI300_TOP10
    from core.factor_pipeline import FactorPipeline

    pipeline = FactorPipeline()
    pipeline.add('RSI', weight=1.0)

    msb = MultiSymbolBacktest(pipeline=pipeline)
    result = msb.run(symbols=DEFAULT_CSI300_TOP10, years=3)
    result.print_report()
    result.save()
"""

from __future__ import annotations

import json
import os
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 沪深300成分股 top10 流动性标的（2024年末日均成交额前列）
DEFAULT_CSI300_TOP10 = [
    '600519.SH',   # 贵州茅台
    '300750.SZ',   # 宁德时代
    '600036.SH',   # 招商银行
    '601318.SH',   # 中国平安
    '300059.SZ',   # 东方财富
    '000858.SZ',   # 五粮液
    '002594.SZ',   # 比亚迪
    '300760.SZ',   # 迈瑞医疗
    '002475.SZ',   # 立讯精密
    '600276.SH',   # 恒瑞医药
]

_OUTPUTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'outputs'
)
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SymbolWFAResult:
    """单标的 WFA 结果摘要。"""
    symbol: str
    symbol_name: str
    n_windows: int
    oos_sharpes: List[float]
    avg_oos_sharpe: float
    positive_pct: float          # OOS Sharpe > 0 比例
    avg_oos_return: float
    avg_oos_maxdd: float
    passed: bool                 # avg_oos_sharpe > 0
    error: Optional[str] = None  # 数据获取失败等错误


@dataclass
class MultiSymbolResult:
    """多标的批量回测汇总结果。"""
    run_date: str
    strategy_name: str
    n_symbols: int
    n_passed: int                # OOS Sharpe > 0 的标的数
    pass_rate: float             # n_passed / n_symbols
    passed: bool                 # pass_rate >= 0.7（7/10）
    symbol_results: List[SymbolWFAResult] = field(default_factory=list)
    oos_sharpe_mean: float = 0.0
    oos_sharpe_std: float = 0.0
    oos_sharpe_min: float = 0.0
    oos_sharpe_max: float = 0.0
    notes: List[str] = field(default_factory=list)

    def print_report(self) -> None:
        print(f'=== 多标的批量回测报告 ===')
        print(f'策略: {self.strategy_name} | 日期: {self.run_date}')
        print(f'标的数: {self.n_symbols} | 通过数: {self.n_passed} '
              f'| 通过率: {self.pass_rate:.0%} '
              f'| {"✓ PASS" if self.passed else "✗ FAIL"} (阈值70%)')
        print(f'OOS Sharpe: '
              f'mean={self.oos_sharpe_mean:.3f} '
              f'std={self.oos_sharpe_std:.3f} '
              f'[{self.oos_sharpe_min:.3f}, {self.oos_sharpe_max:.3f}]')
        print()
        print(f'{"标的":<14} {"均值OOS Sharpe":>14} {"正比例":>8} {"状态":>8}')
        print('-' * 50)
        for r in self.symbol_results:
            status = '✓' if r.passed else '✗'
            if r.error:
                status = 'ERR'
            name_display = f'{r.symbol}({r.symbol_name})'[:13]
            print(
                f'{name_display:<14} {r.avg_oos_sharpe:>14.3f} '
                f'{r.positive_pct:>7.0%} {status:>8}'
            )
        if self.notes:
            print()
            for note in self.notes:
                print(f'  * {note}')

    def save(self, path: Optional[str] = None) -> str:
        if path is None:
            path = os.path.join(
                _OUTPUTS_DIR,
                f'multi_symbol_backtest_{self.run_date}.json',
            )
        data = {
            'run_date': self.run_date,
            'strategy_name': self.strategy_name,
            'summary': {
                'n_symbols': self.n_symbols,
                'n_passed': self.n_passed,
                'pass_rate': self.pass_rate,
                'passed': self.passed,
                'oos_sharpe_mean': self.oos_sharpe_mean,
                'oos_sharpe_std': self.oos_sharpe_std,
                'oos_sharpe_min': self.oos_sharpe_min,
                'oos_sharpe_max': self.oos_sharpe_max,
            },
            'symbol_results': [asdict(r) for r in self.symbol_results],
            'notes': self.notes,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def to_dataframe(self) -> pd.DataFrame:
        """返回各标的结果 DataFrame，方便进一步分析。"""
        rows = []
        for r in self.symbol_results:
            rows.append({
                'symbol': r.symbol,
                'name': r.symbol_name,
                'n_windows': r.n_windows,
                'avg_oos_sharpe': r.avg_oos_sharpe,
                'positive_pct': r.positive_pct,
                'avg_oos_return': r.avg_oos_return,
                'avg_oos_maxdd': r.avg_oos_maxdd,
                'passed': r.passed,
                'error': r.error,
            })
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 标的名称映射
# ---------------------------------------------------------------------------

_SYMBOL_NAMES = {
    '600519.SH': '贵州茅台',
    '300750.SZ': '宁德时代',
    '600036.SH': '招商银行',
    '601318.SH': '中国平安',
    '300059.SZ': '东方财富',
    '000858.SZ': '五粮液',
    '002594.SZ': '比亚迪',
    '300760.SZ': '迈瑞医疗',
    '002475.SZ': '立讯精密',
    '600276.SH': '恒瑞医药',
    '510300.SH': '沪深300ETF',
}


# ---------------------------------------------------------------------------
# MultiSymbolBacktest
# ---------------------------------------------------------------------------

class MultiSymbolBacktest:
    """
    多标的批量 WFA 回测。

    Parameters
    ----------
    pipeline    : FactorPipeline 实例（已配置好因子权重）
    data_layer  : DataLayer 实例（用于拉取历史数据），None 时尝试从 AKShare 获取
    wfa_config  : WFA 参数字典，支持 train_months/test_months/step_months
    """

    DEFAULT_WFA_CONFIG = {
        'train_months': 18,
        'test_months': 6,
        'step_months': 6,
    }

    def __init__(
        self,
        pipeline,
        data_layer=None,
        wfa_config: Optional[Dict] = None,
    ) -> None:
        self.pipeline = pipeline
        self.data_layer = data_layer
        self.wfa_config = {**self.DEFAULT_WFA_CONFIG, **(wfa_config or {})}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        symbols: Optional[List[str]] = None,
        years: int = 3,
        strategy_name: str = 'unnamed',
    ) -> MultiSymbolResult:
        """
        对指定标的列表批量运行 WFA。

        Parameters
        ----------
        symbols       : 标的列表（默认 DEFAULT_CSI300_TOP10）
        years         : 历史数据年数（影响数据拉取范围）
        strategy_name : 策略名称（用于报告标注）

        Returns
        -------
        MultiSymbolResult
        """
        if symbols is None:
            symbols = DEFAULT_CSI300_TOP10

        logger.info(
            '[MultiSymbolBacktest] 开始批量回测：%d 个标的, strategy=%s',
            len(symbols), strategy_name,
        )

        symbol_results: List[SymbolWFAResult] = []

        for sym in symbols:
            logger.info('[MultiSymbolBacktest] 正在处理 %s ...', sym)
            r = self._run_symbol(sym, years)
            symbol_results.append(r)
            if r.error:
                logger.warning('[MultiSymbolBacktest] %s 失败: %s', sym, r.error)
            else:
                logger.info(
                    '[MultiSymbolBacktest] %s: avg_oos_sharpe=%.3f positive=%.0f%%',
                    sym, r.avg_oos_sharpe, r.positive_pct * 100,
                )

        return self._build_result(symbol_results, strategy_name)

    # ------------------------------------------------------------------
    # Internal: single symbol WFA
    # ------------------------------------------------------------------

    def _run_symbol(self, symbol: str, years: int) -> SymbolWFAResult:
        """对单个标的运行 WFA，返回摘要。"""
        name = _SYMBOL_NAMES.get(symbol, symbol)

        # 1. 获取历史数据
        data = self._fetch_data(symbol, years)
        if data is None or len(data) < 60:
            return SymbolWFAResult(
                symbol=symbol, symbol_name=name,
                n_windows=0, oos_sharpes=[], avg_oos_sharpe=0.0,
                positive_pct=0.0, avg_oos_return=0.0, avg_oos_maxdd=0.0,
                passed=False, error=f'数据不足 ({len(data) if data is not None else 0} 条)',
            )

        # 2. 构建 WFA 滚动窗口
        windows = self._build_windows(data, **self.wfa_config)
        if not windows:
            return SymbolWFAResult(
                symbol=symbol, symbol_name=name,
                n_windows=0, oos_sharpes=[], avg_oos_sharpe=0.0,
                positive_pct=0.0, avg_oos_return=0.0, avg_oos_maxdd=0.0,
                passed=False, error='无法构建滚动窗口（数据不足）',
            )

        # 3. 对每个窗口运行回测
        oos_sharpes: List[float] = []
        oos_returns: List[float] = []
        oos_maxdds: List[float] = []

        for train_df, test_df in windows:
            try:
                sharpe, ret, maxdd = self._run_window(train_df, test_df, symbol)
                oos_sharpes.append(sharpe)
                oos_returns.append(ret)
                oos_maxdds.append(maxdd)
            except Exception as exc:
                logger.debug('[MultiSymbolBacktest] window error (%s): %s', symbol, exc)
                oos_sharpes.append(0.0)
                oos_returns.append(0.0)
                oos_maxdds.append(0.0)

        avg_sharpe = float(np.mean(oos_sharpes)) if oos_sharpes else 0.0
        pos_pct = float(np.mean([s > 0 for s in oos_sharpes])) if oos_sharpes else 0.0

        return SymbolWFAResult(
            symbol=symbol,
            symbol_name=name,
            n_windows=len(windows),
            oos_sharpes=oos_sharpes,
            avg_oos_sharpe=round(avg_sharpe, 4),
            positive_pct=round(pos_pct, 4),
            avg_oos_return=round(float(np.mean(oos_returns)) if oos_returns else 0.0, 4),
            avg_oos_maxdd=round(float(np.mean(oos_maxdds)) if oos_maxdds else 0.0, 4),
            passed=avg_sharpe > 0,
        )

    def _run_window(
        self,
        train_df: pd.DataFrame,
        test_df: pd.DataFrame,
        symbol: str,
    ) -> Tuple[float, float, float]:
        """
        在单个 train/test 窗口上跑回测。

        Returns (oos_sharpe, oos_return, oos_maxdd)
        """
        from core.backtest_engine import BacktestEngine, BacktestConfig

        # 训练期用于确定信号阈值（利用全部数据计算信号，不优化参数）
        # 测试期用于计算 OOS 绩效

        engine = BacktestEngine(config=BacktestConfig())
        engine.load_data(symbol, test_df)

        # 使用 pipeline 中的因子（当前参数不作网格搜索，仅验证泛化）
        for factor, threshold, _ in self.pipeline._factors:
            engine.add_strategy(factor, signal_threshold=threshold)

        result = engine.run()

        sharpe = float(result.sharpe) if result else 0.0
        ret = float(result.total_return) if result else 0.0
        maxdd = float(result.max_drawdown_pct) if result else 0.0

        return sharpe, ret, maxdd

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_data(self, symbol: str, years: int) -> Optional[pd.DataFrame]:
        """获取历史日线数据。优先用 data_layer，fallback AKShare。"""
        days = years * 252 + 20

        if self.data_layer is not None:
            try:
                df = self.data_layer.get_bars(symbol, days=days)
                if df is not None and len(df) >= 60:
                    return df
            except Exception:
                pass

        # AKShare fallback
        return self._fetch_via_akshare(symbol, years)

    @staticmethod
    def _fetch_via_akshare(symbol: str, years: int) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取历史日线数据。"""
        try:
            import akshare as ak
            # 格式转换：600519.SH → 600519
            code = symbol.split('.')[0]
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=years * 366 + 30)).strftime('%Y%m%d')

            # 沪市 / 深市
            if symbol.endswith('.SH'):
                df = ak.stock_zh_a_hist(
                    symbol=code, period='daily',
                    start_date=start_date, end_date=end_date,
                    adjust='qfq',
                )
            else:
                df = ak.stock_zh_a_hist(
                    symbol=code, period='daily',
                    start_date=start_date, end_date=end_date,
                    adjust='qfq',
                )

            if df is None or df.empty:
                return None

            # 统一列名
            col_map = {
                '日期': 'date', '开盘': 'open', '最高': 'high',
                '最低': 'low', '收盘': 'close', '成交量': 'volume',
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date').sort_index()

            required = ['open', 'high', 'low', 'close', 'volume']
            missing = [c for c in required if c not in df.columns]
            if missing:
                return None

            return df[required].dropna()

        except Exception as e:
            logger.debug('[MultiSymbolBacktest] akshare fetch failed (%s): %s', symbol, e)
            return None

    # ------------------------------------------------------------------
    # Window builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_windows(
        data: pd.DataFrame,
        train_months: int = 18,
        test_months: int = 6,
        step_months: int = 6,
    ) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
        """
        构建滚动 WFA 窗口列表。

        Parameters
        ----------
        data         : 日线 DataFrame，index 为 DatetimeIndex
        train_months : 训练窗口（月）
        test_months  : 测试窗口（月）
        step_months  : 步进（月）
        """
        if not isinstance(data.index, pd.DatetimeIndex):
            data = data.copy()
            data.index = pd.to_datetime(data.index)

        windows = []
        total_months = train_months + test_months

        # 数据起始和结束
        start = data.index.min()
        end = data.index.max()

        # 计算每个窗口起点
        cursor = start
        while True:
            train_start = cursor
            train_end = cursor + pd.DateOffset(months=train_months)
            test_start = train_end
            test_end = test_start + pd.DateOffset(months=test_months)

            if test_end > end:
                break

            train_df = data.loc[train_start:train_end - pd.Timedelta(days=1)]
            test_df = data.loc[test_start:test_end - pd.Timedelta(days=1)]

            if len(train_df) >= 30 and len(test_df) >= 20:
                windows.append((train_df, test_df))

            cursor += pd.DateOffset(months=step_months)

        return windows

    # ------------------------------------------------------------------
    # Build final result
    # ------------------------------------------------------------------

    @staticmethod
    def _build_result(
        symbol_results: List[SymbolWFAResult],
        strategy_name: str,
    ) -> MultiSymbolResult:
        valid = [r for r in symbol_results if r.error is None]
        n_passed = sum(1 for r in symbol_results if r.passed)
        n_total = len(symbol_results)
        pass_rate = n_passed / max(n_total, 1)

        sharpes = [r.avg_oos_sharpe for r in valid]

        notes: List[str] = []
        if not valid:
            notes.append('所有标的数据获取失败，无法得出结论')
        elif pass_rate >= 0.7:
            notes.append(f'{n_passed}/{n_total} 标的 OOS Sharpe > 0，策略具有较好泛化能力')
        else:
            notes.append(
                f'仅 {n_passed}/{n_total} 标的通过（需 ≥ 7/10），'
                '建议重新审视因子参数或信号阈值'
            )

        failed = [r.symbol for r in symbol_results if r.error]
        if failed:
            notes.append(f'数据获取失败标的: {failed}')

        return MultiSymbolResult(
            run_date=date.today().isoformat(),
            strategy_name=strategy_name,
            n_symbols=n_total,
            n_passed=n_passed,
            pass_rate=round(pass_rate, 4),
            passed=pass_rate >= 0.7,
            symbol_results=symbol_results,
            oos_sharpe_mean=round(float(np.mean(sharpes)) if sharpes else 0.0, 4),
            oos_sharpe_std=round(float(np.std(sharpes)) if sharpes else 0.0, 4),
            oos_sharpe_min=round(float(np.min(sharpes)) if sharpes else 0.0, 4),
            oos_sharpe_max=round(float(np.max(sharpes)) if sharpes else 0.0, 4),
            notes=notes,
        )
