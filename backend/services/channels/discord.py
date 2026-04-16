# -*- coding: utf-8 -*-
"""
discord.py — Discord Webhook 渠道实现（预留）
==============================================
尚未接入，接口已定义。

接入方式：
  1. 在 Discord 服务器创建 Webhook（频道设置 → 集成 → Webhook）
  2. 复制 Webhook URL
  3. 实现 send() 方法
  4. 在 ChannelManager 中注册

Discord Webhook 限制：
  - 每个消息最大 2000 字符
  - 长消息需要拆分成多条发送
  - 支持 Embed（类似卡片）
"""

from __future__ import annotations

import logging
from typing import Optional

from . import Channel, ReportMessage, MessageType

logger = logging.getLogger('channels.discord')


class DiscordChannel(Channel):
    """
    Discord Webhook 渠道（预留实现）。

    配置：
      webhook_url: Discord Webhook URL
      username: 机器人显示名称（可选）

    特点：
      - 纯 HTTP POST，无需认证
      - 支持 Embed 格式（标题/颜色/内容/字段）
      - 每条消息最多 2000 字符
    """

    def __init__(
        self,
        webhook_url: str,
        username: Optional[str] = None,
    ):
        self.webhook_url = webhook_url
        self.username = username or 'Quant Bot'

    @property
    def name(self) -> str:
        return 'discord'

    def send(self, message: ReportMessage) -> bool:
        """
        发送消息到 Discord Webhook。
        TODO: 实现 HTTP POST 调用。
        """
        logger.warning("[Discord] Not implemented yet")
        return False

    def health_check(self) -> bool:
        """验证 Webhook URL 是否可访问（HEAD 请求）"""
        logger.warning("[Discord] Not implemented yet")
        return False

    def format(self, message: ReportMessage) -> str:
        """
        将消息格式化为 Discord 支持的普通文本。
        Discord 的 webhook 消息是纯文本，不解析 markdown。
        """
        # 去掉 markdown 符号（简单处理）
        text = message.to_text()
        text = text.replace('**', '').replace('*', '').replace('`', '').replace('#', '')
        return text
