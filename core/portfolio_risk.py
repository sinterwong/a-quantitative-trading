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

    def check_cvar(
        self,
        snapshot: 'PortfolioSnapshot',
        confidence: float = 0.95,
        cvar_limit: float = 0.05,
    ) -> 'RiskResult':
        """
        CVaR / Expected Shortfall 检查。

        CVaR(α) = 损失超过 VaR(α) 时的期望损失，比 VaR 更保守。
        用历史模拟法计算组合日收益在最差 (1-α) 部分的均值。

        Parameters
        ----------
        snapshot : PortfolioSnapshot
        confidence : float
            置信水平（默认 0.95）
        cvar_limit : float
            CVaR 上限（占权益比例，默认 0.05 = 5%）
        """
        if not snapshot.returns or not snapshot.positions:
            return RiskResult.ok()

        weights = snapshot.position_weights
        valid = {
            sym: ret for sym, ret in snapshot.returns.items()
            if sym in weights and len(ret) >= self.min_returns_days
        }
        if not valid:
            return RiskResult.ok()

        combined = pd.DataFrame(valid).dropna()
        if len(combined) < self.min_returns_days:
            return RiskResult.ok()

        w_vec = np.array([weights.get(sym, 0.0) for sym in combined.columns])
        total_w = w_vec.sum()
        if total_w <= 0:
            return RiskResult.ok()
        w_vec = w_vec / total_w

        portfolio_returns = combined.values @ w_vec
        var_threshold = np.percentile(portfolio_returns, (1 - confidence) * 100)
        tail_losses = portfolio_returns[portfolio_returns <= var_threshold]

        if len(tail_losses) == 0:
            return RiskResult.ok()

        cvar = float(abs(np.mean(tail_losses)))

        if cvar >= cvar_limit:
            return RiskResult.reject(
                f'CVaR({confidence*100:.0f}%) = {cvar*100:.2f}% >= limit {cvar_limit*100:.1f}%',
                cvar_pct=cvar,
                limit=cvar_limit,
                confidence=confidence,
            )
        if cvar >= cvar_limit * 0.80:
            return RiskResult.warn(
                f'CVaR({confidence*100:.0f}%) = {cvar*100:.2f}% approaching limit',
                cvar_pct=cvar,
            )
        return RiskResult.ok()


# ---------------------------------------------------------------------------
# MonteCarloStressTest — 蒙特卡洛压力测试
# ---------------------------------------------------------------------------

@dataclass
class MonteCarloResult:
    """蒙特卡洛压力测试结果"""
    n_simulations: int
    horizon_days: int
    initial_equity: float
    # 分位数统计（基于模拟期末净值）
    p5_final: float          # 最差 5% 情境的期末净值
    p25_final: float
    p50_final: float
    p75_final: float
    p95_final: float
    # 风险指标
    prob_loss: float         # 亏损概率
    expected_shortfall: float  # CVaR(95%)：最差 5% 情境下的期望亏损
    max_drawdown_mean: float   # 平均最大回撤
    max_drawdown_p95: float    # 最差 5% 情境的最大回撤
    # 压力情境
    stress_scenarios: Dict[str, float]  # {'base': final_p50, 'bear': ...}

    def summary(self) -> str:
        lines = [
            "=" * 60,
            f"  蒙特卡洛压力测试（{self.n_simulations} 次，{self.horizon_days} 天）",
            "=" * 60,
            f"  初始净值:       ¥{self.initial_equity:,.0f}",
            f"  P5  (最差5%):   ¥{self.p5_final:,.0f}  ({(self.p5_final/self.initial_equity-1)*100:+.1f}%)",
            f"  P25:            ¥{self.p25_final:,.0f}  ({(self.p25_final/self.initial_equity-1)*100:+.1f}%)",
            f"  P50 (中位数):   ¥{self.p50_final:,.0f}  ({(self.p50_final/self.initial_equity-1)*100:+.1f}%)",
            f"  P75:            ¥{self.p75_final:,.0f}  ({(self.p75_final/self.initial_equity-1)*100:+.1f}%)",
            f"  P95 (最好5%):   ¥{self.p95_final:,.0f}  ({(self.p95_final/self.initial_equity-1)*100:+.1f}%)",
            f"  亏损概率:       {self.prob_loss*100:.1f}%",
            f"  ES(95%):        {self.expected_shortfall*100:.2f}%",
            f"  平均最大回撤:   {self.max_drawdown_mean*100:.1f}%",
            f"  P95 最大回撤:   {self.max_drawdown_p95*100:.1f}%",
            "=" * 60,
        ]
        return "\n".join(lines)


class MonteCarloStressTest:
    """
    基于历史收益率的蒙特卡洛压力测试。

    方法：参数法（正态）或历史重采样法。
    每次模拟生成一条 horizon_days 长的净值路径，
    统计期末净值分布、亏损概率、最大回撤等。

    用法：
        mc = MonteCarloStressTest(n_simulations=10000, horizon_days=252)
        result = mc.run(
            daily_returns=portfolio_returns_series,
            initial_equity=100_000,
        )
        print(result.summary())
        mc.plot(result, 'outputs/monte_carlo.png')
    """

    def __init__(
        self,
        n_simulations: int = 5000,
        horizon_days: int = 252,
        method: str = 'bootstrap',   # 'bootstrap' | 'parametric'
        seed: int = 42,
    ) -> None:
        self.n_simulations = n_simulations
        self.horizon_days = horizon_days
        self.method = method
        self.seed = seed

    def run(
        self,
        daily_returns: pd.Series,
        initial_equity: float = 100_000,
    ) -> MonteCarloResult:
        """
        执行蒙特卡洛模拟。

        Parameters
        ----------
        daily_returns : pd.Series
            历史每日收益率序列
        initial_equity : float
            初始净值
        """
        rng = np.random.default_rng(self.seed)
        rets = daily_returns.dropna().values

        if len(rets) < 20:
            raise ValueError(f"需要至少 20 条历史收益率，只有 {len(rets)}")

        # 生成模拟路径矩阵 [n_sim × horizon_days]
        if self.method == 'bootstrap':
            idx = rng.integers(0, len(rets), size=(self.n_simulations, self.horizon_days))
            sim_rets = rets[idx]
        else:  # parametric
            mu = np.mean(rets)
            sigma = np.std(rets)
            sim_rets = rng.normal(mu, sigma, (self.n_simulations, self.horizon_days))

        # 累积净值曲线
        cum_returns = np.cumprod(1 + sim_rets, axis=1)  # [n_sim × horizon]
        final_values = initial_equity * cum_returns[:, -1]

        # 最大回撤
        max_drawdowns = self._calc_max_drawdowns(cum_returns)

        # 统计
        p5, p25, p50, p75, p95 = np.percentile(final_values, [5, 25, 50, 75, 95])
        prob_loss = float(np.mean(final_values < initial_equity))

        # Expected Shortfall：最差 5% 情境的期望损失率
        cutoff = np.percentile(final_values, 5)
        tail = final_values[final_values <= cutoff]
        es = float(abs(np.mean(tail / initial_equity - 1))) if len(tail) > 0 else 0.0

        # 压力情境
        bear_sim_rets = sim_rets * 1.5   # 收益率放大 1.5 倍（熊市情境）
        bear_cum = np.cumprod(1 + bear_sim_rets, axis=1)
        bull_sim_rets = np.abs(sim_rets)   # 全部正收益（极端牛市）
        bull_cum = np.cumprod(1 + bull_sim_rets, axis=1)

        scenarios = {
            'base': round(float(np.median(final_values)), 2),
            'bear': round(float(initial_equity * np.median(bear_cum[:, -1])), 2),
            'crash': round(float(np.percentile(final_values, 1)), 2),
            'bull': round(float(initial_equity * np.median(bull_cum[:, -1])), 2),
        }

        return MonteCarloResult(
            n_simulations=self.n_simulations,
            horizon_days=self.horizon_days,
            initial_equity=initial_equity,
            p5_final=round(float(p5), 2),
            p25_final=round(float(p25), 2),
            p50_final=round(float(p50), 2),
            p75_final=round(float(p75), 2),
            p95_final=round(float(p95), 2),
            prob_loss=round(prob_loss, 4),
            expected_shortfall=round(es, 4),
            max_drawdown_mean=round(float(np.mean(max_drawdowns)), 4),
            max_drawdown_p95=round(float(np.percentile(max_drawdowns, 95)), 4),
            stress_scenarios=scenarios,
        )

    @staticmethod
    def _calc_max_drawdowns(cum_returns: np.ndarray) -> np.ndarray:
        """计算每条路径的最大回撤（向量化）。"""
        running_max = np.maximum.accumulate(cum_returns, axis=1)
        drawdowns = (cum_returns - running_max) / running_max
        return np.abs(drawdowns.min(axis=1))

    def plot(
        self,
        result: MonteCarloResult,
        output_path: str = '',
        n_paths: int = 200,
        daily_returns: Optional[pd.Series] = None,
    ) -> None:
        """绘制蒙特卡洛路径扇形图（分位数带）。"""
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError:
            print("[MonteCarloStressTest] matplotlib 未安装，跳过绘图")
            return

        if daily_returns is None:
            print("[MonteCarloStressTest] 需要传入 daily_returns 才能绘图")
            return

        rng = np.random.default_rng(self.seed)
        rets = daily_returns.dropna().values

        if self.method == 'bootstrap':
            idx = rng.integers(0, len(rets), size=(n_paths, self.horizon_days))
            sim_rets = rets[idx]
        else:
            mu, sigma = np.mean(rets), np.std(rets)
            sim_rets = rng.normal(mu, sigma, (n_paths, self.horizon_days))

        cum = result.initial_equity * np.cumprod(1 + sim_rets, axis=1)
        days = np.arange(1, self.horizon_days + 1)

        fig, ax = plt.subplots(figsize=(12, 6))

        # 绘制部分路径（半透明）
        for i in range(min(100, n_paths)):
            ax.plot(days, cum[i], color='steelblue', alpha=0.05, linewidth=0.5)

        # 分位数带
        all_rets_arr = rets[rng.integers(0, len(rets), size=(5000, self.horizon_days))]
        all_cum = result.initial_equity * np.cumprod(1 + all_rets_arr, axis=1)
        p5s = np.percentile(all_cum, 5, axis=0)
        p25s = np.percentile(all_cum, 25, axis=0)
        p50s = np.percentile(all_cum, 50, axis=0)
        p75s = np.percentile(all_cum, 75, axis=0)
        p95s = np.percentile(all_cum, 95, axis=0)

        ax.fill_between(days, p5s, p95s, alpha=0.15, color='steelblue', label='P5-P95')
        ax.fill_between(days, p25s, p75s, alpha=0.25, color='steelblue', label='P25-P75')
        ax.plot(days, p50s, color='navy', linewidth=2, label='中位数')
        ax.axhline(result.initial_equity, color='gray', linestyle='--', linewidth=1, label='初始净值')

        ax.set_xlabel('交易日')
        ax.set_ylabel('净值（元）')
        ax.set_title(
            f'蒙特卡洛压力测试（{result.n_simulations}次，{result.horizon_days}天）\n'
            f'亏损概率={result.prob_loss*100:.1f}%  ES(95%)={result.expected_shortfall*100:.2f}%  '
            f'平均最大回撤={result.max_drawdown_mean*100:.1f}%'
        )
        ax.legend(loc='upper left')
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'¥{x:,.0f}'))
        plt.tight_layout()

        if not output_path:
            output_path = os.path.join(
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'outputs'),
                'monte_carlo.png'
            )
        plt.savefig(output_path, dpi=120)
        plt.close(fig)
        print(f"[MonteCarloStressTest] 图表已保存: {output_path}")
