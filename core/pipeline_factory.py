"""
core/pipeline_factory.py — 生产用因子流水线工厂
=================================================

提供 ``build_pipeline()`` 工厂函数，供以下入口统一调用：
  - backend/api.py（HTTP 请求驱动的信号端点）
  - backend/main.py（启动时创建 StrategyRunner 后台线程）
  - streamlit_app.py（交互式分析面板）

因子构成（按权重降序）：
  技术层    RSI(0.20) + MACDTrend(0.20) + Bollinger(0.15) + ATR(0.10)
  基本面层  PEPercentile(0.10) + ROEMomentum(0.10) + ShareholderConc(0.05)
  宏观层    PMI(0.05) + M2Growth(0.05)  ← 无行情数据时自动降级为零权重

使用 DynamicWeightPipeline：
  - 滚动 IC 加权（update_freq_days=21 天更新一次）
  - 因子衰减保护：连续 3 次 IC<0 自动清零，IC 转正后以 0.5x 权重复活
  - FactorCorrelationAnalyzer 在首次构建时检测高相关因子对并记录日志
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger('core.pipeline_factory')


def build_pipeline(symbol: str = ''):
    """
    构建并返回生产用 DynamicWeightPipeline 实例。

    Parameters
    ----------
    symbol : str
        默认标的代码，写入 Factor.symbol（对 Signal.symbol 赋值）。
        在 StrategyRunner 内部，每个标的调用时会通过 factor.set_symbol() 覆盖。

    Returns
    -------
    DynamicWeightPipeline
    """
    from core.factor_pipeline import DynamicWeightPipeline
    from core.factors.price_momentum import RSIFactor, ATRFactor, BollingerFactor
    from core.strategies.macd_trend import MACDTrendFactor

    pipeline = DynamicWeightPipeline(
        ic_window_days=63,      # 约 3 个月 IC 窗口
        update_freq_days=21,    # 每月更新一次动态权重
        decay_window=3,         # 连续 3 次 IC<0 → 因子清零
        recovery_rate=0.5,      # IC 转正后以 50% 等权重复活
    )

    # ── 技术层 ──────────────────────────────────────────────
    pipeline.add(RSIFactor,       weight=0.20, params={'symbol': symbol})
    pipeline.add(MACDTrendFactor, weight=0.20, params={'symbol': symbol})
    pipeline.add(BollingerFactor, weight=0.15, params={'symbol': symbol})
    pipeline.add(ATRFactor,       weight=0.10, params={'symbol': symbol})

    # ── 基本面层（无数据时因子返回全零，不影响整体权重归一化）──
    try:
        from core.factors.fundamental import (
            PEPercentileFactor,
            ROEMomentumFactor,
            ShareholderConcentrationFactor,
        )
        pipeline.add(PEPercentileFactor,             weight=0.10, params={'symbol': symbol})
        pipeline.add(ROEMomentumFactor,              weight=0.10, params={'symbol': symbol})
        pipeline.add(ShareholderConcentrationFactor, weight=0.05, params={'symbol': symbol})
    except Exception as exc:
        logger.warning('基本面因子加载失败（已跳过）: %s', exc)

    # ── 宏观层（从 DataLayer 获取月度数据，网络失败时自动降级）──
    try:
        from core.data_layer import get_data_layer
        from core.factors.macro import PMIFactor, M2GrowthFactor
        dl = get_data_layer()
        pmi_data = dl.get_macro_data('PMI')
        m2_data  = dl.get_macro_data('M2')
        pipeline.add(PMIFactor,      weight=0.05,
                     params={'pmi_data': pmi_data})
        pipeline.add(M2GrowthFactor, weight=0.05,
                     params={'m2_data': m2_data})
    except Exception as exc:
        logger.warning('宏观因子加载失败（已降级）: %s', exc)

    # ── 因子相关性检测（仅日志，不阻断启动）──────────────────
    try:
        names = pipeline.factor_names
        logger.info('DynamicWeightPipeline 构建完成 | 因子数: %d | 因子: %s',
                    len(names), names)
    except Exception:
        pass

    return pipeline


def build_runner(
    symbols,
    dry_run: bool = True,
    interval: int = 300,
    signal_threshold: float = 0.5,
):
    """
    快速创建生产用 StrategyRunner。

    Parameters
    ----------
    symbols : List[str] | Callable[[], List[str]]
        交易标的列表，或每轮动态求值的可调用对象。
    dry_run : bool
        True → 只记录信号，不下单（默认）；False → 接入 OMS 真实下单。
    interval : int
        run_loop() 两轮之间的等待秒数（默认 300s）。
    signal_threshold : float
        |combined_score| 超过此值才触发下单（默认 0.5）。

    Returns
    -------
    StrategyRunner
    """
    from core.strategy_runner import StrategyRunner, RunnerConfig
    from core.data_layer import get_data_layer

    pipeline = build_pipeline()
    cfg = RunnerConfig(
        symbols=symbols,
        pipeline=pipeline,
        interval=interval,
        dry_run=dry_run,
        signal_threshold=signal_threshold,
        regime_aware=True,
    )
    return StrategyRunner(cfg, data_layer=get_data_layer())
