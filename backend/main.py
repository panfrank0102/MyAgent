"""后端入口：uvicorn 启动脚本。

用法（在 backend/ 目录下执行）：
    python main.py --port 8765

说明：
- 仅绑定 127.0.0.1（指导 §4.1），不接受外部访问；
- 端口被占用时退出报错即可，由前端 BackendManager 负责 +1 重试（最多 20 次）；
- 本机 conda env_1 的 python 全路径：
  D:\\ProgramData\\Conda_Data\\envs\\env_1\\python.exe
  （不要用 conda run，其 envs 目录未注册会报 NoWritableEnvsDirError）。
"""

from __future__ import annotations

import argparse
import logging

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import db
from app.config import DEFAULT_HOST, DEFAULT_PORT, setup_logging
from app.routers import chat as chat_router
from app.routers import config as config_router
from app.routers import model_presets as model_presets_router
from app.routers import sessions as sessions_router
from app.routers import worldbook as worldbook_router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """创建 FastAPI 应用并挂载路由。

    路由模块由各智能体分别实现（sessions/worldbook → backend-core，
    chat → backend-chat）；本函数的注册由 lead 统一维护，其余智能体不得修改。
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期：启动时初始化 SQLite（幂等迁移）。

        DAO 层（db.py）要求先调用 init_db(path)；这里使用默认 DB_PATH。
        各测试不走此入口，用 init_db(tmp) 与自建 app fixture 保持隔离。
        """
        db.init_db()
        logger.info("数据库已就绪（默认路径 app.config.DB_PATH）")
        yield

    app = FastAPI(title="MyAgent Backend", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc: RequestValidationError):
        """把 FastAPI 默认的 422 {"detail": [...]} 统一为契约错误格式。

        契约（指导 §4.3）：所有错误统一 {"error": {"code", "message"}}，
        前端只解析该结构，不再兼容 detail 列表。
        """
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = first.get("msg", "参数校验失败")
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error",
                               "message": f"请求参数校验失败：{loc} {msg}"}},
        )

    app.include_router(config_router.router)
    app.include_router(sessions_router.router)
    app.include_router(worldbook_router.router)
    app.include_router(chat_router.router)
    app.include_router(model_presets_router.router)  # v0.6 全局模型预设
    return app


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="MyAgent 后端服务")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 8765）")
    return parser.parse_args()


def main() -> None:
    """程序入口。"""
    args = parse_args()
    setup_logging()
    app = create_app()
    logger.info("MyAgent 后端启动：http://%s:%s", args.host, args.port)
    # log_config=None：让 uvicorn 使用我们自己的日志配置（控制台 + 文件）
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
