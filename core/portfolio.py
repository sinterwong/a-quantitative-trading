"""
core/portfolio.py — 组合优化器

Phase 5 组件：
  1. MeanVarianceOptimizer: Markowitz 均值方差组合优化
  2. BlackLittermanModel: BL 预期收益（均衡收益 + 主观观点）
  3. RiskParityOptimizer: 风险平价组合
  4. SignalWeighter: 多因子信号 → 组合权重

数学基础：
  - Markowitz: max Sharpe = max (w^T μ - r_f) / sqrt(w^T Σ w)
  - Black-Litterman: π = δ Σ w_mkt; μ_BL = [(τΣ)^-1 + P^T Ω^-1 P]^-1 [(τΣ)^-1 π + P^T Ω^-1 q]
  - Risk Parity: w_i = σ_portfolio / (N × σ_i)，各资产对组合风险贡献相等
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class PortfolioResult:
    """组合优化结果"""
    weights: Dict[str, float]     # {symbol: weight}
    expected_return: float         # 组合预期收益（年化）
    expected_vol: float           # 组合波动率（年化）
    sharpe: float                # 夏普比率
    method: str                  # 优化方法
    status: str                 # 'optimal' | 'optimal_near' | 'infeasible'

    def validate(self, total: float) -> bool:
        return abs(total - 1.0) < 1e-6


@dataclass
class AssetReturn:
    """资产收益统计"""
    symbol: str
    expected_return: float   # 年化预期收益（BL估算）
    volatility: float         # 年化波动率
    correlations: Dict[str, float]  # 与其他资产的相关性


# ─── Black-Litterman ─────────────────────────────────────────────────────────

class BlackLittermanModel:
    """
    Black-Litterman 预期收益模型。

    BL 核心思想：
      - 均衡收益 π = δ × Σ × w_mkt（市场均衡下各资产收益率）
      - 主观观点 Q = P × μ + ε，ε ~ N(0, Ω)
      - 合并：μ_BL = [(τΣ)^-1 + P^T Ω^-1 P]^-1 × [(τΣ)^-1 π + P^T Ω^-1 Q]

    参数：
      delta: 风险厌恶系数（通常 2.0~3.0）
      tau: 观点不确定性（通常 1/N，N=资产数）
      market_cap_weights: 市场权重（dict）
    """

    def __init__(
        self,
        delta: float = 2.5,
        tau: float = 0.1,
        risk_free_rate: float = 0.03,
    ):
        self.delta = delta          # 风险厌恶系数
        self.tau = tau              # 观点不确定性缩放
        self.rf = risk_free_rate   # 无风险利率

    def compute_equilibrium_returns(
        self,
        cov_matrix: np.ndarray,
        market_cap_weights: Dict[str, float],
        symbols: List[str],
    ) -> np.ndarray:
        """
        计算均衡收益率 π = δ × Σ × w_mkt

        π: n×1 向量
        Σ: n×n 协方差矩阵
        w_mkt: n×1 市场权重向量
        """
        w = np.array([market_cap_weights.get(s, 1.0/len(symbols)) for s in symbols])
        w = w / w.sum()  # 归一化
        pi = self.delta * cov_matrix @ w
        return pi

    def compute_posterior_returns(
        self,
        pi: np.ndarray,
        cov_matrix: np.ndarray,
        P: np.ndarray,      # K×n 观点矩阵
        q: np.ndarray,      # K×1 观点向量
        Omega: Optional[np.ndarray] = None,  # K×K 观点噪声矩阵
    ) -> np.ndarray:
        """
        BL 合并：
        μ_BL = [(τΣ)^-1 + P^T Ω^-1 P]^-1 × [(τΣ)^-1 π + P^T Ω^-1 q]

        P 格式（示例，K=2个观点，n=3个资产）：
          观点1: 腾讯涨幅 > 阿里  → P = [[1, -1, 0]]
          观点2: 港交所看涨 3%   → P = [[0, 0, 1]]
        """
        n = len(pi)
        tau_Sigma = self.tau * cov_matrix
        tau_Sigma_inv = np.linalg.inv(tau_Sigma + 1e-8 * np.eye(n))

        # Omega: 观点噪声协方差（对角线 = 意见方差）
        if Omega is None:
            k = len(q)
            Omega = np.eye(k) * 0.01  # 默认 1% 方差

        # MLE 合并公式
        M1 = tau_Sigma_inv + P.T @ np.linalg.inv(Omega) @ P
        M1_inv = np.linalg.inv(M1 + 1e-8 * np.eye(n))

        M2 = tau_Sigma_inv @ pi + P.T @ np.linalg.inv(Omega) @ q
        mu_bl = M1_inv @ M2

        return mu_bl

    def fit(
        self,
        cov_matrix: np.ndarray,
        market_cap_weights: Dict[str, float],
        symbols: List[str],
        views: Optional[Dict[Tuple[str, str], float]] = None,
        view_confidence: float = 0.01,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        完整 BL 流程。

        views: 主观观点 dict
          {('HK:00700', None): 0.05}     → 腾讯预期涨 5%
          {('HK:00700', 'HK:09988'): 0.03} → 腾讯比阿里多涨 3%
          {('HK:01810', None): -0.02}    → 小米看跌 2%

        view_confidence: 观点方差（越小 = 越自信）

        返回: (mu_bl, pi)
          mu_bl: n×1 BL 预期收益向量
          pi:    n×1 均衡收益向量
        """
        symbols = list(symbols)
        n = len(symbols)

        # 1. 均衡收益
        pi = self.compute_equilibrium_returns(cov_matrix, market_cap_weights, symbols)

        if not views:
            return pi, pi

        # 2. 构建 P, q, Omega
        k = len(views)
        P = np.zeros((k, n))
        q = np.zeros(k)
        Omega = np.eye(k) * view_confidence

        for i, (view_key, view_return) in enumerate(views.items()):
            asset1, asset2 = view_key
            if asset2 is None:
                # 绝对观点：资产1 预期收益 = view_return
                idx1 = symbols.index(asset1)
                P[i, idx1] = 1.0
                q[i] = view_return + self.rf  # 转绝对收益
            else:
                # 相对观点：资产1 - 资产2 预期收益差 = view_return
                idx1 = symbols.index(asset1)
                idx2 = symbols.index(asset2)
                P[i, idx1] = 1.0
                P[i, idx2] = -1.0
                q[i] = view_return  # 相对收益

        # 3. 合并
        mu_bl = self.compute_posterior_returns(pi, cov_matrix, P, q, Omega)
        return mu_bl, pi


# ─── Mean Variance Optimizer ─────────────────────────────────────────────────

class MeanVarianceOptimizer:
    """
    Markowitz 均值方差组合优化器。

    目标函数：max Sharpe = max (w^T μ - r_f) / sqrt(w^T Σ w)
    约束：
      - Σ w_i = 1（权重归一）
      - w_i >= 0（做多约束）
      - 可选：w_i <= max_weight（单资产上限）
      - 可选：行业中性

    使用解析解（无 scipy.optimize 依赖）：
      最优夏普组合在给定无卖空约束下有解析近似解。
    """

    def __init__(
        self,
        method: str = 'max_sharpe',
        max_weight: float = 0.30,
        allow_short: bool = False,
        risk_aversion: float = 2.5,
    ):
        """
        method: 'max_sharpe' | 'min_volatility' | 'risk_parity'
        max_weight: 单资产最大权重
        allow_short: 是否允许卖空
        risk_aversion: 风险厌恶系数（仅 max_sharpe 时生效）
        """
        self.method = method
        self.max_weight = max_weight
        self.allow_short = allow_short
        self.delta = risk_aversion

    def optimize(
        self,
        expected_returns: np.ndarray,     # n×1
        cov_matrix: np.ndarray,           # n×n
        symbols: Optional[List[str]] = None,
    ) -> PortfolioResult:
        """
        组合优化主入口。

        返回: PortfolioResult
        """
        n = expected_returns.size
        symbols = symbols or [f'asset_{i}' for i in range(n)]

        if self.method == 'max_sharpe':
            return self._max_sharpe(expected_returns, cov_matrix, symbols)
        elif self.method == 'min_volatility':
            return self._min_volatility(cov_matrix, symbols)
        elif self.method == 'equal_weight':
            return self._equal_weight(symbols)
        else:
            raise ValueError(f'Unknown method: {self.method}')

    def _max_sharpe(
        self,
        mu: np.ndarray,
        Sigma: np.ndarray,
        symbols: List[str],
    ) -> PortfolioResult:
        """
        最大夏普组合（解析近似解）。
        解析解：w* = (Σ^-1 × (μ - r_f × 1)) / (1^T × Σ^-1 × (μ - r_f × 1))
        """
        n = len(mu)
        rf_array = np.full(n, self.delta * 0.0)  # rf 已含在 mu 中

        # 中心化收益
        delta_mu = mu - rf_array

        # Σ^-1 × Δμ
        try:
            Sigma_inv = np.linalg.inv(Sigma + 1e-8 * np.eye(n))
        except np.linalg.LinAlgError:
            # Σ 奇异 → 用伪逆
            Sigma_inv = np.linalg.pinv(Sigma)

        w_unnorm = Sigma_inv @ delta_mu
        w_raw = w_unnorm / w_unnorm.sum() if w_unnorm.sum() != 0 else w_unnorm

        # 裁剪到 [0, max_weight]
        if not self.allow_short:
            w_raw = np.maximum(w_raw, 0)
        w_raw = np.clip(w_raw, 0, self.max_weight)

        # 归一化到权重和 = 1
        if w_raw.sum() > 0:
            w = w_raw / w_raw.sum()
        else:
            w = np.ones(n) / n  # fallback 等权

        return self._make_result(w, mu, Sigma, symbols, 'max_sharpe')

    def _min_volatility(
        self,
        Sigma: np.ndarray,
        symbols: List[str],
    ) -> PortfolioResult:
        """
        最小方差组合（解析解）。
        w* = Σ^-1 × 1 / (1^T × Σ^-1 × 1)
        """
        n = Sigma.shape[0]
        ones = np.ones(n)
        try:
            Sigma_inv = np.linalg.inv(Sigma + 1e-8 * np.eye(n))
        except np.linalg.LinAlgError:
            Sigma_inv = np.linalg.pinv(Sigma)

        w_unnorm = Sigma_inv @ ones
        w_raw = w_unnorm / w_unnorm.sum()

        if not self.allow_short:
            w_raw = np.maximum(w_raw, 0)
        w_raw = np.clip(w_raw, 0, self.max_weight)
        if w_raw.sum() > 0:
            w = w_raw / w_raw.sum()
        else:
            w = np.ones(n) / n

        # 预期收益用均衡（等权均值）
        mu_eq = np.zeros(n)
        return self._make_result(w, mu_eq, Sigma, symbols, 'min_volatility')

    def _equal_weight(self, symbols: List[str]) -> PortfolioResult:
        n = len(symbols)
        w = np.ones(n) / n
        return self._make_result(
            w,
            expected_returns=np.zeros(n),
            cov_matrix=np.eye(n),
            symbols=symbols,
            method='equal_weight'
        )

    def _make_result(
        self,
        w: np.ndarray,
        expected_returns: np.ndarray,
        cov_matrix: np.ndarray,
        symbols: List[str],
        method: Optional[str] = None,
    ) -> PortfolioResult:
        weights = {s: float(wi) for s, wi in zip(symbols, w)}

        # 组合预期收益
        port_return = float(w @ expected_returns)

        # 组合波动率
        port_vol = float(np.sqrt(w @ cov_matrix @ w))

        # 夏普比率（年化）
        rf_annual = 0.03
        sharpe = (port_return - rf_annual) / port_vol if port_vol > 0 else 0

        return PortfolioResult(
            weights=weights,
            expected_return=port_return,
            expected_vol=port_vol,
            sharpe=float(sharpe),
            method=method or self.method,
            status='optimal',
        )


# ─── Risk Parity ─────────────────────────────────────────────────────────────

class RiskParityOptimizer:
    """
    风险平价组合。
    各资产对组合总风险的贡献相等。

    风险贡献：RC_i = w_i × (Σw)_i / σ_portfolio
    目标：RC_i = RC_j  ∀i,j

    迭代算法：Newton 法求解
    """

    def __init__(self, max_iter: int = 100, tol: float = 1e-6):
        self.max_iter = max_iter
        self.tol = tol

    def optimize(
        self,
        cov_matrix: np.ndarray,
        symbols: Optional[List[str]] = None,
    ) -> PortfolioResult:
        n = cov_matrix.shape[0]
        symbols = symbols or [f'asset_{i}' for i in range(n)]

        # 初始等权
        w = np.ones(n) / n

        for _ in range(self.max_iter):
            port_vol = np.sqrt(w @ cov_matrix @ w)
            if port_vol < 1e-10:
                break

            # 各资产风险贡献
            marginal_contrib = cov_matrix @ w
            rc = w * marginal_contrib / port_vol  # n×1

            # 目标：所有 RC 相等
            target_rc = rc.mean()
            gradient = rc - target_rc  # 接近0时收敛

            if np.linalg.norm(gradient) < self.tol:
                break

            # 梯度更新（简化版）
            step = gradient / (w + 1e-8)
            w = w - 0.5 * step
            w = np.maximum(w, 0.001)  # 不允许接近0
            w = w / w.sum()

        return self._make_result(w, cov_matrix, symbols)

    def _make_result(
        self,
        w: np.ndarray,
        cov_matrix: np.ndarray,
        symbols: List[str],
    ) -> PortfolioResult:
        weights = {s: float(wi) for s, wi in zip(symbols, w)}
        port_vol = float(np.sqrt(w @ cov_matrix @ w))
        port_return = 0.0  # 风险平价不关心收益
        sharpe = 0.0

        return PortfolioResult(
            weights=weights,
            expected_return=port_return,
            expected_vol=port_vol,
            sharpe=sharpe,
            method='risk_parity',
            status='optimal',
        )


# ─── Signal → Weight 桥接 ─────────────────────────────────────────────────

class SignalWeighter:
    """
    将因子信号（多空方向 + 强度）映射为组合权重。

    用法：
      weighter = SignalWeighter()
      weights = weighter.weight_from_signals(
          signals={
              'HK:00700': Signal(direction='BUY', strength=0.8, factor_name='RSI'),
              'HK:01810': Signal(direction='SELL', strength=0.6, factor_name='MACD'),
          },
          method='strength_weighted',   # 或 'rank_equal', 'blend'
      )
    """

    def __init__(self, long_bias: float = 1.0):
        """
        long_bias: 多空暴露比率（>1 = 偏多头，<1 = 偏空头）
        """
        self.long_bias = long_bias

    def weight_from_signals(
        self,
        signals: Dict[str, 'Signal'],
        method: str = 'strength_weighted',
        market_cap_weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """
        将信号 dict 转换为权重 dict。

        signals: {symbol: Signal}
        method:
          - 'strength_weighted': 按信号强度分配权重（归一化）
          - 'rank_equal': 等权分配（按 rank）
          - 'blend': 信号强度 × 市场权重
        """
        if not signals:
            return {}

        if method == 'strength_weighted':
            return self._strength_weighted(signals)
        elif method == 'rank_equal':
            return self._rank_equal(signals)
        elif method == 'blend':
            return self._blend(signals, market_cap_weights or {})
        else:
            raise ValueError(f'Unknown method: {method}')

    def _strength_weighted(self, signals: Dict) -> Dict[str, float]:
        """按信号强度加权（做多强度 / 做空强度 分别归一）"""
        long_signals = [(s, sig.strength) for s, sig in signals.items() if sig.direction == 'BUY']
        short_signals = [(s, sig.strength) for s, sig in signals.items() if sig.direction == 'SELL']

        weights = {}

        if long_signals:
            total_long = sum(strength for _, strength in long_signals)
            for s, strength in long_signals:
                weights[s] = strength / total_long * (self.long_bias / (1 + self.long_bias))

        if short_signals:
            total_short = sum(strength for _, strength in short_signals)
            for s, strength in short_signals:
                weights[s] = -strength / total_short * (1 / (1 + self.long_bias))

        # 归一化（使多头 + |空头| = 1）
        total_abs = sum(abs(v) for v in weights.values())
        if total_abs > 0:
            weights = {k: v / total_abs for k, v in weights.items()}

        return weights

    def _rank_equal(self, signals: Dict) -> Dict[str, float]:
        """按 rank 等权（多头/空头分开归一）"""
        longs = sorted(
            [(s, sig.strength) for s, sig in signals.items() if sig.direction == 'BUY'],
            key=lambda x: x[1], reverse=True
        )
        shorts = sorted(
            [(s, sig.strength) for s, sig in signals.items() if sig.direction == 'SELL'],
            key=lambda x: x[1], reverse=True
        )

        weights = {}
        n_long = len(longs)
        n_short = len(shorts)

        for s, _ in longs:
            weights[s] = 1.0 / n_long if n_long else 0

        for s, _ in shorts:
            weights[s] = -1.0 / n_short if n_short else 0

        return weights

    def _blend(self, signals: Dict, mcap: Dict[str, float]) -> Dict[str, float]:
        """信号强度 × 市场权重"""
        result = {}
        for sym, sig in signals.items():
            mcap_w = mcap.get(sym, 1.0)
            result[sym] = sig.strength * mcap_w
        # 归一化
        total = sum(abs(v) for v in result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}
        return result
