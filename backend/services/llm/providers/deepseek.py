"""
providers/deepseek.py — DeepSeek API Provider（Anthropic 兼容接口）
==================================================================
使用 Anthropic SDK 通过 DeepSeek Anthropic 兼容端点访问。
"""

import os
import time
import logging
from typing import Optional

import anthropic
from anthropic import Anthropic, APIError, InternalServerError

from backend.services.llm.providers.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

# DeepSeek Anthropic 兼容端点
DEEPSEEK_ANTHROPIC_BASE = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL = "deepseek-chat"  # fallback，优先从 .env DEEPSEEK_MODEL 读取


class DeepSeekProvider(LLMProvider):
    """
    DeepSeek API Provider（Anthropic 兼容接口）。

    使用官方 anthropic SDK，通过 Anthropic 兼容接口访问 DeepSeek。
    """

    name = "deepseek"
    supports_streaming = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 500,
        max_retries: int = 3,
    ):
        """
        Args:
            api_key: DeepSeek API Key，留空则从环境变量 DEEPSEEK_API_KEY 读取
            base_url: Anthropic 兼容端点，留空则用 DEEPSEEK_BASE_URL，默认 deepseek anthropic
            model: 模型名，默认从环境变量 DEEPSEEK_MODEL 读取
            timeout: 请求超时（秒）
            max_retries: API 错误时最大重试次数
        """
        self.api_key = api_key or os.environ.get('DEEPSEEK_API_KEY', '')
        self.base_url = (
            base_url
            or os.environ.get('DEEPSEEK_BASE_URL', DEEPSEEK_ANTHROPIC_BASE)
        )
        self.model = model or os.environ.get('DEEPSEEK_MODEL', DEEPSEEK_MODEL)
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[Anthropic] = None

    def _get_client(self) -> Anthropic:
        if self._client is None:
            self._client = Anthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        return self._client

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], **kwargs) -> LLMResponse:
        """
        通过 Anthropic SDK 发送请求到 DeepSeek。

        注意：DeepSeek 推理模型会先生成 thinking block 再输出 text。
        max_tokens 需要足够大（建议 ≥ 2000）才能在 thinking 之后留出 text token 空间。
        """
        temperature = kwargs.get('temperature')
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
            logger.warning("DeepSeek Anthropic API InternalServerError: %s", e)
            raise RuntimeError(f"DeepSeek API error: {e}") from e
        except APIError as e:
            logger.warning("DeepSeek Anthropic API Error: %s", e)
            raise RuntimeError(f"DeepSeek API error: {e}") from e

        latency_ms = int((time.time() - start) * 1000)

        # 解析响应（thinking block 跳过，只取 text）
        content_parts = []
        for block in response.content:
            if block.type == 'text':
                content_parts.append(block.text)
            elif block.type == 'thinking':
                # DeepSeek 推理模型会先产生 thinking block
                if hasattr(block, 'thinking') and block.thinking:
                    logger.debug("DeepSeek thinking block (omitted): %s...",
                                 block.thinking[:100])
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
