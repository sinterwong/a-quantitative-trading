"""
core/async_runner.py — asyncio 驱动的策略主循环（P3-B）

替换 StrategyRunner 中的 time.sleep() 轮询和同步 EventBus，
支持真正并发的多标的数据获取和信号处理。

核心改进：
  - asyncio.gather() 并发获取行情：N 标的延迟 N×200ms → 200ms
  - asyncio.Queue 替代线程 Queue，零锁竞争
  - run_sync() / run_once_sync() 供非 async 调用者使用
  - 与 StrategyRunner.RunResult / RunnerConfig 完全兼容

用法（async）：
    import asyncio
    from core.async_runner import AsyncStrategyRunner
    from core.strategy_runner import RunnerConfig

    cfg = RunnerConfig(symbols=['600519.SH', '000858.SZ'], pipeline=pipeline)
    runner = AsyncStrategyRunner(cfg, data_layer=dl)
    asyncio.run(runner.run_loop())

用法（非 async 代码）：
    runner.run_sync(duration=3600)      # 运行1小时后自动停止
    results = runner.run_once_sync()    # 单轮扫描
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.data_layer import DataLayer, get_data_layer
from core.factor_pipeline import FactorPipeline, PipelineResult
from core.regime import RegimeInfo, get_regime
from core.strategy_runner import RunnerConfig, RunResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AsyncEventBus
# ---------------------------------------------------------------------------

class AsyncEventBus:
    """
    asyncio 版事件总线。

    用 asyncio.Queue 替代 threading.Queue，在 event loop 内零锁竞争。
    subscribe() 注册协程处理器，emit() 将事件放入队列，
    start_consuming() 消费并调用所有 handler。
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._handlers: Dict[str, List[Any]] = {}  # event_type → [coro_func]

    def subscribe(self, event_type: str, handler) -> None:
        """注册处理器（同步函数或协程均可）。"""
        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event_type: str, payload: Any) -> None:
        """异步放入队列。"""
        await self._queue.put((event_type, payload))

    def emit_nowait(self, event_type: str, payload: Any) -> None:
        """非阻塞放入队列（丢弃满队列）。"""
        try:
            self._queue.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            logger.warning('[AsyncEventBus] queue full, event dropped: %s', event_type)

    async def start_consuming(self) -> None:
        """持续消费队列，调用所有已注册 handler（协程优先）。"""
        while True:
            event_type, payload = await self._queue.get()
            for handler in self._handlers.get(event_type, []):
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(payload)
                    else:
                        handler(payload)
                except Exception as exc:
                    logger.error(
                        '[AsyncEventBus] handler error (%s): %s', event_type, exc
                    )
            self._queue.task_done()

    async def drain(self) -> None:
        """等待队列中所有事件处理完毕。"""
        await self._queue.join()

    @property
    def qsize(self) -> int:
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# AsyncStrategyRunner
# ---------------------------------------------------------------------------

class AsyncStrategyRunner:
    """
    asyncio 驱动的策略主循环。

    与 StrategyRunner 完全 API 兼容，但底层用 asyncio 实现：
      - run_once()        — async，gather 所有标的并发执行
      - run_loop()        — async，asyncio.sleep() 替代 time.sleep()
      - run_once_sync()   — 同步包装（可从普通代码调用）
      - run_sync()        — 同步主循环（可从普通代码调用）
      - stop()            — 线程安全，设置 asyncio.Event

    Parameters
    ----------
    config      : RunnerConfig（与 StrategyRunner 相同）
    data_layer  : DataLayer 实例（需支持 async 调用，否则 run_in_executor）
    event_bus   : 可选，AsyncEventBus；未提供时只 log
    """

    def __init__(
        self,
        config: RunnerConfig,
        data_layer: Optional[DataLayer] = None,
        event_bus: Optional[AsyncEventBus] = None,
        risk_engine=None,
        oms=None,
    ) -> None:
        self.config = config
        self.data_layer = data_layer or get_data_layer()
        self.event_bus = event_bus
        self.risk_engine = risk_engine
        self.oms = oms

        self._stop_event: Optional[asyncio.Event] = None
        self._run_count = 0
        self._last_results: List[RunResult] = []
        self._current_regime: Optional[RegimeInfo] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------

    async def run_once(self) -> List[RunResult]:
        """
        对所有标的并发执行一轮扫描。

        所有标的的数据获取和因子计算通过 asyncio.gather() 并发执行。
        """
        symbols = self._resolve_symbols()
        ts = datetime.now()

        # 1. 检测市场环境（单次，全轮复用）
        if self.config.regime_aware:
            try:
                loop = asyncio.get_event_loop()
                self._current_regime = await loop.run_in_executor(None, get_regime)
                logger.info(
                    '[AsyncRunner] Regime=%s | %s',
                    self._current_regime.regime,
                    self._current_regime.reason,
                )
            except Exception as exc:
                logger.warning('[AsyncRunner] get_regime failed: %s', exc)
                self._current_regime = None

        # 2. 并发处理所有标的
        tasks = [self._process_symbol_async(sym, ts) for sym in symbols]
        results: List[RunResult] = await asyncio.gather(*tasks, return_exceptions=False)

        self._run_count += 1
        self._last_results = results
        acted = sum(1 for r in results if r.acted)
        logger.info(
            '[AsyncRunner] run #%d: %d symbols, %d actions (concurrent)',
            self._run_count, len(results), acted,
        )
        return results

    async def run_loop(self, duration: Optional[float] = None) -> None:
        """
        生产主循环（async）。

        Parameters
        ----------
        duration : 最大运行秒数（None = 无限循环直到 stop()）
        """
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_event_loop()

        start = time.monotonic()
        logger.info(
            '[AsyncRunner] Starting loop (interval=%ds, dry_run=%s)',
            self.config.interval,
            self.config.dry_run,
        )

        while not self._stop_event.is_set():
            if duration is not None and (time.monotonic() - start) >= duration:
                break

            try:
                await self.run_once()
            except Exception as exc:
                logger.exception('[AsyncRunner] run_once error: %s', exc)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.interval,
                )
            except asyncio.TimeoutError:
                pass  # 正常超时，继续下一轮

        logger.info('[AsyncRunner] Stopped after %d runs.', self._run_count)

    def stop(self) -> None:
        """从任意线程安全地停止 run_loop()。"""
        if self._stop_event and self._loop:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    # ------------------------------------------------------------------
    # Sync wrappers（供非 async 代码调用）
    # ------------------------------------------------------------------

    def run_once_sync(self) -> List[RunResult]:
        """同步版 run_once()，在新 event loop 中执行。"""
        return asyncio.run(self.run_once())

    def run_sync(self, duration: Optional[float] = None) -> None:
        """
        同步版 run_loop()，阻塞直到 stop() 或达到 duration 秒。

        Parameters
        ----------
        duration : 最大运行秒数（None = 无限循环直到 KeyboardInterrupt）
        """
        try:
            asyncio.run(self.run_loop(duration=duration))
        except KeyboardInterrupt:
            logger.info('[AsyncRunner] KeyboardInterrupt received.')

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def run_count(self) -> int:
        return self._run_count

    @property
    def last_results(self) -> List[RunResult]:
        return list(self._last_results)

    @property
    def current_regime(self) -> Optional[RegimeInfo]:
        return self._current_regime

    # ------------------------------------------------------------------
    # Internal: per-symbol async processing
    # ------------------------------------------------------------------

    async def _process_symbol_async(self, symbol: str, ts: datetime) -> RunResult:
        """
        异步处理单个标的。

        DataLayer 的 get_bars / get_realtime 是同步 I/O，
        通过 run_in_executor 移入线程池，不阻塞 event loop。
        """
        loop = asyncio.get_event_loop()

        # 1. 异步获取历史 K 线
        try:
            data = await loop.run_in_executor(
                None,
                lambda: self.data_layer.get_bars(symbol, days=self.config.bars_lookback),
            )
        except Exception as exc:
            logger.warning('[AsyncRunner] get_bars(%s) failed: %s', symbol, exc)
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

        # 2. 异步获取实时报价
        price: Optional[float] = None
        try:
            quote = await loop.run_in_executor(
                None,
                lambda: self.data_layer.get_realtime(symbol),
            )
            if quote:
                price = quote.get('price') or quote.get('close')
        except Exception:
            pass

        # 3. 因子流水线（CPU 密集，同样放入 executor）
        try:
            pr = await loop.run_in_executor(
                None,
                lambda: self.config.pipeline.run(symbol=symbol, data=data, price=price),
            )
        except Exception as exc:
            logger.warning('[AsyncRunner] pipeline(%s) failed: %s', symbol, exc)
            return RunResult(
                symbol=symbol, timestamp=ts,
                pipeline_result=None,
                action='ERROR', reason=f'pipeline failed: {exc}',
            )

        # 4. 信号判断（同 StrategyRunner，含 Regime 调整）
        score = pr.combined_score
        dominant = pr.dominant_signal
        effective_threshold = self.config.signal_threshold
        regime = self._current_regime

        if regime is not None:
            effective_threshold *= regime.signal_threshold_multiplier
            if not regime.allow_new_buys and dominant == 'BUY':
                return RunResult(
                    symbol=symbol, timestamp=ts, pipeline_result=pr,
                    action='SKIPPED',
                    reason=f'regime_bear_no_buy(regime={regime.regime})',
                    metadata={'combined_score': score, 'regime': regime.regime},
                )

        if abs(score) < effective_threshold or dominant == 'HOLD':
            return RunResult(
                symbol=symbol, timestamp=ts, pipeline_result=pr,
                action='NONE',
                reason=f'score={score:.4f} below threshold={effective_threshold:.4f}',
                metadata={'combined_score': score, 'dominant': dominant},
            )

        # 5. 用户回调拦截
        if self.config.on_signal:
            try:
                allow = self.config.on_signal(symbol, pr, self)
                if not allow:
                    return RunResult(
                        symbol=symbol, timestamp=ts, pipeline_result=pr,
                        action='SKIPPED', reason='blocked_by_on_signal_hook',
                        metadata={'combined_score': score},
                    )
            except Exception as exc:
                logger.warning('[AsyncRunner] on_signal hook error: %s', exc)

        # 6. 风控检查
        risk_ok, risk_reason = self._risk_check(symbol, dominant, price or 0.0)
        if not risk_ok:
            return RunResult(
                symbol=symbol, timestamp=ts, pipeline_result=pr,
                action='SKIPPED', reason=f'risk_rejected: {risk_reason}',
                metadata={'combined_score': score},
            )

        # 7. 执行（dry_run / live）
        action = dominant
        if self.config.dry_run:
            logger.info(
                '[AsyncRunner DRY] %s %s | score=%.4f | price=%s',
                action, symbol, score, price,
            )
        else:
            await self._emit_signal_async(symbol, pr, dominant, price or 0.0)

        return RunResult(
            symbol=symbol, timestamp=ts, pipeline_result=pr,
            action=action, reason='signal_triggered',
            metadata={
                'combined_score': score,
                'dry_run': self.config.dry_run,
                'signals_count': len(pr.signals),
            },
        )

    # ------------------------------------------------------------------
    # Signal emission (async)
    # ------------------------------------------------------------------

    async def _emit_signal_async(
        self,
        symbol: str,
        pr: PipelineResult,
        direction: str,
        price: float,
    ) -> None:
        """向 AsyncEventBus 发射信号，或调用 OMS。"""
        candidates = [s for s in pr.signals if s.direction == direction]
        if not candidates:
            return
        top_signal = max(candidates, key=lambda s: s.strength)

        if self.event_bus is not None:
            await self.event_bus.emit('signal', top_signal)
        elif self.oms is not None:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self.oms.submit_from_signal(top_signal),
                )
            except Exception as exc:
                logger.error('[AsyncRunner] OMS submit failed: %s', exc)
        else:
            logger.warning(
                '[AsyncRunner] No EventBus/OMS configured, signal dropped: %s %s',
                direction, symbol,
            )

    # ------------------------------------------------------------------
    # Risk check（同步，在 executor 里已安全）
    # ------------------------------------------------------------------

    def _risk_check(self, symbol: str, direction: str, price: float):
        if self.risk_engine is None:
            return True, ''
        try:
            from core.factors.base import Signal as FactorSignal
            dummy = FactorSignal(
                timestamp=datetime.now(), symbol=symbol,
                direction=direction, strength=1.0,
                factor_name='AsyncRunner', price=price,
            )
            result = self.risk_engine.check(dummy)
            return result.passed, result.reason
        except Exception as exc:
            logger.warning('[AsyncRunner] risk_check error: %s', exc)
            return True, ''

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_symbols(self) -> List[str]:
        s = self.config.symbols
        if callable(s):
            return list(s())
        return list(s)

    # ------------------------------------------------------------------
    # Concurrency metrics
    # ------------------------------------------------------------------

    async def benchmark_concurrency(self, n_rounds: int = 3) -> Dict[str, float]:
        """
        对比串行 vs 并发耗时（用于验证 asyncio 加速效果）。

        Returns
        -------
        {'serial_ms': float, 'concurrent_ms': float, 'speedup': float}
        """
        import time as _time

        symbols = self._resolve_symbols()
        if not symbols:
            return {'serial_ms': 0, 'concurrent_ms': 0, 'speedup': 1.0}

        ts = datetime.now()

        # 串行
        t0 = _time.perf_counter()
        for _ in range(n_rounds):
            for sym in symbols:
                await self._process_symbol_async(sym, ts)
        serial_ms = (_time.perf_counter() - t0) * 1000 / n_rounds

        # 并发
        t1 = _time.perf_counter()
        for _ in range(n_rounds):
            tasks = [self._process_symbol_async(sym, ts) for sym in symbols]
            await asyncio.gather(*tasks)
        concurrent_ms = (_time.perf_counter() - t1) * 1000 / n_rounds

        speedup = serial_ms / max(concurrent_ms, 0.001)
        logger.info(
            '[AsyncRunner] benchmark: serial=%.1fms concurrent=%.1fms speedup=%.1fx',
            serial_ms, concurrent_ms, speedup,
        )
        return {
            'serial_ms': round(serial_ms, 2),
            'concurrent_ms': round(concurrent_ms, 2),
            'speedup': round(speedup, 2),
        }
