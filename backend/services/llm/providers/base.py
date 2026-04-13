"""
providers/base.py — LLM Provider 抽象基类
==========================================
独立文件，避免循环导入。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: dict  # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    latency_ms: int


class LLMProvider(ABC):
    """LLM Provider 抽象基类"""

    name: str = "base"
    supports_streaming: bool = False

    @abstractmethod
    def _call(self, prompt: str, **kwargs) -> LLMResponse:
        """
        发送请求到 LLM，返回原始响应内容。
        由子类实现具体协议（OpenAI兼容/自定义）。
        """
        raise NotImplementedError

    def chat(self, messages: list[dict], **kwargs) -> LLMResponse:
        """
        聊天补全接口。
        messages: [{"role": "user"|"assistant"|"system", "content": str}, ...]
        """
        prompt = self._messages_to_prompt(messages)
        return self._call(prompt, **kwargs)

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        """将 messages 列表格式化为单个 prompt 字符串"""
        parts = []
        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            if role == 'system':
                parts.append(f"[系统指示]\n{content}")
            elif role == 'user':
                parts.append(f"[用户]\n{content}")
            elif role == 'assistant':
                parts.append(f"[助手]\n{content}")
        return '\n\n'.join(parts)

    @property
    def is_available(self) -> bool:
        """检查 Provider 是否已配置（API key 存在等）"""
        return True
