# -*- coding: utf-8 -*-
"""
telegram.py — Telegram 渠道实现（预留）
======================================
尚未接入，接口已定义。

接入方式：
  1. 配置 BOT_TOKEN 和 CHAT_ID
  2. 实现 send() 方法
  3. 在 ChannelManager 中注册 primary=False

BotFather 创建机器人：https://t.me/BotFather
获取 chat_id：https://t.me/userinfobot
"""

from __future__ import annotations

import logging
from typing import Optional

from . import Channel, ReportMessage, MessageType

logger = logging.getLogger('channels.telegram')


class TelegramChannel(Channel):
    """
    Telegram Bot 渠道（预留实现）。

    配置：
      bot_token: 机器人 Token（从 @BotFather 获取）
      chat_id: 接收消息的 Chat ID（用户或群组）
      parse_mode: 'HTML' / 'Markdown' / 'MarkdownV2'（默认 Markdown）

    特点：
      - 消息最大 4096 字符
      - 支持 HTML 格式（比飞书更完整）
      - 支持图片/文件/音频发送
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        parse_mode: str = 'Markdown',
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode

    @property
    def name(self) -> str:
        return 'telegram'

    def send(self, message: ReportMessage) -> bool:
        """
        发送消息到 Telegram。
        TODO: 实现 HTTP API 调用。
        """
        logger.warning("[Telegram] Not implemented yet")
        return False

    def health_check(self) -> bool:
        """调用 getMe API 检查 bot 是否有效"""
        logger.warning("[Telegram] Not implemented yet")
        return False

    def format(self, message: ReportMessage) -> str:
        """
        将消息格式化为 Telegram 支持的 HTML 或 Markdown。
        注意：Telegram HTML 不支持完整 Markdown。
        """
        text = message.to_text()
        if self.parse_mode == 'HTML':
            # 简单的 Markdown → HTML 转换
            text = text.replace('**', '<b>').replace('**', '</b>')
            text = text.replace('*', '<i>').replace('*', '</i>')
            text = text.replace('`', '<code>').replace('`', '</code>')
        return text
