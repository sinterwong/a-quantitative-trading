"""
risk.py — IntradayMonitor 风控过滤 Mixin。

负责: 仓位计算 (Kelly + 占比裁剪)、新闻情绪过滤、ExitEngine 调用、
      行业集中度检查、Kelly 历史回看更新、策略健康度检查。
"""

import os
import sys
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger('intraday_monitor')


# 单标的最大仓位占总权益比例。与 PaperBroker.max_position_pct 对齐，
# 在 IntradayMonitor 内做"前置裁剪"，避免提交后被 broker 拒单。
MAX_POSITION_PCT = 0.25


class RiskMixin:
    """风控相关逻辑：仓位裁剪、退出引擎、健康度。"""

    BEARISH_BLOCK_CONFIDENCE = 0.60  # 空方置信度 >此值则阻止建仓/换仓

    # ── 仓位计算 ───────────────────────────────────────────

    def _calc_shares(self, symbol: str, price: float) -> int:
        """
        计算可买股数(整手 100 股)，在 IntradayMonitor 层做"前置裁剪"：

          1. Kelly 比例约束：max_cost = cash × _kelly_pct
          2. 单标的占比约束：position_value ≤ total_equity × MAX_POSITION_PCT
             其中 position_value 包含已有持仓的当前市值
          3. 取两者较小（避免提交到 broker 后再被 max_position_pct 拒单）

        broker 内部仍会做最终裁剪（兜底），但前置可避免无谓的"提交 → 拒单 → 告警"
        循环，且让仓位逻辑在监控层就可观测。
        """
        try:
            cash = self._svc.get_cash()
        except Exception:
            cash = 0
        if cash <= 0 or price <= 0:
            return 0

        kelly_cost = cash * self._kelly_pct

        try:
            equity = self._svc.get_total_equity()
        except Exception:
            equity = cash
        try:
            existing_pos = self._svc.get_position(symbol)
            existing_shares = (existing_pos or {}).get('shares', 0) or 0
        except Exception:
            existing_shares = 0
        max_pos_value = equity * MAX_POSITION_PCT
        existing_value = existing_shares * price
        max_pos_cost = max(0.0, max_pos_value - existing_value)

        budget = min(kelly_cost, max_pos_cost)
        raw_shares = int(budget / price)
        shares = (raw_shares // 100) * 100
        return shares if shares >= 100 else 0

    # ── 新闻情绪过滤（Method A & B 共享）─────────────────────

    def _check_news_sentiment(self, symbol: str) -> tuple[bool, Optional[str], Optional[float], Optional[str]]:
        """
        检查标的的新闻情绪。

        Returns:
            (blocked, sentiment, confidence, summary)
            blocked=True  → 新闻情绪强烈看空,不应建仓/不追加
            blocked=False → 可以交易(或无法获取情绪)

        情绪缓存:每天早上刷新一次(盘中不重复请求 LLM)。
        """
        today = date.today().isoformat()
        with self._state_lock:
            if self._sentiment_cache_date != today:
                self._sentiment_cache = {}
                self._sentiment_cache_date = today
            cached = self._sentiment_cache.get(symbol)

        if cached is not None:
            sent, conf, summ = cached
            blocked = (sent == 'bearish' and conf >= self.BEARISH_BLOCK_CONFIDENCE)
            return blocked, sent, conf, summ

        if self._llm is None:
            return False, None, None, None

        params = self._get_params(symbol)
        name = params.get('name', symbol)
        news_text = f"{name} ({symbol}) 最新财经新闻"

        try:
            result = self._llm.analyze_news(news_text, timeout=12)
            sentiment = getattr(result, 'sentiment', 'neutral')
            confidence = getattr(result, 'confidence', 0.0)
            summary = getattr(result, 'summary', '')
            with self._state_lock:
                self._sentiment_cache[symbol] = (sentiment, confidence, summary)
            blocked = (sentiment == 'bearish' and confidence >= self.BEARISH_BLOCK_CONFIDENCE)
            logger.info(
                'NewsSentiment %s: sentiment=%s conf=%.2f blocked=%s',
                symbol, sentiment, confidence, blocked
            )
            return blocked, sentiment, confidence, summary
        except Exception as e:
            logger.warning('NewsSentiment %s failed: %s', symbol, e)
            with self._state_lock:
                self._sentiment_cache[symbol] = ('unknown', 0.0, '')
            return False, 'unknown', 0.0, ''

    # ── Kelly 更新 ────────────────────────────────────────

    def _refresh_kelly_from_trades(self):
        """
        每交易日上午 9:05(params_cache 刷新时)根据历史交易记录更新 Kelly 仓位。
        从 PortfolioService.get_trades() 获取全部历史交易,计算 P&L 后更新 _kelly_pct。
        """
        try:
            for k in list(os.environ.keys()):
                if 'proxy' in k.lower():
                    del os.environ[k]
            BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if BACKEND_DIR not in sys.path:
                sys.path.insert(0, BACKEND_DIR)
            from scripts.quant.position_sizer import compute_kelly_from_trades

            trades_raw = self._svc.get_trades(limit=500)
            if not trades_raw:
                return
            trades = [{'pnl': float(t.get('pnl', 0))} for t in trades_raw]
            new_kelly = compute_kelly_from_trades(trades)

            with self._state_lock:
                if abs(new_kelly - self._kelly_pct) > 0.005:
                    logger.info('Kelly updated: %.1f%% -> %.1f%% (from %d trades)',
                               self._kelly_pct * 100, new_kelly * 100, len(trades))
                self._kelly_pct = new_kelly
                self._kelly_last_updated = date.today().isoformat()
        except Exception as e:
            logger.warning('_refresh_kelly_from_trades failed: %s', e)

    # ── 策略健康监控 ───────────────────────────────────────

    def _run_daily_health_check(self):
        """每日开盘时运行一次 StrategyHealthMonitor,检查策略健康度并推送告警。"""
        today = date.today().isoformat()
        if self._health_check_date == today:
            return
        self._health_check_date = today
        try:
            from core.strategy_health import StrategyHealthMonitor
            raw = self._svc.get_daily_metas(limit=60)
            if not raw or len(raw) < 2:
                return
            raw.sort(key=lambda r: r.get('trade_date', ''))
            stats = []
            for i, row in enumerate(raw):
                equity = float(row.get('equity', 0) or 0)
                prev_eq = float(raw[i - 1].get('equity', 0) or 0) if i > 0 else equity
                daily_ret = (equity - prev_eq) / prev_eq if prev_eq > 0 else 0.0
                stats.append({
                    'date': row.get('trade_date', today),
                    'daily_return': daily_ret,
                    'n_trades': int(row.get('n_trades', 0) or 0),
                    'equity': equity,
                })
            monitor = StrategyHealthMonitor(notify=True)
            report = monitor.check(stats)
            if report.has_warn():
                logger.warning('StrategyHealth: %s', report.worst_level())
                self._deliver_alert(report.to_feishu_text())
            else:
                logger.info('StrategyHealth: OK (sharpe_20d=%.3f)', report.rolling_sharpe_20d)
        except Exception as e:
            logger.warning('Daily health check failed: %s', e)

    # ── 行业集中度风控 ─────────────────────────────────────

    def _check_sector_concentration(self, positions: list):
        """
        检查行业集中度风险。
        单一行业 > 40% 权益 → 推送飞书警告 + 强制减仓至 40%。
        """
        try:
            from services.portfolio import check_sector_concentration
        except Exception:
            return

        violations = check_sector_concentration(positions, max_sector_pct=0.40)
        if not violations:
            return

        for v in violations:
            logger.warning('Sector concentration violation: %s=%.1f%% (max 40%%)',
                          v['sector'], v['pct'])
            msg = (
                f'[WARNING] 行业集中度风险!\n'
                f'  行业: {v["sector"]}\n'
                f'  当前占比: {v["pct"]}% (上限 40%)\n'
                f'  需减仓: {v["reduce_value"]:.0f}元 ({v["reduce_pct"]}% 仓位)\n'
                f'  时间: {datetime.now().strftime("%H:%M")}\n'
                f'  ACTION: 减仓至 40%'
            )
            self._deliver_alert(msg)

            if self._broker:
                from services.portfolio import _load_sector_map
                sector_map = _load_sector_map()
                for pos in positions:
                    sym = pos.get('symbol', '')
                    shares = pos.get('shares', 0)
                    if shares <= 0:
                        continue
                    sym_key = sym.replace('.SH', '').replace('.SZ', '')
                    for key, name in sector_map.items():
                        if sym_key.startswith(key) or key in sym_key:
                            if name == v['sector']:
                                half = shares // 2
                                if half >= 100:
                                    self._submit_market_sell(sym, half, reason='sector_concentration')
                            break

    # ── 统一退出引擎 ───────────────────────────────────────

    def _run_exit_engine(self, positions: list, now: datetime):
        """
        统一卖出信号引擎。
        所有止损/止盈/组合熔断都通过 ExitEngine 生成优先级排序的退出信号并统一执行。
        旧的分散方法（_check_stop_losses / _check_take_profits / _check_portfolio_risk）
        已删除，避免双路径不一致。
        """
        try:
            from core.exit_engine import ExitEngine
        except ImportError as e:
            logger.error(
                'ExitEngine import failed: %s. NO exit signals will be generated this cycle. '
                'Sell-side risk control is DOWN until import is restored.', e,
            )
            self._deliver_alert(
                f'🚨 ExitEngine 导入失败：本轮不会生成任何退出信号\n'
                f'   错误：{e}\n'
                f'   止损/止盈/熔断暂时失效，请立即检查 core.exit_engine 模块'
            )
            return

        # ── 准备 price_bars(ATR/RSI 所需的 OHLCV 数据)──
        price_bars: dict = {}
        try:
            from core.data_layer import get_data_layer
            dl = get_data_layer()
            for pos in positions:
                sym = pos.get('symbol')
                if not sym:
                    continue
                try:
                    bars = dl.get_bars(sym, days=60)
                    if bars is not None and len(bars) >= 20:
                        price_bars[sym] = bars
                except Exception:
                    pass
        except Exception as e:
            logger.debug('price_bars fetch failed: %s', e)

        # ── 准备 per-symbol 参数 ──
        params_map = {
            pos['symbol']: self._get_params(pos['symbol'])
            for pos in positions if pos.get('symbol')
        }

        # ── 补全 current_price ──
        enriched: list = []
        from services.signals import fetch_realtime
        for pos in positions:
            p = dict(pos)
            if not p.get('current_price') or p['current_price'] <= 0:
                try:
                    snap = fetch_realtime(p.get('symbol', ''))
                    if snap and snap.get('price', 0) > 0:
                        p['current_price'] = snap['price']
                        self._svc.update_position_price(p['symbol'], snap['price'])
                        if p['current_price'] > float(p.get('peak_price', 0) or 0):
                            p['peak_price'] = p['current_price']
                except Exception:
                    pass
            enriched.append(p)

        # ── 更新历史峰值 ──
        current_equity = 0.0
        try:
            summary = self._svc.get_portfolio_summary(refresh_prices_now=False)
            current_equity = float(summary.get('total_equity', 0) or 0)
        except Exception:
            pass
        with self._state_lock:
            if current_equity > self._peak_equity:
                self._peak_equity = current_equity
                self._risk_warn_fired = False
                self._risk_stop_fired = False
            equity_peak = self._peak_equity

        # ── 因子评分(FACTOR_REVERSAL 检查所需)──
        pipeline_scores: dict = {}
        if self._strategy_runner is not None:
            try:
                for rr in self._strategy_runner.last_results:
                    if rr.pipeline_result is not None:
                        pipeline_scores[rr.symbol] = rr.pipeline_result.combined_score
            except Exception:
                pass

        # ── 生成信号 ──
        engine = ExitEngine(
            dd_warn=self._dd_warn,
            dd_stop=self._dd_stop,
        )
        signals = engine.generate(
            positions=enriched,
            equity_peak=equity_peak,
            current_equity=current_equity,
            pipeline_scores=pipeline_scores or None,
            price_bars=price_bars or None,
            params_map=params_map or None,
        )

        if not signals:
            return

        logger.info('ExitEngine: %d signals generated at %s', len(signals), now.strftime('%H:%M'))

        _EMOJI = {0: '🚨', 1: '⚠️', 2: '🛑', 3: '📉',
                  4: '↩️', 5: '⚡', 6: '🎯', 7: '🏆', 8: '📊', 9: '⏰'}

        for sig in signals:
            sym = sig.symbol
            is_portfolio_level = sig.priority.value <= 1  # P0=EMERGENCY, P1=PORTFOLIO_REDUCE

            if is_portfolio_level:
                with self._state_lock:
                    if sig.priority.value == 0 and self._risk_stop_fired:
                        continue
                    if sig.priority.value == 1 and self._risk_warn_fired:
                        continue
                    if sig.priority.value == 0:
                        self._risk_stop_fired = True
                    if sig.priority.value == 1:
                        self._risk_warn_fired = True
            else:
                cooldown_key = f'exit_{sym}'
                if not self._cooldown.can_fire(cooldown_key):
                    continue

            pos_dict = next((p for p in enriched if p.get('symbol') == sym), None)
            if not pos_dict:
                continue
            shares = pos_dict.get('shares', 0)
            if shares <= 0:
                continue
            sell_shares = sig.shares_to_sell(shares)
            current_price = sig.current_price or float(pos_dict.get('current_price', 0) or 0)
            if current_price <= 0:
                continue

            emoji = _EMOJI.get(sig.priority.value, '📤')
            label = sig.priority.name.replace('_', ' ').title()
            pnl_str = f'{sig.unrealized_pct * 100:+.1f}%'

            if not self._can_trade():
                self._deliver_alert(
                    f'📋 [{sym}] 模拟模式:退出信号跳过执行\n'
                    f'   {label} | 卖出: {sell_shares}股 ({sig.exit_pct*100:.0f}%仓) | 价: {current_price:.2f}\n'
                    f'   浮盈: {pnl_str} | 原因: {sig.reason}\n'
                    f'   (切换"实盘"模式后生效)'
                )
                logger.info('Simulation: skipped ExitEngine %s %s %d @ %.2f',
                            sig.priority.name, sym, sell_shares, current_price)
                continue

            try:
                result = self._submit_with_routing(
                    symbol=sym,
                    direction='SELL',
                    shares=sell_shares,
                    price=current_price,
                    price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'{emoji}[{sym}] {label}(ExitEngine 自动平仓)\n'
                    f'   {status_str} {sell_shares}股 @ {result.avg_price:.2f} | 浮盈: {pnl_str}\n'
                    f'   原因: {sig.reason}'
                )
                logger.info('ExitEngine SELL %s %s %d @ %.2f => %s',
                            sig.priority.name, sym, sell_shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('ExitEngine order failed %s %s: %s', sig.priority.name, sym, e)
