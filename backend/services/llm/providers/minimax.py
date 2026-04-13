"""
providers/minimax.py — MiniMax API Provider (双接口支持)
======================================================
支持两个接口：
  1. Anthropic 兼容接口（优先）：https://api.minimaxi.com/anthropic/v1/messages
  2. OpenAI 兼容接口（备用）：https://api.minimaxi.com/v1/chat/completions

优先使用 Anthropic 接口（与用户提供的 API Format 一致）。
当 Anthropic 接口返回 529 时，自动切换到 OpenAI 接口。
"""

import os
import time
import urllib.request
import urllib.error
import json
import logging
from typing import Optional

from backend.services.llm.providers.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class MiniMaxProvider(LLMProvider):
    """
    MiniMax API Provider。

    支持 Anthropic Messages API 和 OpenAI Chat Completions API 两种格式。
    优先使用 Anthropic 接口，遇到 529 (overloaded) 时自动切换。
    """

    name = "minimax"
    supports_streaming = False

    # API endpoints
    ANTHROPIC_URL = "https://api.minimaxi.com/anthropic/v1/messages"
    OPENAI_URL = "https://api.minimaxi.com/v1/chat/completions"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
        preferred_interface: str = "anthropic",
    ):
        """
        Args:
            api_key: MiniMax API Key，留空则从环境变量 MINIMAX_API_KEY 读取
            base_url: 覆盖默认 base URL（一般不需要）
            model: 模型名，默认 MiniMax-M2.7
            timeout: 请求超时（秒）
            preferred_interface: "anthropic"（默认）或 "openai"
        """
        self.api_key = api_key or os.environ.get('MINIMAX_API_KEY', '')
        self.model = model or os.environ.get('MINIMAX_MODEL', 'MiniMax-M2.7')
        self.timeout = timeout
        self.preferred_interface = preferred_interface
        self._active_interface = preferred_interface

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], **kwargs) -> LLMResponse:
        """
        通过首选接口发送请求。失败时自动切换备用接口。
        """
        # 先尝试首选接口
        try:
            if self._active_interface == "anthropic" or self.preferred_interface == "anthropic":
                return self._chat_anthropic(messages, **kwargs)
            else:
                return self._chat_openai(messages, **kwargs)
        except MiniMaxOverloadedError:
            # 切换到备用接口
            if self._active_interface == "anthropic":
                logger.warning("MiniMax Anthropic API overloaded (529). Switching to OpenAI interface.")
                self._active_interface = "openai"
                return self._chat_openai(messages, **kwargs)
            else:
                logger.warning("MiniMax OpenAI API overloaded (529). Switching to Anthropic interface.")
                self._active_interface = "anthropic"
                return self._chat_anthropic(messages, **kwargs)

    def _chat_anthropic(self, messages: list[dict], **kwargs) -> LLMResponse:
        """通过 Anthropic Messages API 发送请求"""
        # 解析 system 和 messages
        system_content = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get('role', 'user')
            raw_content = msg.get('content', '')

            if role == 'system':
                system_content = raw_content
            elif role == 'user':
                if isinstance(raw_content, str):
                    content_blocks = [{"type": "text", "text": raw_content}]
                elif isinstance(raw_content, list):
                    content_blocks = raw_content
                else:
                    content_blocks = [{"type": "text", "text": str(raw_content)}]
                anthropic_messages.append({"role": "user", "content": content_blocks})
            elif role == 'assistant':
                if isinstance(raw_content, str):
                    content_blocks = [{"type": "text", "text": raw_content}]
                elif isinstance(raw_content, list):
                    content_blocks = raw_content
                else:
                    content_blocks = [{"type": "text", "text": str(raw_content)}]
                anthropic_messages.append({"role": "assistant", "content": content_blocks})

        payload = {
            "model": kwargs.get('model', self.model),
            "messages": anthropic_messages,
            "max_tokens": kwargs.get('max_tokens', 1024),
        }

        if system_content:
            payload["system"] = system_content

        if kwargs.get('temperature') is not None:
            payload["temperature"] = kwargs['temperature']

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'anthropic-version': '2023-06-01',
        }

        return self._http_post(self.ANTHROPIC_URL, payload, headers, self._parse_anthropic_response)

    def _chat_openai(self, messages: list[dict], **kwargs) -> LLMResponse:
        """通过 OpenAI Chat Completions API 发送请求"""
        # OpenAI 格式：content 是简单字符串
        openai_messages = []
        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            if isinstance(content, list):
                # 取第一个 text 块
                content = next((c.get('text', '') for c in content if c.get('type') == 'text'), str(content[0]) if content else '')
            openai_messages.append({"role": role, "content": content})

        payload = {
            "model": kwargs.get('model', self.model),
            "messages": openai_messages,
            "max_tokens": kwargs.get('max_tokens', 1024),
        }

        if kwargs.get('temperature') is not None:
            payload["temperature"] = kwargs['temperature']

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }

        return self._http_post(self.OPENAI_URL, payload, headers, self._parse_openai_response)

    def _http_post(self, url: str, payload: dict, headers: dict, parser) -> LLMResponse:
        """通用的 HTTP POST 逻辑"""
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')

        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8')
                data_resp = json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            if e.code == 529 or 'overloaded' in body.lower():
                raise MiniMaxOverloadedError(f"MiniMax API overloaded (HTTP {e.code})")
            logger.error("MiniMax HTTP %d: %s", e.code, body)
            raise RuntimeError(f"MiniMax API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            logger.error("MiniMax URL error: %s", e.reason)
            raise RuntimeError(f"MiniMax connection error: {e.reason}") from e

        latency_ms = int((time.time() - start) * 1000)
        return parser(data_resp, latency_ms)

    def _parse_anthropic_response(self, data: dict, latency_ms: int) -> LLMResponse:
        """解析 Anthropic API 响应"""
        content_blocks = data.get('content', [])
        content_parts = []
        for block in content_blocks:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    content_parts.append(block.get('text', ''))
                elif block.get('type') == 'thinking':
                    pass  # 跳过推理内容
                elif block.get('type') == 'tool_use':
                    inp = block.get('input', {})
                    content_parts.append(f"[tool_use: {block.get('name', '')}({inp})]")
                elif block.get('type') == 'tool_result':
                    content_parts.append(f"[tool_result: {block.get('content', '')}]")

        content = '\n'.join(content_parts)
        usage = data.get('usage', {})
        return LLMResponse(
            content=content,
            model=data.get('model', self.model),
            usage={
                'prompt_tokens': usage.get('input_tokens', 0),
                'completion_tokens': usage.get('output_tokens', 0),
                'total_tokens': usage.get('input_tokens', 0) + usage.get('output_tokens', 0),
            },
            latency_ms=latency_ms,
        )

    def _parse_openai_response(self, data: dict, latency_ms: int) -> LLMResponse:
        """解析 OpenAI Chat Completions API 响应"""
        choices = data.get('choices', [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get('message', {})
            content = message.get('content', '')
        else:
            content = ''

        usage = data.get('usage', {})
        return LLMResponse(
            content=content,
            model=data.get('model', self.model),
            usage={
                'prompt_tokens': usage.get('prompt_tokens', 0),
                'completion_tokens': usage.get('completion_tokens', 0),
                'total_tokens': usage.get('total_tokens', 0),
            },
            latency_ms=latency_ms,
        )

    def _call(self, prompt: str, **kwargs) -> LLMResponse:
        """直接发送 prompt"""
        return self.chat([{'role': 'user', 'content': [{"type": "text", "text": prompt}]}], **kwargs)


class MiniMaxOverloadedError(RuntimeError):
    """MiniMax API 服务器过载（HTTP 529）"""
    pass
