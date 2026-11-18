"""Pydantic 请求/响应模型（API 契约的代码化，开发步骤 Step 3/4）。

契约出处：《需求与方案指导.md》§4.3（REST API 一览）。
所有权约定（§5.1）：本文件仅 backend-core 修改；chat 的请求模型由
backend-chat 定义在 app/routers/chat.py 内部，不放在这里。

设计说明：
- 响应模型中 DB 的 0/1 整型（worldbook_enabled / interrupted / enabled）
  统一经 mode="before" 校验器转成 JSON bool，便于 WPF 端反序列化；
- worldbook 条目的 keywords 在 DB 中为"一行一个"的 TEXT（§5），
  响应模型校验器自动把 TEXT 拆成 list[str]，与请求格式对称；
- 错误响应统一 {"error": {"code", "message"}}（§4.3），由 api_error 生成。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

# ===========================================================================
# 通用工具
# ===========================================================================


def normalize_keywords(keywords: list[str] | None) -> str:
    """关键词清洗：trim → 去空行 → 去重（保持首次出现顺序）→ 换行分隔。

    契约出处：§5 worldbook_entries.keywords「一行一个（保存前 trim、去空行、
    去重；可为空 = 不参与匹配）」与任务要求「keywords 传入列表，存库时
    trim/去空行/去重后以换行分隔存 TEXT」。
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for keyword in keywords or []:
        text = str(keyword).strip()
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return "\n".join(cleaned)


def api_error(status_code: int, code: str, message: str) -> JSONResponse:
    """构造契约统一的错误响应：{"error": {"code": ..., "message": ...}}（§4.3）。

    直接返回 JSONResponse 而非抛出 HTTPException，是为了精确控制响应体
    结构（HTTPException 的 detail 会被 FastAPI 包成 {"detail": ...}）。
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


# ===========================================================================
# 会话（§4.3）
# ===========================================================================


class SessionCreate(BaseModel):
    """POST /api/sessions 请求体：{"name": "..."}（§4.3）。

    其余字段一律按 create_session 契约取默认值，不接受额外配置。
    """

    name: str = ""


class SessionUpdate(BaseModel):
    """PATCH /api/sessions/{id} 请求体：全部字段可选（§4.3 可更新字段清单）。

    - api_key 仅在传入非空值时更新（DAO 层兜底，见 db.update_session）；
    - scan_rounds 最小 0（§5：只扫当前输入）；max_context_chars 最小 0
      （0 = 不裁剪，§6.3）。
    """

    name: str | None = None
    system_prompt: str | None = None
    scan_rounds: int | None = Field(default=None, ge=0)
    worldbook_enabled: bool | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_context_chars: int | None = Field(default=None, ge=0)
    proxy_url: str | None = None


class SessionOut(BaseModel):
    """会话响应模型：完整配置，api_key 已打码（§4.3 / §5.1 安全约定）。

    worldbook_enabled 在 DB 中为 0/1 整型（§5），响应统一输出 bool。
    """

    id: str
    name: str
    system_prompt: str
    scan_rounds: int
    worldbook_enabled: bool
    base_url: str | None
    api_key: str
    model: str | None
    temperature: float
    top_p: float
    max_tokens: int | None
    max_context_chars: int
    proxy_url: str
    created_at: str
    updated_at: str

    @field_validator("worldbook_enabled", mode="before")
    @classmethod
    def _to_bool(cls, value: object) -> bool:
        """0/1（int）→ bool；已是 bool 时原样返回。"""
        return bool(value)


class SessionListOut(SessionOut):
    """GET /api/sessions 列表项：会话详情 + message_count（§4.3）。"""

    message_count: int


# ===========================================================================
# 消息（§4.3）
# ===========================================================================


class MessageOut(BaseModel):
    """消息响应模型（GET /api/sessions/{id}/messages，含 interrupted 标记）。

    interrupted 在 DB 中为 0/1 整型（§5），响应统一输出 bool。
    """

    id: int
    session_id: str
    role: str
    content: str
    interrupted: bool
    created_at: str

    @field_validator("interrupted", mode="before")
    @classmethod
    def _to_bool(cls, value: object) -> bool:
        return bool(value)


# ===========================================================================
# 世界书条目（§4.3 / §5，每会话独立一套）
# ===========================================================================


class WorldbookEntryCreate(BaseModel):
    """POST /api/sessions/{sid}/worldbook 请求体（§4.3）。

    - content 必填且去空白后非空（空禁止保存，§5 / §10）；
    - keywords 传入列表，路由层清洗后以换行分隔存 TEXT；
    - sort_order 缺省 = None → DAO 取本会话 max+1（§5.1）。
    """

    name: str = ""
    keywords: list[str] = Field(default_factory=list)
    content: str
    enabled: bool = True
    sort_order: int | None = None

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        """content 去空白后必须非空，否则 422（§5：空禁止保存）。"""
        if not value.strip():
            raise ValueError("世界书条目内容不能为空")
        return value


class WorldbookEntryUpdate(BaseModel):
    """PATCH /api/sessions/{sid}/worldbook/{eid} 请求体：字段可选（§4.3）。"""

    name: str | None = None
    keywords: list[str] | None = None
    content: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None

    @field_validator("content")
    @classmethod
    def _content_not_blank_if_given(cls, value: str | None) -> str | None:
        """content 一旦传入（非 None）就必须去空白后非空。"""
        if value is not None and not value.strip():
            raise ValueError("世界书条目内容不能为空")
        return value


class WorldbookEntryOut(BaseModel):
    """世界书条目响应模型。

    - keywords：DB 存换行 TEXT（§5），响应拆成 list[str]，与请求对称；
    - enabled：DB 存 0/1 整型，响应输出 bool。
    """

    id: str
    session_id: str
    name: str
    keywords: list[str]
    content: str
    enabled: bool
    sort_order: int

    @field_validator("keywords", mode="before")
    @classmethod
    def _split_keywords(cls, value: object) -> list[str]:
        """TEXT（换行分隔）→ list[str]；已是列表时原样返回。"""
        if isinstance(value, str):
            return [line for line in value.split("\n") if line]
        return value  # type: ignore[return-value]

    @field_validator("enabled", mode="before")
    @classmethod
    def _to_bool(cls, value: object) -> bool:
        return bool(value)
