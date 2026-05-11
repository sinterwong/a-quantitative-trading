# -*- coding: utf-8 -*-
"""
data_gateway.http — 统一 HTTP 客户端(整个系统对外网的唯一出口)

提供:
  - HttpClient: 连接池 + UA 轮换 + 超时 + 重试 + 错误归一化
  - parse_jsonp(): 解析腾讯/东方财富的 JSONP 回包
  - HttpError: 网络/HTTP 错误统一异常,gateway 据此触发健康度记录失败

设计:
  - provider 不直接 import requests/urllib,而是通过注入的 HttpClient 发请求
  - 默认单例 get_http_client() 供 provider 复用同一连接池
  - 测试时可注入 mock client 绕过真实网络
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any, Dict, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger("data_gateway.http")


# 默认 UA 池(模拟主流浏览器)
_DEFAULT_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


class HttpError(Exception):
    """统一 HTTP 错误。

    Attributes:
        status: HTTP 状态码(None 表示连接/超时类错误)
        url: 触发错误的 URL
        retriable: 是否值得让上游重试(目前 client 内部已做重试,
            该字段供 gateway 健康度评分参考)
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        url: Optional[str] = None,
        retriable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.url = url
        self.retriable = retriable


# ─── JSONP 解析 ────────────────────────────────────────────────────────────────

_JSONP_RE = re.compile(r"^[A-Za-z_$][\w$]*\((.*)\)\s*;?\s*$", re.DOTALL)


def parse_jsonp(text: str) -> str:
    """从 JSONP 回包剥离回调名,返回内部 JSON 字符串。

    支持形如 `callback({"k": "v"});` / `jQuery_xxx([1,2]);` 等。
    剥离失败时返回原文(让上游 JSON 解析自然失败,而不是隐藏问题)。
    """
    if not text:
        return text
    s = text.strip()
    m = _JSONP_RE.match(s)
    if m:
        return m.group(1)
    return s


# ─── HTTP 客户端 ───────────────────────────────────────────────────────────────


class HttpClient:
    """轻量包装 requests.Session,统一横切关注点。

    用法:
        client = get_http_client()
        text = client.get_text("https://example.com/api", timeout=5)
        data = client.get_json("https://example.com/api.json", params={...})

    特性:
      - 单 Session 连接池(线程安全)
      - 每请求随机 UA(可通过 headers 覆盖)
      - 内置 urllib3 Retry: 5xx / 429 / 连接错误 → 退避重试 2 次
      - 错误归一化为 HttpError
      - 可注入 transport(用于测试)
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_factor: float = 0.3,
        user_agents: tuple[str, ...] = _DEFAULT_UAS,
        session: Optional[requests.Session] = None,
    ):
        self._timeout = timeout
        self._user_agents = user_agents
        self._lock = threading.Lock()

        if session is not None:
            self._session = session
        else:
            self._session = requests.Session()
            retry = Retry(
                total=max_retries,
                connect=max_retries,
                read=max_retries,
                status=max_retries,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "POST"}),
                backoff_factor=backoff_factor,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)

    def _build_headers(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        headers = {"User-Agent": random.choice(self._user_agents)}
        if extra:
            headers.update(extra)
        return headers

    def get_bytes(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        """发起 GET 请求,返回 raw bytes(由调用方决定编码)。"""
        try:
            resp = self._session.get(
                url,
                params=params,
                headers=self._build_headers(headers),
                timeout=timeout or self._timeout,
            )
        except requests.RequestException as exc:
            raise HttpError(
                f"GET {url} 请求失败: {exc}",
                url=url,
                retriable=True,
            ) from exc

        if resp.status_code >= 400:
            raise HttpError(
                f"GET {url} HTTP {resp.status_code}",
                status=resp.status_code,
                url=url,
                retriable=resp.status_code in (429, 500, 502, 503, 504),
            )

        return resp.content

    def get_text(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        encoding: str = "utf-8",
    ) -> str:
        """GET 请求 + 文本解码。encoding 默认 utf-8(新浪需传 gbk)。"""
        raw = self.get_bytes(url, params=params, headers=headers, timeout=timeout)
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError as exc:
            raise HttpError(f"未知编码 {encoding}", url=url) from exc

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        encoding: str = "utf-8",
        jsonp: bool = False,
    ) -> Union[Dict[str, Any], list]:
        """GET 请求 + JSON(可选 JSONP 剥离)解析。

        解析失败抛 HttpError(retriable=False),由 gateway 当作 provider 失败。
        """
        import json

        text = self.get_text(url, params=params, headers=headers,
                             timeout=timeout, encoding=encoding)
        if jsonp:
            text = parse_jsonp(text)
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            preview = text[:200] if text else "<empty>"
            raise HttpError(
                f"GET {url} JSON 解析失败: {exc} | 预览: {preview!r}",
                url=url,
                retriable=False,
            ) from exc

    def post_json(
        self,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Union[Dict[str, Any], list]:
        """POST JSON 请求 → JSON 响应。"""
        try:
            resp = self._session.post(
                url,
                json=json_body,
                params=params,
                headers=self._build_headers(headers),
                timeout=timeout or self._timeout,
            )
        except requests.RequestException as exc:
            raise HttpError(f"POST {url} 请求失败: {exc}", url=url, retriable=True) from exc

        if resp.status_code >= 400:
            raise HttpError(
                f"POST {url} HTTP {resp.status_code}",
                status=resp.status_code,
                url=url,
                retriable=resp.status_code in (429, 500, 502, 503, 504),
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"POST {url} JSON 解析失败: {exc}", url=url) from exc

    def close(self) -> None:
        self._session.close()


# ─── 全局单例 ──────────────────────────────────────────────────────────────────


_client: Optional[HttpClient] = None
_client_lock = threading.Lock()


def get_http_client() -> HttpClient:
    """获取全局 HttpClient 单例(连接池共享)。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = HttpClient()
    return _client


def reset_http_client(client: Optional[HttpClient] = None) -> None:
    """重置/替换全局单例(测试用)。"""
    global _client
    with _client_lock:
        if _client is not None and client is not _client:
            try:
                _client.close()
            except Exception:
                pass
        _client = client


__all__ = [
    "HttpClient",
    "HttpError",
    "get_http_client",
    "reset_http_client",
    "parse_jsonp",
]
