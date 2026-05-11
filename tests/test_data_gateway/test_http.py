# -*- coding: utf-8 -*-
"""
http.py 单元测试 — HttpClient + JSONP 解析 + 错误归一化。
完全 mock requests.Session,不发真实请求。
"""

from unittest.mock import MagicMock

import pytest
import requests

from core.data_gateway.http import (
    HttpClient,
    HttpError,
    parse_jsonp,
    get_http_client,
    reset_http_client,
)


# ── JSONP 剥离 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("inp,expected", [
    ('callback({"k": "v"});', '{"k": "v"}'),
    ('jQuery_1234([1,2,3]);', '[1,2,3]'),
    ('foo({"a": 1})', '{"a": 1}'),
    ('{"already_json": true}', '{"already_json": true}'),
    ('', ''),
    ('not jsonp at all', 'not jsonp at all'),
])
def test_parse_jsonp(inp, expected):
    assert parse_jsonp(inp) == expected


# ── HttpClient: get_text / get_json / get_bytes ──────────────────────────────


def _make_session(*, status=200, content=b"hello", raise_exc=None) -> requests.Session:
    sess = MagicMock(spec=requests.Session)
    if raise_exc is not None:
        sess.get.side_effect = raise_exc
        return sess
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    sess.get.return_value = resp
    return sess


def test_get_bytes_success():
    sess = _make_session(content=b"\xe4\xbd\xa0\xe5\xa5\xbd")
    client = HttpClient(session=sess)
    assert client.get_bytes("https://x.com") == b"\xe4\xbd\xa0\xe5\xa5\xbd"


def test_get_text_utf8():
    sess = _make_session(content="你好".encode("utf-8"))
    client = HttpClient(session=sess)
    assert client.get_text("https://x.com") == "你好"


def test_get_text_gbk():
    """新浪行情用 GBK,确保支持。"""
    sess = _make_session(content="你好".encode("gbk"))
    client = HttpClient(session=sess)
    assert client.get_text("https://x.com", encoding="gbk") == "你好"


def test_get_json_simple():
    sess = _make_session(content=b'{"a": 1, "b": [2,3]}')
    client = HttpClient(session=sess)
    data = client.get_json("https://x.com")
    assert data == {"a": 1, "b": [2, 3]}


def test_get_json_jsonp():
    sess = _make_session(content=b'cb({"k":"v"});')
    client = HttpClient(session=sess)
    data = client.get_json("https://x.com", jsonp=True)
    assert data == {"k": "v"}


def test_get_json_decode_error():
    sess = _make_session(content=b"<<not json>>")
    client = HttpClient(session=sess)
    with pytest.raises(HttpError) as exc_info:
        client.get_json("https://x.com")
    assert exc_info.value.retriable is False
    assert "JSON 解析失败" in str(exc_info.value)


# ── HttpClient: 错误路径 ───────────────────────────────────────────────────────


def test_http_4xx_not_retriable():
    sess = _make_session(status=404)
    client = HttpClient(session=sess)
    with pytest.raises(HttpError) as exc_info:
        client.get_bytes("https://x.com")
    assert exc_info.value.status == 404
    assert exc_info.value.retriable is False


def test_http_5xx_retriable():
    sess = _make_session(status=503)
    client = HttpClient(session=sess)
    with pytest.raises(HttpError) as exc_info:
        client.get_bytes("https://x.com")
    assert exc_info.value.status == 503
    assert exc_info.value.retriable is True


def test_connection_error_normalized():
    sess = _make_session(raise_exc=requests.ConnectionError("DNS fail"))
    client = HttpClient(session=sess)
    with pytest.raises(HttpError) as exc_info:
        client.get_bytes("https://x.com")
    assert exc_info.value.retriable is True
    assert exc_info.value.status is None


def test_timeout_normalized():
    sess = _make_session(raise_exc=requests.Timeout("too slow"))
    client = HttpClient(session=sess)
    with pytest.raises(HttpError):
        client.get_bytes("https://x.com")


# ── 单例 ─────────────────────────────────────────────────────────────────────


def test_get_http_client_singleton():
    reset_http_client(None)
    a = get_http_client()
    b = get_http_client()
    assert a is b
    reset_http_client(None)


def test_reset_http_client_replaces():
    custom = HttpClient(session=_make_session())
    reset_http_client(custom)
    assert get_http_client() is custom
    reset_http_client(None)


# ── UA 轮换 ──────────────────────────────────────────────────────────────────


def test_user_agent_header_set():
    sess = _make_session()
    client = HttpClient(session=sess)
    client.get_bytes("https://x.com")
    headers = sess.get.call_args.kwargs["headers"]
    assert "User-Agent" in headers
    assert "Mozilla" in headers["User-Agent"]


def test_custom_headers_override():
    sess = _make_session()
    client = HttpClient(session=sess)
    client.get_bytes("https://x.com", headers={"X-Custom": "v"})
    headers = sess.get.call_args.kwargs["headers"]
    assert headers["X-Custom"] == "v"
    assert "User-Agent" in headers  # 仍然带上 UA
