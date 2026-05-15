"""
signaling.py — IntradayMonitor 信号生成 Mixin。

负责: 调 use case 生成新仓 / 加仓信号、主循环 _check_and_push 编排。
"""

import logging
from datetime import datetime

logger = logging.getLogger('intraday_monitor')


# ── 信号阈值（统一管理，禁止散落硬编码）────────────────────
# 新仓建仓阈值：watchlist 标的 pipeline_score 超过此值才考虑建仓。
# 与 RunnerConfig.signal_threshold 对齐，确保 StrategyRunner 与 IntradayMonitor
# 两侧使用相同基线。
BUY_THRESHOLD_NEW = 0.50
# 持仓加仓阈值：已有持仓的标的 pipeline_score 超过此值视为加仓积累信号。
# 低于建仓阈值，因为加仓的边际成本（已知风险敞口）小于建仓。
BUY_THRESHOLD_ADD = 0.30


class SignalingMixin:
    """信号生成 / 主循环编排逻辑。"""

    def _check_new_positions(self, now: datetime):
        """
        检查动态选股列表中的标的，如有买入信号则自动建仓。

        唯一信号来源：StrategyRunner.last_scores（FactorPipeline 动态 IC 加权）。
        evaluate_signal() 降级分支已删除（消除双信号并行架构隐患）。
        """
        # 组合警告状态联动：回撤超 dd_warn / dd_stop 时禁止所有新仓建仓，
        # 避免 ExitEngine 刚释放的现金被立即重新分配出去（违背风控初衷）。
        if self._risk_warn_fired or self._risk_stop_fired:
            logger.info(
                '_check_new_positions: portfolio drawdown active '
                '(warn=%s stop=%s), skipping all new buys',
                self._risk_warn_fired, self._risk_stop_fired,
            )
            return

        from services.signals import confirm_signal_minute, fetch_realtime
        from core.use_cases.intraday_signals import (
            IntradaySignalRequest, generate_intraday_signals,
        )
        watched = self._get_watched_symbols()
        if not watched:
            return

        # 获取 pipeline scores（无 StrategyRunner 时直接跳过，不降级到 RSI 硬编码）
        pipeline_scores: dict = {}
        if self._strategy_runner is not None:
            try:
                pipeline_scores = self._strategy_runner.last_scores
            except Exception:
                pass

        if not pipeline_scores:
            logger.warning(
                '_check_new_positions: no pipeline scores (runner=%s), skipping all symbols. '
                'Check that StrategyRunner is running and FactorPipeline is producing output.',
                self._strategy_runner is not None
            )
            return

        threshold = getattr(self._strategy_runner.config, 'signal_threshold', 0.5)

        # P2-3: 信号筛选下沉到 use case 层(纯逻辑,易测)
        sig_resp = generate_intraday_signals(IntradaySignalRequest(
            watched_symbols=list(watched),
            pipeline_scores=pipeline_scores,
            threshold=threshold,
        ))

        for cand in sig_resp.candidates:
            sym = cand.symbol
            score = cand.score
            # 冷却：每天每个标的只尝试一次（用 new_ 前缀区分）
            if not self._cooldown.can_fire(f'new_{sym}'):
                continue
            try:
                try:
                    rt = fetch_realtime(sym)
                    price = float(rt.get('price', 0)) if rt else 0
                except Exception:
                    price = 0
                if price <= 0:
                    continue

                signal_reason = cand.reason
                logger.info('Pipeline %s score=%.3f > threshold=%.3f', sym, score, threshold)

                # 分钟确认
                confirmed, m_rsi, reason = confirm_signal_minute(sym, 'BUY')
                logger.info('DynamicSelector %s @ %.2f: minute_rsi=%s → %s',
                           sym, price,
                           f'{m_rsi:.0f}' if m_rsi else 'N/A', reason)
                if not confirmed:
                    self._deliver_alert(
                        f'🚫 [{sym}] 动态选股触发但分钟RSI拒绝建仓\n'
                        f'   现价：{price:.2f} | {reason}'
                    )
                    continue

                # 新闻情绪检查
                if self._llm is not None:
                    blocked, sent, conf, summ = self._check_news_sentiment(sym)
                    if blocked:
                        self._deliver_alert(
                            f'⛔[{sym}] 新闻情绪利空，拒绝建仓\n'
                            f'   情绪：{sent}（置信度 {conf:.0%}）\n'
                            f'   摘要：{summ[:80] if summ else "无"}'
                        )
                        continue

                shares = self._calc_shares(sym, price)

                # LLM 终极审核（构造兼容 alert-like 对象）
                class _PipelineAlert:
                    pass
                _pa = _PipelineAlert()
                _pa.symbol = sym
                _pa.price = price
                _pa.reason = signal_reason
                _pa.signal = 'BUY'
                llm_approved, llm_reason, llm_conf, size_rec = self._llm_review_signal(_pa, 'BUY')
                if not llm_approved:
                    self._deliver_alert(
                        f'❌ [{sym}] LLM 审核否决新仓买入\n'
                        f'   理由：{llm_reason}\n'
                        f'   置信度：{llm_conf:.0%}'
                    )
                    logger.info('LLM rejected new BUY %s: %s', sym, llm_reason)
                    continue
                logger.info('LLM approved new BUY %s: %s (conf=%.0f%%)', sym, llm_reason, llm_conf * 100)
                if size_rec == 'half':
                    shares = shares // 2
                if shares < 100:
                    continue

                # PreTrade 风控检查
                if self._strategy_runner is not None and self._strategy_runner.risk_engine is not None:
                    try:
                        from core.factors.base import Signal as _Sig
                        _dummy = _Sig(
                            timestamp=now, symbol=sym, direction='BUY',
                            strength=1.0, factor_name='DynamicSelector', price=price,
                        )
                        rr = self._strategy_runner.risk_engine.check(_dummy)
                        if not rr.passed:
                            logger.info('RiskEngine rejected new BUY %s: %s', sym, rr.reason)
                            continue
                    except Exception as e:
                        logger.warning('RiskEngine check failed for %s: %s', sym, e)

                if not self._can_trade():
                    self._deliver_alert(
                        f'📋 [{sym}] 模拟模式：信号触发但跳过执行\n'
                        f'   方向：BUY | 股数：{shares} | 价：{price:.2f}\n'
                        f'   原因：{signal_reason}（切换到“实盘”模式后生效）'
                    )
                    logger.info('Simulation mode: skipped BUY %s %d @ %.2f', sym, shares, price)
                    continue

                result = self._submit_with_routing(
                    symbol=sym, direction='BUY',
                    shares=shares, price=price, price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'🆕[{sym}] 自动建仓（Pipeline→分钟确认）\n'
                    f'   {status_str} {shares}股 @ {result.avg_price:.2f}\n'
                    f'   原因: {signal_reason} | {reason}'
                )
                logger.info('DynamicSelector auto BUY %s %d @ %.2f => %s',
                           sym, shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('DynamicSelector check %s error: %s', sym, e)

    def _check_and_push(self, now: datetime):
        """获取持仓 → 检查信号 → 推送飞书 + 自动下单(使用WFA优化参数)。"""
        self._scan_count += 1
        self._last_scan_time = now.strftime('%Y-%m-%d %H:%M:%S')

        # 每日策略健康度检查(开盘时运行一次)
        self._run_daily_health_check()

        # 同步市场环境(供 LLM 审核使用)
        self._sync_market_regime()

        # 驱动 StrategyRunner 刷新 pipeline scores
        if self._strategy_runner is not None:
            try:
                self._strategy_runner.run_once()
            except Exception as e:
                logger.warning('StrategyRunner.run_once() failed (will fallback): %s', e)

        # 大盘指数异动检查(每次轮询都检查,独立冷却)
        try:
            self._check_market_index(now)
        except Exception as e:
            logger.warning('Market index check error: %s', e)

        # 自选股异动检查
        try:
            self._check_watchlist(now)
        except Exception as e:
            logger.warning('Watchlist check error: %s', e)

        # 板块资金流向突变检查
        try:
            self._check_sector_flow(now)
        except Exception as e:
            logger.warning('Sector flow check error: %s', e)

        # 刷新持仓价格并获取持仓
        try:
            self._svc.refresh_prices()
        except Exception as e:
            logger.warning('refresh_prices failed: %s', e)
        try:
            positions = self._svc.get_positions()
        except Exception as e:
            logger.warning('get_positions failed: %s', e)
            return

        if not positions:
            logger.debug('No positions, skipping signal check')
            self._peak_equity = self._svc.get_portfolio_summary().get('total_equity', 0) or self._peak_equity
            self._risk_warn_fired = False
            self._risk_stop_fired = False
            return

        # 行业集中度检查
        try:
            self._check_sector_concentration(positions)
        except Exception as e:
            logger.warning('Sector concentration check error: %s', e)

        # 持仓追加买入：Pipeline combined_score 驱动
        # 旧 evaluate_signal 降级分支已删除（RSI 硬编码与 Pipeline 双信号并行有隐患）
        # 信号来源：FactorPipeline scores（经 WFA 优化，动态 IC 加权）
        # 触发阈值：combined_score > 0.30 视为买入积累信号
        from services.signals import SignalAlert, format_feishu_message, fetch_realtime
        from datetime import datetime as dt

        pipeline_scores: dict = {}
        if self._strategy_runner is not None:
            try:
                pipeline_scores = self._strategy_runner.last_scores
            except Exception:
                pass

        alerts = []
        for pos in positions:
            sym = pos.get('symbol')
            if not sym:
                continue
            self._last_scan_symbol = sym
            score = pipeline_scores.get(sym, 0.0)
            if score <= BUY_THRESHOLD_ADD:
                continue

            quote = fetch_realtime(sym)
            price = quote.get('close', 0) if quote else pos.get('current_price', 0)
            pct = quote.get('pct', 0) if quote else 0.0
            day_chg = quote.get('day_chg', 0) if quote else 0.0
            reason = f'Pipeline score={score:.4f} > {BUY_THRESHOLD_ADD}，持仓加仓信号'

            alert = SignalAlert(
                symbol=sym,
                signal='BUY',
                price=price,
                pct=pct,
                prev_rsi=None,
                volume_ratio=quote.get('volume_ratio') if quote else None,
                day_chg=day_chg,
                reason=reason,
                emitted_at=dt.now().strftime('%H:%M:%S'),
            )
            alerts.append(alert)
            logger.debug('Position add signal: %s score=%.4f', sym, score)

        # 过滤冷却期内标的（先找出 cooldown 跳过的，记录一次；剩余的才是可执行信号）
        cooldown_skipped = [a for a in alerts if not self._cooldown.can_fire(a.symbol)]
        for a in cooldown_skipped:
            self._record_skip(a.symbol, 'cooldown active', 'cooldown')
        actionable = [a for a in alerts if self._cooldown.can_fire(a.symbol)]

        # 推送飞书(有信号时)
        if actionable:
            check_time = now.strftime('%H:%M')
            msg = format_feishu_message(actionable, check_time)
            if msg:
                self._deliver_alert(msg)
                logger.info('Pushed %d alerts to Feishu at %s', len(actionable), check_time)

            # 自动下单(使用 per-symbol 参数)
            if self._broker:
                for alert in actionable:
                    self._submit_order_for_signal(alert)
        else:
            logger.debug('No buy/sell alerts at %s', now.strftime('%H:%M'))

        # 动态选股:主动建仓检查
        if self._broker and self._daily_refresh:
            self._check_new_positions(now)

        # 统一退出引擎:止损 + 止盈 + 组合熔断(替代三个分散方法)
        try:
            self._run_exit_engine(positions, now)
        except Exception as e:
            logger.error('ExitEngine error: %s', e, exc_info=True)
