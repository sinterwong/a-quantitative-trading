# -*- coding: utf-8 -*-
"""
channels — 多渠道消息推送抽象层
=================================

架构：
  Channel (ABC)
  ├── name          渠道名称
  ├── send()        发送消息（返回 bool）
  ├── health_check()  渠道健康检查
  └── format()      消息格式化（可选，子类覆盖）

  FeishuChannel    飞书（已实现）
  TelegramChannel Telegram（预留）
  DiscordChannel  Discord（预留）

用法：
  from channels import ChannelManager, FeishuChannel

  cm = ChannelManager()
  cm.register(FeishuChannel(...), primary=True)
  cm.send_all("Hello")

  # 或直接用 manager 的全局实例
  from channels import global_manager
  global_manager().send_all("Report content")
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger('channels')


# ─── 消息类型枚举 ────────────────────────────────────────────────

class MessageType(Enum):
    TEXT       = 'text'       # 纯文本
    MARKDOWN   = 'markdown'   # Markdown 格式
    CARD       = 'card'       # 卡片（飞书特有）
    HTML       = 'html'       # HTML 格式（Telegram/Discord）


# ─── 消息结构 ────────────────────────────────────────────────────

@dataclass
class ReportMessage:
    """
    统一消息结构。所有报告内容都先构造成这个格式，
    再由各 Channel 根据自己的能力格式化发送。
    """
    title: str = ''
    body: str = ''           # 主要内容（markdown 格式）
    msg_type: MessageType = MessageType.MARKDOWN

    # 可选元数据
    timestamp: datetime = field(default_factory=datetime.now)
    tags: List[str] = field(default_factory=list)  # e.g. ['morning', 'stock']

    # 频道特定选项
    feishu_receive_id: Optional[str] = None   # 飞书用户 open_id
    feishu_chat_id: Optional[str] = None        # 飞书群 ID
    telegram_parse_mode: str = 'Markdown'       # HTML / Markdown

    def to_text(self) -> str:
        """降级为纯文本（所有渠道都支持）"""
        if self.title:
            return f"{self.title}\n\n{self.body}"
        return self.body


# ─── Channel 基类 ────────────────────────────────────────────────

class Channel(ABC):
    """
    消息渠道抽象基类。
    所有渠道实现此接口，保持推送逻辑统一。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """渠道名称，如 'feishu' / 'telegram' / 'discord'"""
        ...

    @abstractmethod
    def send(self, message: ReportMessage) -> bool:
        """
        发送消息，返回是否成功。
        失败时返回 False，不抛异常（让 ChannelManager 处理重试/fallback）。
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """检查渠道是否可用（如 token 有效）"""
        ...

    def format(self, message: ReportMessage) -> str:
        """
        将 ReportMessage 格式化为当前渠道能接受的内容格式。
        默认实现：直接返回 body 文本（TEXT 类型）。
        子类可覆盖做格式转换（如 markdown → HTML）。
        """
        return message.to_text()

    def __repr__(self):
        return f"<Channel: {self.name}>"


# ─── ChannelManager 全局单例 ────────────────────────────────────

class ChannelManager:
    """
    多渠道管理 + Fallback 机制。

    用法：
        cm = ChannelManager()
        cm.register(FeishuChannel(...), primary=True)
        cm.send_all("Hello")           # 推送到所有渠道
        cm.send_primary("Hello")        # 仅主渠道
        cm.send_best_effort("Hello")    # 至少一个成功即可
    """

    def __init__(self):
        self._channels: Dict[str, Channel] = {}
        self._primary: Optional[str] = None
        self._enabled: Dict[str, bool] = {}  # 运行时开关

    def register(self, channel: Channel, primary: bool = False,
                 enabled: bool = True) -> None:
        """
        注册渠道。

        Args:
            channel: Channel 实例
            primary: 是否设为主渠道（send_primary 时使用）
            enabled: 是否启用（可动态切换）
        """
        name = channel.name
        self._channels[name] = channel
        self._enabled[name] = enabled
        if primary or self._primary is None:
            self._primary = name
        logger.info("[ChannelManager] Registered: %s (primary=%s)", name, primary)

    def unregister(self, name: str) -> None:
        if name in self._channels:
            del self._channels[name]
            if self._primary == name:
                self._primary = next(iter(self._channels), None)

    def set_enabled(self, name: str, enabled: bool) -> None:
        """运行时开关"""
        if name in self._enabled:
            self._enabled[name] = enabled

    @property
    def primary(self) -> Optional[Channel]:
        if self._primary and self._primary in self._channels:
            return self._channels[self._primary]
        return None

    @property
    def enabled_channels(self) -> List[Channel]:
        return [ch for name, ch in self._channels.items() if self._enabled.get(name, False)]

    def send_all(self, message: ReportMessage) -> Dict[str, bool]:
        """
        推送到所有已启用渠道。
        Returns: {channel_name: success}
        """
        results = {}
        for ch in self.enabled_channels:
            ok = self._safe_send(ch, message)
            results[ch.name] = ok
            status = 'OK' if ok else 'FAIL'
            logger.info("[ChannelManager] %s -> %s: %s", message.timestamp, ch.name, status)
        return results

    def send_primary(self, message: ReportMessage) -> bool:
        """仅推送主渠道，返回是否成功"""
        if self.primary is None:
            logger.warning("[ChannelManager] No primary channel configured")
            return False
        return self._safe_send(self.primary, message)

    def send_best_effort(self, message: ReportMessage) -> bool:
        """
        推送到所有渠道，至少一个成功即可。
        适合不重要/高频率的消息。
        """
        results = self.send_all(message)
        return any(results.values())

    def _safe_send(self, channel: Channel, message: ReportMessage) -> bool:
        """捕获异常，防止一个渠道失败影响其他渠道"""
        try:
            return channel.send(message)
        except Exception as e:
            logger.error("[ChannelManager] %s send failed: %s", channel.name, e)
            return False

    def health_check_all(self) -> Dict[str, bool]:
        """检查所有渠道健康状态"""
        return {name: ch.health_check() for name, ch in self._channels.items()}

    def __repr__(self):
        enabled = [ch.name for ch in self.enabled_channels]
        return f"<ChannelManager primary={self._primary} enabled={enabled}>"


# ─── 全局单例 ─────────────────────────────────────────────────

_global_manager: Optional[ChannelManager] = None

def global_manager() -> ChannelManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = ChannelManager()
    return _global_manager


def setup_default_channels(
    feishu_app_id: str = None,
    feishu_app_secret: str = None,
    feishu_user_id: str = None,
) -> ChannelManager:
    """
    初始化默认渠道（飞书）。
    从环境变量或显式参数读取配置。

    自动从 settings / os.environ 读取，飞书配置：
      FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_USER_ID
    """
    cm = global_manager()

    if feishu_app_id and feishu_app_secret and feishu_user_id:
        from .feishu import FeishuChannel
        fc = FeishuChannel(
            app_id=feishu_app_id,
            app_secret=feishu_app_secret,
            default_receive_id=feishu_user_id,
        )
        cm.register(fc, primary=True)
        logger.info("[ChannelManager] Feishu channel registered")

    return cm
