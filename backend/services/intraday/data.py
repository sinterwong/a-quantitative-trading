"""
data.py — IntradayMonitor 数据/行情拉取 Mixin。

负责: 选股名单加载、参数缓存、市场指数、自选股、板块资金流、市场环境同步。
"""

import os
import logging
from datetime import datetime, date

logger = logging.getLogger('intraday_monitor')


class DataMixin:
    """行情、选股、参数等数据接入逻辑。"""

    # 监控的指数及其预警阈值(涨跌幅绝对值超过此值则告警)
    INDEX_CONFIG = {
        'sh000001': {'name': '上证指数', 'alert_pct': 1.5},
        'sz399001': {'name': '深证成指', 'alert_pct': 1.5},
        'sz399006': {'name': '创业板指', 'alert_pct': 2.0},
        'sh000688': {'name': '科创50',   'alert_pct': 2.0},
        'sh000300': {'name': '沪深300', 'alert_pct': 1.5},
    }

    # ── 每日动态选股 ─────────────────────────────────────────

    def _load_selector_once(self):
        """每天开盘前只加载一次动态选股结果。"""
        today = date.today().isoformat()
        with self._state_lock:
            if self._selector_loaded_date == today and self._selector_cache:
                return
            self._selector_loaded_date = today
            self._selector_cache = []
        if not self._broker:
            return
        try:
            import sys as _sys
            PROJ_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if PROJ_DIR not in _sys.path:
                _sys.path.insert(0, PROJ_DIR)
            from scripts.dynamic_selector import DynamicStockSelector
            sel = DynamicStockSelector()
            sel.fetch_market_news(30)
            sel.fetch_sectors()
            sel.calc_all_scores()
            selected = sel.select_stocks(top_n=self._selector_top_n)
            # ── Method B:新闻情绪过滤 ──
            if self._llm is not None:
                filtered = []
                for sym in selected:
                    blocked, sent, conf, summ = self._check_news_sentiment(sym)
                    if blocked:
                        logger.info('DynamicSelector: %s blocked by news sentiment (%s conf=%.2f)',
                                   sym, sent, conf)
                        self._deliver_alert(
                            f'⛔[{sym}] 开盘前新闻情绪过滤\n'
                            f'   情绪:{sent}(置信度 {conf:.0%})\n'
                            f'   摘要:{summ[:60] if summ else "无"}\n'
                            f'   原因:利空强烈,暂不纳入候选'
                        )
                    else:
                        filtered.append(sym)
                selected = filtered

            with self._state_lock:
                self._selector_cache = selected
            logger.info('DynamicSelector: loaded %d stocks (after news filter)', len(selected))
        except Exception as e:
            logger.warning('DynamicSelector: failed to load: %s', e)
            with self._state_lock:
                self._selector_cache = []

    def _get_watched_symbols(self) -> set:
        """返回今日动态选股列表(仅未持仓的标的)。"""
        self._load_selector_once()
        existing = {p.get('symbol') for p in self._svc.get_positions() if p.get('symbol')}
        with self._state_lock:
            cache_snapshot = list(self._selector_cache)
        return {s for s in cache_snapshot if s not in existing}

    # ── per-symbol 参数缓存 ────────────────────────────────

    def _get_params(self, symbol: str) -> dict:
        """返回股票的参数集(WFA优先,fallback到params.json)。每天刷新一次。"""
        today = date.today().isoformat()
        need_kelly_refresh = False
        with self._state_lock:
            if self._params_cache_date != today:
                self._params_cache = {}
                self._params_cache_date = today
                need_kelly_refresh = True
            cached = self._params_cache.get(symbol)
        if need_kelly_refresh:
            # 不在锁内做远程调用
            self._refresh_kelly_from_trades()
        if cached is None:
            from services.signals import load_symbol_params
            loaded = load_symbol_params(symbol)
            with self._state_lock:
                self._params_cache[symbol] = loaded
            return loaded
        return cached

    def _sync_market_regime(self):
        """从 StrategyRunner 同步最新市场环境到 _market_regime(供 LLM prompt 使用)。"""
        if self._strategy_runner is not None:
            try:
                r = self._strategy_runner.current_regime
                if r is not None:
                    payload = {
                        'regime': r.regime,
                        'reason': r.reason,
                        'atr_ratio': getattr(r, 'atr_ratio', 0.0),
                    }
                    with self._state_lock:
                        self._market_regime = payload
                    return
            except Exception:
                pass
        try:
            from core.regime import get_regime
            r = get_regime()
            payload = {
                'regime': r.regime,
                'reason': r.reason,
                'atr_ratio': getattr(r, 'atr_ratio', 0.0),
            }
            with self._state_lock:
                self._market_regime = payload
        except Exception:
            pass

    # ── 大盘指数监控 ───────────────────────────────────────

    def _fetch_index_data(self) -> dict:
        """获取所有监控指数的当前行情。"""
        from services.signals import fetch_bulk
        codes = list(self.INDEX_CONFIG.keys())
        return fetch_bulk(codes)

    def _check_market_index(self, now: datetime):
        """检查大盘指数是否出现显著异动(涨跌超过阈值)。"""
        data = self._fetch_index_data()
        if not data:
            logger.debug('Index data fetch failed')
            return

        for code, cfg in self.INDEX_CONFIG.items():
            row = None
            for k, v in data.items():
                if k.upper().replace('.SH', '').replace('.SZ', '').replace('SH', '').replace('SZ', '') \
                   == code.replace('sh', '').replace('sz', '').upper():
                    row = v
                    break
            if not row:
                continue

            pct = row.get('pct', 0)
            if abs(pct) < cfg['alert_pct']:
                continue

            cooldown_key = f'idx_{code}'
            if not self._cooldown.can_fire(cooldown_key):
                continue

            direction = '暴涨' if pct > 0 else '暴跌'
            emoji = '🚀' if pct > 0 else ('🚨' if pct < -2 else '⚠️')
            name = cfg['name']
            price = row.get('price', 0)
            msg = (
                f'{emoji}【大盘异动】{name}{direction}\n'
                f'   当前: {price} ({pct:+.2f}%)\n'
                f'   阈值: ±{cfg["alert_pct"]}% | 时间: {now.strftime("%H:%M")}'
            )
            self._deliver_alert(msg)
            from services.alert_history import record_alert
            record_alert('INDEX', msg, symbol=code, price=price, pct_change=pct)
            logger.info('Market index alert: %s %+.2f%%', name, pct)

    # ── 自选股监控 ─────────────────────────────────────────

    def _check_watchlist(self, now: datetime):
        """检查自选股列表中的股票是否出现异动(涨跌幅超过各股阈值)。只预警,不交易。"""
        from services.watchlist import get_watchlist
        from services.signals import fetch_bulk

        watchlist = get_watchlist()
        if not watchlist:
            return

        codes = [w['symbol'] for w in watchlist]
        data = fetch_bulk(codes)
        if not data:
            return

        for w in watchlist:
            sym = w['symbol']
            row = None
            for k, v in data.items():
                if k.upper().replace('.SH', '').replace('.SZ', '').replace('SH', '').replace('SZ', '') \
                   == sym.replace('.SH', '').replace('.SZ', '').upper():
                    row = v
                    break
            if not row:
                continue

            pct = row.get('pct', 0)
            threshold = w.get('alert_pct', 5.0)
            if abs(pct) < threshold:
                continue

            cooldown_key = f'wl_{sym}'
            if not self._cooldown.can_fire(cooldown_key):
                continue

            price = row.get('price', 0)
            emoji = '🔺' if pct > 0 else '🔻'
            direction = '大涨' if pct > 0 else '大跌'
            name = w.get('name', sym)
            alert_reason = w.get('reason', '')
            reason_str = f' | 自选理由: {alert_reason}' if alert_reason else ''

            msg = (
                f'{emoji}【自选股异动】{name}({sym}) {direction}\n'
                f'   当前: {price} ({pct:+.2f}%)\n'
                f'   预警阈值: ±{threshold}%{reason_str}\n'
                f'   时间: {now.strftime("%H:%M")}'
            )
            self._deliver_alert(msg)
            from services.alert_history import record_alert
            record_alert('WATCHLIST', msg, symbol=sym, price=price, pct_change=pct)
            logger.info('Watchlist alert: %s %+.2f%%', sym, pct)

    # ── 板块资金流向监控 ───────────────────────────────────

    def _load_sector_flows(self):
        """加载今日板块资金流向数据(从 dynamic_selector)。"""
        try:
            import sys as _sys
            PROJ_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            SCRIPTS_DIR = os.path.join(PROJ_DIR, 'scripts')
            if SCRIPTS_DIR not in _sys.path:
                _sys.path.insert(0, SCRIPTS_DIR)
            from dynamic_selector import DynamicStockSelector
            sel = DynamicStockSelector()
            sel.fetch_sectors()
            return sel.sector_scores
        except Exception as e:
            logger.debug('Sector flow load failed: %s', e)
            return {}

    def _check_sector_flow(self, now: datetime):
        """检查板块资金流向是否出现异常突变(评分跃升 > 20)。"""
        if not hasattr(self, '_prev_sector_flows'):
            self._prev_sector_flows = {}

        current_flows = self._load_sector_flows()
        if not current_flows:
            return

        for bk, info in current_flows.items():
            prev = self._prev_sector_flows.get(bk, {})
            prev_flow = prev.get('flow', 0)
            curr_flow = info.get('flow', 0)

            if prev_flow > 0 and (curr_flow - prev_flow) > 20:
                cooldown_key = f'sf_{bk}'
                if not self._cooldown.can_fire(cooldown_key):
                    continue

                name = info.get('name', bk)
                chg = info.get('change_pct', 0)
                chg_emoji = '🔺' if chg > 0 else '➖'
                msg = (
                    f'💰【资金异动】{name} 资金大幅流入\n'
                    f'   板块涨幅: {chg_emoji}{chg:+.2f}%\n'
                    f'   资金评分: {prev_flow:.0f} → {curr_flow:.0f} (+{curr_flow - prev_flow:.0f})\n'
                    f'   可能受消息面驱动,关注持续性\n'
                    f'   时间: {now.strftime("%H:%M")}'
                )
                self._deliver_alert(msg)
                from services.alert_history import record_alert
                record_alert('SECTOR_FLOW', msg, symbol=bk, pct_change=chg)
                logger.info('Sector flow alert: %s flow %d→%d', name, prev_flow, curr_flow)

        self._prev_sector_flows = current_flows
