"""Step 8 测试：上游客户端（app/services/provider.py）。

用 httpx.MockTransport 打桩上游，覆盖：
- 流式增量文本与 usage 捕获、请求体/请求头断言；
- 错误分类：401/403→auth_failed、429→rate_limited、超时→timeout、
  连接/代理失败→upstream_unreachable、其余→internal；
- max_tokens=None 不传该字段；reasoning_effort 为 None/空串时不传（v0.5①）；代理参数不炸。

说明：环境未安装 pytest-asyncio，async 用例统一用 asyncio.run 在同步
测试函数内驱动（不新增依赖、不修改共享环境）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import httpx
import pytest

from app.services.provider import (
    CODE_AUTH_FAILED,
    CODE_INTERNAL,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    CODE_UPSTREAM_UNREACHABLE,
    ProviderError,
    _build_body,
    stream_chat,
    # 别名避免 pytest 把导入的函数名 test_connection 误收集为测试用例
    test_connection as provider_test_connection,
)

BASE = "https://upstream.example.com/v1"
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


def _sse_response(*chunks: dict) -> httpx.Response:
    """构造 SSE 响应体（OpenAI 兼容流式格式）。"""
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    lines.append("data: [DONE]")
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="\n\n".join(lines) + "\n\n")


def collect(gen: AsyncGenerator[str, None]) -> list[str]:
    """以 asyncio.run 驱动异步生成器，收集全部增量文本。"""
    async def _run() -> list[str]:
        return [text async for text in gen]

    return asyncio.run(_run())


def test_stream_yields_delta_and_usage() -> None:
    """正常流式：增量文本 + 最后 usage 写入 usage_out；请求体/头断言。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return _sse_response(
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}}]},
            {"choices": [{"delta": {"content": "！"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        )

    usage_out: dict[str, Any] = {"usage": None}
    texts = collect(stream_chat(
        BASE, "sk-test", "gpt-x", MESSAGES, 0.7, 0.9, None, None,
        transport=httpx.MockTransport(handler), usage_out=usage_out,
    ))
    assert texts == ["你", "好", "！"]
    assert usage_out["usage"] == {"prompt_tokens": 12, "completion_tokens": 3}
    # 请求体断言（§6.1 第 7 点）
    assert captured["url"] == "https://upstream.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["stream"] is True
    assert captured["body"]["model"] == "gpt-x"
    assert captured["body"]["temperature"] == 0.7
    assert captured["body"]["top_p"] == 0.9
    assert "max_tokens" not in captured["body"]  # None 时不传


def test_stream_max_tokens_included() -> None:
    """max_tokens 非 None 时传入。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _sse_response({"choices": [{"delta": {"content": "x"}}]})

    collect(stream_chat(BASE, "", "m", MESSAGES, 1.0, 1.0, 512, None,
                        transport=httpx.MockTransport(handler)))
    assert captured["body"]["max_tokens"] == 512


def test_build_body_reasoning_effort() -> None:
    """v0.5①：_build_body 仅在 reasoning_effort 非空时加入请求体。"""
    body = _build_body("m", MESSAGES, 1.0, 1.0, None, "high")
    assert body["reasoning_effort"] == "high"
    # None / 空串 = 不设置 → 不发送该字段（部分模型不支持，发送会报错）
    assert "reasoning_effort" not in _build_body("m", MESSAGES, 1.0, 1.0, None)
    assert "reasoning_effort" not in _build_body("m", MESSAGES, 1.0, 1.0, None, None)
    assert "reasoning_effort" not in _build_body("m", MESSAGES, 1.0, 1.0, None, "")


def test_stream_reasoning_effort_included() -> None:
    """v0.5①：stream_chat 设置 reasoning_effort 时请求体包含该字段。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _sse_response({"choices": [{"delta": {"content": "x"}}]})

    texts = collect(stream_chat(
        BASE, "", "m", MESSAGES, 1.0, 1.0, None, None,
        transport=httpx.MockTransport(handler), reasoning_effort="high",
    ))
    assert texts == ["x"]
    assert captured["body"]["reasoning_effort"] == "high"


def test_stream_reasoning_effort_absent_when_none_or_blank() -> None:
    """v0.5①：None / 空串时请求体不含 reasoning_effort 字段。"""
    for value in (None, ""):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return _sse_response({"choices": [{"delta": {"content": "x"}}]})

        collect(stream_chat(
            BASE, "", "m", MESSAGES, 1.0, 1.0, None, None,
            transport=httpx.MockTransport(handler), reasoning_effort=value,
        ))
        assert "reasoning_effort" not in captured["body"], value


def test_stream_tolerates_junk_lines() -> None:
    """容错：跳过无法解析的行/注释行（不中断流）。"""
    text = (
        "data: {\"choices\":[{\"delta\":{\"content\":\"A\"}}]}\n"
        "data: {broken json\n"
        ": keep-alive\n"
        "data: {\"choices\":[{\"delta\":{\"content\":\"B\"}}]}\n"
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    texts = collect(stream_chat(BASE, "", "m", MESSAGES, 1.0, 1.0, None, None,
                                transport=httpx.MockTransport(handler)))
    assert texts == ["A", "B"]


def test_stream_proxy_passed_to_client(monkeypatch) -> None:
    """代理参数被转发给 httpx.AsyncClient（用 Spy 客户端验证）。

    说明：httpx 的 MockTransport 与 proxy 组合会抛错，因此不直接走
    MockTransport + proxy，改为 Spy 包装 AsyncClient 捕获构造参数，
    再注入 MockTransport 完成流式响应。
    """
    from app.services import provider as provider_mod

    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    class SpyClient(real_client):
        def __init__(self, *args, **kwargs):
            # 捕获后从 kwargs 移除 proxy：MockTransport 不支持代理（组合会抛错），
            # 本测试只需证明 provider 把 proxy 传给了客户端。
            captured["proxy"] = kwargs.pop("proxy", None)
            captured["timeout"] = kwargs.get("timeout")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(provider_mod.httpx, "AsyncClient", SpyClient)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response({"choices": [{"delta": {"content": "ok"}}]})

    texts = collect(stream_chat(BASE, "", "m", MESSAGES, 1.0, 1.0, None,
                                "http://127.0.0.1:7890",
                                transport=httpx.MockTransport(handler)))
    assert texts == ["ok"]
    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert isinstance(captured["timeout"], httpx.Timeout)


def test_stream_blank_proxy_means_direct(monkeypatch) -> None:
    """空白代理串应等价于直连（避免被 httpx 当作非法代理地址）。"""
    from app.services import provider as provider_mod

    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    class SpyClient(real_client):
        def __init__(self, *args, **kwargs):
            captured["proxy"] = kwargs.pop("proxy", None)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(provider_mod.httpx, "AsyncClient", SpyClient)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response({"choices": [{"delta": {"content": "ok"}}]})

    collect(stream_chat(BASE, "", "m", MESSAGES, 1.0, 1.0, None, "   ",
                        transport=httpx.MockTransport(handler)))
    assert captured["proxy"] is None


def test_stream_aclose_releases_upstream() -> None:
    """消费者 aclose()（停止生成）→ 关闭上游流（资源释放，§4.2 停止语义）。"""

    class RecordingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            await asyncio.sleep(30)  # 未 aclose 时会挂住

        async def aclose(self) -> None:
            self.closed = True

    stream = RecordingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=stream)

    async def scenario() -> None:
        gen = stream_chat(BASE, "k", "m", MESSAGES, 1.0, 1.0, None, None,
                          transport=httpx.MockTransport(handler))
        assert await gen.__anext__() == "A"
        await gen.aclose()
        assert stream.closed is True

    asyncio.run(scenario())


def test_stream_cancellation_not_mapped_to_provider_error() -> None:
    """取消（CancelledError）必须原样传播，不能被错误映射吞成 internal。

    停止生成依赖取消一路向上传播到 chat 路由，由后者落库 interrupted=1。
    """

    class CancelStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            raise asyncio.CancelledError()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=CancelStream())

    async def scenario() -> None:
        gen = stream_chat(BASE, "k", "m", MESSAGES, 1.0, 1.0, None, None,
                          transport=httpx.MockTransport(handler))
        assert await gen.__anext__() == "A"
        with pytest.raises(asyncio.CancelledError):
            await gen.__anext__()

    asyncio.run(scenario())


# ---------- 错误分类（§6.1 第 10 点） ----------


def _expect_code(handler, expected_code: str, *,
                 proxy: str | None = None, assert_status: int | None = None) -> None:
    """驱动一次流式调用并断言抛出的 ProviderError 分类。"""
    with pytest.raises(ProviderError) as exc_info:
        collect(stream_chat(BASE, "k", "m", MESSAGES, 1.0, 1.0, None, proxy,
                            transport=httpx.MockTransport(handler)))
    assert exc_info.value.code == expected_code
    if assert_status is not None:
        assert exc_info.value.status_code == assert_status


def test_error_401_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    _expect_code(handler, CODE_AUTH_FAILED, assert_status=401)


def test_error_403_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    _expect_code(handler, CODE_AUTH_FAILED, assert_status=403)


def test_error_429_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota"}})

    _expect_code(handler, CODE_RATE_LIMITED, assert_status=429)


def test_error_500_internal() -> None:
    """其余 HTTP 状态 → internal。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _expect_code(handler, CODE_INTERNAL, assert_status=500)


def test_error_timeout() -> None:
    """超时 → timeout（handler 抛 ReadTimeout 模拟）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _expect_code(handler, CODE_TIMEOUT)


def test_error_connect_failed_unreachable() -> None:
    """连接失败 → upstream_unreachable。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _expect_code(handler, CODE_UPSTREAM_UNREACHABLE)


def test_error_proxy_failed_unreachable() -> None:
    """代理失败 → upstream_unreachable。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("proxy dead")

    _expect_code(handler, CODE_UPSTREAM_UNREACHABLE, proxy="http://127.0.0.1:1")


def test_error_unexpected_internal() -> None:
    """未知异常 → internal。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    _expect_code(handler, CODE_INTERNAL)


def test_error_mid_stream_classified() -> None:
    """流中途断连（先产出部分文本后 ReadError）→ upstream_unreachable。"""

    class _CutStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"\xe9\x83\xa8"}}]}\n\n'
            raise httpx.ReadError("connection cut")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_CutStream())

    async def scenario() -> None:
        gen = stream_chat(BASE, "k", "m", MESSAGES, 1.0, 1.0, None, None,
                          transport=httpx.MockTransport(handler))
        # 第一个增量正常产出
        first = await gen.__anext__()
        assert first == "部"
        # 继续迭代时网络中断 → 分类为 upstream_unreachable
        with pytest.raises(ProviderError) as exc_info:
            await gen.__anext__()
        assert exc_info.value.code == CODE_UPSTREAM_UNREACHABLE

    asyncio.run(scenario())


# ---------- 真实流式响应上的错误映射（响应体未预读的回归） ----------


def _streaming_error_handler(status: int, body: bytes):
    """模拟真实上游：错误响应以流式通道返回（client.stream 不预读响应体）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(body))

    return handler


def test_error_streaming_401_auth_failed() -> None:
    """回归：流式错误体未预读时，401 仍必须映射 auth_failed。

    若在 raise_for_status 后直接访问 resp.text 会抛 ResponseNotRead，
    导致错误码退化为 internal（违反 §6.1 第 10 点）。
    """
    handler = _streaming_error_handler(401, b'{"error":{"message":"bad key"}}')
    _expect_code(handler, CODE_AUTH_FAILED, assert_status=401)


def test_error_streaming_429_rate_limited() -> None:
    handler = _streaming_error_handler(429, b'{"error":{"message":"quota"}}')
    _expect_code(handler, CODE_RATE_LIMITED, assert_status=429)


def test_error_streaming_500_detail_extracted() -> None:
    """其余状态仍为 internal，且尽量带上上游 error.message 详情。"""
    handler = _streaming_error_handler(500, b'{"error":{"message":"boom detail"}}')
    with pytest.raises(ProviderError) as exc_info:
        collect(stream_chat(BASE, "k", "m", MESSAGES, 1.0, 1.0, None, None,
                            transport=httpx.MockTransport(handler)))
    assert exc_info.value.code == CODE_INTERNAL
    assert exc_info.value.status_code == 500
    assert "boom detail" in exc_info.value.message


# ---------- test-connection（provider 层） ----------


def test_test_connection_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is False
        assert body["max_tokens"] == 1
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    async def scenario() -> None:
        result = await provider_test_connection(BASE, "k", "m", None,
                                                transport=httpx.MockTransport(handler))
        assert result == {"ok": True}

    asyncio.run(scenario())


def test_test_connection_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad"}})

    async def scenario() -> None:
        with pytest.raises(ProviderError) as exc_info:
            await provider_test_connection(BASE, "k", "m", "",
                                           transport=httpx.MockTransport(handler))
        assert exc_info.value.code == CODE_AUTH_FAILED
        assert "API Key" in exc_info.value.message

    asyncio.run(scenario())
