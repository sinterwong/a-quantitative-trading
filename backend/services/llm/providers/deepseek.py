"""
providers/deepseek.py — DeepSeek API Provider
==============================================
使用 DeepSeek Chat API（OpenAI 兼容格式）。
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


class DeepSeekProvider(LLMProvider):
    """DeepSeek API Provider（OpenAI 兼容接口）"""

    name = "deepseek"
    supports_streaming = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
    ):
        """
        Args:
            api_key: DeepSeek API Key，留空则从环境变量 DEEPSEEK_API_KEY 读取
            base_url: API endpoint，留空则用 DEEPSEEK_BASE_URL
            model: 模型名，留空则用 DEEPSEEK_MODEL 环境变量，默认 deepseek-chat
            timeout: 请求超时（秒）
        """
        self.api_key = api_key or os.environ.get('DEEPSEEK_API_KEY', '')
        self.base_url = base_url or os.environ.get(
            'DEEPSEEK_BASE_URL', 'https://api.deepseek.com'
        )
        self.model = model or os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
        self.timeout = timeout

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _call(self, prompt: str, **kwargs) -> LLMResponse:
        """调用 DeepSeek Chat Completion API"""
        if not self.api_key:
            raise RuntimeError(
                "DeepSeek API key not configured. "
                "Set DEEPSEEK_API_KEY in .env or pass api_key=... to provider."
            )

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": kwargs.get('model', self.model),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get('temperature', 0.1),
            "max_tokens": kwargs.get('max_tokens', 1024),
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_key}',
                'Accept': 'application/json',
            },
            method='POST',
        )

        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8') if e.fp else ''
            logger.error("DeepSeek HTTP %d: %s", e.code, body)
            raise RuntimeError(f"DeepSeek API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            logger.error("DeepSeek URL error: %s", e.reason)
            raise RuntimeError(f"DeepSeek connection error: {e.reason}") from e

        latency_ms = int((time.time() - start) * 1000)
        usage = data.get('usage', {})
        content = data['choices'][0]['message']['content']

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
