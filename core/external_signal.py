"""
core/external_signal.py — 外盘领先信号统计验证（P2-C）

组件：
  1. SP500GrangerAnalyzer   — SP500期货隔夜涨跌 → A股次日开盘涨跌 Granger 因果检验
  2. NorthboundStatsAnalyzer — 北向资金净流入 > 50亿时 A股次日上涨概率统计

合格标准：
  SP500:      Granger p-value < 0.05，IC > 0.05
  Northbound: 净流入 > 50亿时次日上涨概率 > 55%，样本量 ≥ 100

实现细节：
  - Granger 检验用纯 numpy 自回归 OLS（不依赖 statsmodels）
  - 数据来源：yfinance（SP500）、AKShare（A股/北向）
  - 输出：ExternalSignalReport dataclass + save_report() JSON
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_OUTPUTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'outputs'
)
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Granger 因果检验（纯 numpy，不依赖 statsmodels）
# ---------------------------------------------------------------------------

def _ols_rss(X: np.ndarray, y: np.ndarray) -> float:
    """OLS 残差平方和。X 已含截距列。"""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    return float(resid @ resid)


def granger_test(
    y: np.ndarray,
    x: np.ndarray,
    max_lag: int = 3,
) -> Tuple[float, int, float]:
    """
    双变量 Granger 因果检验：x → y。

    用 F 检验：
      restricted:  y_t = a0 + Σa_i * y_{t-i} + ε
      unrestricted: y_t = a0 + Σa_i * y_{t-i} + Σb_i * x_{t-i} + ε

    Parameters
    ----------
    y, x : 等长 1D 数组（已对齐）
    max_lag : 最大滞后阶数

    Returns
    -------
    (p_value, best_lag, f_stat)
      best_lag: F 统计量最大对应的滞后阶数
    """
    n = len(y)
    best_p = 1.0
    best_lag = 1
    best_f = 0.0

    for lag in range(1, max_lag + 1):
        start = lag
        T = n - start

        # 构建 y 的历史矩阵
        Y = y[start:]
        ones = np.ones((T, 1))
        Y_lags = np.column_stack([y[start - k: n - k] for k in range(1, lag + 1)])

        # Restricted：仅 y 的自回归
        X_r = np.hstack([ones, Y_lags])
        rss_r = _ols_rss(X_r, Y)

        # Unrestricted：加入 x 的历史
        X_lags = np.column_stack([x[start - k: n - k] for k in range(1, lag + 1)])
        X_u = np.hstack([X_r, X_lags])
        rss_u = _ols_rss(X_u, Y)

        # F 统计量
        df1 = lag          # 新增参数数 = lag
        df2 = T - X_u.shape[1]
        if df2 <= 0 or rss_u <= 0:
            continue

        f_stat = ((rss_r - rss_u) / df1) / (rss_u / df2)

        # F 分布 p-value（近似用 chi2/df）
        chi2 = f_stat * df1
        p_val = float(_chi2_sf(chi2, df1))

        if f_stat > best_f:
            best_f = f_stat
            best_p = p_val
            best_lag = lag

    return best_p, best_lag, best_f


def _chi2_sf(x: float, df: int) -> float:
    """
    chi2 生存函数近似（df 较小时精度足够）。
    用 Wilson-Hilferty 正态近似。
    """
    if x <= 0:
        return 1.0
    mu = df
    sigma = (2 * df) ** 0.5
    # Wilson-Hilferty 近似
    k = df / 2
    z = (x / df) ** (1 / 3)
    z_norm = (z - (1 - 1 / (9 * k))) / (1 / (9 * k)) ** 0.5
    # 标准正态 SF
    return float(_norm_sf(z_norm))


def _norm_sf(z: float) -> float:
    """标准正态 SF（ERFC 近似）。"""
    import math
    return 0.5 * math.erfc(z / math.sqrt(2))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SP500GrangerResult:
    """SP500 Granger 因果检验结果。"""
    start_date: str
    end_date: str
    n_samples: int
    best_lag: int
    f_stat: float
    p_value: float
    ic: float                  # Spearman IC：SP500隔夜涨跌 vs A股次日开盘涨跌
    passed: bool               # p < 0.05 且 IC > 0.05
    notes: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        status = 'PASS' if self.passed else 'FAIL'
        return (
            f'SP500→CSI300 Granger检验 [{status}] '
            f'lag={self.best_lag} F={self.f_stat:.2f} p={self.p_value:.4f} '
            f'IC={self.ic:.4f} n={self.n_samples}'
        )


@dataclass
class NorthboundStatsResult:
    """北向资金信号统计结果。"""
    start_date: str
    end_date: str
    threshold_bn: float        # 净流入阈值（亿元）
    n_samples: int             # 总样本数
    n_above_threshold: int     # 净流入 > threshold 的天数
    next_day_up_pct: float     # 净流入 > threshold 时次日上涨概率
    baseline_up_pct: float     # 基准上涨概率（全样本）
    lift: float                # next_day_up_pct - baseline_up_pct
    passed: bool               # n_above >= 100 且 next_day_up_pct > 0.55
    notes: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        status = 'PASS' if self.passed else 'FAIL'
        return (
            f'北向资金信号 [{status}] '
            f'threshold={self.threshold_bn}亿 '
            f'条件P(次日↑)={self.next_day_up_pct:.1%} '
            f'基准={self.baseline_up_pct:.1%} '
            f'lift={self.lift:+.1%} '
            f'n_above={self.n_above_threshold} total={self.n_samples}'
        )


# ---------------------------------------------------------------------------
# SP500GrangerAnalyzer
# ---------------------------------------------------------------------------

class SP500GrangerAnalyzer:
    """
    验证 SP500 期货隔夜涨跌对 A 股次日开盘涨跌的 Granger 因果关系。

    数据流：
      SP500期货收盘价（前日）→ 当日开盘价 → 隔夜涨跌幅
      沪深300 次日开盘涨跌幅
    """

    def __init__(self, sp500_symbol: str = 'ES=F', csi300_symbol: str = '000300.SH') -> None:
        self.sp500_symbol = sp500_symbol
        self.csi300_symbol = csi300_symbol

    def fetch_data(self, days: int = 500) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """
        获取 SP500 期货历史 + 沪深300历史。

        Returns
        -------
        (sp500_df, csi300_df) or (None, None) if fetch fails
        """
        sp_df = self._fetch_sp500(days)
        csi_df = self._fetch_csi300(days)
        return sp_df, csi_df

    def analyze(self, days: int = 500, max_lag: int = 3) -> SP500GrangerResult:
        """
        执行完整的 Granger 检验和 IC 计算。

        Parameters
        ----------
        days    : 历史数据天数
        max_lag : Granger 最大滞后阶数
        """
        sp_df, csi_df = self.fetch_data(days)

        notes: List[str] = []

        if sp_df is None or sp_df.empty:
            notes.append('SP500数据获取失败，无法进行 Granger 检验')
            return SP500GrangerResult(
                start_date='', end_date='', n_samples=0,
                best_lag=0, f_stat=0, p_value=1.0, ic=0.0,
                passed=False, notes=notes,
            )

        if csi_df is None or csi_df.empty:
            notes.append('CSI300数据获取失败，无法进行 Granger 检验')
            return SP500GrangerResult(
                start_date='', end_date='', n_samples=0,
                best_lag=0, f_stat=0, p_value=1.0, ic=0.0,
                passed=False, notes=notes,
            )

        # 构建对齐序列
        x_series, y_series = self._align_series(sp_df, csi_df)

        if len(x_series) < 30:
            notes.append(f'对齐后样本量不足 ({len(x_series)} < 30)，结果不可靠')

        n = len(x_series)
        x_arr = x_series.values.astype(float)
        y_arr = y_series.values.astype(float)

        # Granger 检验
        p_val, best_lag, f_stat = granger_test(y_arr, x_arr, max_lag=max_lag)

        # Spearman IC
        ic = self._spearman_ic(x_arr, y_arr)

        passed = (p_val < 0.05) and (abs(ic) > 0.05)

        if not passed:
            if p_val >= 0.05:
                notes.append(f'Granger p={p_val:.4f} ≥ 0.05，SP500对A股无显著领先效应')
            if abs(ic) <= 0.05:
                notes.append(f'IC={ic:.4f} ≤ 0.05，信号强度不足，不建议作为过滤条件')
        else:
            notes.append(
                f'SP500隔夜涨跌对CSI300次日开盘具有显著领先效应 '
                f'(lag={best_lag}, p={p_val:.4f}, IC={ic:.4f})'
            )
            notes.append('建议：外盘大跌时（SP500隔夜 < -1%）抑制A股买入信号')

        dates = x_series.index
        return SP500GrangerResult(
            start_date=str(dates.min().date()) if len(dates) > 0 else '',
            end_date=str(dates.max().date()) if len(dates) > 0 else '',
            n_samples=n,
            best_lag=best_lag,
            f_stat=round(f_stat, 4),
            p_value=round(p_val, 6),
            ic=round(ic, 4),
            passed=passed,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_sp500(self, days: int) -> Optional[pd.DataFrame]:
        """通过 yfinance 获取 SP500 期货历史。"""
        try:
            import yfinance as yf
            period = f'{min(days + 10, 730)}d'
            df = yf.download(self.sp500_symbol, period=period, auto_adjust=True, progress=False)
            if df.empty:
                return None
            df.index = pd.to_datetime(df.index).tz_localize(None)
            return df.tail(days)
        except Exception as e:
            return None

    def _fetch_csi300(self, days: int) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取沪深300历史。"""
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol='sh000300')
            if df is None or df.empty:
                return None
            df.index = pd.to_datetime(df['date'])
            df = df.sort_index()
            return df.tail(days + 10)
        except Exception:
            # fallback: yfinance
            try:
                import yfinance as yf
                df = yf.download('000300.SS', period=f'{days+10}d',
                                 auto_adjust=True, progress=False)
                if df.empty:
                    return None
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df.tail(days)
            except Exception:
                return None

    @staticmethod
    def _align_series(
        sp_df: pd.DataFrame,
        csi_df: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        构建对齐后的序列对：
          x = SP500 当日收盘涨跌幅（隔夜，代表前日close→当日open）
          y = CSI300 次日开盘涨跌幅（open[t+1]/close[t] - 1）
        """
        # SP500 日涨跌幅（以收盘价计，近似隔夜效应）
        sp_close = sp_df['Close'] if 'Close' in sp_df.columns else sp_df.iloc[:, 3]
        sp_ret = sp_close.pct_change().dropna()

        # CSI300 次日开盘涨跌
        if 'open' in csi_df.columns:
            csi_open = csi_df['open']
            csi_close = csi_df['close']
        elif 'Open' in csi_df.columns:
            csi_open = csi_df['Open']
            csi_close = csi_df['Close']
        else:
            return pd.Series(dtype=float), pd.Series(dtype=float)

        csi_open_ret = (csi_open / csi_close.shift(1) - 1).dropna()

        # 次日对齐：y[t] = CSI open_ret[t+1]
        sp_ret.index = pd.to_datetime(sp_ret.index)
        csi_open_ret.index = pd.to_datetime(csi_open_ret.index)

        # inner join by date string（跨时区对齐）
        sp_dates = pd.Series(
            sp_ret.values, index=sp_ret.index.normalize(), name='sp'
        )
        csi_dates = pd.Series(
            csi_open_ret.values,
            index=csi_open_ret.index.normalize(),
            name='csi',
        )

        # x[t]（SP500 day t）配对 y[t+1]（CSI 次日开盘）
        csi_shifted = csi_dates.shift(-1)

        combined = pd.DataFrame({'x': sp_dates, 'y': csi_shifted}).dropna()
        return combined['x'], combined['y']

    @staticmethod
    def _spearman_ic(x: np.ndarray, y: np.ndarray) -> float:
        if len(x) < 3:
            return 0.0
        rx = pd.Series(x).rank().values
        ry = pd.Series(y).rank().values
        c = np.corrcoef(rx, ry)[0, 1]
        return float(c) if not np.isnan(c) else 0.0


# ---------------------------------------------------------------------------
# NorthboundStatsAnalyzer
# ---------------------------------------------------------------------------

class NorthboundStatsAnalyzer:
    """
    验证北向资金净流入 > threshold 时 A 股次日上涨概率。

    数据来源：AKShare（东方财富北向历史）
    合格标准：n_above ≥ 100 且 P(次日↑|净流入>threshold) > 55%
    """

    def __init__(
        self,
        threshold_bn: float = 50.0,    # 亿元
        csi300_symbol: str = 'sh000300',
    ) -> None:
        self.threshold_bn = threshold_bn
        self.csi300_symbol = csi300_symbol

    def analyze(self, days: int = 500) -> NorthboundStatsResult:
        """
        执行北向资金统计验证。

        Parameters
        ----------
        days : 历史数据天数
        """
        notes: List[str] = []

        nb_df = self._fetch_northbound(days)
        csi_df = self._fetch_csi300(days)

        if nb_df is None or nb_df.empty:
            notes.append('北向资金数据获取失败')
            return self._empty_result(notes)

        if csi_df is None or csi_df.empty:
            notes.append('CSI300数据获取失败')
            return self._empty_result(notes)

        # 构建日度净流入（亿元）序列
        flow = self._build_flow_series(nb_df)
        # 构建 CSI300 次日涨跌序列
        csi_ret = self._build_next_day_ret(csi_df)

        # 对齐
        combined = pd.DataFrame({'flow': flow, 'next_ret': csi_ret}).dropna()

        if len(combined) < 20:
            notes.append(f'对齐后样本量不足 ({len(combined)})，结果不可靠')
            return self._empty_result(notes)

        n_total = len(combined)
        baseline_up = float((combined['next_ret'] > 0).mean())

        mask = combined['flow'] > self.threshold_bn
        n_above = int(mask.sum())

        if n_above == 0:
            notes.append(f'无净流入 > {self.threshold_bn}亿 的样本，阈值过高')
            return NorthboundStatsResult(
                start_date=str(combined.index.min().date()),
                end_date=str(combined.index.max().date()),
                threshold_bn=self.threshold_bn,
                n_samples=n_total,
                n_above_threshold=0,
                next_day_up_pct=0.0,
                baseline_up_pct=round(baseline_up, 4),
                lift=0.0,
                passed=False,
                notes=notes,
            )

        cond_up = float((combined.loc[mask, 'next_ret'] > 0).mean())
        lift = cond_up - baseline_up
        passed = (n_above >= 100) and (cond_up > 0.55)

        if n_above < 100:
            notes.append(f'样本量不足：净流入>{self.threshold_bn}亿 仅 {n_above} 天 (需≥100)')
        if not passed:
            notes.append(
                f'条件上涨概率 {cond_up:.1%} ≤ 55%，北向信号统计显著性不足'
            )
        else:
            notes.append(
                f'北向净流入>{self.threshold_bn}亿 时次日上涨概率 {cond_up:.1%}，'
                f'高于基准 {baseline_up:.1%}，lift={lift:+.1%}'
            )
            notes.append('建议：将北向净流入作为买入信号的辅助确认条件')

        return NorthboundStatsResult(
            start_date=str(combined.index.min().date()),
            end_date=str(combined.index.max().date()),
            threshold_bn=self.threshold_bn,
            n_samples=n_total,
            n_above_threshold=n_above,
            next_day_up_pct=round(cond_up, 4),
            baseline_up_pct=round(baseline_up, 4),
            lift=round(lift, 4),
            passed=passed,
            notes=notes,
        )

    def _empty_result(self, notes: List[str]) -> NorthboundStatsResult:
        return NorthboundStatsResult(
            start_date='', end_date='',
            threshold_bn=self.threshold_bn,
            n_samples=0, n_above_threshold=0,
            next_day_up_pct=0.0, baseline_up_pct=0.0, lift=0.0,
            passed=False, notes=notes,
        )

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_northbound(days: int) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取北向资金历史净流入。"""
        try:
            import akshare as ak
            # 沪深港通资金流向历史
            df = ak.stock_connect_hist_em(symbol='沪深港通')
            if df is None or df.empty:
                # 尝试沪股通
                df = ak.stock_hsgt_north_net_flow_in_em(start_date='20200101',
                                                         end_date=datetime.now().strftime('%Y%m%d'))
            return df
        except Exception:
            return None

    @staticmethod
    def _fetch_csi300(days: int) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取沪深300历史。"""
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol='sh000300')
            if df is None or df.empty:
                return None
            df.index = pd.to_datetime(df['date'])
            df = df.sort_index()
            return df.tail(days + 10)
        except Exception:
            return None

    @staticmethod
    def _build_flow_series(nb_df: pd.DataFrame) -> pd.Series:
        """
        从北向资金 DataFrame 提取日度净流入（亿元）序列。
        AKShare 不同接口字段名不同，尝试多种列名。
        """
        date_cols = ['日期', 'date', 'trade_date']
        flow_cols = ['北向资金', '净买入额', '当日净买入', '净流入', 'value', 'net_buy']

        # 找 date 列
        date_col = next((c for c in date_cols if c in nb_df.columns), None)
        if date_col is None:
            date_col = nb_df.columns[0]

        # 找 flow 列
        flow_col = next((c for c in flow_cols if c in nb_df.columns), None)
        if flow_col is None:
            flow_col = nb_df.columns[-1]

        try:
            dates = pd.to_datetime(nb_df[date_col])
            values = pd.to_numeric(nb_df[flow_col], errors='coerce')
            series = pd.Series(values.values, index=dates).dropna()
            # 单位统一到亿元
            if series.abs().mean() > 1e8:   # 如果是元，换算为亿
                series = series / 1e8
            elif series.abs().mean() > 1e4:  # 如果是万元
                series = series / 1e4
            return series.sort_index()
        except Exception:
            return pd.Series(dtype=float)

    @staticmethod
    def _build_next_day_ret(csi_df: pd.DataFrame) -> pd.Series:
        """计算 CSI300 次日收益（close[t+1]/close[t] - 1）。"""
        close_col = 'close' if 'close' in csi_df.columns else 'Close'
        if close_col not in csi_df.columns:
            return pd.Series(dtype=float)
        close = csi_df[close_col].astype(float)
        ret = close.pct_change().shift(-1).dropna()  # 次日收益
        return ret


# ---------------------------------------------------------------------------
# 统一报告输出
# ---------------------------------------------------------------------------

@dataclass
class ExternalSignalReport:
    """外盘信号综合报告。"""
    generated_at: str
    sp500_granger: Optional[SP500GrangerResult] = None
    northbound_stats: Optional[NorthboundStatsResult] = None

    def save(self, path: Optional[str] = None) -> str:
        if path is None:
            path = os.path.join(
                _OUTPUTS_DIR,
                f'external_signal_report_{date.today().isoformat()}.json',
            )
        data: Dict = {'generated_at': self.generated_at}
        if self.sp500_granger:
            data['sp500_granger'] = asdict(self.sp500_granger)
        if self.northbound_stats:
            data['northbound_stats'] = asdict(self.northbound_stats)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def print_report(self) -> None:
        print('=== 外盘领先信号验证报告 ===')
        print(f'生成时间：{self.generated_at}')
        if self.sp500_granger:
            print()
            print(self.sp500_granger.summary)
            for n in self.sp500_granger.notes:
                print(f'  * {n}')
        if self.northbound_stats:
            print()
            print(self.northbound_stats.summary)
            for n in self.northbound_stats.notes:
                print(f'  * {n}')


def run_full_analysis(
    sp500_days: int = 500,
    nb_days: int = 500,
    nb_threshold_bn: float = 50.0,
    max_lag: int = 3,
    save: bool = True,
) -> ExternalSignalReport:
    """
    一键运行全部外盘信号验证并输出报告。

    Parameters
    ----------
    sp500_days      : SP500 历史天数
    nb_days         : 北向资金历史天数
    nb_threshold_bn : 北向净流入阈值（亿元）
    max_lag         : Granger 最大滞后阶数
    save            : 是否保存 JSON 报告

    Returns
    -------
    ExternalSignalReport
    """
    sp_result = SP500GrangerAnalyzer().analyze(days=sp500_days, max_lag=max_lag)
    nb_result = NorthboundStatsAnalyzer(threshold_bn=nb_threshold_bn).analyze(days=nb_days)

    report = ExternalSignalReport(
        generated_at=datetime.now().isoformat(timespec='seconds'),
        sp500_granger=sp_result,
        northbound_stats=nb_result,
    )

    if save:
        path = report.save()
        print(f'[ExternalSignal] 报告已保存至 {path}')

    return report
