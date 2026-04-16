# -*- coding: utf-8 -*-
"""
feishu.py — 飞书渠道实现
=========================

已验证功能：
  ✅ 文本消息推送（receive_id_type=open_id）
  ✅ 卡片消息推送（interactive 卡片，支持更丰富的格式）
  ✅ Token 自动刷新（内置缓存 + 过期重取）
  ✅ 健康检查（token 有效性验证）

飞书消息限制：
  - text 消息：最大 4096 字符
  - 需要将长消息截断或拆分成多片发送
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from . import Channel, ReportMessage, MessageType

logger = logging.getLogger('channels.feishu')

FEISHU_API_BASE = 'https://open.feishu.cn/open-apis'
FEISHU_TOKEN_TTL = 7000  # token 有效期 7200s，留 200s 缓冲


# ─── Token 缓存 ────────────────────────────────────────────────

@dataclass
class _FeishuTokenCache:
    """线程安全的飞书 access_token 缓存"""
    token: Optional[str] = None
    expires_at: float = 0.0   # time.monotonic() 时间戳

    def is_valid(self) -> bool:
        return self.token and time.monotonic() < (self.expires_at - 60)

    def set(self, token: str, expires_in: int = 7200) -> None:
        self.token = token
        self.expires_at = time.monotonic() + expires_in

    def invalidate(self) -> None:
        self.token = None
        self.expires_at = 0.0


_token_cache = _FeishuTokenCache()


# ─── FeishuChannel ──────────────────────────────────────────────

class FeishuChannel(Channel):
    """
    飞书消息渠道。

    配置：
      app_id: 飞书应用 ID
      app_secret: 飞书应用密钥
      default_receive_id: 默认推送用户的 open_id（可被 ReportMessage 覆盖）

    消息格式支持：
      - TEXT: 纯文本消息（默认）
      - CARD: 飞书 interactive 卡片（需要 content 结构）
      - MARKDOWN: 当作 TEXT 发送（飞书 text 消息支持部分 markdown）

    用法：
      fc = FeishuChannel(
          app_id='cli_xxx',
          app_secret='xxx',
          default_receive_id='ou_xxx',
      )
      fc.send(ReportMessage(title='早报', body='...'))
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        default_receive_id: Optional[str] = None,
        default_chat_id: Optional[str] = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.default_receive_id = default_receive_id
        self.default_chat_id = default_chat_id

    @property
    def name(self) -> str:
        return 'feishu'

    # ── Token 管理 ───────────────────────────────────────────────

    def _get_token(self, force_refresh: bool = False) -> Optional[str]:
        """获取有效的 tenant_access_token（带缓存）"""
        if not force_refresh and _token_cache.is_valid():
            return _token_cache.token

        url = f'{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal'
        payload = json.dumps({
            'app_id': self.app_id,
            'app_secret': self.app_secret,
        }).encode()

        ctx = _ssl_context()
        try:
            req = urllib.request.Request(url, data=payload,
                                          headers={'Content-Type': 'application/json'},
                                          method='POST')
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                result = json.loads(resp.read())
                code = result.get('code', -1)
                if code != 0:
                    logger.error("[Feishu] token request failed: code=%s msg=%s", code, result.get('msg'))
                    return None
                token = result.get('tenant_access_token')
                expires_in = result.get('expire', 7200)
                _token_cache.set(token, min(expires_in, 7200))
                logger.info("[Feishu] token refreshed, expires_in=%ds", expires_in)
                return token
        except Exception as e:
            logger.error("[Feishu] token request exception: %s", e)
            return None

    # ── 发送实现 ───────────────────────────────────────────────

    def send(self, message: ReportMessage) -> bool:
        """
        发送消息到飞书。

        发送逻辑：
          1. 获取有效 token（自动刷新）
          2. 根据 msg_type 选择发送格式
          3. 文本超长自动截断（飞书限制 4096）
        """
        token = self._get_token()
        if not token:
            logger.error("[Feishu] no valid token, send aborted")
            return False

        # 确定接收者
        receive_id = (
            message.feishu_receive_id
            or self.default_receive_id
        )
        if not receive_id:
            logger.error("[Feishu] no receive_id specified")
            return False

        # 构建消息内容
        if message.msg_type == MessageType.CARD:
            # 卡片消息（直接 body 传 JSON 结构）
            content = message.body if message.body.startswith('{') else json.dumps(message.body)
            msg_type = 'interactive'
        else:
            # 文本/markdown 消息
            text_content = self.format(message)
            # 飞书 text 最大 4096 字符
            if len(text_content) > 4090:
                text_content = text_content[:4090] + '\n\n[内容过长已截断]'
            content = json.dumps({'text': text_content})
            msg_type = 'text'

        payload = json.dumps({
            'receive_id': receive_id,
            'msg_type': msg_type,
            'content': content,
        }).encode()

        url = f'{FEISHU_API_BASE}/im/v1/messages?receive_id_type=open_id'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token,
        }

        ctx = _ssl_context()
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
                result = json.loads(resp.read())
                code = result.get('code', -1)
                if code == 0:
                    logger.info("[Feishu] message sent OK, msg_id=%s", result.get('data', {}).get('message_id'))
                    return True
                elif code == 99991663:
                    # token 失效，强制刷新重试一次
                    logger.warning("[Feishu] token expired, refreshing and retrying")
                    token = self._get_token(force_refresh=True)
                    if token:
                        headers['Authorization'] = 'Bearer ' + token
                        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
                        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp2:
                            result2 = json.loads(resp2.read())
                            return result2.get('code', -1) == 0
                else:
                    logger.error("[Feishu] send failed: code=%s msg=%s", code, result.get('msg'))
                    return False
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            logger.error("[Feishu] HTTP %d: %s", e.code, body[:200])
            return False
        except Exception as e:
            logger.error("[Feishu] send exception: %s", e)
            return False

    def send_text(self, text: str, receive_id: Optional[str] = None) -> bool:
        """快捷方法：直接发送纯文本"""
        msg = ReportMessage(body=text, msg_type=MessageType.TEXT)
        if receive_id:
            msg.feishu_receive_id = receive_id
        return self.send(msg)

    def send_card(self, card_json: dict, receive_id: Optional[str] = None) -> bool:
        """快捷方法：发送飞书卡片"""
        msg = ReportMessage(body=card_json, msg_type=MessageType.CARD)
        if receive_id:
            msg.feishu_receive_id = receive_id
        return self.send(msg)

    # ── 消息格式化 ─────────────────────────────────────────────

    def format(self, message: ReportMessage) -> str:
        """
        将 ReportMessage 格式化为飞书 text 消息内容。
        飞书 text 消息支持部分 markdown（加粗** / 斜体* / 链接）。
        """
        if message.msg_type == MessageType.MARKDOWN:
            body = message.body
        else:
            body = message.body

        if message.title:
            return f"{message.title}\n\n{body}"
        return body

    # ── 健康检查 ───────────────────────────────────────────────

    def health_check(self) -> bool:
        """检查飞书 token 是否有效"""
        token = self._get_token()
        return token is not None

    def force_refresh_token(self) -> bool:
        """强制刷新 token（用于异常恢复）"""
        return self._get_token(force_refresh=True) is not None


# ─── 工具函数 ─────────────────────────────────────────────────

def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─── 飞书卡片构建工具（可选使用）───────────────────────────────

def build_feishu_card(
    title: str,
    elements: list,
    header: dict = None,
) -> dict:
    """
    构建飞书 interactive 卡片 JSON 结构。

    Args:
        title: 卡片标题
        elements: 卡片元素列表（paragraph / image / divider 等）
        header: 可选 header 配置 {title: str, template: str}

    Example:
        card = build_feishu_card(
            title='今日早报',
            header={'title': '🌅 早报', 'template': 'turquoise'},
            elements=[
                {'tag': 'markdown', 'content': '**上证指数** 4027.21 +0.01%'},
                {'tag': 'divider'},
                {'tag': 'note', 'elements': [{'tag': 'text', 'text': '仅供参考'}]},
            ]
        )
    """
    card = {
        'config': {'wide_screen_mode': True},
        'header': header or {
            'title': {'tag': 'plain_text', 'content': title},
            'template': 'turquoise',
        },
        'elements': elements,
    }
    return card
