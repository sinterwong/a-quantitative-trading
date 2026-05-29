"""
StrategyRunner — 策略主循环（同步版）

P2-15 收敛建议：生产环境推荐使用 `core.async_runner.AsyncStrategyRunner`
（asyncio.gather 并发取数，N 标的延迟 N×200ms → 200ms）。本类保留为
回测 / 单测 / 同步上下文兼容入口。两者共享 `RunnerConfig` 与 `RunResult`，
通过 `core.pipeline_factory.build_runner(runtime='async')` 或环境变量
`RUNNER_RUNTIME=async` 切换。

数据流：
  DataLayer.get_bars() → FactorPipeline.run() → RiskEngine.check()
  → SignalEvent → OMS.submit_from_signal()

两种运行模式：
  run_once()  — 单轮扫描（回测/测试/手动触发）
  run_loop()  — 主循环（内部 sleep + 线程安全停止）

dry_run=True 时只打印信号，不真正下单（调试首选）。

用法（同步，回测/单测）：
    from core.strategy_runner import StrategyRunner, RunnerConfig
    from core.factor_pipeline import DynamicWeightPipeline
    from core.data_layer import get_data_layer

    pipeline = DynamicWeightPipeline()
    pipeline.add('RSI', weight=0.5).add('MACD', weight=0.3).add('ATR', weight=0.2)

    cfg = RunnerConfig(
        symbols=['600519.SH', '000858.SZ'],
        pipeline=pipeline,
        interval=300,       # 5 分钟一轮
        dry_run=False,
    )
    runner = StrategyRunner(cfg, data_layer=get_data_layer())
    runner.run_loop()       # 阻塞，Ctrl+C 退出

用法（回测/测试）：
    results = runner.run_once()  # 返回本轮所有 RunResult
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Callable, Dict, List, Optional

from core.data_layer import DataLayer, get_data_layer
from core.factor_pipeline import FactorPipeline, DynamicWeightPipeline, PipelineResult
from core.regime import RegimeInfo, get_regime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult — 单标的单轮结果
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """一次 run_once() 对单标的的执行结果。"""
    symbol: str
    timestamp: datetime
    pipeline_result: Optional[PipelineResult]
    action: str                # 'NONE' | 'BUY' | 'SELL' | 'SKIPPED' | 'ERROR'
    reason: str = ''
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def acted(self) -> bool:
        return self.action in ('BUY', 'SELL')


# ---------------------------------------------------------------------------
# RunnerConfig
# ---------------------------------------------------------------------------

@dataclass
class RunnerConfig:
    """
    StrategyRunner 配置。

    Parameters
    ----------
    symbols:
        标的列表，或返回标的列表的可调用对象（每轮重新求值）
    pipeline:
        配置好的 FactorPipeline 实例
    interval:
        run_loop() 两轮之间的等待秒数（默认 300s = 5 分钟）
    dry_run:
        True → 只计算信号，不调用 OMS / 不发射 SignalEvent
    signal_threshold:
        |combined_score| 超过此值才触发下单（默认 0.5）
    bars_lookback:
        获取历史 K 线的天数（传给 DataLayer.get_bars 的 days 参数）
    on_signal:
        可选回调 on_signal(symbol, pipeline_result, runner) → bool
        返回 False 表示拦截（不下单）
    """
    symbols: Any                           # List[str] | Callable[[], List[str]]
    pipeline: FactorPipeline               # FactorPipeline 或 DynamicWeightPipeline（推荐）
    interval: int = 300
    dry_run: bool = True
    signal_threshold: float = 0.5
    bars_lookback: int = 120
    on_signal: Optional[Callable] = None
    regime_aware: bool = True
    """
    True → 在每轮 run_once() 开始前检测市场环境，并据此调整行为：
      BEAR     → 禁止新开多仓，信号阈值 ×1.4
      VOLATILE → 信号阈值 ×1.2
      BULL / CALM → 不做调整
    """

    # P0-1: 可选 ExitEngine 钩子（默认 False）
    # 注意：生产 IntradayMonitor 已经在 _run_exit_engine() 中调用 ExitEngine，
    # 此处再开会导致同标的同周期触发两次 SELL。仅在 IntradayMonitor 不参与的
    # 场景下（如纯 dry_run 信号预览、离线回测脚本）开启。
    use_exit_engine: bool = False
    exit_engine_params: Optional[Dict[str, Any]] = None

    # P0-3: 组合再平衡（PortfolioOptimizer + Allocator）
    enable_rebalance: bool = False
    """启用每轮 run_once() 末尾检查再平衡。生产中默认 False（避免与人工干预冲突）。"""

    rebalance_method: str = 'max_sharpe'
    """优化方法：'max_sharpe' | 'min_variance' | 'risk_parity'"""

    rebalance_period_days: int = 21
    """周期触发：距上次再平衡超 N 天 → 触发"""

    rebalance_drift_threshold: float = 0.05
    """漂移触发：单标的实际权重相对目标偏离 ≥ 此值 → 触发"""

    rebalance_max_weight: float = 0.25
    """单标的最大权重（传给 PortfolioOptimizer.max_weight）"""

    rebalance_returns_lookback: int = 252
    """优化用历史收益率窗口（默认 252 个交易日）"""


# ---------------------------------------------------------------------------
# StrategyRunner
# ---------------------------------------------------------------------------

class StrategyRunner:
    """
    策略主循环。

    线程安全：stop() 可从任意线程调用，run_loop() 最终退出。
    """

    def __init__(
        self,
        config: RunnerConfig,
        data_layer: Optional[DataLayer] = None,
        risk_engine=None,         # Optional[RiskEngine] — 避免循环 import
        oms=None,                 # Optional[OMS]
        event_bus=None,           # Optional[EventBus]
    ) -> None:
        self.config = config
        self.data_layer = data_layer or get_data_layer()
        self.risk_engine = risk_engine
        self.oms = oms
        self.event_bus = event_bus

        self._stop_event = threading.Event()
        self._running = False
        self._run_count = 0                       # 已完成轮次
        self._last_run_results: List[RunResult] = []
        self._results_lock = threading.Lock()     # 保护跨线程读写
        self._current_regime: Optional[RegimeInfo] = None

        # P0-1: ExitEngine（可选）
        self._exit_engine = None
        self._equity_peak: float = 0.0
        if config.use_exit_engine:
            from core.exit_engine import ExitEngine
            self._exit_engine = ExitEngine(**(config.exit_engine_params or {}))
        self._last_exit_signals: List[Any] = []   # ExitSignal 列表（诊断用）

        # P0-3: 组合再平衡状态
        self._last_rebalance_dt: Optional[datetime] = None
        self._last_target_weights: Dict[str, float] = {}   # 上次优化输出
        self._last_rebalance_diff: Dict[str, float] = {}   # 上次目标 vs 当前差额

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> List[RunResult]:
        """
        对所有标的执行一轮扫描。

        Returns
        -------
        本轮所有标的的 RunResult 列表（不论是否触发信号）
        """
        symbols = self._resolve_symbols()
        results: List[RunResult] = []
        ts = datetime.now()

        # 检测市场环境（每轮只调用一次，结果全轮复用）
        if self.config.regime_aware:
            try:
                self._current_regime = get_regime()
                logger.info(
                    "[StrategyRunner] Regime=%s | %s",
                    self._current_regime.regime,
                    self._current_regime.reason,
                )
            except Exception as exc:
                logger.warning("[StrategyRunner] get_regime failed: %s", exc)
                self._current_regime = None

        for symbol in symbols:
            r = self._process_symbol(symbol, ts)
            results.append(r)

        # P0-1: ExitEngine 钩子（仅 use_exit_engine=True 时启用）
        if self._exit_engine is not None:
            try:
                self._run_exit_engine(results, ts)
            except Exception as exc:
                logger.warning("[StrategyRunner] exit engine error: %s", exc)

        # P0-3: 组合再平衡钩子
        if self.config.enable_rebalance:
            try:
                self._maybe_rebalance(symbols, ts)
            except Exception as exc:
                logger.warning("[StrategyRunner] rebalance error: %s", exc)

        self._run_count += 1
        with self._results_lock:
            self._last_run_results = results
        logger.info(
            "[StrategyRunner] run #%d: %d symbols, %d actions",
            self._run_count,
            len(results),
            sum(1 for r in results if r.acted),
        )
        return results

    def run_loop(self) -> None:
        """
        生产主循环，阻塞直到 stop() 被调用或 KeyboardInterrupt。
        """
        self._running = True
        self._stop_event.clear()
        logger.info(
            "[StrategyRunner] Starting loop (interval=%ds, dry_run=%s)",
            self.config.interval,
            self.config.dry_run,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception as exc:
                    logger.exception("[StrategyRunner] run_once error: %s", exc)
                # 分段 sleep，保证 stop() 可快速响应
                self._interruptible_sleep(self.config.interval)
        except KeyboardInterrupt:
            logger.info("[StrategyRunner] KeyboardInterrupt received, stopping.")
        finally:
            self._running = False
            logger.info("[StrategyRunner] Stopped after %d runs.", self._run_count)

    def stop(self) -> None:
        """从任意线程安全地停止 run_loop()。"""
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def run_count(self) -> int:
        return self._run_count

    @property
    def last_results(self) -> List[RunResult]:
        with self._results_lock:
            return list(self._last_run_results)

    @property
    def current_regime(self) -> Optional[RegimeInfo]:
        """最近一次检测到的市场环境，未检测时为 None。"""
        return self._current_regime

    @property
    def last_scores(self) -> Dict[str, float]:
        """
        返回 {symbol: combined_score} 字典，供 IntradayMonitor 读取。

        - 无结果或 pipeline_result 为 None 时，该标的不出现
        - 线程安全（复用 _results_lock）
        """
        with self._results_lock:
            return {
                r.symbol: r.pipeline_result.combined_score
                for r in self._last_run_results
                if r.pipeline_result is not None
            }

    # ------------------------------------------------------------------
    # Internal: per-symbol processing
    # ------------------------------------------------------------------

    def _process_symbol(self, symbol: str, ts: datetime) -> RunResult:
        """处理单个标的，返回 RunResult。"""
        # 1. 获取历史 K 线
        try:
            data = self.data_layer.get_bars(symbol, days=self.config.bars_lookback)
        except (OSError, ValueError, KeyError) as exc:
            # 数据层失败：网络 IO / 缓存损坏 / 列缺失 → 该 symbol 跳过本轮
            logger.warning("[StrategyRunner] get_bars(%s) failed: %s", symbol, exc)
            return RunResult(
                symbol=symbol, timestamp=ts,
                pipeline_result=None,
                action='ERROR', reason=f'get_bars failed: {exc}',
            )

        if data is None or len(data) == 0:
            return RunResult(
                symbol=symbol, timestamp=ts,
                pipeline_result=None,
                action='SKIPPED', reason='no_data',
            )

        # 2. 获取实时报价（作为 price 参数）
        price: Optional[float] = None
        try:
            quote = self.data_layer.get_realtime(symbol)
            if quote is not None:
                # Quote 是 dataclass，直接用属性访问
                price = getattr(quote, 'price', None) or getattr(quote, 'close', None)
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            # 实时报价失败 → 让 pipeline 用 DataFrame 末行 close 兜底
            logger.debug("[StrategyRunner] get_realtime(%s) failed (using bar fallback): %s",
                         symbol, exc)

        # 3. 运行因子流水线
        try:
            pr = self.config.pipeline.run(symbol=symbol, data=data, price=price)
        except Exception as exc:
            logger.warning("[StrategyRunner] pipeline.run(%s) failed: %s", symbol, exc)
            return RunResult(
                symbol=symbol, timestamp=ts,
                pipeline_result=None,
                action='ERROR', reason=f'pipeline failed: {exc}',
            )

        # 4. 判断是否触发信号（含 Regime 调整）
        score = pr.combined_score
        dominant = pr.dominant_signal

        # Regime 风控：调整有效阈值
        effective_threshold = self.config.signal_threshold
        regime = self._current_regime
        if regime is not None:
            effective_threshold *= regime.signal_threshold_multiplier
            # BEAR 状态下禁止新开多仓
            if not regime.allow_new_buys and dominant == 'BUY':
                return RunResult(
                    symbol=symbol, timestamp=ts,
                    pipeline_result=pr,
                    action='SKIPPED',
                    reason=f'regime_bear_no_buy(regime={regime.regime})',
                    metadata={'combined_score': score, 'regime': regime.regime},
                )

        if abs(score) < effective_threshold or dominant == 'HOLD':
            return RunResult(
                symbol=symbol, timestamp=ts,
                pipeline_result=pr,
                action='NONE',
                reason=(
                    f'score={score:.4f} below threshold={effective_threshold:.4f}'
                    + (f'(regime={regime.regime})' if regime else '')
                ),
                metadata={
                    'combined_score': score,
                    'dominant': dominant,
                    'regime': regime.regime if regime else 'unknown',
                },
            )

        # 5. 用户回调拦截
        if self.config.on_signal:
            try:
                allow = self.config.on_signal(symbol, pr, self)
                if not allow:
                    return RunResult(
                        symbol=symbol, timestamp=ts,
                        pipeline_result=pr,
                        action='SKIPPED', reason='blocked_by_on_signal_hook',
                        metadata={'combined_score': score},
                    )
            except Exception as exc:
                logger.warning("[StrategyRunner] on_signal hook error: %s", exc)

        # 6. 风控 PreTrade 检查
        risk_ok, risk_reason = self._risk_check(symbol, dominant, price or 0.0)
        if not risk_ok:
            return RunResult(
                symbol=symbol, timestamp=ts,
                pipeline_result=pr,
                action='SKIPPED', reason=f'risk_rejected: {risk_reason}',
                metadata={'combined_score': score},
            )

        # 7. 执行（dry_run / live）
        action = dominant  # 'BUY' or 'SELL'
        if self.config.dry_run:
            logger.info(
                "[DRY RUN] %s %s | score=%.4f | price=%s | signals=%d",
                action, symbol, score, price, len(pr.signals),
            )
        else:
            logger.info(
                "[LIVE] %s %s | score=%.4f | price=%.4f | signals=%d",
                action, symbol, score, price or 0.0, len(pr.signals),
            )
            self._emit_signal(symbol, pr, dominant, price or 0.0)

        return RunResult(
            symbol=symbol, timestamp=ts,
            pipeline_result=pr,
            action=action,
            reason='signal_triggered',
            metadata={
                'combined_score': score,
                'dry_run': self.config.dry_run,
                'signals_count': len(pr.signals),
            },
        )

    # ------------------------------------------------------------------
    # Risk check
    # ------------------------------------------------------------------

    def _risk_check(self, symbol: str, direction: str, price: float) -> tuple:
        """
        调用 RiskEngine.check() 做 PreTrade 检查。
        Returns (passed: bool, reason: str)
        """
        if self.risk_engine is None:
            return True, ''
        try:
            from core.factors.base import Signal as FactorSignal
            dummy_signal = FactorSignal(
                timestamp=datetime.now(),
                symbol=symbol,
                direction=direction,
                strength=1.0,
                factor_name='StrategyRunner',
                price=price,
            )
            result = self.risk_engine.check(dummy_signal)
            return result.passed, result.reason
        except Exception as exc:
            logger.error("[StrategyRunner] risk_check exception, rejecting order: %s", exc)
            return False, f'risk_check_exception: {exc}'

    # ------------------------------------------------------------------
    # Signal emission
    # ------------------------------------------------------------------

    def _emit_signal(
        self,
        symbol: str,
        pr: PipelineResult,
        direction: str,
        price: float,
    ) -> None:
        """向 EventBus 发射 SignalEvent 或直接调用 OMS。"""
        # 取强度最高的对应方向信号
        candidates = [s for s in pr.signals if s.direction == direction]
        if not candidates:
            return
        top_signal = max(candidates, key=lambda s: s.strength)
        # 空 symbol 信号防御：追溯源头
        if not top_signal.symbol:
            logger.warning(
                "[_emit_signal] EMPTY symbol signal from factor=%s direction=%s",
                top_signal.factor_name, direction,
            )
        if self.event_bus is not None:
            try:
                from core.event_bus import SignalEvent
                self.event_bus.emit(SignalEvent(signal=top_signal))
            except Exception as exc:
                logger.error("[StrategyRunner] emit SignalEvent failed: %s", exc)
        elif self.oms is not None:
            try:
                self.oms.submit_from_signal(top_signal)
            except Exception as exc:
                logger.error("[StrategyRunner] OMS submit failed: %s", exc)
        else:
            logger.warning(
                "[StrategyRunner] No EventBus/OMS configured, signal dropped: %s %s",
                direction, symbol,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_symbols(self) -> List[str]:
        """解析 config.symbols（支持列表或可调用对象）。"""
        s = self.config.symbols
        if callable(s):
            return list(s())
        return list(s)

    def _interruptible_sleep(self, seconds: int) -> None:
        """分段 sleep，每秒检查一次 stop 信号。"""
        for _ in range(seconds):
            if self._stop_event.is_set():
                break
            time.sleep(1)

    # ------------------------------------------------------------------
    # P0-1: ExitEngine 钩子
    # ------------------------------------------------------------------

    def _run_exit_engine(self, results: List[RunResult], ts: datetime) -> None:
        """
        对当前持仓调用 ExitEngine.generate()。

        - 持仓数据从 RiskEngine.book 或 OMS 读取（取决于注入哪个）
        - 每个 ExitSignal 转换为 SELL Signal 并经 _emit_signal 提交
        - dry_run=True 时只记录到 self._last_exit_signals（不下单）
        """
        positions = self._collect_positions()
        if not positions:
            self._last_exit_signals = []
            return

        equity = sum(
            p.get('shares', 0) * p.get('current_price', 0) for p in positions
        )
        # 维护峰值（独立于 OMS 的 _peak_equity，避免污染）
        if equity > self._equity_peak:
            self._equity_peak = equity

        pipeline_scores = {
            r.symbol: r.pipeline_result.combined_score
            for r in results if r.pipeline_result is not None
        }

        try:
            exit_signals = self._exit_engine.generate(
                positions=positions,
                equity_peak=self._equity_peak or equity,
                current_equity=equity,
                pipeline_scores=pipeline_scores,
            )
        except Exception as exc:
            logger.warning("[StrategyRunner] ExitEngine.generate failed: %s", exc)
            return

        self._last_exit_signals = exit_signals
        if not exit_signals:
            return

        for esig in exit_signals:
            if self.config.dry_run:
                logger.info(
                    "[DRY RUN] ExitEngine SELL %s pri=%s pct=%.2f reason=%s",
                    esig.symbol, esig.priority.name, esig.exit_pct, esig.reason,
                )
                continue

            # 真实下单：构造 SELL Signal
            try:
                from core.factors.base import Signal as FactorSignal
                target_shares = next(
                    (int(p.get('shares', 0)) for p in positions
                     if p.get('symbol') == esig.symbol),
                    0,
                )
                shares_to_sell = int(target_shares * esig.exit_pct) // 100 * 100
                if shares_to_sell <= 0:
                    shares_to_sell = target_shares
                sig = FactorSignal(
                    timestamp=ts,
                    symbol=esig.symbol,
                    direction='SELL',
                    strength=1.0,
                    factor_name=f'ExitEngine.{esig.priority.name}',
                    price=esig.current_price,
                    metadata={
                        'shares': shares_to_sell,
                        'exit_pct': esig.exit_pct,
                        'exit_priority': esig.priority.name,
                        'exit_reason': esig.reason,
                    },
                )
                if self.event_bus is not None:
                    from core.event_bus import SignalEvent
                    self.event_bus.emit(SignalEvent(signal=sig))
                elif self.oms is not None:
                    self.oms.submit_from_signal(sig)
            except Exception as exc:
                logger.error(
                    "[StrategyRunner] failed to emit exit signal for %s: %s",
                    esig.symbol, exc,
                )

    # ------------------------------------------------------------------
    # P0-3: 组合再平衡钩子
    # ------------------------------------------------------------------

    def _maybe_rebalance(self, symbols: List[str], ts: datetime) -> None:
        """
        判断是否需要触发组合再平衡。

        触发条件：
          - 距上次再平衡 >= rebalance_period_days
          - 或某标的实际权重相对目标偏离 >= rebalance_drift_threshold

        触发后：
          - 用过去 rebalance_returns_lookback 天的历史收益率运行 PortfolioOptimizer
          - 对比 target vs current，记录差额到 _last_rebalance_diff
          - dry_run=True 时仅记录日志；否则按差额发射调仓信号
        """
        positions = self._collect_positions()
        if not positions and not symbols:
            return

        # 当前权重（按市值）
        current_mv = {p['symbol']: p['shares'] * p['current_price']
                      for p in positions if p.get('shares', 0) > 0}
        total_mv = sum(current_mv.values())
        current_weights = {
            s: (mv / total_mv) if total_mv > 0 else 0.0
            for s, mv in current_mv.items()
        }

        # 触发判定
        period_due = (
            self._last_rebalance_dt is not None
            and (ts - self._last_rebalance_dt).days >= self.config.rebalance_period_days
        ) or (self._last_rebalance_dt is None and self._run_count > 0)

        drift_due = False
        if self._last_target_weights:
            for sym, target in self._last_target_weights.items():
                actual = current_weights.get(sym, 0.0)
                if abs(actual - target) >= self.config.rebalance_drift_threshold:
                    drift_due = True
                    logger.info(
                        '[StrategyRunner] rebalance drift trigger %s: '
                        'actual=%.2f%% target=%.2f%% threshold=%.2f%%',
                        sym, actual * 100, target * 100,
                        self.config.rebalance_drift_threshold * 100,
                    )
                    break

        if not (period_due or drift_due):
            return

        # 组装收益率矩阵（symbols ∪ 持仓）
        rebalance_pool = sorted(set(symbols) | set(current_mv.keys()))
        if len(rebalance_pool) < 2:
            logger.info('[StrategyRunner] rebalance skipped: <2 symbols in pool')
            return

        returns = self._fetch_returns_matrix(rebalance_pool)
        if returns is None or returns.shape[1] < 2 or len(returns) < 30:
            logger.info('[StrategyRunner] rebalance skipped: insufficient history '
                        '(symbols=%d, days=%d)',
                        returns.shape[1] if returns is not None else 0,
                        len(returns) if returns is not None else 0)
            return

        # 运行优化
        try:
            from core.portfolio_optimizer import PortfolioOptimizer
            opt = PortfolioOptimizer(
                returns=returns,
                cov_method='ledoit_wolf',
                max_weight=self.config.rebalance_max_weight,
            )
            method = self.config.rebalance_method
            if method == 'max_sharpe':
                target_weights = opt.max_sharpe()
            elif method == 'min_variance':
                target_weights = opt.min_variance()
            elif method == 'risk_parity':
                target_weights = opt.risk_parity()
            else:
                logger.warning('[StrategyRunner] unknown rebalance_method=%s, fallback max_sharpe',
                               method)
                target_weights = opt.max_sharpe()
        except Exception as exc:
            logger.warning('[StrategyRunner] PortfolioOptimizer failed: %s', exc)
            return

        target_dict = {s: float(w) for s, w in target_weights.items()}
        # 对比 current 与 target，计算各标的偏离权重
        diff = {
            s: target_dict.get(s, 0.0) - current_weights.get(s, 0.0)
            for s in set(target_dict) | set(current_weights)
        }

        self._last_target_weights = target_dict
        self._last_rebalance_diff = diff
        self._last_rebalance_dt = ts

        trigger = 'periodic' if period_due else 'drift'
        max_drift = max(abs(v) for v in diff.values()) if diff else 0.0
        logger.info(
            '[StrategyRunner] rebalance(%s) triggered method=%s | max_drift=%.2f%% | '
            'targets=%s',
            trigger, method, max_drift * 100,
            {k: round(v, 4) for k, v in target_dict.items()},
        )

        # dry_run：仅记录；否则按差额发射调仓信号
        if not self.config.dry_run:
            self._emit_rebalance_orders(diff, total_mv or 0.0, ts)

    def _emit_rebalance_orders(
        self, diff: Dict[str, float], total_mv: float, ts: datetime,
    ) -> None:
        """
        把权重差额转换为 BUY/SELL 信号并发射。

        diff[s] > 0 → 加仓（BUY）；diff[s] < 0 → 减仓（SELL）
        份额 = |diff| * total_mv / price
        """
        if total_mv <= 0:
            return
        from core.factors.base import Signal as FactorSignal

        for sym, dw in diff.items():
            if abs(dw) < self.config.rebalance_drift_threshold * 0.5:
                continue   # 偏差太小，节省手续费
            try:
                quote = self.data_layer.get_realtime(sym)
                price = float(getattr(quote, 'price', 0) or 0) if quote else 0.0
            except (OSError, ValueError, KeyError, AttributeError) as exc:
                # 实时报价失败 → 跳过本 symbol 的 rebalance
                logger.debug("[StrategyRunner] rebalance get_realtime(%s) failed: %s",
                             sym, exc)
                price = 0.0
            if price <= 0:
                continue

            target_value = abs(dw) * total_mv
            shares = int(target_value / price) // 100 * 100
            if shares <= 0:
                continue
            direction = 'BUY' if dw > 0 else 'SELL'
            sig = FactorSignal(
                timestamp=ts, symbol=sym, direction=direction,
                strength=1.0, factor_name='PortfolioRebalance', price=price,
                metadata={'shares': shares, 'rebalance_diff': dw},
            )
            self._emit_signal(sym, None, direction, price)  # None pr — emit_signal 会跳过 candidates
            # 直接调用 OMS（_emit_signal 需要 PipelineResult 选信号；rebalance 没有 pipeline）
            try:
                if self.event_bus is not None:
                    from core.event_bus import SignalEvent
                    self.event_bus.emit(SignalEvent(signal=sig))
                elif self.oms is not None:
                    self.oms.submit_from_signal(sig)
            except Exception as exc:
                logger.error('[StrategyRunner] rebalance emit %s failed: %s', sym, exc)

    def _fetch_returns_matrix(self, symbols: List[str]) -> Optional[Any]:
        """从 DataLayer 拉取 symbols 的历史日收益率矩阵。"""
        import pandas as pd
        cols = {}
        for sym in symbols:
            try:
                df = self.data_layer.get_bars(
                    sym, days=self.config.rebalance_returns_lookback,
                )
                if df is None or len(df) < 30:
                    continue
                cols[sym] = df['close'].pct_change().dropna()
            except (OSError, ValueError, KeyError) as exc:
                # 数据层失败 → 跳过该 symbol，不影响整个矩阵
                logger.debug("[StrategyRunner] returns matrix skip %s: %s", sym, exc)
                continue
        if not cols:
            return None
        return pd.DataFrame(cols).dropna()

    def _collect_positions(self) -> List[Dict[str, Any]]:
        """优先从 RiskEngine.book 读取持仓快照；否则尝试 OMS；都没有返回空。"""
        if self.risk_engine is not None:
            try:
                positions = []
                for sym, p in self.risk_engine.book.get_all().items():
                    if p.shares <= 0:
                        continue
                    positions.append({
                        'symbol': sym,
                        'shares': p.shares,
                        'avg_price': p.avg_price,
                        'entry_price': p.avg_price,
                        'current_price': p.current_price,
                        'peak_price': p.entry_high,
                        'entry_date': p.entry_date,
                    })
                if positions:
                    return positions
            except Exception as exc:
                # R0-4: 不再静默吞错。RiskEngine.book 读取失败若不出声，
                # 后续再平衡会被当作"没历史数据"静默跳过，无人察觉。
                logger.warning(
                    '[StrategyRunner] read positions from RiskEngine failed: %s',
                    exc,
                )

        if self.oms is not None:
            try:
                return [
                    {
                        'symbol': p.symbol,
                        'shares': p.shares,
                        'avg_price': p.avg_price,
                        'entry_price': p.avg_price,
                        'current_price': p.current_price,
                    }
                    for p in self.oms.broker.get_positions()
                    if p.shares > 0
                ]
            except Exception as exc:
                # R0-4: 同上——OMS 持仓读取失败必须留痕，不能假装"无持仓"。
                logger.warning(
                    '[StrategyRunner] read positions from OMS failed: %s', exc,
                )

        return []
