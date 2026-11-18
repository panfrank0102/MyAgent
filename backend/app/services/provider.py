"""OpenAI 兼容上游客户端（httpx 流式 + 代理 + 错误分类）。

契约出处：《需求与方案指导.md》§6.1 第 7/10 点 / 《开发步骤分解.md》Step 8。

设计要点：
- stream_chat 为 async 生成器，逐段 yield 增量文本（choices[0].delta.content）；
- 请求体 {"model","messages","stream":true,"temperature","top_p","max_tokens"}，
  max_tokens 为 None 时不传（§6.1 第 7 点）；
- headers 含 Authorization: Bearer <key>；
- 支持每会话 HTTP 代理（proxy_url，空 = 直连，§2 第 20 行）；
- 默认超时 120s（§6.1 第 10 点）；
- 错误统一抛 ProviderError，分类（§6.1 第 10 点）：
    401/403 → auth_failed；429 → rate_limited；超时 → timeout；
    连接/代理失败 → upstream_unreachable；其余 → internal。
- 生成器被 aclose()（前端停止）时通过 async with 关闭响应与 client，
  从而中止上游请求；
- usage_out：可选输出参数（keyword-only，向后兼容建议接口）——
  上游最后一个带 usage 的 chunk 会写入 usage_out["usage"]，供 done 事件使用。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, AsyncGenerator, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

# 错误码常量（§6.1 第 10 点冻结的分类）。
CODE_AUTH_FAILED = "auth_failed"            # 401/403：Key 无效或无权限
CODE_RATE_LIMITED = "rate_limited"          # 429：限流
CODE_TIMEOUT = "timeout"                    # 上游响应超时
CODE_UPSTREAM_UNREACHABLE = "upstream_unreachable"  # 连接/代理失败
CODE_INTERNAL = "internal"                  # 其余错误

# 网络层失败（连接/代理/读写/协议）统一映射为 upstream_unreachable。
# 集中定义一份，避免 stream_chat 与 test_connection 两处 except 列表漂移。
_UNREACHABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ProxyError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.CloseError,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
)


class ProviderError(Exception):
    """上游调用错误（结构化错误码，§6.1 第 10 点）。

    chat 路由捕获后序列化为 NDJSON error 事件；test-connection 捕获后返回
    {"error": {"code","message"}}。
    """

    def __init__(self, code: str, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _endpoint(base_url: str) -> str:
    """拼接 chat/completions 地址：base_url 去尾斜杠 + /chat/completions。"""
    return base_url.rstrip("/") + "/chat/completions"


def _headers(api_key: str) -> dict[str, str]:
    """构造请求头（§6.1 第 7 点：Authorization: Bearer <key>）。

    Accept 头提示上游返回 SSE（对个别实现更友好，属无害增强）。
    """
    headers = {
        "Authorization": f"Bearer {api_key or ''}",
        "Accept": "text/event-stream",
    }
    return headers


def _client(
    timeout: float,
    proxy_url: Optional[str],
    transport: Optional[httpx.AsyncBaseTransport],
) -> httpx.AsyncClient:
    """构造上游 httpx 客户端（统一超时/代理/测试传输注入）。

    proxy_url 先 strip：空白串应等价于"直连"（§2 第 20 行），
    否则会被 httpx 当作非法代理地址而报 upstream_unreachable。
    transport 仅供测试注入 httpx.MockTransport（生产传 None）。
    """
    proxy = (proxy_url or "").strip() or None
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        proxy=proxy,  # 空 = 直连（§2 第 20 行：仅 HTTP 代理，不支持 SOCKS）
        transport=transport,
    )


def _build_body(
    model: str,
    messages: list[dict[str, str]],
    temperature: Optional[float],
    top_p: Optional[float],
    max_tokens: Optional[int],
) -> dict[str, Any]:
    """构造请求体（§6.1 第 7 点）；max_tokens 为 None 时不传该字段。"""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body


def _upstream_detail(resp: httpx.Response) -> str:
    """尽量提取上游错误详情（OpenAI 兼容格式 {"error":{"message":...}}）。

    注意：client.stream() 拿到的响应体默认未读取，直接访问 resp.json()/resp.text
    会抛 httpx.ResponseNotRead。stream_chat 已在非 2xx 时先 aread() 错误体；
    这里仍做全量防御——任何读取失败都返回空串，绝不向上抛异常，否则会把
    401/429 等状态码误分类成 internal（§6.1 第 10 点）。
    """
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if data.get("message"):
                return str(data["message"])
    except Exception:  # noqa: BLE001 —— 解析失败忽略，走兜底文案
        pass
    try:
        # 兜底：截取响应体前 200 字符，避免把整段 HTML 塞给前端。
        return (resp.text or "").strip()[:200]
    except Exception:  # noqa: BLE001 —— 响应体不可读（未 aread）时放弃详情
        return ""


def _classify_httpx_error(exc: Exception) -> ProviderError:
    """把 httpx 异常映射为 ProviderError（§6.1 第 10 点的错误分类）。

    分类顺序说明（httpx 异常继承层级）：
    - HTTPStatusError（独立于 RequestError）：按状态码分类；
    - TimeoutException（TransportError 子类）：先于 NetworkError/RequestError 判断；
    - ConnectError/ProxyError/ReadError 等网络层错误 → upstream_unreachable；
    - 其余 RequestError（如 InvalidURL、DecodingError）→ internal；
    - 非 httpx 异常 → internal。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = _upstream_detail(exc.response)
        if status in (401, 403):
            return ProviderError(CODE_AUTH_FAILED, "上游返回 401/403，认证失败，请检查 API Key", status)
        if status == 429:
            return ProviderError(CODE_RATE_LIMITED, "上游返回 429，请求过于频繁，请稍后再试", status)
        return ProviderError(CODE_INTERNAL, f"上游返回 HTTP {status}：{detail or '未知错误'}", status)

    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(CODE_TIMEOUT, "上游响应超时（120 秒），请稍后重试", None)

    if isinstance(exc, _UNREACHABLE_EXCEPTIONS):
        return ProviderError(CODE_UPSTREAM_UNREACHABLE, "无法连接上游服务，请检查网络或代理设置", None)

    if isinstance(exc, httpx.RequestError):
        return ProviderError(CODE_INTERNAL, f"上游请求失败：{exc}", None)

    return ProviderError(CODE_INTERNAL, "上游请求发生未知错误", None)


@contextmanager
def _translate_errors(scope: str) -> Iterator[None]:
    """把 httpx/未知异常统一转换为 ProviderError（§6.1 第 10 点）。

    流式与测试连接共用同一套映射，避免两份 except 列表长期漂移。
    CancelledError / GeneratorExit 属 BaseException，这里刻意不捕获：
    停止生成必须让取消继续向上传播，由 chat 路由负责落库 interrupted=1。
    """
    try:
        yield
    except ProviderError:
        raise  # 已分类，原样上抛
    except httpx.HTTPStatusError as exc:
        raise _classify_httpx_error(exc) from exc
    except httpx.TimeoutException as exc:
        raise _classify_httpx_error(exc) from exc
    except _UNREACHABLE_EXCEPTIONS as exc:
        raise _classify_httpx_error(exc) from exc
    except httpx.RequestError as exc:
        raise _classify_httpx_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 —— 未知异常兜底为 internal
        logger.exception("%s发生未预期异常：%s", scope, exc)
        raise _classify_httpx_error(exc) from exc


async def stream_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: Optional[float],
    top_p: Optional[float],
    max_tokens: Optional[int],
    proxy_url: Optional[str],
    timeout: float = 120.0,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    usage_out: Optional[dict[str, Any]] = None,
) -> AsyncGenerator[str, None]:
    """调用上游 {base_url}/chat/completions（stream=true），逐段 yield 增量文本。

    - transport：仅供测试注入 httpx.MockTransport（生产不传）；
    - usage_out：可选 dict，生成过程中最后一个含 usage 的 chunk 会写入 usage_out["usage"]，
      chat 路由据此填充 done 事件的 usage（契约允许 usage 为空，因此该参数可选）；
    - 消费者 aclose() 本生成器（前端停止/客户端断开）时，async with 会关闭响应与
      client，从而中止对上游的请求（§4.2 停止语义）；
    - 所有异常统一转为 ProviderError 抛出（§6.1 第 10 点）。
    """
    url = _endpoint(base_url)
    body = _build_body(model, messages, temperature, top_p, max_tokens)
    with _translate_errors("上游调用"):
        # timeout 统一用于连接/读写/池化（契约仅给出总超时 120s，未细分）。
        async with _client(timeout, proxy_url, transport) as client:
            async with client.stream("POST", url, headers=_headers(api_key), json=body) as resp:
                # 非 2xx 时先读取错误体：client.stream() 的响应体默认未读取，
                # 若直接 raise_for_status() 再解析详情会抛 ResponseNotRead，
                # 使 401/403/429 被误分类为 internal（§6.1 第 10 点）。
                if not resp.is_success:
                    await resp.aread()
                # 非 2xx 立即抛 HTTPStatusError（401/403/429 等），由统一映射分类。
                resp.raise_for_status()
                # 解析 SSE：每行 "data: {...}"；"[DONE]" 结束；无法解析的行跳过（容错）。
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue  # 个别实现可能夹带注释/空行，跳过而非中断
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
                    usage = obj.get("usage")
                    if usage and usage_out is not None:
                        usage_out["usage"] = usage


async def test_connection(
    base_url: str,
    api_key: str,
    model: str,
    proxy_url: Optional[str],
    timeout: float = 15.0,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    """测试模型连通性（§4.3 POST /api/config/test-connection 的服务侧实现）。

    非流式最小请求：1-token 对话（max_tokens=1, stream=false）；
    超时默认 15s（交互式按钮场景，比生成 120s 短，契约未冻结该值）。
    成功返回 {"ok": True}；失败抛 ProviderError（由路由转 {"error": {...}}）。
    """
    url = _endpoint(base_url)
    with _translate_errors("测试连接"):
        async with _client(timeout, proxy_url, transport) as client:
            resp = await client.post(
                url,
                headers=_headers(api_key),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                },
            )
            resp.raise_for_status()
    return {"ok": True}
