"""
alerts.py — IntradayMonitor 告警 / 记录 Mixin。

负责: 飞书推送、状态快照(可观测性)、信号/跳过/LLM 审核日志、
      LLM 终极审核(完整上下文构建)。
"""

import os
import json
import ssl
import urllib.request
import logging
from datetime import datetime

from ..signals import SignalAlert

logger = logging.getLogger('intraday_monitor')


class AlertsMixin:
    """告警推送 + 可观测性日志。"""

    # ── 可观测性：事件记录 ─────────────────────────────────

    def _record_signal(self, symbol: str, signal: str, price: float,
                       reason: str, result: str = ''):
        """记录一次信号触发事件。"""
        self._signal_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'signal': signal,
            'price': round(price, 2),
            'reason': reason[:200],
            'result': result,
        })
        if len(self._signal_log) > 50:
            self._signal_log = self._signal_log[-50:]

    def _record_skip(self, symbol: str, reason: str, category: str = ''):
        """记录一次信号被跳过事件。"""
        self._skip_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'reason': reason[:200],
            'category': category,
        })
        if len(self._skip_log) > 50:
            self._skip_log = self._skip_log[-50:]

    def _record_llm_review(self, symbol: str, direction: str, approved: bool,
                           reason: str, confidence: float):
        """记录一次 LLM 审核事件。"""
        self._llm_review_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'direction': direction,
            'approved': approved,
            'reason': reason[:200],
            'confidence': round(confidence, 2),
        })
        if len(self._llm_review_log) > 50:
            self._llm_review_log = self._llm_review_log[-50:]

    def _record_position_alert(self, alert_type: str, symbol: str, message: str,
                                price: float = None, pct: float = None):
        """记录持仓相关预警到历史。"""
        try:
            from services.alert_history import record_alert
            record_alert(alert_type, message, symbol=symbol, price=price, pct_change=pct)
        except Exception as e:
            logger.debug('record_position_alert failed: %s', e)

    def get_status(self) -> dict:
        """返回监控状态摘要，供 API 暴露。"""
        thread_alive = self._thread.is_alive() if self._thread else False
        return {
            'running': self._running,
            'thread_alive': thread_alive,
            'trading_mode': self._trading_mode,
            'interval_seconds': self._interval,
            'last_scan_time': self._last_scan_time,
            'last_scan_symbol': self._last_scan_symbol,
            'scan_count': self._scan_count,
            'error_count': self._error_count,
            'last_error': self._last_error,
            'kelly_pct': round(self._kelly_pct, 4),
            'kelly_last_updated': self._kelly_last_updated,
            'dd_warn': self._dd_warn,
            'dd_stop': self._dd_stop,
            'peak_equity': round(self._peak_equity, 2),
            'risk_warn_fired': self._risk_warn_fired,
            'risk_stop_fired': self._risk_stop_fired,
            'cooldown_active': len(self._cooldown._last),
            'signals': list(reversed(self._signal_log[-10:])),
            'skips': list(reversed(self._skip_log[-10:])),
            'llm_reviews': list(reversed(self._llm_review_log[-5:])),
        }

    # ── LLM 终极审核 ───────────────────────────────────────

    def _llm_review_signal(self, alert: SignalAlert, direction: str):
        """
        LLM 终极审核:收集全部上下文,让大模型决定是否执行交易。
        返回 (approved: bool, reason: str, confidence: float, size_rec: str)
        """
        if self._llm is None:
            return True, 'LLM unavailable, auto-approve', 0.5, 'full'

        try:
            sym = alert.symbol
            params = self._get_params(sym)
            cash = self._svc.get_cash()
            positions = self._svc.get_positions()
            pos = self._svc.get_position(sym)
            recent_trades = self._svc.get_recent_trades(sym, limit=5) if hasattr(self._svc, 'get_recent_trades') else []

            try:
                from services.signals import get_market_brief
                mb = get_market_brief()
            except Exception:
                mb = {}

            sent_key = sym
            sentiment_info = ''
            if sent_key in self._sentiment_cache:
                sent, conf_s, summ = self._sentiment_cache[sent_key]
                sentiment_info = f'情绪={sent}(置信度{conf_s:.0%}),摘要:{summ[:60]}'

            # 持仓摘要
            if pos:
                _pos_label = f"是({pos.get('shares', 0)}股,成本{'{:.2f}'.format(pos.get('entry_price', 0))})"
            else:
                _pos_label = "否(可建仓)"

            pos_summary = []
            for p in (positions or []):
                if p.get('shares', 0) > 0:
                    pos_summary.append(
                        f"{p['symbol']}: {p['shares']}股,成本{p.get('entry_price', 0):.2f}"
                    )

            trade_summary = []
            for t in (recent_trades or []):
                trade_summary.append(
                    f"{t.get('direction','')} {t.get('symbol','')} "
                    f"{t.get('shares',0)}@{t.get('price',0):.2f} "
                    f"pnl={t.get('pnl', 0):+.0f}"
                )

            if direction == 'BUY':
                system_prompt = (
                    "你是一个严格的A股量化交易员。每笔买入都需要通过你的最终审核。\n"
                    "你极其重视:\n"
                    "1. 当前市场环境是否适合建仓(不要在熊市/高波动环境重仓)\n"
                    "2. RSI 是否真的处于低位(是否有足够的安全边际)\n"
                    "3. ATR 波动率是否在合理范围(排除极度高波动标的)\n"
                    "4. 板块是否处于强势(避免逆势买入)\n"
                    "5. 资金管理是否合理(单只仓位不超过25%,Kelly半仓原则)\n\n"
                    "输出严格JSON格式:\n"
                    "{\"decision\": \"approve\"或\"reject\"或\"delay\"(仅当充分理由时delay,否则reject), "
                    "\"confidence\": 0.0~1.0, "
                    "\"reason\": \"简短理由(20字内)\", "
                    "\"risk_note\": \"风险提示(如有)\", "
                    "\"size_rec\": \"full\"(按Kelly满仓)或\"half\"(半仓)或\"skip\"(跳过)\"\n"
                    "}"
                )
                user_prompt = (
                    f"【买入信号审核】\n"
                    f"标的:{sym}(名称:{params.get('name', sym)})\n"
                    f"信号类型:{alert.signal}\n"
                    f"当前价:{alert.price:.2f}(今日涨幅:{getattr(alert, 'pct', 0):+.2f}%)\n"
                    f"触发原因:{alert.reason}\n"
                    f"RSI 参数:买入阈值={params.get('rsi_buy', 25)},当前RSI≈{alert.prev_rsi:.0f if alert.prev_rsi is not None else 'N/A'}\n"
                    f"ATR 阈值:{params.get('atr_threshold', 0.85)}(当前ATR ratio={getattr(alert, 'atr_ratio', 'N/A')})\n"
                    f"市场环境:{self._market_regime.get('regime', 'UNKNOWN')}(ATR ratio={self._market_regime.get('atr_ratio', 0):.3f})\n"
                    f"大盘状态:{mb.get('趋势', '未知')} | 情绪:{mb.get('情绪', '未知')}\n"
                    f"可用现金:¥{cash:,.0f}(总权益:¥{self._svc.get_total_equity():,.0f})\n"
                    f"该股已有持仓:{_pos_label}\n"
                    f"当前持仓:{' | '.join(pos_summary) if pos_summary else '空仓'}\n"
                    f"近期交易:{' | '.join(trade_summary) if trade_summary else '无'}\n"
                    f"新闻情绪:{sentiment_info if sentiment_info else '无情绪数据(自动放行)'}"
                )
            else:
                system_prompt = (
                    "你是一个纪律严明的A股交易员,专注于精准止盈止损。\n"
                    "卖出决策依据:\n"
                    "1. 止盈:是否达到预设目标(TakeProfit),趋势是否已衰竭\n"
                    "2. 止损:是否触发 ATR 止损线(Chandelier Exit),还是假突破\n"
                    "3. 仓位管理:是否需要减仓还是清仓\n"
                    "4. 相对大盘:标的是否跑输大盘(弱势股优先清仓)\n\n"
                    "输出严格JSON格式:\n"
                    "{\"decision\": \"approve\"或\"reject\"或\"hold\"(持有不卖), "
                    "\"confidence\": 0.0~1.0, "
                    "\"reason\": \"简短理由(20字内)\", "
                    "\"risk_note\": \"风险提示(如有)\", "
                    "\"size_rec\": \"full\"(清仓)或\"half\"(半仓)或\"hold\"(持有)\"\n"
                    "}"
                )
                user_prompt = (
                    f"【卖出信号审核】\n"
                    f"标的:{sym}(名称:{params.get('name', sym)})\n"
                    f"信号类型:{alert.signal}\n"
                    f"当前价:{alert.price:.2f}(持仓成本:{pos.get('entry_price', 0):.2f},浮动盈亏:{((alert.price - pos.get('entry_price', 0)) / pos.get('entry_price', 1) * 100):+.1f}%)\n"
                    f"触发原因:{alert.reason}\n"
                    f"RSI 参数:卖出阈值={params.get('rsi_sell', 65)}\n"
                    f"止盈目标:{params.get('take_profit', 0.20):.0%},止损线:{params.get('stop_loss', 0.05):.0%}\n"
                    f"市场环境:{self._market_regime.get('regime', 'UNKNOWN')}(ATR ratio={self._market_regime.get('atr_ratio', 0):.3f})\n"
                    f"持仓数量:{pos.get('shares', 0)}股(整手:{(pos.get('shares', 0) // 100) * 100}股)\n"
                    f"当前持仓:{' | '.join(pos_summary) if pos_summary else '空仓'}\n"
                    f"近期交易:{' | '.join(trade_summary) if trade_summary else '无'}\n"
                )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            resp = self._llm.provider.chat(messages, max_tokens=4096, temperature=0.3)
            content = resp.content.strip()

            import re as _re
            json_match = _re.search(r'\{[^{}]*\}', content, _re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                decision = parsed.get('decision', 'reject').lower()
                confidence = float(parsed.get('confidence', 0.5))
                reason = parsed.get('reason', 'LLM review')
                size_rec = parsed.get('size_rec', 'full' if decision == 'approve' else 'skip')
                approved = decision in ('approve', 'yes')
                logger.info('LLM review %s %s: decision=%s conf=%.0f reason=%s',
                           direction, sym, decision, confidence, reason)
                return approved, reason, confidence, size_rec
            else:
                logger.warning('LLM response parse failed: %s', content[:200])
                return True, f'LLM parse failed({content[:50]}),自动放行', 0.0, 'full'

        except Exception as e:
            logger.error('LLM review error for %s: %s', alert.symbol, e)
            return True, f'LLM异常({str(e)[:30]}),自动放行', 0.0, 'full'

    # ── 飞书 IM 推送 ───────────────────────────────────────

    def _deliver_alert(self, text: str, alert_type: str = 'POSITION',
                       symbol: str = '', price: float = None, pct: float = None):
        """通过飞书 IM API 推送文本消息给用户,并记录到历史。"""
        app_id = os.environ.get('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        user_open_id = os.environ.get('FEISHU_USER_OPEN_ID', '')

        if not app_id or not app_secret or not user_open_id:
            logger.debug('Feishu not configured (FEISHU_APP_ID/SECRET/USER_OPEN_ID), skipping push')
            return

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # 1. 获取 tenant_access_token
        try:
            token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
            payload = json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode()
            req = urllib.request.Request(token_url, data=payload,
                                        headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                token_result = json.loads(resp.read())
            token = token_result.get('tenant_access_token', '')
            if not token:
                logger.warning('Feishu: no tenant_access_token returned: %s', token_result)
                return
        except Exception as e:
            logger.error('Feishu token request failed: %s', e)
            return

        # 2. 发送消息
        try:
            send_url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id'
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
            msg_payload = json.dumps({
                'receive_id': user_open_id,
                'msg_type': 'text',
                'content': json.dumps({'text': text})
            }).encode()
            req2 = urllib.request.Request(send_url, data=msg_payload, headers=headers, method='POST')
            with urllib.request.urlopen(req2, timeout=8, context=ctx) as resp2:
                result = json.loads(resp2.read())
                code = result.get('code', -1)
                if code == 0:
                    logger.info('Feishu push succeeded: msg_id=%s', result.get('data', {}).get('message_id'))
                else:
                    logger.warning('Feishu push code=%s: %s', code, result.get('msg'))
        except Exception as e:
            logger.error('Feishu send failed: %s', e)

        # 3. 记录到预警历史
        try:
            from services.alert_history import record_alert
            record_alert(alert_type, text, symbol=symbol or '',
                          price=price, pct_change=pct)
        except Exception:
            pass
