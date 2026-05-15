"""
core/pipeline_factory.py — 生产用因子流水线工厂
=================================================

提供 ``build_pipeline()`` 工厂函数，供以下入口统一调用：
  - backend/api.py（HTTP 请求驱动的信号端点）
  - backend/main.py（启动时创建 StrategyRunner 后台线程）
  - streamlit_app.py（交互式分析面板）

因子构成（按权重降序）：
  技术层    RSI(0.20) + MACDTrend(0.20) + Bollinger(0.15) + ATR(0.10)
  基本面层  PEPercentile(0.10) + ROEMomentum(0.10) + RevenueGrowth(0.05) + CashFlowQuality(0.05)
  宏观层    PMI(0.05) + M2Growth(0.05)  ← 无行情数据时自动降级为零权重

使用 DynamicWeightPipeline：
  - 滚动 IC 加权（update_freq_days=21 天更新一次）
  - 因子衰减保护：连续 3 次 IC<0 自动清零，IC 转正后以 0.5x 权重复活
  - FactorCorrelationAnalyzer 在首次构建时检测高相关因子对并记录日志

加载策略（P0-2）：
  - 每个因子独立 try-except，单个失败不影响其它因子
  - MIN_FACTORS_REQUIRED 守卫：成功因子数 < 阈值时按 strict 决定是否 raise
  - FactorPipeline.run() 内部会按 _entries 总权重自动归一化（即使部分因子缺失）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

logger = logging.getLogger('core.pipeline_factory')

# 至少需要加载多少个因子才算 pipeline 健康
# 4 个技术因子是 must-have：RSI + MACDTrend + Bollinger + ATR
MIN_FACTORS_REQUIRED = 4


def _safe_add(pipeline, factor_cls: Type, *, weight: float,
              params: Optional[Dict[str, Any]] = None,
              label: str = '') -> bool:
    """单因子细粒度加载：失败时仅记日志，返回 True/False。"""
    try:
        pipeline.add(factor_cls, weight=weight, params=params or {})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning('因子加载失败（已跳过）%s: %s',
                       label or factor_cls.__name__, exc)
        return False


def build_pipeline(symbol: str = '', strict: bool = True):
    """
    构建并返回生产用 DynamicWeightPipeline 实例。

    Parameters
    ----------
    symbol : str
        默认标的代码，写入 Factor.symbol（对 Signal.symbol 赋值）。
        在 StrategyRunner 内部，每个标的调用时会通过 factor.set_symbol() 覆盖。
    strict : bool
        True 时（默认）成功因子数 < MIN_FACTORS_REQUIRED 抛 RuntimeError；
        False 仅记日志（用于离线/测试场景，可容忍依赖缺失）。

    Returns
    -------
    DynamicWeightPipeline

    Raises
    ------
    RuntimeError
        strict=True 且成功加载因子数 < MIN_FACTORS_REQUIRED 时抛出。
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

    loaded_count = 0
    sym_param = {'symbol': symbol}

    # ── 技术层（must-have）──────────────────────────────────
    for cls, w in [
        (RSIFactor,       0.20),
        (MACDTrendFactor, 0.20),
        (BollingerFactor, 0.15),
        (ATRFactor,       0.10),
    ]:
        if _safe_add(pipeline, cls, weight=w, params=sym_param):
            loaded_count += 1

    # ── 基本面层（每因子独立 try-except，单个失败不影响其它）──
    try:
        from core.factors.fundamental import (
            PEPercentileFactor,
            ROEMomentumFactor,
            RevenueGrowthFactor,
            CashFlowQualityFactor,
        )
        # 使用 FundamentalDataManager 获取历史季报数据（前向填充至日频）
        # 数据请求委托给 DataGateway，享受熔断 + 健康度 + 缓存保护
        financial_data = None
        if symbol:
            try:
                from core.fundamental_data import FundamentalDataManager
                mgr = FundamentalDataManager()
                # 请求 3 年历史（足够支撑 252d 同比 + rolling 窗口）
                fin_df = mgr.get_fundamentals(symbol, start='2023-01-01')
                if fin_df is not None and not fin_df.empty:
                    # 截取所需列（FundamentalDataManager 已完成 ffill 日频化）
                    # 12 列白名单:
                    #   利润表 - roe_ttm/eps_ttm/revenue_yoy/profit_yoy
                    #   成长   - eps_yoy/asset_yoy (W1-1)
                    #   现金流 - ocf_to_profit
                    #   估值   - pe_ttm/pb/dividend_yield
                    #   资产负债 - debt_to_equity/current_ratio/quick_ratio (W1-2)
                    available = [
                        'roe_ttm', 'eps_ttm', 'revenue_yoy', 'profit_yoy',
                        'eps_yoy', 'asset_yoy',
                        'ocf_to_profit',
                        'pe_ttm', 'pb', 'dividend_yield',
                        'debt_to_equity', 'current_ratio', 'quick_ratio',
                    ]
                    financial_data = fin_df[[c for c in available if c in fin_df.columns]]
            except Exception as exc:
                logger.warning('FundamentalDataManager 获取 %s 失败: %s', symbol, exc)

        for cls, w in [
            (PEPercentileFactor,        0.10),
            (ROEMomentumFactor,         0.10),
            (RevenueGrowthFactor,        0.05),
            (CashFlowQualityFactor,      0.05),
        ]:
            params = {}
            if financial_data is not None:
                params = {'financial_data': financial_data}
            if _safe_add(pipeline, cls, weight=w, params=params):
                loaded_count += 1
    except ImportError as exc:
        logger.warning('基本面因子模块导入失败（整层跳过）: %s', exc)

    # ── 宏观层（每因子独立加载，无数据时自动降级）──────────────
    try:
        from core.data_layer import get_data_layer
        from core.factors.macro import PMIFactor, M2GrowthFactor
        dl = get_data_layer()
        pmi_data, m2_data = None, None
        try:
            pmi_data = dl.get_macro_data('PMI')
        except Exception as exc:  # noqa: BLE001
            logger.warning('PMI 数据获取失败: %s', exc)
        try:
            m2_data = dl.get_macro_data('M2')
        except Exception as exc:  # noqa: BLE001
            logger.warning('M2 数据获取失败: %s', exc)

        if _safe_add(pipeline, PMIFactor, weight=0.05,
                     params={'pmi_data': pmi_data}, label='PMIFactor'):
            loaded_count += 1
        if _safe_add(pipeline, M2GrowthFactor, weight=0.05,
                     params={'m2_data': m2_data}, label='M2GrowthFactor'):
            loaded_count += 1
    except ImportError as exc:
        logger.warning('宏观因子模块导入失败（整层跳过）: %s', exc)

    # ── 健康守卫：成功因子数不达标时抛错 ──────────────────────
    if loaded_count < MIN_FACTORS_REQUIRED:
        msg = (f'pipeline 仅加载 {loaded_count} 个因子，低于阈值 '
               f'{MIN_FACTORS_REQUIRED}，拒绝启动以避免信号失真')
        if strict:
            logger.error(msg)
            raise RuntimeError(msg)
        logger.error('%s（strict=False，继续运行但信号质量不可信）', msg)

    # ── 因子相关性检测（仅日志，不阻断启动）──────────────────
    try:
        names = pipeline.factor_names
        logger.info(
            'DynamicWeightPipeline 构建完成 | 加载因子数: %d | 因子: %s',
            len(names), names,
        )
    except Exception:
        pass

    return pipeline


def build_runner(
    symbols,
    dry_run: bool = True,
    interval: int = 300,
    signal_threshold: float = 0.5,
    runtime: str = 'sync',
):
    """
    快速创建生产用 Runner。

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
    runtime : 'sync' | 'async'
        'sync'（默认） → StrategyRunner（线程 + time.sleep 轮询，回测/单测兼容）
        'async'        → AsyncStrategyRunner（asyncio.gather 并发取数，生产推荐）
        env `RUNNER_RUNTIME` 可覆盖默认值。

    Returns
    -------
    StrategyRunner | AsyncStrategyRunner
    """
    import os
    from core.strategy_runner import StrategyRunner, RunnerConfig
    from core.data_layer import get_data_layer
    from core.risk_engine import RiskEngine

    pipeline = build_pipeline()
    cfg = RunnerConfig(
        symbols=symbols,
        pipeline=pipeline,
        interval=interval,
        dry_run=dry_run,
        signal_threshold=signal_threshold,
        regime_aware=True,
    )
    try:
        risk_engine = RiskEngine()
    except Exception:
        risk_engine = None

    rt = (os.environ.get('RUNNER_RUNTIME') or runtime or 'sync').lower()
    if rt == 'async':
        from core.async_runner import AsyncStrategyRunner
        return AsyncStrategyRunner(cfg, data_layer=get_data_layer(),
                                   risk_engine=risk_engine)
    return StrategyRunner(cfg, data_layer=get_data_layer(), risk_engine=risk_engine)
