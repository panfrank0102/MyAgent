"""MyAgent 后端应用包。

模块划分（详见《需求与方案指导.md》§7）：
- config.py：路径、日志、版本等全局常量
- db.py：SQLite 存储层（Step 2 实现）
- schemas.py：Pydantic 请求/响应模型（Step 3 实现）
- routers/：HTTP 路由
- services/：业务逻辑（世界书匹配、上下文裁剪、提示词组装、上游客户端）
"""
