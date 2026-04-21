"""
providers/minimax.py — MiniMax API Provider（官方 Anthropic SDK）
=============================================================
MiniMax 提供 Anthropic 兼容接口，使用官方 anthropic SDK 访问。
参考：https://www.minimaxi.com/document
"""

import os
import time
import logging
from typing import Optional

import anthropic
from anthropic import Anthropic, APIError, InternalServerError

from backend.services.llm.providers.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

# MiniMax Anthropic 兼容端点
MINIMAX_ANTHROPIC_BASE = "https://api.minimaxi.com/anthropic"
MINIMAX_MODEL = "MiniMax-M2.7"


class MiniMaxProvider(LLMProvider):
    """
    MiniMax API Provider。

    使用官方 anthropic SDK，通过 Anthropic 兼容接口访问 MiniMax。
    """

    name = "minimax"
    supports_streaming = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        """
        Args:
            api_key: MiniMax API Key，留空则从环境变量 MINIMAX_API_KEY 读取
            model: 模型名，默认 MiniMax-M2.7
            timeout: 请求超时（秒）
            max_retries: API 错误时最大重试次数
        """
        self.api_key = api_key or os.environ.get('MINIMAX_API_KEY', '')
        self.model = model or os.environ.get('MINIMAX_MODEL', MINIMAX_MODEL)
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[Anthropic] = None

    def _get_client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic(
                api_key=self.api_key,
                base_url=MINIMAX_ANTHROPIC_BASE,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        return self._client

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], **kwargs) -> LLMResponse:
        """
        通过 Anthropic SDK 发送请求。

        注意：Minimax-M2.7 是推理模型，会先生成 thinking block 再输出 text。
        max_tokens 需要足够大（建议 ≥ 2000）才能在 thinking 之后留出 text token 空间。
        """
        temperature = kwargs.get('temperature')
        # M2.7 是推理模型，需要更大 budget
        max_tokens = kwargs.get('max_tokens', 64000)
        model = kwargs.get('model', self.model)

        # 分离 system 消息
        system_content = ""
        anthropic_messages = []
        for msg in messages:
            role = msg.get('role', 'user')
            raw_content = msg.get('content', '')

            if role == 'system':
                system_content = raw_content if isinstance(
                    raw_content, str) else str(raw_content)
            elif role in ('user', 'assistant'):
                if isinstance(raw_content, str):
                    content_blocks = [{"type": "text", "text": raw_content}]
                elif isinstance(raw_content, list):
                    content_blocks = raw_content
                else:
                    content_blocks = [
                        {"type": "text", "text": str(raw_content)}]
                anthropic_messages.append(
                    {"role": role, "content": content_blocks})

        start = time.time()
        try:
            response = self._get_client().messages.create(
                model=model,
                system=system_content if system_content else None,
                messages=anthropic_messages,
                temperature=temperature if temperature is not None else 0.1,
                max_tokens=max_tokens,
            )
        except InternalServerError as e:
            logger.warning("MiniMax Anthropic API InternalServerError: %s", e)
            raise RuntimeError(f"MiniMax API error: {e}") from e
        except APIError as e:
            logger.warning("MiniMax Anthropic API Error: %s", e)
            raise RuntimeError(f"MiniMax API error: {e}") from e

        latency_ms = int((time.time() - start) * 1000)

        # 解析响应（thinking block 跳过，只取 text）
        content_parts = []
        thinking_parts = []
        for block in response.content:
            if block.type == 'text':
                content_parts.append(block.text)
            elif block.type == 'thinking':
                # M2.7 推理模型会先产生 thinking block，内容不作为回复正文
                if hasattr(block, 'thinking') and block.thinking:
                    thinking_parts.append(block.thinking)
            elif block.type == 'tool_use':
                inp = block.input
                content_parts.append(f"[tool_use: {block.name}({inp})]")
            elif block.type == 'tool_result':
                content_parts.append(f"[tool_result: {block.content}]")

        content = '\n'.join(content_parts)
        usage = response.usage

        return LLMResponse(
            content=content,
            model=response.model or model,
            usage={
                'prompt_tokens': usage.input_tokens or 0,
                'completion_tokens': usage.output_tokens or 0,
                'total_tokens': (usage.input_tokens or 0) + (usage.output_tokens or 0),
            },
            latency_ms=latency_ms,
        )

    def _call(self, prompt: str, **kwargs) -> LLMResponse:
        """实现基类抽象方法"""
        messages = [{'role': 'user', 'content': prompt}]
        return self.chat(messages, **kwargs)
