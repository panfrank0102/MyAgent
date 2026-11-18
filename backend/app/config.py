"""全局配置与日志初始化。

本模块只放"跨模块共享的常量与工具"，业务配置（如会话模型参数默认值）
放在各自模块中，避免循环依赖。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ---- 版本 ----
VERSION = "0.1.0"

# ---- 路径 ----
BASE_DIR = Path(__file__).resolve().parent.parent  # backend/ 目录
DATA_DIR = BASE_DIR / "data"                       # 运行时数据目录（数据库、日志）
DB_PATH = DATA_DIR / "app.db"                      # SQLite 数据库文件（Step 2 启用）
LOG_PATH = DATA_DIR / "backend.log"                # 日志文件

# ---- 服务 ----
DEFAULT_HOST = "127.0.0.1"  # 仅绑定本机回环地址，防止局域网访问（指导 §4.1）
DEFAULT_PORT = 8765         # 默认端口；被占用时由前端 BackendManager 自行 +1 重试


def ensure_dirs() -> None:
    """确保运行时目录存在（幂等，可重复调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    """初始化日志：同时输出到控制台与 data/backend.log（UTF-8）。"""
    ensure_dirs()
    root = logging.getLogger()
    # 避免重复初始化（uvicorn reload 等场景）
    if root.handlers:
        return

    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
