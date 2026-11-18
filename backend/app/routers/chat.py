"""流式对话路由：POST /api/sessions/{id}/chat 与 /regenerate。

契约出处：《需求与方案指导.md》§4.2（NDJSON 流式协议）/ §4.3 / §6.1 / §6.2 / §6.3，
《开发步骤分解.md》Step 8。

每轮处理流程（§6.1）：
  1. 校验：会话存在、content 去空白后非空、该会话无进行中的生成（否则 409）；
  2. 立即存库用户消息（生成失败也不丢用户输入）；
  3. 世界书匹配（本会话条目，最近 scan_rounds 轮 + 当前输入）；
  4. 组装 system（系统提示词 + 世界书块，§6.2）；
  5. 预算裁剪（§6.3，动态预留 system 与当前输入，max_context_chars=0 跳过）；
  6. 组装上游 messages（system 最前、历史居中、用户消息最后）；
  7. 调上游流式接口逐段转发 delta，结束发 done（usage 可空）；
  8. 存库 assistant 完整回复；
  9. 客户端断开（停止生成）→ 中止上游，已生成部分存库 interrupted=1；
  10. 异常映射：401/403→auth_failed、429→rate_limited、超时→timeout、
      连接/代理失败→upstream_unreachable、其余→internal；失败不存库 AI 回复。

NDJSON 事件（§4.2）：每行 json.dumps(obj, ensure_ascii=False) + "\\n"；
顺序 meta（必发，含 matched_entries）→ delta* → done | error（恰好一次且为最后一行）。

设计说明（歧义解释，均已在代码内标注）：
- "轮"（user+assistant 成对）近似为"最近 2×scan_rounds 条历史消息"；
- 4xx 的具体 code 值契约未冻结，本文件采用 not_found/bad_request/generation_in_progress；
- 中断时若已生成文本为空则不落空 assistant 消息（无意义的空气泡）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# db 为冻结 DAO 契约（§5.1，backend-core 实现）。get_session 打码回显，
# 聊天链路必须用 get_session_raw 拿真实 api_key（backend-core 已确认提供）。
from app import db
from app.services import context, prompt_builder, provider, worldbook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# ---- 常量 ----
NDJSON_MEDIA_TYPE = "application/x-ndjson"  # §4.2 冻结的 Content-Type
UPSTREAM_TIMEOUT = 120.0                    # §6.1 第 10 点：默认 120s

# 错误码（§4.3 统一格式 {"error":{"code","message"}}；4xx 的具体 code 值契约未冻结，
# 这里自定并保持与 backend-core 常用风格一致）。
CODE_NOT_FOUND = "not_found"
CODE_BAD_REQUEST = "bad_request"
CODE_GENERATION_IN_PROGRESS = "generation_in_progress"


class ChatRequest(BaseModel):
    """chat 请求模型（§4.3；按所有权约定定义在本文件，schemas.py 归 backend-core）。"""

    content: str = Field(..., description="用户消息内容；去空白后非空")


# ---- 并发控制（§6.1 第 1 点：同会话同一时刻只允许一个生成）----
# 任务要求"模块级 in-flight 集合 + asyncio 锁"。此处临界区只做同步的 set 增删
# （不含 await），用 threading.Lock 实现同一目的，且跨事件循环/线程安全
# （pytest 的 TestClient 每个实例拥有独立事件循环，asyncio.Lock 会绑定到首个
# 循环而报错；生产 uvicorn 单循环下两者行为一致）。已在最终报告标注此偏离。
_in_flight: set[str] = set()
_in_flight_guard = threading.Lock()


def _acquire_generation(session_id: str) -> bool:
    """尝试占用会话的生成权；已被占用返回 False（由调用方回 409）。"""
    with _in_flight_guard:
        if session_id in _in_flight:
            return False
        _in_flight.add(session_id)
        return True


def _release_generation(session_id: str) -> None:
    """释放会话的生成权（幂等）。"""
    with _in_flight_guard:
        _in_flight.discard(session_id)


def _api_error(status_code: int, code: str, message: str) -> JSONResponse:
    """构造统一错误格式的响应（§4.3：{"error":{"code","message"}}）。

    不用 HTTPException：FastAPI 会把 detail 包进 {"detail": ...}，破坏契约形状；
    直接返回 JSONResponse 才能保证 body 顶层就是 "error" 键。
    """
    return JSONResponse(status_code=status_code,
                        content={"error": {"code": code, "message": message}})


def _ndjson(obj: dict[str, Any]) -> str:
    """序列化一行 NDJSON 事件（§4.2：ensure_ascii=False + 换行）。"""
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _matched_meta(matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 match_entries 的输出投影为 meta 事件的 matched_entries（§4.2 示例）。"""
    return [
        {"name": item["entry"].get("name") or "", "hit_keywords": item["hit_keywords"]}
        for item in matched
    ]


def _prepare(session: dict[str, Any], content: str) -> tuple[list[dict[str, Any]], str, list[dict[str, str]]]:
    """执行 §6.1 第 3~6 点：匹配 → 组装 system → 预算裁剪 → 组装 messages。

    前置约定：调用前"当前输入"必须是该会话最后一条 user 消息（chat 已 append，
    regenerate 已删除旧 assistant 回复），因此这里取 history[:-1] 作为纯历史。
    """
    session_id = session["id"]
    history: list[dict[str, Any]] = db.list_messages(session_id) or []
    prev = history[:-1]  # 不含当前输入

    # ---- 第 3 点：世界书匹配 ----
    matched: list[dict[str, Any]] = []
    if session.get("worldbook_enabled", 1):
        entries = db.list_worldbook(session_id)
        scan_rounds = int(session.get("scan_rounds") or 0)
        # "最近 scan_rounds 轮（user+assistant 成对，从新到旧）"近似为
        # "最近 2×scan_rounds 条历史消息"（歧义解释，见文件头说明）。
        window = prev[-2 * scan_rounds:] if scan_rounds > 0 else []
        scan_text = "\n".join([m.get("content") or "" for m in window] + [content])
        matched = worldbook.match_entries(scan_text, entries)

    # ---- 第 4 点：组装 system（系统提示词 + 世界书块）----
    system_text = prompt_builder.build_system(session.get("system_prompt") or "", matched)

    # ---- 第 5 点：预算与裁剪（max_context_chars=0 → 不裁剪）----
    budget = context.budget_for_history(int(session.get("max_context_chars") or 0), system_text, content)
    trimmed = context.trim_history(prev, budget)

    # ---- 第 6 点：上游 messages 数组 ----
    messages = prompt_builder.build_messages(system_text, trimmed, content)
    return matched, system_text, messages


async def _stream_events(
    request: Request,
    session: dict[str, Any],
    content: str,
    matched: list[dict[str, Any]],
    messages: list[dict[str, str]],
) -> AsyncGenerator[str, None]:
    """生成 NDJSON 事件流（§4.2）：meta → delta* → done | error。

    由 chat/regenerate 端点包进 StreamingResponse；本函数负责：
    - 上游流式调用与逐段转发；
    - 客户端断开检测（request.is_disconnected）→ 中止上游、部分文本落库 interrupted=1；
    - done/error 事件（error 时按 §6.1 第 10 点不落库 AI 回复）；
    - 无论何种退出路径都在 finally 释放生成权。
    """
    session_id = session["id"]
    full_text = ""
    stored = False  # 是否已落库 assistant 消息（含中断部分）

    # 上游配置（§6.1 第 7 点）。api_key 用原始值（get_session_raw，见 backend-core 说明）。
    base_url = (session.get("base_url") or "").strip()
    model = (session.get("model") or "").strip()
    api_key = session.get("api_key") or ""
    proxy_url = (session.get("proxy_url") or "").strip() or None
    temperature = session.get("temperature", 1.0)
    top_p = session.get("top_p", 1.0)
    max_tokens = session.get("max_tokens")  # None = 上游默认（§5 sessions 表）
    usage_holder: dict[str, Any] = {"usage": None}

    try:
        # meta 事件必发且为首行（§4.2），未命中时 matched_entries 为空数组；
        # 即使后续因配置缺失/上游失败而发 error，meta 也必须先出现。
        yield _ndjson({
            "type": "meta",
            "turn_id": str(uuid.uuid4()),
            "matched_entries": _matched_meta(matched),
        })

        # 会话未配置模型接口：error 事件（契约未为此定义专属 code，归 internal）。
        if not base_url or not model:
            yield _ndjson({"type": "error", "code": provider.CODE_INTERNAL,
                           "message": "会话未配置模型接口（Base URL 或模型名），请先在会话设置中完成配置"})
            return

        gen = provider.stream_chat(
            base_url, api_key, model, messages,
            temperature, top_p, max_tokens, proxy_url,
            timeout=UPSTREAM_TIMEOUT, usage_out=usage_holder,
        )
        interrupted = False
        try:
            async for text in gen:
                full_text += text
                yield _ndjson({"type": "delta", "text": text})
                # 第 9 点：生成循环中检测客户端断开（前端"停止生成"= 取消请求）。
                if await request.is_disconnected():
                    interrupted = True
                    await gen.aclose()  # 中止上游请求（provider 内部 finally 关闭连接）
                    break
        finally:
            # 任何跳出路径（正常/断开/上游异常）都确保上游生成器被关闭。
            await gen.aclose()

        if interrupted:
            # 第 9 点：把已生成部分存库（interrupted=1）。客户端已断开，不再发事件。
            # 空文本不落库（避免无意义的空气泡，歧义解释见文件头）。
            if full_text:
                try:
                    db.append_message(session_id, "assistant", full_text, interrupted=1)
                    stored = True
                except Exception:  # noqa: BLE001
                    logger.exception("中断部分文本落库失败 session=%s", session_id)
            return

        # 第 8 点：正常完成，先落库完整回复再发 done（落库失败时改为发 error，
        # 保证客户端看到的最终事件与库内状态一致；协议只要求 done/error 恰一次且最后）。
        try:
            db.append_message(session_id, "assistant", full_text)
            stored = True
        except Exception:  # noqa: BLE001
            logger.exception("assistant 回复落库失败 session=%s", session_id)
            yield _ndjson({"type": "error", "code": provider.CODE_INTERNAL,
                           "message": "回复保存失败，请稍后重试"})
            return

        yield _ndjson({"type": "done", "full_text": full_text,
                       "usage": usage_holder.get("usage")})  # usage 可空（§4.2）

    except provider.ProviderError as exc:
        # 第 10 点：上游失败不落库 AI 回复，发 error 事件（中文 message）。
        logger.warning("上游调用失败 session=%s code=%s", session_id, exc.code)
        yield _ndjson({"type": "error", "code": exc.code, "message": exc.message})
    except (asyncio.CancelledError, GeneratorExit):
        # 兜底停止语义：框架级取消（客户端断开但 is_disconnected 尚未及时感知）。
        # 已生成部分同样落库 interrupted=1，然后继续传播取消/退出。
        if full_text and not stored:
            try:
                db.append_message(session_id, "assistant", full_text, interrupted=1)
            except Exception:  # noqa: BLE001
                logger.exception("取消路径部分文本落库失败 session=%s", session_id)
        raise
    except Exception:  # noqa: BLE001
        logger.exception("生成过程发生未预期异常 session=%s", session_id)
        yield _ndjson({"type": "error", "code": provider.CODE_INTERNAL,
                       "message": "生成过程中发生未知错误，请稍后重试"})
    finally:
        _release_generation(session_id)


def _chat_response(request: Request, session: dict[str, Any], content: str) -> StreamingResponse:
    """把已校验会话与输入组装成流式响应（chat/regenerate 共用）。"""
    matched, _system_text, messages = _prepare(session, content)
    return StreamingResponse(
        _stream_events(request, session, content, matched, messages),
        media_type=NDJSON_MEDIA_TYPE,
    )


@router.post("/api/sessions/{session_id}/chat")
async def chat(session_id: str, body: ChatRequest, request: Request):
    """发送消息（流式，§4.2 / §6.1 全流程）。"""
    content = (body.content or "").strip()
    if not content:
        # 第 1 点：content 去空白后非空。
        return _api_error(400, CODE_BAD_REQUEST, "消息内容不能为空")

    # 会话存在性。api_key 用原始值（backend-core 的 get_session_raw，§5.1 打码版仅供 HTTP 回显）。
    session = db.get_session_raw(session_id)
    if session is None:
        return _api_error(404, CODE_NOT_FOUND, "会话不存在")

    # 第 1 点：同会话同时只允许一个生成，否则 409。
    if not _acquire_generation(session_id):
        return _api_error(409, CODE_GENERATION_IN_PROGRESS,
                          "该会话正在生成回复，请等待完成或停止后再试")

    try:
        # 第 2 点：立即存库用户消息（后续生成失败也不丢用户输入）。
        db.append_message(session_id, "user", content)
        return _chat_response(request, session, content)
    except Exception:
        _release_generation(session_id)
        raise


@router.post("/api/sessions/{session_id}/regenerate")
async def regenerate(session_id: str, request: Request):
    """重新生成上一条回复（流式，§4.3 / §6.1）。

    分支（§4.3）：末条为 assistant → 先删除该条再生成；末条为 user → 直接重试
    （以该条为输入、不新增用户消息）；无消息 → 4xx。
    """
    session = db.get_session_raw(session_id)
    if session is None:
        return _api_error(404, CODE_NOT_FOUND, "会话不存在")

    history: list[dict[str, Any]] = db.list_messages(session_id) or []
    if not history:
        return _api_error(400, CODE_BAD_REQUEST, "会话暂无消息，无法重新生成")

    # 先占生成权再删除/重试，避免"删了旧回复却因 409 没生成"。
    if not _acquire_generation(session_id):
        return _api_error(409, CODE_GENERATION_IN_PROGRESS,
                          "该会话正在生成回复，请等待完成或停止后再试")

    try:
        last = history[-1]
        if last["role"] == "assistant":
            # 末条为 assistant：删除该条（它必是最后一条，级联删除等价于删它自身）。
            db.delete_message_and_after(session_id, last["id"])
            # 删除后当前输入 = 其前一条消息（按删除级联语义必为 user 消息）。
            content = history[-2]["content"] if len(history) >= 2 else ""
        else:
            # 末条为 user：直接以其为输入重试，不新增用户消息。
            content = last["content"] or ""
    except Exception:
        _release_generation(session_id)
        raise

    if not content.strip():
        _release_generation(session_id)
        return _api_error(400, CODE_BAD_REQUEST, "上一条消息内容为空，无法重新生成")
    return _chat_response(request, session, content)
