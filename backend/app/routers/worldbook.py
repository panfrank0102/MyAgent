"""每会话世界书接口路由（开发步骤 Step 4，契约：指导 §4.3 / §5）。

端点一览（前缀 /api/sessions/{session_id}/worldbook）：
    GET    ""               该会话世界书列表（按 sort_order 升序）
    POST   ""               新建条目 {"name","keywords":[..],"content","enabled","sort_order"}
    PATCH  "/{entry_id}"    更新条目（字段可选）
    DELETE "/{entry_id}"    删除条目

校验规则（§5 / Step 4）：
- content 去空白后非空才能保存（否则 422）；
- keywords 传入列表，存库前 trim / 去空行 / 去重，以换行分隔存 TEXT；
- sort_order 缺省 = 本会话 max+1（DAO 层实现，§5.1）；
- 会话隔离：跨会话访问条目一律 404（get/update/delete 都按 session_id+id 定位）；
- 会话不存在 404。
"""

from __future__ import annotations

from fastapi import APIRouter

from app import db
from app.schemas import (
    WorldbookEntryCreate,
    WorldbookEntryOut,
    WorldbookEntryUpdate,
    api_error,
    normalize_keywords,
)

router = APIRouter(prefix="/api/sessions/{session_id}/worldbook", tags=["worldbook"])


def _check_session(session_id: str) -> bool:
    """会话存在性检查；不存在时由调用方返回 404（§4.3：会话不存在）。"""
    return db.get_session(session_id) is not None


# ---------------------------------------------------------------------------
# 条目 CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[WorldbookEntryOut])
def list_worldbook_entries(session_id: str) -> list[dict]:
    """该会话世界书列表，按 sort_order 升序（§4.3）。

    keywords 在响应模型层由换行 TEXT 拆回 list[str]（见 schemas.WorldbookEntryOut）。
    """
    if not _check_session(session_id):
        return api_error(404, "session_not_found", "会话不存在")
    return db.list_worldbook(session_id)


@router.post("", response_model=WorldbookEntryOut)
def create_worldbook_entry(session_id: str, payload: WorldbookEntryCreate):
    """新建世界书条目（§4.3）。

    - content 非空校验由 Pydantic 模型完成（空白 → 422）；
    - keywords 清洗（trim/去空行/去重）后以换行分隔传入 DAO；
    - sort_order 缺省时 DAO 取本会话 max+1。
    """
    if not _check_session(session_id):
        return api_error(404, "session_not_found", "会话不存在")
    data = payload.model_dump()
    data["keywords"] = normalize_keywords(payload.keywords)
    return db.create_worldbook_entry(session_id, data)


@router.patch("/{entry_id}", response_model=WorldbookEntryOut)
def update_worldbook_entry(session_id: str, entry_id: str, payload: WorldbookEntryUpdate):
    """更新世界书条目，字段可选（§4.3）。

    跨会话访问条目（不属于该会话或不存在）一律 404（会话隔离）。
    """
    if not _check_session(session_id):
        return api_error(404, "session_not_found", "会话不存在")
    fields = payload.model_dump(exclude_unset=True)
    if "keywords" in fields:
        fields["keywords"] = normalize_keywords(fields["keywords"])
    entry = db.update_worldbook_entry(session_id, entry_id, fields)
    if entry is None:
        return api_error(404, "worldbook_entry_not_found", "世界书条目不存在")
    return entry


@router.delete("/{entry_id}")
def delete_worldbook_entry(session_id: str, entry_id: str) -> dict:
    """删除世界书条目；跨会话删除返回 404（会话隔离，§4.3）。"""
    if not _check_session(session_id):
        return api_error(404, "session_not_found", "会话不存在")
    if not db.delete_worldbook_entry(session_id, entry_id):
        return api_error(404, "worldbook_entry_not_found", "世界书条目不存在")
    return {"deleted": True}
