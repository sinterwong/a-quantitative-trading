"""
execution.py — IntradayMonitor 订单执行 Mixin。

负责: 交易模式（simulation/live）持久化、智能路由（TWAP/VWAP 拆单）、
      信号→订单转换、市价卖出辅助。
"""

import os
import json
import logging
from datetime import datetime

from ..signals import SignalAlert, confirm_signal_minute

logger = logging.getLogger('intraday_monitor')


# backend/ 根目录(用于读写 trading_mode.json)
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ExecutionMixin:
    """订单提交相关逻辑(模式管理、智能路由、信号→订单)。"""

    # 信号 → 订单方向映射(涨跌停类不交易)
    SIGNAL_TO_ORDER = {
        'RSI_BUY':     'BUY',
        'WATCH_BUY':   'BUY',
        'BUY':         'BUY',      # Pipeline combined_score 驱动
        'RSI_SELL':    'SELL',
        'WATCH_SELL':  'SELL',
        'SELL':        'SELL',     # Pipeline combined_score 驱动
    }
    # 涨跌停类信号不交易(无法以合理价格买入/卖出)
    NO_TRADE_SIGNALS = {
        'LIMIT_UP', 'LIMIT_DOWN',
        'LIMIT_RISK_UP', 'LIMIT_RISK_DOWN',
        'WATCH_LIMIT_UP', 'WATCH_LIMIT_DOWN',
        'VOLATILE',
    }

    # ── 交易模式 ───────────────────────────────────────────

    @property
    def trading_mode(self) -> str:
        return self._trading_mode

    def set_trading_mode(self, mode: str):
        """动态切换交易模式:'simulation' | 'live'"""
        old = self._trading_mode
        self._trading_mode = mode
        self._save_trading_mode()
        logger.info('Trading mode changed: %s → %s', old, mode)

    def _load_trading_mode(self):
        mode_file = os.path.join(_BACKEND_DIR, 'trading_mode.json')
        if os.path.exists(mode_file):
            try:
                with open(mode_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._trading_mode = data.get('mode', 'simulation')
                logger.info('Loaded trading mode: %s', self._trading_mode)
            except Exception as e:
                logger.warning('Failed to load trading_mode.json: %s', e)
                self._trading_mode = 'simulation'
        else:
            self._trading_mode = 'simulation'

    def _save_trading_mode(self):
        mode_file = os.path.join(_BACKEND_DIR, 'trading_mode.json')
        try:
            with open(mode_file, 'w', encoding='utf-8') as f:
                json.dump({'mode': self._trading_mode, 'updated_at': datetime.now().isoformat()},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning('Failed to save trading_mode.json: %s', e)

    def _can_trade(self) -> bool:
        """检查是否允许执行实单(Broker下单)。simulation 模式返回 False。"""
        return self._trading_mode == 'live'

    # ── 算法单逐 slice 复检 ─────────────────────────────────

    def _get_pretrade_risk_engine(self):
        """返回 StrategyRunner 上的 RiskEngine,无则 None(降级:不校验)。"""
        try:
            sr = getattr(self, '_strategy_runner', None)
            return getattr(sr, 'risk_engine', None) if sr is not None else None
        except Exception:
            return None

    def _check_slice_pretrade(self, risk_engine, symbol: str, direction: str,
                              price: float):
        """对每个 slice 重做一次 PreTrade,返回 (passed, reason)。

        前几个 slice 成交后头寸/账面权益已变,母单时的校验失效。
        这里构造同样的 Signal 重新走 risk_engine.check() —— 复用风控配置,
        避免规则散落在两处。任何检查异常一律视为拒绝(保守)。
        """
        try:
            from core.factors.base import Signal as _Sig
            sig = _Sig(
                timestamp=datetime.now(), symbol=symbol, direction=direction,
                strength=1.0, factor_name='AlgoSlice', price=price,
            )
            rr = risk_engine.check(sig)
            if rr.passed:
                return True, ''
            return False, rr.reason
        except Exception as exc:
            return False, f'PreTrade raised: {exc}'

    # ── 算法路由（P1-7）─────────────────────────────────────

    def _algo_config(self):
        """读取 trading.yaml execution 节点。失败时返回内联默认值。"""
        try:
            from core.config import load_config
            return load_config().execution
        except Exception:
            class _Default:
                enable_algo_routing = True
                algo_threshold_amount = 500_000.0
                algo_threshold_shares = 10_000
                algo_method = 'TWAP'
                algo_duration_minutes = 30
                algo_slice_interval = 5
            return _Default()

    def _submit_with_routing(
        self, symbol: str, direction: str, shares: int,
        price: float, price_type: str = 'market',
    ):
        """
        智能路由（P1-7）：
        - 订单金额 < threshold 且 股数 < shares_threshold → 直接走 self._broker.submit_order
        - 否则 → 用 TWAPExecutor / VWAPExecutor 生成 N 个子单同步执行，聚合返回

        所有路径都通过 self._broker 撮合（PaperBroker 即时成交），保持持仓口径一致。
        每个子单按 sleep(0) 顺序执行 — 真实 Futu 接入后将是 N×interval 的真实拆单。
        """
        ec = self._algo_config()
        order_value = shares * (price or 0.0)

        if (
            not getattr(ec, 'enable_algo_routing', True)
            or getattr(ec, 'algo_method', 'TWAP') == 'NONE'
            or (order_value < ec.algo_threshold_amount
                and shares < ec.algo_threshold_shares)
        ):
            return self._broker.submit_order(
                symbol=symbol, direction=direction,
                shares=shares, price=price, price_type=price_type,
            )

        # 大单：拆单
        try:
            method = (getattr(ec, 'algo_method', 'TWAP') or 'TWAP').upper()
            if method == 'VWAP':
                from core.execution.vwap_executor import VWAPExecutor
                executor = VWAPExecutor(
                    symbol=symbol, direction=direction,
                    total_shares=shares,
                    duration_minutes=ec.algo_duration_minutes,
                    reference_price=price,
                    slice_interval=ec.algo_slice_interval,
                )
            else:
                from core.execution.twap_executor import TWAPExecutor
                executor = TWAPExecutor(
                    symbol=symbol, direction=direction,
                    total_shares=shares,
                    duration_minutes=ec.algo_duration_minutes,
                    reference_price=price,
                    slice_interval=ec.algo_slice_interval,
                )
            slices = executor.generate_slices()
        except Exception as exc:
            logger.warning('algo routing failed (fallback to single): %s', exc)
            return self._broker.submit_order(
                symbol=symbol, direction=direction,
                shares=shares, price=price, price_type=price_type,
            )

        if not slices:
            return self._broker.submit_order(
                symbol=symbol, direction=direction,
                shares=shares, price=price, price_type=price_type,
            )

        # 同步执行子单（PaperBroker 即时撮合）。
        # 每个 slice 前重新走一次 PreTrade —— 前几个 slice 成交后头寸已变化,
        # 必须重新校验,否则就会出现"父单通过但后续 slice 越限"的场景。
        children = []
        total_filled = 0
        total_value = 0.0
        # getattr 兜底:部分单元测试用 _MockMonitor 只复制了 _submit_with_routing,
        # 没有挂载新加的 helper。生产路径(走 ExecutionMixin)正常拿到。
        get_re = getattr(self, '_get_pretrade_risk_engine', lambda: None)
        check_slice = getattr(self, '_check_slice_pretrade', None)
        risk_engine = get_re()
        for sl in slices:
            sl_shares = (sl.target_shares // 100) * 100
            if sl_shares < 100:
                continue

            if risk_engine is not None and check_slice is not None:
                passed, reason = check_slice(
                    risk_engine, symbol, direction, price,
                )
                if not passed:
                    logger.warning(
                        'algo slice PreTrade rejected %s slice=%d: %s — stop emitting further slices',
                        symbol, len(children), reason,
                    )
                    break

            try:
                r = self._broker.submit_order(
                    symbol=symbol, direction=direction,
                    shares=sl_shares, price=price, price_type=price_type,
                )
            except Exception as exc:
                logger.warning('algo child order error %s slice=%d: %s',
                               symbol, len(children), exc)
                continue
            children.append(r)
            if getattr(r, 'status', '') == 'filled':
                total_filled += r.filled_shares
                total_value += r.filled_shares * r.avg_price

        if not children:
            return self._broker.submit_order(
                symbol=symbol, direction=direction,
                shares=shares, price=price, price_type=price_type,
            )

        # 聚合 OrderResult
        from backend.services.broker import OrderResult
        avg_price = total_value / total_filled if total_filled > 0 else (price or 0.0)
        signal_price = price or 0.0
        slip_bps = (
            (avg_price - signal_price) / signal_price * 10_000
            if signal_price > 0 else 0.0
        )
        first = children[0]
        last = children[-1]

        agg = OrderResult(
            order_id=f'ALGO-{first.order_id}',
            status='filled' if total_filled >= shares * 0.99 else 'partial',
            symbol=symbol,
            direction=direction,
            submitted_shares=shares,
            filled_shares=total_filled,
            avg_price=round(avg_price, 4),
            signal_price=signal_price,
            slippage_bps=round(slip_bps, 2),
            submitted_at=getattr(first, 'submitted_at', ''),
            filled_at=getattr(last, 'filled_at', ''),
        )

        logger.info(
            'algo route %s %s %d shares → %d children, fill avg=%.3f slip=%.2fbps',
            method, symbol, shares, len(children), avg_price, slip_bps,
        )

        return agg

    # ── 信号 → 订单 ───────────────────────────────────────

    def _submit_order_for_signal(self, alert: SignalAlert):
        """将信号转换为订单并提交(含分钟级二次确认)。"""
        signal = alert.signal

        if signal in self.NO_TRADE_SIGNALS:
            self._record_skip(alert.symbol, f'no-trade signal: {signal}', 'no_trade_signal')
            logger.debug('Skipping order for signal %s (no-trade signal)', signal)
            return None

        direction = self.SIGNAL_TO_ORDER.get(signal)
        if not direction:
            self._record_skip(alert.symbol, f'no order mapping for {signal}', 'no_mapping')
            logger.debug('No order mapping for signal %s', signal)
            return None

        # 组合警告状态联动：回撤超 dd_warn / dd_stop 时禁止 BUY（含持仓加仓）。
        # SELL 信号不受影响，正常通过 ExitEngine 执行。
        if direction == 'BUY' and (self._risk_warn_fired or self._risk_stop_fired):
            self._record_skip(
                alert.symbol,
                f'portfolio drawdown active (warn={self._risk_warn_fired} stop={self._risk_stop_fired})',
                'portfolio_warn',
            )
            logger.info('BUY %s blocked: portfolio drawdown active', alert.symbol)
            return None

        # 分钟确认(仅对 BUY 信号)
        if direction == 'BUY':
            confirmed, m_rsi, reason = confirm_signal_minute(alert.symbol, 'BUY')
            logger.info('Minute confirm %s %s: %s', alert.symbol, alert.signal, reason)
            if not confirmed:
                self._record_skip(alert.symbol, reason, 'minute_rsi_reject')
                self._deliver_alert(
                    f'⚠️ [{alert.symbol}] 持仓信号触发但分钟RSI拒绝追高\n'
                    f'   现价:{alert.price:.2f} | {reason}'
                )
                return None

        # Method A: news sentiment check before buying (existing position)
        if self._llm is not None and direction == 'BUY':
            blocked, sent, conf, summ = self._check_news_sentiment(alert.symbol)
            if blocked:
                self._deliver_alert(
                    f'⛔[{alert.symbol}] 新闻情绪利空，暂停买入\n'
                    f'   情绪：{sent}（置信度 {conf:.0%}）\n'
                    f'   摘要：{summ[:80] if summ else "无"}\n'
                )
                return None

        # ══ LLM 终极审核 ══
        size_rec = 'full'
        if direction in ('BUY', 'SELL'):
            llm_approved, llm_reason, llm_conf, size_rec = self._llm_review_signal(alert, direction)
            self._record_llm_review(alert.symbol, direction, llm_approved,
                                    llm_reason, llm_conf)
            if not llm_approved:
                self._deliver_alert(
                    f'❌ [{alert.symbol}] LLM 审核否决 {direction}\n'
                    f'   理由：{llm_reason}\n'
                    f'   置信度：{llm_conf:.0%}'
                )
                logger.info('LLM rejected %s %s: %s (conf=%.0f)',
                           direction, alert.symbol, llm_reason, llm_conf)
                return None
            logger.info('LLM approved %s %s: %s (conf=%.0f%%)',
                       direction, alert.symbol, llm_reason, llm_conf * 100)

        # 优先使用 alert 携带的股数（如 _submit_market_sell 传入），
        # 否则通过 Kelly 计算
        explicit_shares = getattr(alert, 'shares', 0) or 0
        shares = explicit_shares if explicit_shares > 0 else self._calc_shares(alert.symbol, alert.price)
        if shares < 100:
            self._record_skip(alert.symbol, f'insufficient cash (calculated {shares} shares)', 'kelly_insufficient')
            logger.warning('Insufficient cash for %s: calculated %d shares (min 100)',
                           alert.symbol, shares)
            return None

        # 卖出时用全部持仓（若无显式股数）
        if direction == 'SELL':
            pos = self._svc.get_position(alert.symbol)
            if not pos or pos.get('shares', 0) == 0:
                logger.debug('No position to sell for %s', alert.symbol)
                return None
            held = (pos['shares'] // 100) * 100
            if explicit_shares > 0:
                # 显式股数：取 min(请求, 持仓)，再取整到 100 的倍数
                shares = (min(explicit_shares, held) // 100) * 100
            else:
                shares = held
            if size_rec == 'hold':
                logger.info('LLM SELL hold recommended for %s', alert.symbol)
                return None
            if size_rec == 'half':
                shares = max(100, shares // 2)

        # 提交订单
        try:
            if not self._can_trade():
                self._record_skip(alert.symbol, 'simulation mode', 'simulation')
                self._deliver_alert(
                    f'📋 [{alert.symbol}] 模拟模式:持仓信号跳过执行\n'
                    f'   方向:{direction} | 股数:{shares} | 价:{alert.price:.2f}\n'
                    f'   信号:{signal}(切换到"实盘"模式后生效)'
                )
                logger.info('Simulation mode: skipped %s %s %d @ %.2f',
                            direction, alert.symbol, shares, alert.price)
                return None
            result = self._submit_with_routing(
                symbol=alert.symbol,
                direction=direction,
                shares=shares,
                price=alert.price,
                price_type='market',
            )
            logger.info('Auto-order: %s %s %d @ %.2f => %s',
                        direction, alert.symbol, shares, alert.price, result.status)
            self._record_signal(alert.symbol, direction, alert.price,
                                alert.reason, result.status)
            return result
        except Exception as e:
            logger.error('Order submission failed for %s: %s', alert.symbol, e)
            self._record_signal(alert.symbol, direction, alert.price,
                                alert.reason, f'error: {e}')
            return None

    def _submit_market_sell(self, sym, shares, reason=""):
        """Helper: 市价卖出（供 _check_sector_concentration 行业减仓使用）。"""
        class _F:
            def __init__(self, s, sh, r):
                self.symbol = s; self.shares = sh; self.signal = "RSI_SELL"
                self.price = 0.0; self.reason = r; self.direction = "SELL"
        self._submit_order_for_signal(_F(sym, shares, reason))
