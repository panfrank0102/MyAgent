"""基础配置类端点。

已实现：GET /healthz（健康检查）、POST /api/config/test-connection（测试模型连通性）
待实现（后续步骤）：POST /api/shutdown（优雅关闭）
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import VERSION
from app.services.provider import ProviderError, test_connection

router = APIRouter(tags=["config"])


@router.get("/healthz")
def healthz() -> dict:
    """健康检查。

    前端 BackendManager 启动后端后轮询此端点（500ms 间隔）判断服务是否就绪，
    返回体格式为契约（指导 §4.3）所冻结，不得随意改动。
    """
    return {"status": "ok", "version": VERSION}


class TestConnectionRequest(BaseModel):
    """测试连接请求体（§4.3 POST /api/config/test-connection）。

    base_url/model 必填；api_key/proxy_url 可空（本地模型/直连场景）。
    """

    base_url: str = Field(..., description="上游 Base URL，如 https://api.openai.com/v1")
    api_key: str = Field(default="", description="API Key，可为空")
    model: str = Field(..., description="模型名")
    proxy_url: str = Field(default="", description="HTTP 代理，可为空（直连）")


@router.post("/api/config/test-connection")
async def api_test_connection(body: TestConnectionRequest) -> dict:
    """测试模型连通性（§4.3 / §6.1）。

    服务侧做非流式最小请求（1-token 对话，provider.test_connection，超时 15s）。
    返回约定：无论成败 HTTP 200 ——
      成功：{"ok": true, "model": ..., "message": ...}；
      失败：{"error": {"code": ..., "message": ...}}（统一错误格式 §4.3）。
    说明：契约只写"返回成功或 {"error":...}"，未冻结失败时的 HTTP 状态码；
    这里统一 200 便于前端测试按钮直接解析 body 展示原因（歧义解释，见最终报告）。
    """
    base_url = (body.base_url or "").strip()
    model = (body.model or "").strip()
    if not base_url or not model:
        return {"error": {"code": "bad_request", "message": "Base URL 与模型名不能为空"}}
    try:
        await test_connection(base_url, body.api_key or "", model, body.proxy_url or "")
    except ProviderError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}
    return {"ok": True, "model": model, "message": "连接成功，模型可用"}
