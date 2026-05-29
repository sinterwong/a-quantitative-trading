"""
order_gate.py — 统一订单执行入口
=================================

所有交易信号必须通过 OrderGate.submit() 提交，禁止绕过直接调 Broker。

职责：
  1. 持仓状态校验（BUY 去重 / SELL 空仓拦截）
  2. 冷却检查（统一 CooldownTracker）
  3. 风控 PreTrade 检查（RiskEngine）
  4. LLM 审核
  5. _can_trade() 模式检查
  6. 计算股数（Kelly + max_position_pct）
  7. 提交给 Broker
  8. 记录日志 + 飞书推送

用法：
    gate = OrderGate(broker, svc, cooldown, risk_engine, llm_service)
    gate.set_can_trade_fn(lambda: trading_mode == 'live')
    result = gate.submit(
        symbol='600519.SH', direction='BUY', price=1800.0,
        source='pipeline', reason='RSI oversold',
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('order_gate')


# ── 信号请求 ──────────────────────────────────────────────────

@dataclass
class OrderRequest:
    """提交给 OrderGate 的订单请求。"""
    symbol: str
    direction: str             # 'BUY' | 'SELL'
    price: float
    source: str = 'unknown'    # 'pipeline' | 'exit_engine' | 'add_position' | 'new_position' | 'rebalance'
    shares: int = 0            # 0 = 由 OrderGate 计算
    reason: str = ''
    factor_name: str = ''
    strength: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── 订单结果 ──────────────────────────────────────────────────

@dataclass
class GateResult:
    """OrderGate 处理结果。"""
    status: str                # 'filled' | 'rejected' | 'simulation'
    symbol: str = ''
    direction: str = ''
    shares: int = 0
    price: float = 0.0
    avg_price: float = 0.0
    reason: str = ''
    source: str = ''
    skip_reason: str = ''      # rejected 时的详细原因


# ── 跳过记录 ──────────────────────────────────────────────────

@dataclass
class SkipRecord:
    """被拒绝/跳过的信号记录（可观测性）。"""
    timestamp: str
    symbol: str
    direction: str
    source: str
    reason: str
    category: str              # 'no_position' | 'already_held' | 'cooldown' | 'risk' | 'llm' | 'simulation' | 'insufficient_cash'


# ── OrderGate ─────────────────────────────────────────────────

class OrderGate:
    """
    统一订单执行入口。

    所有信号（StrategyRunner / ExitEngine / IntradayMonitor / rebalance）
    必须通过此门提交，禁止绕过直接调 broker。

    Parameters
    ----------
    broker : BrokerBase
        订单执行器（PaperBroker）
    svc : PortfolioService
        持仓/现金查询
    cooldown : CooldownTracker
        冷却管理器
    risk_engine : optional
        风控引擎（RiskEngine）
    llm_service : optional
        LLM 审核服务
    max_position_pct : float
        单标的最大仓位占比（默认 0.25）
    kelly_pct_fn : callable
        返回当前 Kelly 仓位比例的函数（默认返回 0.10）
    alert_fn : callable, optional
        飞书推送函数（signature: alert_fn(msg: str)）
    """

    def __init__(
        self,
        broker,
        svc,
        cooldown,
        risk_engine=None,
        llm_service=None,
        max_position_pct: float = 0.25,
        kelly_pct_fn: Optional[Callable[[], float]] = None,
        alert_fn: Optional[Callable[[str], None]] = None,
    ):
        self._broker = broker
        self._svc = svc
        self._cooldown = cooldown
        self._risk_engine = risk_engine
        self._llm = llm_service
        self._max_pos_pct = max_position_pct
        self._kelly_pct_fn = kelly_pct_fn or (lambda: 0.10)
        self._alert_fn = alert_fn
        self._can_trade_fn: Callable[[], bool] = lambda: False
        self._llm_review_fn: Optional[Callable] = None
        self._skip_log: List[SkipRecord] = []
        self._confirm_minute_fn: Optional[Callable] = None
        self._news_sentiment_fn: Optional[Callable] = None

    # ── 配置注入 ──────────────────────────────────────────────

    def set_can_trade_fn(self, fn: Callable[[], bool]):
        """设置交易模式检查函数。"""
        self._can_trade_fn = fn

    def set_llm_review_fn(self, fn: Callable):
        """设置 LLM 审核函数。

        signature: fn(ctx: dict, direction: str) -> (approved: bool, reason: str, confidence: float, size_rec: str)
        """
        self._llm_review_fn = fn

    def set_confirm_minute_fn(self, fn: Callable):
        """设置分钟RSI确认函数。

        signature: fn(symbol: str, direction: str) -> (confirmed: bool, rsi: float, reason: str)
        """
        self._confirm_minute_fn = fn

    def set_news_sentiment_fn(self, fn: Callable):
        """设置新闻情绪检查函数。

        signature: fn(symbol: str) -> (blocked: bool, sentiment: str, confidence: float, summary: str)
        """
        self._news_sentiment_fn = fn

    # ── 核心提交接口 ──────────────────────────────────────────

    def submit(self, req: OrderRequest) -> GateResult:
        """
        统一订单提交入口。

        按顺序执行所有检查，任一环节失败即返回 rejected。
        """
        sym = req.symbol
        direction = req.direction

        # ── 0. 基础校验 ──
        if not sym or not sym.strip():
            return self._reject(req, 'empty symbol', 'validation')
        if direction not in ('BUY', 'SELL'):
            return self._reject(req, f'invalid direction: {direction}', 'validation')

        # ── 1. 持仓状态校验 ──
        try:
            pos = self._svc.get_position(sym)
            has_position = pos is not None and (pos.get('shares', 0) or 0) > 0
        except Exception:
            has_position = False

        if direction == 'BUY' and has_position:
            # BUY 但已有持仓 → 由加仓逻辑处理，此处不拦截
            # 只有来自 pipeline 的信号才检查去重（加仓信号 shares > 0 时跳过此检查）
            if req.source == 'pipeline' and req.shares == 0:
                pass  # pipeline BUY 允许加仓，不拦截

        if direction == 'SELL' and not has_position:
            return self._reject(req, f'no position to sell ({sym})', 'no_position')

        # ── 2. 冷却检查 ──
        cooldown_key = f'{direction}_{sym}'
        if not self._cooldown.can_fire(cooldown_key):
            return self._reject(req, f'cooldown active for {cooldown_key}', 'cooldown')

        # ── 3. 风控 PreTrade 检查 ──
        if self._risk_engine is not None:
            try:
                from core.factors.base import Signal as FactorSignal
                dummy = FactorSignal(
                    timestamp=datetime.now(), symbol=sym, direction=direction,
                    strength=req.strength or 1.0,
                    factor_name=req.source or 'OrderGate',
                    price=req.price,
                )
                result = self._risk_engine.check(dummy)
                if not result.passed:
                    return self._reject(req, f'risk rejected: {result.reason}', 'risk')
            except Exception as e:
                logger.warning('[OrderGate] risk_check failed for %s: %s', sym, e)
                # 风控异常时保守拒绝
                return self._reject(req, f'risk_check_exception: {e}', 'risk')

        # ── 4. 新闻情绪检查（仅 BUY）──
        if direction == 'BUY' and self._news_sentiment_fn is not None:
            try:
                blocked, sent, conf, summ = self._news_sentiment_fn(sym)
                if blocked:
                    return self._reject(req, f'news sentiment bearish ({sent}, conf={conf:.0%})', 'news_sentiment')
            except Exception as e:
                logger.debug('[OrderGate] news_sentiment check failed for %s: %s', sym, e)

        # ── 5. 分钟RSI确认（仅 BUY）──
        if direction == 'BUY' and self._confirm_minute_fn is not None:
            try:
                confirmed, m_rsi, reason = self._confirm_minute_fn(sym, 'BUY')
                if not confirmed:
                    return self._reject(req, f'minute RSI rejected: {reason}', 'minute_rsi')
            except Exception as e:
                logger.debug('[OrderGate] minute confirm failed for %s: %s', sym, e)

        # ── 6. LLM 审核 ──
        if self._llm_review_fn is not None and direction in ('BUY', 'SELL'):
            try:
                ctx = {
                    'symbol': sym,
                    'price': req.price,
                    'signal': direction,
                    'reason': req.reason,
                    'pipeline_score': req.strength,
                    'source': req.source,
                    'positions': self._get_positions_safe(),
                    'cash': self._get_cash_safe(),
                }
                approved, reason, confidence, size_rec = self._llm_review_fn(ctx, direction)
                if not approved:
                    return self._reject(req, f'LLM rejected: {reason} (conf={confidence:.0%})', 'llm')
                if size_rec == 'hold':
                    return self._reject(req, f'LLM hold: {reason}', 'llm')
                if size_rec == 'half':
                    req.shares = max(100, (req.shares or self._calc_shares(sym, req.price)) // 2)
            except Exception as e:
                logger.warning('[OrderGate] LLM review failed for %s: %s', sym, e)

        # ── 7. 计算股数 ──
        shares = req.shares
        if shares <= 0:
            shares = self._calc_shares(sym, req.price)
        if shares < 100:
            return self._reject(req, f'insufficient cash (calculated {shares} shares)', 'insufficient_cash')

        # SELL 时确保不超过持仓
        if direction == 'SELL':
            try:
                pos = self._svc.get_position(sym)
                held = (pos or {}).get('shares', 0) or 0
                shares = min(shares, (held // 100) * 100)
                if shares < 100:
                    return self._reject(req, f'position too small to sell ({held} shares)', 'no_position')
            except Exception:
                pass

        # ── 8. 模式检查 ──
        if not self._can_trade_fn():
            self._deliver_alert(
                f'📋 [{sym}] 模拟模式：信号跳过执行\n'
                f'   方向：{direction} | 股数：{shares} | 价：{req.price:.2f}\n'
                f'   来源：{req.source} | 原因：{req.reason}'
            )
            return GateResult(
                status='simulation', symbol=sym, direction=direction,
                shares=shares, price=req.price, reason='simulation mode',
                source=req.source,
            )

        # ── 9. 执行 ──
        try:
            order_result = self._broker.submit_order(
                symbol=sym, direction=direction,
                shares=shares, price=req.price, price_type='market',
            )
        except Exception as e:
            logger.error('[OrderGate] broker.submit_order failed for %s: %s', sym, e)
            return self._reject(req, f'broker error: {e}', 'broker_error')

        # ── 10. 记录 + 推送 ──
        status = getattr(order_result, 'status', 'unknown')
        avg_price = getattr(order_result, 'avg_price', req.price)

        logger.info(
            '[OrderGate] %s %s %s %d @ %.2f => %s (source=%s)',
            direction, sym, req.source, shares, avg_price, status, req.source,
        )

        self._deliver_alert(
            f'✅ [{sym}] {direction} 成交\n'
            f'   {shares}股 @ {avg_price:.2f} | 来源：{req.source}\n'
            f'   原因：{req.reason}'
        )

        return GateResult(
            status=status, symbol=sym, direction=direction,
            shares=shares, price=req.price, avg_price=avg_price,
            reason='filled', source=req.source,
        )

    # ── 批量提交 ──────────────────────────────────────────────

    def submit_batch(self, requests: List[OrderRequest]) -> List[GateResult]:
        """批量提交订单请求。返回每个请求的结果。"""
        return [self.submit(req) for req in requests]

    # ── 可观测性 ──────────────────────────────────────────────

    @property
    def skip_log(self) -> List[SkipRecord]:
        """返回被拒绝/跳过的信号记录。"""
        return list(self._skip_log)

    def get_recent_skips(self, limit: int = 20) -> List[SkipRecord]:
        """返回最近 N 条跳过记录。"""
        return self._skip_log[-limit:]

    # ── 内部方法 ──────────────────────────────────────────────

    def _reject(self, req: OrderRequest, reason: str, category: str) -> GateResult:
        """记录拒绝并返回 GateResult。"""
        record = SkipRecord(
            timestamp=datetime.now().isoformat(),
            symbol=req.symbol,
            direction=req.direction,
            source=req.source,
            reason=reason,
            category=category,
        )
        self._skip_log.append(record)
        # 保留最近 200 条
        if len(self._skip_log) > 200:
            self._skip_log = self._skip_log[-200:]

        logger.info(
            '[OrderGate] SKIP %s %s: %s (source=%s, category=%s)',
            req.direction, req.symbol, reason, req.source, category,
        )
        return GateResult(
            status='rejected', symbol=req.symbol, direction=req.direction,
            price=req.price, reason=reason, source=req.source, skip_reason=reason,
        )

    def _calc_shares(self, symbol: str, price: float) -> int:
        """计算可买股数（Kelly + max_position_pct 约束）。"""
        try:
            cash = self._svc.get_cash()
        except Exception:
            cash = 0
        if cash <= 0 or price <= 0:
            return 0

        kelly_pct = self._kelly_pct_fn()
        kelly_cost = cash * kelly_pct

        try:
            equity = self._svc.get_total_equity()
        except Exception:
            equity = cash
        try:
            existing_pos = self._svc.get_position(symbol)
            existing_shares = (existing_pos or {}).get('shares', 0) or 0
        except Exception:
            existing_shares = 0

        max_pos_value = equity * self._max_pos_pct
        existing_value = existing_shares * price
        max_pos_cost = max(0.0, max_pos_value - existing_value)

        budget = min(kelly_cost, max_pos_cost)
        raw_shares = int(budget / price)
        shares = (raw_shares // 100) * 100
        return shares if shares >= 100 else 0

    def _deliver_alert(self, msg: str):
        """推送飞书消息。"""
        if self._alert_fn:
            try:
                self._alert_fn(msg)
            except Exception as e:
                logger.debug('[OrderGate] alert delivery failed: %s', e)

    def _get_positions_safe(self) -> list:
        """安全获取持仓列表。"""
        try:
            return self._svc.get_positions() or []
        except Exception:
            return []

    def _get_cash_safe(self) -> float:
        """安全获取现金。"""
        try:
            return self._svc.get_cash() or 0.0
        except Exception:
            return 0.0
