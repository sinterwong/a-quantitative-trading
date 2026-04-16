# -*- coding: utf-8 -*-
"""
telegram.py — Telegram 渠道实现
================================

接入条件（环境变量）：
  TELEGRAM_BOT_TOKEN  — BotFather 创建机器人后获得的 Token
  TELEGRAM_CHAT_ID   — 目标用户/群组的 chat_id

获取方式：
  1. @BotFather → /newbot → 得到 BOT_TOKEN
  2. @userinfobot → 发送任意消息 → 得到你的 chat_id
  3. 群组：把机器人拉进群后，发一条消息到群，
     然后访问 https://api.telegram.org/bot<token>/getUpdates 获取 chat_id

Telegram 消息限制：
  - text 消息最大 4096 字符
  - MarkdownV2 格式较严格，特殊字符需转义
  - 推荐 parse_mode='HTML'（最宽松）
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional

from . import Channel, ReportMessage, MessageType

logger = logging.getLogger('channels.telegram')

TG_API_BASE = 'https://api.telegram.org'


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TelegramChannel(Channel):
    """
    Telegram Bot 渠道。

    配置（通过构造函数或环境变量）：
      bot_token: Bot Token（从 @BotFather 获取）
      chat_id: 目标 chat_id（用户或群组）
      parse_mode: 'HTML'（默认）/ 'MarkdownV2' / None
      max_retries: API 调用重试次数（默认 2）
    """

    def __init__(
        self,
        bot_token: str = '',
        chat_id: str = '',
        parse_mode: str = 'HTML',
        max_retries: int = 2,
    ):
        # 支持构造函数注入或环境变量
        self.bot_token = bot_token or _env('TELEGRAM_BOT_TOKEN', '')
        self.chat_id   = chat_id   or _env('TELEGRAM_CHAT_ID', '')
        self.parse_mode = parse_mode
        self.max_retries = max_retries

    @property
    def name(self) -> str:
        return 'telegram'

    @property
    def configured(self) -> bool:
        """检查是否已配置（token + chat_id 都有值）"""
        return bool(self.bot_token and self.chat_id)

    # ── 发送实现 ───────────────────────────────────────────────

    def send(self, message: ReportMessage) -> bool:
        """
        发送消息到 Telegram。
        策略：parse_mode=HTML（最宽松），内容过长自动拆分。
        """
        if not self.configured:
            logger.warning("[Telegram] 未配置（TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未设置），跳过")
            return False

        text = self.format(message)
        # Telegram 消息 4096 字符限制
        chunks = _split_text(text, 4090)

        all_ok = True
        for chunk in chunks:
            ok = self._send_chunk(chunk)
            if not ok:
                all_ok = False
        return all_ok

    def _send_chunk(self, text: str, retry: int = 0) -> bool:
        """发送单个文本片段"""
        url = f'{TG_API_BASE}/bot{self.bot_token}/sendMessage'
        payload = json.dumps({
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': self.parse_mode if self.parse_mode else None,
            'disable_web_page_preview': True,
        }).encode()

        headers = {'Content-Type': 'application/json'}

        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=12, context=_ssl_context()) as resp:
                result = json.loads(resp.read())
                if result.get('ok'):
                    msg_id = result.get('result', {}).get('message_id', '')
                    logger.info("[Telegram] sent OK, msg_id=%s", msg_id)
                    return True
                else:
                    logger.error("[Telegram] send failed: %s", result.get('description', 'unknown'))
                    return False
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            logger.error("[Telegram] HTTP %d: %s", e.code, body[:200])
            # 429 Too Many Requests 等重试
            if e.code == 429 and retry < self.max_retries:
                import time
                wait = int(e.headers.get('Retry-After', 5))
                logger.info("[Telegram] rate limited, waiting %ds then retry %d/%d", wait, retry+1, self.max_retries)
                time.sleep(wait)
                return self._send_chunk(text, retry=retry + 1)
            return False
        except Exception as e:
            logger.error("[Telegram] send exception: %s", e)
            return False

    # ── 消息格式化 ───────────────────────────────────────────

    def format(self, message: ReportMessage) -> str:
        """
        将 ReportMessage 格式化为 Telegram 支持的内容。
        使用 HTML 模式（最宽松）。
        """
        text = message.body
        if message.title:
            text = f"*{message.title}*\n\n{text}"
        return text

    # ── 健康检查 ─────────────────────────────────────────────

    def health_check(self) -> bool:
        """调用 getMe API 验证 bot 是否有效"""
        if not self.configured:
            return False
        url = f'{TG_API_BASE}/bot{self.bot_token}/getMe'
        try:
            req = urllib.request.Request(url, timeout=8, context=_ssl_context())
            with urllib.request.urlopen(req, timeout=8, context=_ssl_context()) as resp:
                result = json.loads(resp.read())
                if result.get('ok'):
                    bot_info = result.get('result', {})
                    logger.info("[Telegram] health OK, bot=%s", bot_info.get('username'))
                    return True
                return False
        except Exception as e:
            logger.error("[Telegram] health_check failed: %s", e)
            return False

    def send_text(self, text: str) -> bool:
        """快捷方法：直接发送纯文本"""
        return self.send(ReportMessage(body=text, msg_type=MessageType.TEXT))


# ─── 工具函数 ─────────────────────────────────────────────────

def _env(key: str, default: str = '') -> str:
    return __import__('os').environ.get(key, default)


def _split_text(text: str, max_len: int) -> list[str]:
    """按行拆分文本，每块不超过 max_len"""
    if len(text) <= max_len:
        return [text]
    lines = text.split('\n')
    chunks = []
    current = ''
    for line in lines:
        if len(current) + len(line) + 1 <= max_len:
            current = current + '\n' + line if current else line
        else:
            if current:
                chunks.append(current)
            current = line
            # 如果单行就超过限制，强拆
            while len(current) > max_len:
                chunks.append(current[:max_len])
                current = current[max_len:]
    if current:
        chunks.append(current)
    return chunks
