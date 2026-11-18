"""会话与消息接口路由（开发步骤 Step 3，契约：指导 §4.3 / §5.1）。

端点一览（前缀 /api/sessions）：
    GET    ""                                会话列表（updated_at 倒序、含 message_count）
    POST   ""                                新建会话（默认值按 §5.1 create_session 契约）
    GET    "/{session_id}"                   会话详情（v0.6：含 model_preset_id）
    PATCH  "/{session_id}"                   部分更新（model_preset_id 校验预设存在；打码字段已移除）
    DELETE "/{session_id}"                   删除会话（级联删除消息与世界书条目）
    GET    "/{session_id}/messages"          历史消息（?limit=200，最新 limit 条按 id 正序，含 interrupted）
    DELETE "/{session_id}/messages"          清空历史
    DELETE "/{session_id}/messages/{msg_id}" 删单条（级联删除该条及其后，返回 {"deleted": N}）

v0.6：会话不再持有 base_url / api_key / model / reasoning_effort（模型配置集中在
全局模型预设，会话通过 model_preset_id 引用）；PATCH 传入不存在的预设 →
400 bad_request（中文提示）。
错误统一 {"error": {"code", "message"}}（§4.3）；会话不存在 404、消息不存在 404、
无效参数 422。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app import db
from app.schemas import (
    MessageOut,
    SessionCreate,
    SessionListOut,
    SessionOut,
    SessionUpdate,
    api_error,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ---------------------------------------------------------------------------
# 会话 CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[SessionListOut])
def list_sessions() -> list[dict]:
    """会话列表：按 updated_at 倒序、含 message_count（§4.3）。

    v0.6：会话已无 api_key 等模型配置字段（改由 model_preset_id 引用预设）。
    """
    return db.list_sessions()


@router.post("", response_model=SessionOut)
def create_session(payload: SessionCreate):
    """新建会话：body {"name": "..."}；其余字段按 §5.1 create_session 默认值。

    返回完整会话（v0.6：model_preset_id 默认 NULL = 未选择预设）。
    """
    return db.create_session(payload.name)


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: str):
    """会话详情：完整配置（v0.6：含 model_preset_id，§4.3）。"""
    session = db.get_session(session_id)
    if session is None:
        return api_error(404, "session_not_found", "会话不存在")
    return session


@router.patch("/{session_id}", response_model=SessionOut)
def update_session(session_id: str, payload: SessionUpdate):
    """部分更新会话（§4.3 PATCH）。

    语义要点：
    - 仅更新传入字段（exclude_unset，客户端未传的字段不动）；
    - 所有字段显式 null = 未提供/不更新，例外 max_tokens（清除配置）与
      model_preset_id（清除预设选择），由 DAO 兜底；
    - model_preset_id 非空时先校验预设存在（v0.6③：不存在 → 400 bad_request
      中文提示），避免会话引用悬空预设；
    - v0.6 起 base_url / api_key / model / reasoning_effort 不再接受（传入被忽略）。
    """
    if db.get_session(session_id) is None:
        return api_error(404, "session_not_found", "会话不存在")
    fields = payload.model_dump(exclude_unset=True)
    preset_id = fields.get("model_preset_id")
    if preset_id is not None and db.get_model_preset(preset_id) is None:
        return api_error(400, "bad_request", "所选模型预设不存在，请重新选择")
    session = db.update_session(session_id, fields)
    if session is None:
        # 极端并发下会话刚被删除
        return api_error(404, "session_not_found", "会话不存在")
    return session


@router.delete("/{session_id}")
def delete_session(session_id: str) -> dict:
    """删除会话：级联删除其消息与世界书条目（§4.3）。"""
    if not db.delete_session(session_id):
        return api_error(404, "session_not_found", "会话不存在")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 消息接口
# ---------------------------------------------------------------------------


@router.get("/{session_id}/messages", response_model=list[MessageOut])
def list_messages(session_id: str, limit: int = Query(default=200, description="返回条数")):
    """历史消息：最新 limit 条，按 id 正序返回，含 interrupted 标记（§4.3）。

    limit 默认 200；limit < 1 视为无效参数 422。上界 10000 是宽松防溢出保护
    （防止 10**19 这类值让 SQLite LIMIT 绑定抛 OverflowError → 500），
    不是业务语义范围；超界同样按 invalid_param 返回 422。
    """
    if db.get_session(session_id) is None:
        return api_error(404, "session_not_found", "会话不存在")
    if limit < 1 or limit > 10000:
        return api_error(422, "invalid_param", "limit 必须为 1~10000 的整数")
    return db.list_messages(session_id, limit)


@router.delete("/{session_id}/messages")
def clear_messages(session_id: str) -> dict:
    """清空历史消息（§4.3），返回删除条数。"""
    if db.get_session(session_id) is None:
        return api_error(404, "session_not_found", "会话不存在")
    deleted = db.clear_messages(session_id)
    return {"deleted": deleted}


@router.delete("/{session_id}/messages/{message_id}")
def delete_message(session_id: str, message_id: int) -> dict:
    """删单条：级联删除该条及其后所有消息，返回 {"deleted": N}（§4.3）。

    DAO 内部先校验锚点消息存在（不存在时返回 0 且不删任何消息），
    避免"删一条不存在的消息却误删其后全部"；0 条 → 消息不存在 404。
    """
    if db.get_session(session_id) is None:
        return api_error(404, "session_not_found", "会话不存在")
    deleted = db.delete_message_and_after(session_id, message_id)
    if deleted == 0:
        return api_error(404, "message_not_found", "消息不存在")
    return {"deleted": deleted}
