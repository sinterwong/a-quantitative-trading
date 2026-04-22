"""
core/strategy_health.py — 策略健康度实时监控（P3-D）

独立模块，无 broker/backend 依赖，可被：
  - backend/services/intraday_monitor.py 调用（带飞书告警）
  - Streamlit Dashboard 直接调用（可视化）
  - 单元测试

监控指标：
  1. Rolling 20日 Sharpe：下降 > 30% 触发 WARN
  2. 单日亏损 > 2% 触发 CRITICAL + 建议暂停自动交易
  3. 连续亏损天数：> 5 天触发 WARN
  4. 持仓周转率异常：日均交易次数突变 > 2×历史均值触发 WARN

用法：
    from core.strategy_health import StrategyHealthMonitor

    monitor = StrategyHealthMonitor()
    report = monitor.check(daily_stats)   # daily_stats: List[DailyStats]

    if report.has_critical():
        print(report.summary())
        # 暂停自动交易
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class HealthAlert:
    """单条健康告警"""
    level: str          # 'OK' | 'WARN' | 'CRITICAL'
    check_name: str     # 告警检查项名称
    message: str        # 人类可读描述
    value: float        # 触发值
    threshold: float    # 阈值
    should_pause: bool = False  # True 时建议暂停自动交易


@dataclass
class HealthReport:
    """策略健康度完整报告"""
    check_date: date
    alerts: List[HealthAlert] = field(default_factory=list)
    # 关键指标快照
    rolling_sharpe_20d: float = 0.0
    rolling_sharpe_60d: float = 0.0
    sharpe_change_pct: float = 0.0   # 20日 vs 60日 Sharpe 变化幅度
    latest_daily_return: float = 0.0
    consecutive_loss_days: int = 0
    win_rate_20d: float = 0.0
    avg_trades_20d: float = 0.0
    avg_trades_hist: float = 0.0

    def has_critical(self) -> bool:
        return any(a.level == 'CRITICAL' for a in self.alerts)

    def has_warn(self) -> bool:
        return any(a.level in ('WARN', 'CRITICAL') for a in self.alerts)

    def should_pause_trading(self) -> bool:
        return any(a.should_pause for a in self.alerts)

    def worst_level(self) -> str:
        if self.has_critical():
            return 'CRITICAL'
        if self.has_warn():
            return 'WARN'
        return 'OK'

    def summary(self) -> str:
        level_emoji = {'OK': '✅', 'WARN': '⚠️', 'CRITICAL': '🚨'}
        emoji = level_emoji.get(self.worst_level(), '❓')
        lines = [
            f"{emoji} 策略健康度报告 [{self.check_date}]",
            f"   Rolling Sharpe(20d): {self.rolling_sharpe_20d:.3f}",
            f"   Rolling Sharpe(60d): {self.rolling_sharpe_60d:.3f}",
            f"   Sharpe 变化:         {self.sharpe_change_pct:+.1f}%",
            f"   最新日收益:          {self.latest_daily_return*100:+.2f}%",
            f"   连续亏损天数:        {self.consecutive_loss_days}",
            f"   近20日胜率:          {self.win_rate_20d*100:.1f}%",
        ]
        if self.alerts:
            lines.append("  告警:")
            for a in self.alerts:
                pause = " [建议暂停交易]" if a.should_pause else ""
                lines.append(f"    [{a.level}] {a.check_name}: {a.message}{pause}")
        else:
            lines.append("  无告警，策略运行正常")
        return "\n".join(lines)

    def to_feishu_text(self) -> str:
        """生成飞书推送文本。"""
        level_emoji = {'OK': '✅', 'WARN': '⚠️', 'CRITICAL': '🚨'}
        emoji = level_emoji.get(self.worst_level(), '❓')
        parts = [
            f"{emoji}【策略健康告警】{self.check_date}",
        ]
        for a in self.alerts:
            parts.append(f"  [{a.level}] {a.check_name}")
            parts.append(f"  {a.message}")
            if a.should_pause:
                parts.append("  ⚠️ 建议暂停自动交易！")
        parts.append(f"  Sharpe(20d)={self.rolling_sharpe_20d:.3f} "
                     f"变化={self.sharpe_change_pct:+.1f}%")
        parts.append(f"  最新日收益={self.latest_daily_return*100:+.2f}%")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# StrategyHealthMonitor
# ---------------------------------------------------------------------------

class StrategyHealthMonitor:
    """
    策略健康度监控器。

    Parameters
    ----------
    sharpe_window_short : int
        短期 Sharpe 窗口（默认 20 天）
    sharpe_window_long : int
        长期 Sharpe 窗口（默认 60 天，作为基准）
    sharpe_drop_threshold : float
        短期 Sharpe 相对长期下降超过此幅度触发 WARN（默认 0.30 = 30%）
    daily_loss_critical : float
        单日亏损超过此比例触发 CRITICAL（默认 0.02 = 2%）
    daily_loss_warn : float
        单日亏损超过此比例触发 WARN（默认 0.015 = 1.5%）
    consecutive_loss_warn : int
        连续亏损天数超过此值触发 WARN（默认 5）
    turnover_spike_factor : float
        日均交易次数超过历史均值此倍数触发 WARN（默认 2.0）
    """

    def __init__(
        self,
        sharpe_window_short: int = 20,
        sharpe_window_long: int = 60,
        sharpe_drop_threshold: float = 0.30,
        daily_loss_critical: float = 0.02,
        daily_loss_warn: float = 0.015,
        consecutive_loss_warn: int = 5,
        turnover_spike_factor: float = 2.0,
        risk_free_rate: float = 0.03,
    ) -> None:
        self.sharpe_window_short = sharpe_window_short
        self.sharpe_window_long = sharpe_window_long
        self.sharpe_drop_threshold = sharpe_drop_threshold
        self.daily_loss_critical = daily_loss_critical
        self.daily_loss_warn = daily_loss_warn
        self.consecutive_loss_warn = consecutive_loss_warn
        self.turnover_spike_factor = turnover_spike_factor
        self.risk_free_rate = risk_free_rate

    def check(self, daily_stats: list) -> HealthReport:
        """
        对 DailyStats 列表执行全部健康度检查。

        Parameters
        ----------
        daily_stats : List[DailyStats]
            BacktestEngine.run().daily_stats 或实盘日度统计记录
            每条需含 .date / .daily_return / .n_trades / .equity 属性（或字典）

        Returns
        -------
        HealthReport
        """
        if not daily_stats:
            return HealthReport(check_date=date.today())

        # 统一为字典格式
        rows = []
        for s in daily_stats:
            if isinstance(s, dict):
                rows.append(s)
            else:
                rows.append({
                    'date': getattr(s, 'date', date.today()),
                    'daily_return': getattr(s, 'daily_return', 0.0),
                    'n_trades': getattr(s, 'n_trades', 0),
                    'equity': getattr(s, 'equity', 0.0),
                })

        df = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
        df['daily_return'] = pd.to_numeric(df['daily_return'], errors='coerce').fillna(0.0)
        df['n_trades'] = pd.to_numeric(df['n_trades'], errors='coerce').fillna(0).astype(int)

        alerts: List[HealthAlert] = []
        check_date = df['date'].iloc[-1] if len(df) > 0 else date.today()
        if hasattr(check_date, 'date'):
            check_date = check_date.date()

        # ── 计算 Rolling Sharpe ──────────────────────────────
        rets = df['daily_return'].values
        rf_daily = self.risk_free_rate / 252

        sharpe_short = self._rolling_sharpe(rets, self.sharpe_window_short, rf_daily)
        sharpe_long = self._rolling_sharpe(rets, self.sharpe_window_long, rf_daily)

        sharpe_change_pct = 0.0
        if abs(sharpe_long) > 1e-6:
            sharpe_change_pct = (sharpe_short - sharpe_long) / abs(sharpe_long) * 100

        if (sharpe_long > 0.1 and
                sharpe_short < sharpe_long * (1 - self.sharpe_drop_threshold)):
            drop = (sharpe_long - sharpe_short) / abs(sharpe_long)
            alerts.append(HealthAlert(
                level='WARN',
                check_name='Rolling Sharpe 下降',
                message=(
                    f"Sharpe(20d)={sharpe_short:.3f} 相对 Sharpe(60d)={sharpe_long:.3f} "
                    f"下降 {drop*100:.1f}% (阈值 {self.sharpe_drop_threshold*100:.0f}%)"
                ),
                value=drop,
                threshold=self.sharpe_drop_threshold,
                should_pause=drop > 0.5,
            ))

        # ── 单日亏损检查 ─────────────────────────────────────
        latest_ret = float(rets[-1]) if len(rets) > 0 else 0.0
        if latest_ret < -self.daily_loss_critical:
            alerts.append(HealthAlert(
                level='CRITICAL',
                check_name='单日大额亏损',
                message=(
                    f"今日亏损 {latest_ret*100:.2f}% 超过 CRITICAL 阈值 "
                    f"{self.daily_loss_critical*100:.1f}%"
                ),
                value=abs(latest_ret),
                threshold=self.daily_loss_critical,
                should_pause=True,
            ))
        elif latest_ret < -self.daily_loss_warn:
            alerts.append(HealthAlert(
                level='WARN',
                check_name='单日亏损偏高',
                message=(
                    f"今日亏损 {latest_ret*100:.2f}% 超过 WARN 阈值 "
                    f"{self.daily_loss_warn*100:.1f}%"
                ),
                value=abs(latest_ret),
                threshold=self.daily_loss_warn,
                should_pause=False,
            ))

        # ── 连续亏损天数 ─────────────────────────────────────
        consec = self._consecutive_losses(rets)
        if consec >= self.consecutive_loss_warn:
            alerts.append(HealthAlert(
                level='WARN',
                check_name='连续亏损',
                message=f"已连续亏损 {consec} 天（阈值 {self.consecutive_loss_warn}）",
                value=float(consec),
                threshold=float(self.consecutive_loss_warn),
                should_pause=consec >= self.consecutive_loss_warn * 2,
            ))

        # ── 换手率异常 ────────────────────────────────────────
        n_trades = df['n_trades'].values
        avg_hist = float(np.mean(n_trades[:-self.sharpe_window_short])) if len(n_trades) > self.sharpe_window_short else 0.0
        avg_recent = float(np.mean(n_trades[-self.sharpe_window_short:])) if len(n_trades) >= self.sharpe_window_short else float(np.mean(n_trades))
        if avg_hist > 0.1 and avg_recent > avg_hist * self.turnover_spike_factor:
            alerts.append(HealthAlert(
                level='WARN',
                check_name='换手率异常飙升',
                message=(
                    f"近20日均交易次数 {avg_recent:.1f} 是历史均值 {avg_hist:.1f} 的 "
                    f"{avg_recent/avg_hist:.1f} 倍（阈值 {self.turnover_spike_factor}x）"
                ),
                value=avg_recent / max(avg_hist, 1e-6),
                threshold=self.turnover_spike_factor,
                should_pause=False,
            ))

        # ── 近20日胜率 ────────────────────────────────────────
        recent_rets = rets[-self.sharpe_window_short:] if len(rets) >= self.sharpe_window_short else rets
        win_rate_20d = float(np.mean(recent_rets > 0)) if len(recent_rets) > 0 else 0.0

        return HealthReport(
            check_date=check_date,
            alerts=alerts,
            rolling_sharpe_20d=round(sharpe_short, 4),
            rolling_sharpe_60d=round(sharpe_long, 4),
            sharpe_change_pct=round(sharpe_change_pct, 2),
            latest_daily_return=round(latest_ret, 6),
            consecutive_loss_days=consec,
            win_rate_20d=round(win_rate_20d, 4),
            avg_trades_20d=round(avg_recent, 2),
            avg_trades_hist=round(avg_hist, 2),
        )

    def check_series(
        self,
        daily_stats: list,
        window: int = 20,
    ) -> pd.DataFrame:
        """
        滚动检查，返回每天的 Sharpe / 胜率 / 告警状态时间序列 DataFrame。
        用于 Streamlit 折线图可视化。

        Returns
        -------
        pd.DataFrame, 列: date / sharpe_20d / sharpe_60d / win_rate / n_alerts / level
        """
        if not daily_stats:
            return pd.DataFrame()

        rows_all = []
        for s in daily_stats:
            if isinstance(s, dict):
                rows_all.append(s)
            else:
                rows_all.append({
                    'date': getattr(s, 'date', date.today()),
                    'daily_return': getattr(s, 'daily_return', 0.0),
                    'n_trades': getattr(s, 'n_trades', 0),
                    'equity': getattr(s, 'equity', 0.0),
                })

        df = pd.DataFrame(rows_all).sort_values('date').reset_index(drop=True)
        df['daily_return'] = pd.to_numeric(df['daily_return'], errors='coerce').fillna(0.0)
        rets = df['daily_return'].values
        rf_daily = self.risk_free_rate / 252

        series_rows = []
        for i in range(len(df)):
            partial_stats = daily_stats[:i + 1]
            if i < self.sharpe_window_short:
                s20 = float('nan')
            else:
                s20 = self._rolling_sharpe(rets[:i + 1], self.sharpe_window_short, rf_daily)

            if i < self.sharpe_window_long:
                s60 = float('nan')
            else:
                s60 = self._rolling_sharpe(rets[:i + 1], self.sharpe_window_long, rf_daily)

            recent_w = rets[max(0, i - window + 1):i + 1]
            wr = float(np.mean(recent_w > 0)) if len(recent_w) > 0 else float('nan')
            consec = self._consecutive_losses(rets[:i + 1])

            report = self.check(partial_stats)
            level = report.worst_level()

            series_rows.append({
                'date': df['date'].iloc[i],
                'sharpe_20d': round(s20, 4) if not np.isnan(s20) else None,
                'sharpe_60d': round(s60, 4) if not np.isnan(s60) else None,
                'win_rate': round(wr, 4) if not np.isnan(wr) else None,
                'consecutive_losses': consec,
                'n_alerts': len(report.alerts),
                'level': level,
            })

        return pd.DataFrame(series_rows)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rolling_sharpe(rets: np.ndarray, window: int, rf_daily: float) -> float:
        if len(rets) < window:
            if len(rets) < 5:
                return 0.0
            window = len(rets)
        tail = rets[-window:]
        excess = tail - rf_daily
        std = float(np.std(excess))
        if std < 1e-10:
            return 0.0
        return float(np.mean(excess) / std * np.sqrt(252))

    @staticmethod
    def _consecutive_losses(rets: np.ndarray) -> int:
        """计算截止最新的连续亏损天数。"""
        count = 0
        for r in reversed(rets):
            if r < 0:
                count += 1
            else:
                break
        return count
