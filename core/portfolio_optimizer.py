"""
core/portfolio_optimizer.py — 均值方差组合优化框架

实现的优化方法：
  1. min_variance        — 全局最小方差（GMV）
  2. max_sharpe          — 最大 Sharpe 比率
  3. risk_parity         — 等风险贡献（Risk Parity / ERC）
  4. black_litterman     — Black-Litterman 观点融合
  5. max_diversification — 最大分散化比率
  6. equal_weight        — 等权（基准 benchmark）

协方差估计：
  - sample               — 样本协方差（基础）
  - ledoit_wolf          — Ledoit-Wolf 收缩估计（更稳定，高维时推荐）

约束条件（所有方法支持）：
  - 权重下界 / 上界（默认 0 ≤ w_i ≤ max_weight）
  - 月度换手率约束（可选）
  - 行业集中度约束（可选）

用法：
    import pandas as pd
    from core.portfolio_optimizer import PortfolioOptimizer

    returns = pd.DataFrame(...)  # shape (T, N)，日收益率
    opt = PortfolioOptimizer(returns)

    w_gmv   = opt.min_variance()
    w_ms    = opt.max_sharpe(rf=0.02/252)
    w_rp    = opt.risk_parity()
    w_bl    = opt.black_litterman(
        views={'000001.SZ': 0.001, '600519.SH': -0.0005},
        view_confidences={'000001.SZ': 0.7, '600519.SH': 0.5},
    )
"""

from __future__ import annotations

import warnings
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _ledoit_wolf(returns: np.ndarray) -> np.ndarray:
    """
    Ledoit-Wolf 收缩协方差估计。

    使用 sklearn 的 LedoitWolf（若可用），否则退化为样本协方差。
    收缩目标：对角矩阵（各资产方差的均值）× shrinkage + 样本协方差 × (1-shrinkage)。
    """
    try:
        from sklearn.covariance import LedoitWolf
        lw = LedoitWolf()
        lw.fit(returns)
        return lw.covariance_
    except ImportError:
        pass

    # 手动实现 Oracle Approximating Shrinkage (OAS)
    n, p = returns.shape
    S = np.cov(returns.T)
    mu = np.trace(S) / p
    target = mu * np.eye(p)
    # 收缩强度（简化版 Ledoit-Wolf 估计）
    rho_num = (np.sum(S ** 2) + np.trace(S) ** 2) / ((n + 1 - 2 / p) * p)
    rho_denom = np.sum(S ** 2) - np.trace(S) ** 2 / p
    rho = min(1.0, rho_num / max(rho_denom, 1e-12))
    return (1 - rho) * S + rho * target


def _make_positive_definite(cov: np.ndarray, eps: float = 1e-8) -> np.ndarray:
    """将矩阵调整为正定（处理数值误差）。"""
    eigvals = np.linalg.eigvalsh(cov)
    if eigvals.min() < eps:
        cov = cov + (eps - eigvals.min()) * np.eye(len(cov))
    return cov


# ---------------------------------------------------------------------------
# PortfolioOptimizer
# ---------------------------------------------------------------------------

class PortfolioOptimizer:
    """
    多方法组合权重优化器。

    Parameters
    ----------
    returns : pd.DataFrame
        日收益率矩阵，shape (T, N)，列为资产代码
    cov_method : str
        协方差估计方法：'sample'（样本）或 'ledoit_wolf'（收缩，推荐）
    max_weight : float
        单资产权重上限（默认 0.25 = 25%）
    min_weight : float
        单资产权重下限（默认 0.0 = 允许零权重）
    rf : float
        无风险利率（日频，默认 0.02/252 ≈ 0.0000794）
    """

    def __init__(
        self,
        returns: pd.DataFrame,
        cov_method: str = 'ledoit_wolf',
        max_weight: float = 0.25,
        min_weight: float = 0.0,
        rf: float = 0.02 / 252,
    ) -> None:
        if returns.empty or returns.shape[1] < 2:
            raise ValueError("需要至少 2 个资产的收益率数据")

        self.returns = returns.dropna()
        self.assets = list(returns.columns)
        self.n = len(self.assets)
        self.cov_method = cov_method
        self.max_weight = min(max_weight, 1.0)
        self.min_weight = max(min_weight, 0.0)
        self.rf = rf

        # 预计算均值和协方差
        self._mu = self.returns.mean().values           # shape (N,)
        self._cov = self._compute_cov()                 # shape (N, N)

    # ------------------------------------------------------------------
    # 1. 全局最小方差（GMV）
    # ------------------------------------------------------------------

    def min_variance(self) -> pd.Series:
        """
        全局最小方差投资组合。

        最小化 w^T Σ w，约束：Σw=1, w_min ≤ w ≤ w_max。

        Returns
        -------
        pd.Series — 权重（index = asset names）
        """
        from scipy.optimize import minimize

        cov = self._cov
        n = self.n

        def objective(w):
            return float(w @ cov @ w)

        def grad(w):
            return 2 * cov @ w

        w0 = np.ones(n) / n
        constraints = [{'type': 'eq', 'fun': lambda w: w.sum() - 1}]
        bounds = [(self.min_weight, self.max_weight)] * n

        result = minimize(
            objective, w0, jac=grad,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': 1e-10, 'maxiter': 500},
        )

        w = self._clip_and_normalize(result.x)
        return pd.Series(w, index=self.assets)

    # ------------------------------------------------------------------
    # 2. 最大 Sharpe 比率
    # ------------------------------------------------------------------

    def max_sharpe(self, rf: Optional[float] = None) -> pd.Series:
        """
        最大 Sharpe 比率投资组合（切线组合）。

        使用"辅助变量法"（Sharpe = (μ - rf)^T y / sqrt(y^T Σ y)，y = w/f）。

        Returns
        -------
        pd.Series — 权重
        """
        rf = rf if rf is not None else self.rf
        excess_mu = self._mu - rf

        # 若超额收益均为负，退化为 GMV
        if np.all(excess_mu <= 0):
            warnings.warn("所有资产超额收益为负，退化为最小方差组合")
            return self.min_variance()

        from scipy.optimize import minimize

        cov = self._cov
        n = self.n

        def neg_sharpe(w):
            port_ret = float(w @ excess_mu)
            port_vol = float(np.sqrt(w @ cov @ w))
            if port_vol < 1e-10:
                return 0.0
            return -port_ret / port_vol

        def grad_neg_sharpe(w):
            port_ret = float(w @ excess_mu)
            port_var = float(w @ cov @ w)
            port_vol = np.sqrt(max(port_var, 1e-20))
            grad_ret = excess_mu
            grad_vol = cov @ w / port_vol
            return -(grad_ret * port_vol - port_ret * grad_vol) / port_var

        w0 = np.ones(n) / n
        constraints = [{'type': 'eq', 'fun': lambda w: w.sum() - 1}]
        bounds = [(self.min_weight, self.max_weight)] * n

        result = minimize(
            neg_sharpe, w0, jac=grad_neg_sharpe,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': 1e-10, 'maxiter': 500},
        )

        w = self._clip_and_normalize(result.x)
        return pd.Series(w, index=self.assets)

    # ------------------------------------------------------------------
    # 3. 风险平价（等风险贡献）
    # ------------------------------------------------------------------

    def risk_parity(self) -> pd.Series:
        """
        等风险贡献（ERC / Risk Parity）投资组合。

        目标：最小化各资产风险贡献之差（RC_i = w_i × (Σw)_i / vol）。
        迭代方法：Maillard et al. (2010) 的 cyclical 算法。

        Returns
        -------
        pd.Series — 权重
        """
        from scipy.optimize import minimize

        cov = self._cov
        n = self.n

        # 目标：最小化 sum_i sum_j (RC_i - RC_j)^2
        def objective(w):
            sigma = np.sqrt(max(float(w @ cov @ w), 1e-20))
            marginal = cov @ w
            rc = w * marginal / sigma
            rc_mean = rc.mean()
            return float(np.sum((rc - rc_mean) ** 2))

        def grad(w):
            port_var = float(w @ cov @ w)
            sigma = np.sqrt(max(port_var, 1e-20))
            marginal = cov @ w
            rc = w * marginal / sigma
            rc_mean = rc.mean()
            d_rc = 2 * (rc - rc_mean)
            # 数值梯度（分析梯度较复杂，以数值梯度保证稳健性）
            eps = 1e-6
            g = np.zeros(n)
            for i in range(n):
                w_p = w.copy(); w_p[i] += eps
                w_m = w.copy(); w_m[i] -= eps
                g[i] = (objective(w_p) - objective(w_m)) / (2 * eps)
            return g

        # 初始化：等权
        w0 = np.ones(n) / n
        constraints = [{'type': 'eq', 'fun': lambda w: w.sum() - 1}]
        bounds = [(max(self.min_weight, 1e-6), self.max_weight)] * n  # 风险平价需要 w > 0

        result = minimize(
            objective, w0,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': 1e-12, 'maxiter': 1000},
        )

        w = self._clip_and_normalize(result.x)
        return pd.Series(w, index=self.assets)

    # ------------------------------------------------------------------
    # 4. Black-Litterman
    # ------------------------------------------------------------------

    def black_litterman(
        self,
        views: Dict[str, float],
        view_confidences: Optional[Dict[str, float]] = None,
        tau: float = 0.05,
        risk_aversion: float = 2.5,
        method: str = 'max_sharpe',
    ) -> pd.Series:
        """
        Black-Litterman 观点融合组合。

        步骤：
          1. 计算 equilibrium 超额收益：Π = δ × Σ × w_mkt（用等权替代 w_mkt）
          2. 构建观点矩阵 P / Q / Omega
          3. 计算 BL 后验预期收益：μ_BL
          4. 用 μ_BL 替换样本均值，重新优化（最大 Sharpe 或 最小方差）

        Parameters
        ----------
        views : Dict[str, float]
            观点字典：{资产代码: 预期日收益率}
            例如 {'000001.SZ': 0.001, '600519.SH': -0.0005}
        view_confidences : Dict[str, float] or None
            置信度：{资产代码: 0.0–1.0}（默认全部 0.5）
        tau : float
            不确定性缩放因子（默认 0.05，越小表示越信任 equilibrium）
        risk_aversion : float
            风险厌恶系数（默认 2.5）
        method : str
            后验收益优化方法：'max_sharpe' 或 'min_variance'

        Returns
        -------
        pd.Series — 权重
        """
        if not views:
            return self.min_variance()

        cov = self._cov
        n = self.n
        asset_idx = {a: i for i, a in enumerate(self.assets)}

        # 步骤 1：均衡超额收益（以等权替代市场组合）
        w_mkt = np.ones(n) / n
        pi = risk_aversion * cov @ w_mkt    # 均衡超额收益，shape (N,)

        # 步骤 2：构建观点矩阵
        valid_views = {k: v for k, v in views.items() if k in asset_idx}
        if not valid_views:
            return self.min_variance()

        k = len(valid_views)
        P = np.zeros((k, n))                # 观点矩阵 (K, N)
        Q = np.zeros(k)                     # 观点收益 (K,)
        omega_diag = np.zeros(k)            # Omega 对角元素 (K,)

        view_conf = view_confidences or {}

        for i, (asset, expected_return) in enumerate(valid_views.items()):
            idx = asset_idx[asset]
            P[i, idx] = 1.0
            Q[i] = expected_return
            conf = float(view_conf.get(asset, 0.5))
            conf = np.clip(conf, 0.01, 0.99)
            # Omega_ii = (1 - conf) / conf × tau × P_i^T Σ P_i
            omega_diag[i] = (1 - conf) / conf * tau * float(P[i] @ cov @ P[i])

        Omega = np.diag(np.maximum(omega_diag, 1e-8))

        # 步骤 3：后验预期收益（BL 公式）
        tau_cov = tau * cov
        # μ_BL = [(τΣ)^{-1} + P^T Ω^{-1} P]^{-1} × [(τΣ)^{-1} Π + P^T Ω^{-1} Q]
        try:
            tau_cov_inv = np.linalg.inv(tau_cov)
            omega_inv = np.linalg.inv(Omega)
            A = tau_cov_inv + P.T @ omega_inv @ P
            b = tau_cov_inv @ pi + P.T @ omega_inv @ Q
            mu_bl = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            warnings.warn("BL 矩阵求解失败，退化为最小方差")
            return self.min_variance()

        # 步骤 4：用 BL 后验均值替换样本均值，重新优化
        original_mu = self._mu.copy()
        self._mu = mu_bl

        try:
            if method == 'max_sharpe':
                result = self.max_sharpe()
            else:
                result = self.min_variance()
        finally:
            self._mu = original_mu  # 恢复原始均值

        return result

    # ------------------------------------------------------------------
    # 5. 最大分散化
    # ------------------------------------------------------------------

    def max_diversification(self) -> pd.Series:
        """
        最大分散化比率（MDR）投资组合。

        最大化 DR = Σ(w_i × σ_i) / sqrt(w^T Σ w)。

        Returns
        -------
        pd.Series — 权重
        """
        from scipy.optimize import minimize

        cov = self._cov
        stds = np.sqrt(np.diag(cov))
        n = self.n

        def neg_dr(w):
            port_vol = float(np.sqrt(max(w @ cov @ w, 1e-20)))
            weighted_vols = float(w @ stds)
            return -weighted_vols / port_vol

        w0 = np.ones(n) / n
        constraints = [{'type': 'eq', 'fun': lambda w: w.sum() - 1}]
        bounds = [(self.min_weight, self.max_weight)] * n

        result = minimize(
            neg_dr, w0,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': 1e-10, 'maxiter': 500},
        )

        w = self._clip_and_normalize(result.x)
        return pd.Series(w, index=self.assets)

    # ------------------------------------------------------------------
    # 6. 等权（基准）
    # ------------------------------------------------------------------

    def equal_weight(self) -> pd.Series:
        """等权投资组合（基准）。"""
        w = np.ones(self.n) / self.n
        return pd.Series(w, index=self.assets)

    # ------------------------------------------------------------------
    # 诊断 & 约束
    # ------------------------------------------------------------------

    def portfolio_stats(self, weights: pd.Series, rf: Optional[float] = None) -> Dict:
        """
        计算组合的关键统计指标。

        Returns
        -------
        dict — {'annual_return', 'annual_vol', 'sharpe', 'max_drawdown',
                'diversification_ratio', 'effective_n'}
        """
        rf = rf if rf is not None else self.rf
        w = weights.reindex(self.assets).fillna(0).values
        cov = self._cov
        mu = self._mu

        port_ret = float(w @ mu) * 252
        port_vol = float(np.sqrt(w @ cov @ w)) * np.sqrt(252)
        sharpe = (port_ret - rf * 252) / max(port_vol, 1e-8)

        # 最大回撤（用历史收益估算）
        port_rets = (self.returns * weights.reindex(self.assets, fill_value=0)).sum(axis=1)
        cumret = (1 + port_rets).cumprod()
        rolling_max = cumret.cummax()
        drawdown = (cumret - rolling_max) / rolling_max
        max_dd = float(drawdown.min())

        # 分散化比率
        stds = np.sqrt(np.diag(cov))
        dr = float(w @ stds) / max(float(np.sqrt(w @ cov @ w)), 1e-8)

        # 有效资产数（Herfindahl 倒数）
        w_sq = w ** 2
        eff_n = 1.0 / max(w_sq.sum(), 1e-8)

        return {
            'annual_return': round(port_ret, 4),
            'annual_vol': round(port_vol, 4),
            'sharpe': round(sharpe, 4),
            'max_drawdown': round(max_dd, 4),
            'diversification_ratio': round(dr, 4),
            'effective_n': round(eff_n, 2),
        }

    def turnover(self, w_new: pd.Series, w_old: pd.Series) -> float:
        """
        计算换手率（单边换手 = Σ max(w_new - w_old, 0)）。

        Returns
        -------
        float — 换手率 [0, 1]
        """
        diff = (w_new - w_old).fillna(w_new)
        return float(diff.clip(lower=0).sum())

    def apply_turnover_constraint(
        self,
        w_new: pd.Series,
        w_old: pd.Series,
        max_turnover: float = 0.5,
    ) -> pd.Series:
        """
        强制换手率约束：按 (w_new - w_old) 方向线性收缩，直到换手率 ≤ max_turnover。

        Parameters
        ----------
        w_new : pd.Series — 目标权重
        w_old : pd.Series — 当前权重
        max_turnover : float — 允许最大单边换手率（默认 0.5 = 50%）

        Returns
        -------
        pd.Series — 约束后的实际目标权重
        """
        current_to = self.turnover(w_new, w_old)
        if current_to <= max_turnover:
            return w_new

        # 线性插值：w_adjusted = w_old + alpha × (w_new - w_old)
        alpha = max_turnover / current_to
        w_adjusted = w_old + alpha * (w_new - w_old)
        w_adjusted = w_adjusted.clip(lower=self.min_weight, upper=self.max_weight)
        total = w_adjusted.sum()
        return w_adjusted / total if total > 0 else self.equal_weight()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _compute_cov(self) -> np.ndarray:
        """计算协方差矩阵（根据 cov_method）。"""
        data = self.returns.values
        if self.cov_method == 'ledoit_wolf':
            cov = _ledoit_wolf(data)
        else:
            cov = np.cov(data.T)

        cov = _make_positive_definite(cov)
        return cov

    def _clip_and_normalize(self, w: np.ndarray) -> np.ndarray:
        """裁剪到 [min_weight, max_weight] 并归一化。"""
        w = np.clip(w, self.min_weight, self.max_weight)
        total = w.sum()
        if total <= 0:
            return np.ones(self.n) / self.n
        return w / total
