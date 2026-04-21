"""
portfolio_risk.py — 组合层风控

在现有 RiskEngine（单标的 PreTrade/InTrade/PostTrade）之上，
增加组合层面的风险检查：

  1. VaR 检查       — 历史模拟法，持仓组合在 95% 置信度的单日最大亏损
  2. 行业集中度     — 单一行业持仓 ≤ 30%
  3. 持仓相关性     — 高相关持仓组合的附加保护
  4. 最大回撤       — 组合净值从峰值回撤超限则停止新开仓

所有检查返回 RiskResult，passed=False 时应拒绝新订单。

设计原则：
  - PortfolioRiskChecker 无状态，可重复调用
  - 所有计算基于传入的 returns DataFrame（不含 API 调用）
  - 可作为 StrategyRunner.on_signal 钩子，也可直接调用
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.risk_engine import RiskResult


# ---------------------------------------------------------------------------
# PortfolioSnapshot — 持仓快照（纯数据，无 API 依赖）
# ---------------------------------------------------------------------------

@dataclass
class PortfolioSnapshot:
    """
    调用方需要提供的持仓快照。

    Parameters
    ----------
    positions:
        {symbol: market_value}，市值（元）
    equity:
        组合总权益（元）
    peak_equity:
        历史峰值权益（元），用于最大回撤计算
    sector_map:
        {symbol: sector_name}，行业映射（可选）
    returns:
        {symbol: pd.Series}，过去 N 日的每日收益率序列（可选，用于 VaR）
    """
    positions: Dict[str, float]
    equity: float
    peak_equity: float
    sector_map: Dict[str, str] = field(default_factory=dict)
    returns: Dict[str, pd.Series] = field(default_factory=dict)

    @property
    def position_weights(self) -> Dict[str, float]:
        """各标的的权重（占总权益比例）。"""
        if self.equity <= 0:
            return {sym: 0.0 for sym in self.positions}
        return {sym: mv / self.equity for sym, mv in self.positions.items()}

    @property
    def total_invested(self) -> float:
        return sum(self.positions.values())

    @property
    def exposure(self) -> float:
        """总净暴露比例。"""
        return self.total_invested / self.equity if self.equity > 0 else 0.0

    @property
    def drawdown(self) -> float:
        """从峰值的回撤比例（0~1）。"""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)


# ---------------------------------------------------------------------------
# PortfolioRiskChecker
# ---------------------------------------------------------------------------

class PortfolioRiskChecker:
    """
    组合层风控检查器。

    Parameters
    ----------
    var_confidence:
        VaR 置信度（默认 0.95）
    var_limit:
        单日 VaR 上限（占权益比例，默认 0.03 = 3%）
    max_sector_weight:
        单行业最大权重（默认 0.30 = 30%）
    max_drawdown:
        最大回撤限制（默认 0.15 = 15%），超过后停止新开仓
    max_correlation:
        高相关标的对（r > max_correlation）触发警告（默认 0.85）
    min_returns_days:
        VaR 计算所需最少收益率天数（默认 30）
    """

    def __init__(
        self,
        var_confidence: float = 0.95,
        var_limit: float = 0.03,
        max_sector_weight: float = 0.30,
        max_drawdown: float = 0.15,
        max_correlation: float = 0.85,
        min_returns_days: int = 30,
    ) -> None:
        self.var_confidence = var_confidence
        self.var_limit = var_limit
        self.max_sector_weight = max_sector_weight
        self.max_drawdown = max_drawdown
        self.max_correlation = max_correlation
        self.min_returns_days = min_returns_days

    # ------------------------------------------------------------------
    # Public: run all checks
    # ------------------------------------------------------------------

    def check_all(self, snapshot: PortfolioSnapshot) -> List[RiskResult]:
        """
        运行所有组合层风控检查。

        Returns
        -------
        触发的 RiskResult 列表（可能为空）。
        调用方应关注 passed=False 的项。
        """
        results: List[RiskResult] = []

        r = self.check_drawdown(snapshot)
        if r.level != 'OK':
            results.append(r)

        r = self.check_sector_concentration(snapshot)
        if r.level != 'OK':
            results.append(r)

        r = self.check_var(snapshot)
        if r.level != 'OK':
            results.append(r)

        r = self.check_correlation(snapshot)
        if r.level != 'OK':
            results.append(r)

        return results

    def check_before_buy(self, snapshot: PortfolioSnapshot) -> RiskResult:
        """
        买入前综合检查（只返回第一个 REJECT）。
        WARN 级别不阻断，REJECT/CRITICAL 阻断。
        """
        for result in self.check_all(snapshot):
            if not result.passed:
                return result
        return RiskResult.ok()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_drawdown(self, snapshot: PortfolioSnapshot) -> RiskResult:
        """最大回撤检查。超过 max_drawdown → REJECT 新开仓。"""
        dd = snapshot.drawdown
        if dd >= self.max_drawdown:
            return RiskResult.reject(
                f'Portfolio drawdown {dd*100:.1f}% >= limit {self.max_drawdown*100:.0f}%',
                drawdown=dd,
                limit=self.max_drawdown,
                equity=snapshot.equity,
                peak_equity=snapshot.peak_equity,
            )
        if dd >= self.max_drawdown * 0.75:
            return RiskResult.warn(
                f'Portfolio drawdown {dd*100:.1f}% approaching limit',
                drawdown=dd,
            )
        return RiskResult.ok()

    def check_sector_concentration(self, snapshot: PortfolioSnapshot) -> RiskResult:
        """行业集中度检查。单一行业持仓 > max_sector_weight → WARN。"""
        if not snapshot.sector_map or not snapshot.positions:
            return RiskResult.ok()

        weights = snapshot.position_weights
        sector_weights: Dict[str, float] = {}
        for sym, w in weights.items():
            sector = snapshot.sector_map.get(sym, 'Unknown')
            sector_weights[sector] = sector_weights.get(sector, 0.0) + w

        violations = {
            s: w for s, w in sector_weights.items()
            if w > self.max_sector_weight
        }
        if violations:
            worst_sector = max(violations, key=violations.get)
            worst_w = violations[worst_sector]
            return RiskResult.warn(
                f'Sector "{worst_sector}" weight {worst_w*100:.1f}% > {self.max_sector_weight*100:.0f}%',
                sector_weights=sector_weights,
                limit=self.max_sector_weight,
            )
        return RiskResult.ok()

    def check_var(self, snapshot: PortfolioSnapshot) -> RiskResult:
        """
        历史模拟法 VaR。

        使用各标的权重 × 收益率序列计算组合日收益，
        取 (1-confidence) 分位数作为 VaR。
        """
        if not snapshot.returns or not snapshot.positions:
            return RiskResult.ok()

        weights = snapshot.position_weights
        # 只考虑有收益率数据的标的
        valid = {
            sym: ret for sym, ret in snapshot.returns.items()
            if sym in weights and len(ret) >= self.min_returns_days
        }
        if not valid:
            return RiskResult.ok()

        # 对齐索引
        combined = pd.DataFrame(valid)
        combined = combined.dropna()
        if len(combined) < self.min_returns_days:
            return RiskResult.ok()

        # 计算组合日收益率
        w_vec = np.array([weights.get(sym, 0.0) for sym in combined.columns])
        total_w = w_vec.sum()
        if total_w <= 0:
            return RiskResult.ok()
        w_vec = w_vec / total_w  # 归一化（只对有数据的标的）

        portfolio_returns = combined.values @ w_vec
        var_pct = float(np.percentile(portfolio_returns, (1 - self.var_confidence) * 100))
        var_abs = abs(min(var_pct, 0.0))  # VaR 用正数表示亏损幅度

        if var_abs >= self.var_limit:
            return RiskResult.reject(
                f'Portfolio VaR({self.var_confidence*100:.0f}%) = {var_abs*100:.2f}% >= limit {self.var_limit*100:.1f}%',
                var_pct=var_abs,
                limit=self.var_limit,
                confidence=self.var_confidence,
            )
        if var_abs >= self.var_limit * 0.75:
            return RiskResult.warn(
                f'Portfolio VaR({self.var_confidence*100:.0f}%) = {var_abs*100:.2f}% approaching limit',
                var_pct=var_abs,
            )
        return RiskResult.ok()

    def check_correlation(self, snapshot: PortfolioSnapshot) -> RiskResult:
        """
        高相关性检查。

        任意两个持仓标的收益率相关系数 > max_correlation → WARN。
        """
        if not snapshot.returns or len(snapshot.positions) < 2:
            return RiskResult.ok()

        syms = [s for s in snapshot.positions if s in snapshot.returns]
        if len(syms) < 2:
            return RiskResult.ok()

        combined = pd.DataFrame({s: snapshot.returns[s] for s in syms}).dropna()
        if len(combined) < 2:
            return RiskResult.ok()

        corr = combined.corr()
        high_corr_pairs: List[Tuple[str, str, float]] = []

        for i, a in enumerate(syms):
            for b in syms[i + 1:]:
                if a not in corr.columns or b not in corr.columns:
                    continue
                r = float(corr.loc[a, b])
                if r > self.max_correlation:
                    high_corr_pairs.append((a, b, round(r, 4)))

        if high_corr_pairs:
            pairs_str = ', '.join(f'{a}/{b}({r})' for a, b, r in high_corr_pairs[:3])
            return RiskResult.warn(
                f'High correlation pairs: {pairs_str}',
                pairs=high_corr_pairs,
                threshold=self.max_correlation,
            )
        return RiskResult.ok()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def from_price_series(
        prices: Dict[str, pd.Series],
    ) -> Dict[str, pd.Series]:
        """将价格序列转换为日收益率序列（方便外部调用）。"""
        return {sym: ps.pct_change().dropna() for sym, ps in prices.items()}
