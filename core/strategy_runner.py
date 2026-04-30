"""
StrategyRunner — 策略主循环

替代散落的 cron job / IntradayMonitor，成为唯一的策略驱动入口。

数据流：
  DataLayer.get_bars() → FactorPipeline.run() → RiskEngine.check()
  → SignalEvent → OMS.submit_from_signal()

两种运行模式：
  run_once()  — 单轮扫描（回测/测试/手动触发）
  run_loop()  — 生产主循环（内部 sleep + 线程安全停止）

dry_run=True 时只打印信号，不真正下单（调试首选）。

用法（生产）：
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

    # ------------------------------------------------------------------
    # Internal: per-symbol processing
    # ------------------------------------------------------------------

    def _process_symbol(self, symbol: str, ts: datetime) -> RunResult:
        """处理单个标的，返回 RunResult。"""
        # 1. 获取历史 K 线
        try:
            data = self.data_layer.get_bars(symbol, days=self.config.bars_lookback)
        except Exception as exc:
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
            if quote:
                price = quote.get('price') or quote.get('close')
        except Exception:
            pass  # 用 DataFrame 末行 close 作为 fallback，由 pipeline 处理

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
