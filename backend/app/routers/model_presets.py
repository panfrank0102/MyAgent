"""全局模型预设接口（v0.6）：/api/model-presets。

本文件当前为**骨架**（仅注册 APIRouter 前缀，无任何端点），由 backend-presets 智能体
实现全部端点（见《需求与方案指导.md》v0.6 变更 ② 与 §4.3 契约）：

- GET    /api/model-presets                 列表（api_key 打码）
- POST   /api/model-presets                 新建
- PATCH  /api/model-presets/{id}            更新（api_key null/空 = 不更新）
- DELETE /api/model-presets/{id}            删除（引用会话 model_preset_id 置 NULL）
- POST   /api/model-presets/{id}/test-connection   用已保存预设测试连通性

main.py 的注册由 lead 完成，智能体不得修改 main.py。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/model-presets", tags=["model-presets"])
