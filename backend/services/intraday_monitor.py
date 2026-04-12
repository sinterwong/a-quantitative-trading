"""
intraday_monitor.py — 盘中实时监控服务
========================================
后台线程，交易时段持续运行：
  - 每 5 分钟检查一次持仓信号
  - 合条件时主动推送 Feishu 消息

使用方法：
  from backend.services.intraday_monitor import IntradayMonitor
  mon = IntradayMonitor(svc=portfolio_service)
  mon.start()   # 启动后台线程
  mon.stop()    # 停止
"""

import os
import sys
import time
import json
import logging
import threading
import ssl
import urllib.request
from datetime import datetime, date
from typing import Optional

# Resolve imports relative to backend dir
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = THIS_DIR
sys.path.insert(0, BACKEND_DIR)

from services.signals import (
    check_portfolio_signals,
    format_feishu_message,
    MARKET_MORNING_START, MARKET_MORNING_END,
    MARKET_AFTERNOON_START, MARKET_AFTERNOON_END,
    TRADING_DAYS,
)

logger = logging.getLogger('intraday_monitor')

# 全局配置
CHECK_INTERVAL  = 300   # 秒（5分钟）
COOLDOWN       = 900   # 同一标的信号推送冷却时间（15分钟）


# ─── 交易时段判断 ─────────────────────────────────────────

def is_market_open(now: Optional[datetime] = None) -> bool:
    """判断当前是否为 A 股交易时段"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute

    def t(h_, m_):
        return h_ * 60 + m_

    cur = h * 60 + m
    morning     = t(*MARKET_MORNING_START) <= cur <= t(*MARKET_MORNING_END)
    afternoon   = t(*MARKET_AFTERNOON_START) <= cur <= t(*MARKET_AFTERNOON_END)
    return morning or afternoon


def next_market_seconds(now: Optional[datetime] = None) -> int:
    """距离下次开市还有多少秒（用于启动前 sleep）"""
    if now is None:
        now = datetime.now()
    h, m = now.hour, now.minute
    cur  = h * 60 + m

    # 今天是否还有下午时段
    afternoon_start = MARKET_AFTERNOON_START[0] * 60 + MARKET_AFTERNOON_START[1]
    if cur < afternoon_start:
        return (afternoon_start - cur) * 60

    # 检查明天开盘
    tomorrow = now.replace(hour=0, minute=0, second=0) + __import__('datetime').timedelta(days=1)
    morning_start = MARKET_MORNING_START[0] * 60 + MARKET_MORNING_START[1]
    return int((tomorrow.timestamp() - now.timestamp())) + (morning_start * 60)


# ─── 冷却追踪 ─────────────────────────────────────────────

class CooldownTracker:
    """防止同一标的信号在 COOLDOWN 秒内重复推送"""

    def __init__(self, cooldown: int = COOLDOWN):
        self._cooldown = cooldown
        self._last: dict[str, float] = {}

    def can_fire(self, symbol: str) -> bool:
        now = time.time()
        last = self._last.get(symbol, 0)
        if now - last < self._cooldown:
            return False
        self._last[symbol] = now
        return True

    def purge_old(self):
        now = time.time()
        self._last = {k: v for k, v in self._last.items() if now - v < self._cooldown}


# ─── 主监控类 ─────────────────────────────────────────────

class IntradayMonitor:
    """
    盘中信号监控后台线程。
    """

    def __init__(self, svc, check_interval: int = CHECK_INTERVAL):
        self._svc       = svc
        self._interval  = check_interval
        self._stop_evt  = threading.Event()
        self._thread:   Optional[threading.Thread] = None
        self._cooldown  = CooldownTracker()
        self._running   = False

    # ── Public API ────────────────────────────────────────

    def start(self):
        if self._running:
            logger.warning('Monitor already running')
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name='IntradayMonitor', daemon=True)
        self._thread.start()
        self._running = True
        logger.info('IntradayMonitor started (interval=%ds)', self._interval)

    def stop(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False
        logger.info('IntradayMonitor stopped')

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internal ───────────────────────────────────────────

    def _run(self):
        logger.info('Monitor thread active, checking market hours...')

        while not self._stop_evt.is_set():
            now = datetime.now()

            if not is_market_open(now):
                # 非交易时段：sleep 到下次开盘
                wait = next_market_seconds(now)
                logger.info('Market closed. Sleeping %ds until next open', wait)
                # 分段 sleep，方便快速响应 stop
                for _ in range(min(wait, 3600)):  # 最多等1小时再检查
                    if self._stop_evt.wait(timeout=1):
                        return
                continue

            # 交易时段：检查信号
            try:
                self._check_and_push(now)
            except Exception as e:
                logger.error('Signal check error: %s', e)

            # 清理过期冷却记录
            self._cooldown.purge_old()

            # 等待下次检查
            self._stop_evt.wait(timeout=self._interval)

    def _check_and_push(self, now: datetime):
        """获取持仓 → 检查信号 → 推送飞书"""
        # 获取当前持仓
        try:
            positions = self._svc.get_positions()
        except Exception as e:
            logger.warning('get_positions failed: %s', e)
            return

        if not positions:
            logger.debug('No positions, skipping signal check')
            return

        # 只保留有 symbol 的持仓
        pos_list = [
            {'symbol': p.get('symbol'), 'shares': p.get('shares', 0),
             'rsi_buy': 35, 'rsi_sell': 70}
            for p in positions
            if p.get('symbol')
        ]
        if not pos_list:
            return

        # 检查信号
        from services.signals import check_portfolio_signals
        alerts = check_portfolio_signals(pos_list)
        if not alerts:
            logger.debug('No signals at %s', now.strftime('%H:%M'))
            return

        # 过滤冷却期内标的
        actionable = [a for a in alerts if self._cooldown.can_fire(a.symbol)]
        if not actionable:
            logger.debug('All alerts in cooldown at %s', now.strftime('%H:%M'))
            return

        # 推送飞书
        check_time = now.strftime('%H:%M')
        msg = format_feishu_message(actionable, check_time)
        if msg:
            self._deliver_alert(msg)
            logger.info('Pushed %d alerts to Feishu at %s', len(actionable), check_time)

    def _deliver_alert(self, text: str):
        """通过飞书 IM API 推送文本消息给用户。"""
        app_id = 'cli_a9217a3f3f389cc2'
        app_secret = '5kOAKAmFzhySMYQB9nV5ndInIlWS43mt'
        user_open_id = 'ou_b8add658ac094464606af32933a02d0b'

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
