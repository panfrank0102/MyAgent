"""全局模型预设接口（v0.6）：/api/model-presets。

契约出处：《需求与方案指导.md》v0.6 变更 ② 与 §4.3 / §5 / §5.1。

端点一览（前缀 /api/model-presets）：
    GET    ""                        预设列表（created_at 升序、api_key 打码）
    POST   ""                        新建（name 非空、reasoning_effort 三选一或 null）
    PATCH  "/{preset_id}"            更新（字段可选；api_key null/空 = 不更新）
    DELETE "/{preset_id}"            删除（引用会话 model_preset_id 自动置 NULL）
    POST   "/{preset_id}/test-connection"
                                     用已保存预设测试连通性（使用服务端存储的
                                     base_url/api_key/model；失败也 HTTP 200 + error）

错误统一 {"error": {"code", "message"}}（§4.3）：预设不存在 404 not_found；
参数校验失败 422 validation_error（main.py 全局 handler）；测试连接上游失败
HTTP 200 + {"error": {...}}（与 POST /api/config/test-connection 一致）。

安全约定（§5.1）：任何读路径的 api_key 一律打码；明文仅 POST/PATCH 可写入。
"""

from __future__ import annotations

from fastapi import APIRouter

from app import db
from app.schemas import ModelPresetCreate, ModelPresetOut, ModelPresetUpdate, api_error
from app.services.provider import ProviderError, test_connection

router = APIRouter(prefix="/api/model-presets", tags=["model-presets"])

# 错误码：沿用项目既有本地错误风格（§4.3 / v0.3④）。
CODE_NOT_FOUND = "not_found"
CODE_BAD_REQUEST = "bad_request"


def _preset_not_found_response():
    """预设不存在：404 统一错误格式（前端 TestModelPresetAsync 依赖 ErrorCode=not_found）。"""
    return api_error(404, CODE_NOT_FOUND, "模型预设不存在")


def _masked(preset: dict) -> dict:
    """响应前打码 api_key（DAO 的 create/update 返回原始值，HTTP 层统一打码）。"""
    preset["api_key"] = db.mask_key(preset.get("api_key"))
    return preset


@router.get("", response_model=list[ModelPresetOut])
def list_model_presets() -> list[dict]:
    """预设列表：按 created_at 升序，api_key 打码（§4.3 / §5.1）。"""
    return db.list_model_presets()


@router.post("", response_model=ModelPresetOut)
def create_model_preset(payload: ModelPresetCreate):
    """新建预设：返回打码 api_key 的完整预设（§4.3）。

    name 非空与 reasoning_effort 取值由 ModelPresetCreate 校验（422）；
    重复名称允许（§5：允许重名）。
    """
    preset = db.create_model_preset(payload.model_dump())
    return _masked(preset)


@router.patch("/{preset_id}", response_model=ModelPresetOut)
def update_model_preset(preset_id: str, payload: ModelPresetUpdate):
    """更新预设：仅更新传入字段；api_key null/空不更新；不存在 404（§4.3）。

    先查存在性再更新：DAO 的 update 在"字段全为空值"时也会返回当前值，
    无法区分"不存在"，故这里显式先取一次（预设不存在 → 404）。
    """
    if db.get_model_preset_raw(preset_id) is None:
        return _preset_not_found_response()
    fields = payload.model_dump(exclude_unset=True)
    preset = db.update_model_preset(preset_id, fields)
    if preset is None:
        # 极端并发下预设刚被删除
        return _preset_not_found_response()
    return _masked(preset)


@router.delete("/{preset_id}")
def delete_model_preset(preset_id: str) -> dict:
    """删除预设：引用它的会话 model_preset_id 自动置 NULL；不存在 404（§4.3）。"""
    if not db.delete_model_preset(preset_id):
        return _preset_not_found_response()
    return {"deleted": True}


@router.post("/{preset_id}/test-connection")
async def test_preset_connection(preset_id: str) -> dict:
    """用已保存预设测试连通性（§4.3 v0.6）。

    - 使用服务端存储的 base_url / api_key / model（明文 Key 不下发前端）；
    - 预设不存在 → 404 统一错误格式；
    - Base URL / 模型名为空 → HTTP 200 + error(bad_request)（与 /api/config/
      test-connection 的"失败也 200"约定一致，前端直接解析 body）；
    - 上游失败 → HTTP 200 + {"error": {"code", "message"}}（provider 错误分类：
      auth_failed / rate_limited / timeout / upstream_unreachable / internal）。
    """
    preset = db.get_model_preset_raw(preset_id)
    if preset is None:
        return _preset_not_found_response()

    base_url = (preset.get("base_url") or "").strip()
    model = (preset.get("model") or "").strip()
    if not base_url or not model:
        return {"error": {"code": CODE_BAD_REQUEST,
                          "message": "预设的 Base URL 与模型名不能为空"}}

    try:
        # 预设不存代理；连通性测试走直连（空串 = 直连，§5）。
        await test_connection(base_url, preset.get("api_key") or "", model, "")
    except ProviderError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}
    return {"ok": True, "model": model, "message": "连接成功，模型可用"}
