"""路由包。

- config.py：healthz / test-connection / shutdown（test-connection 由 backend-chat 补齐）
- sessions.py：会话与消息接口（backend-core，Step 3）
- worldbook.py：每会话世界书接口（backend-core，Step 4）
- chat.py：流式对话 / regenerate（backend-chat，Step 8）
- model_presets.py：全局模型预设（v0.6，backend-presets）

当前 sessions/worldbook/chat 为占位空路由，由对应智能体直接替换文件内容实现；
main.py 的路由注册已由 lead 统一完成，智能体不得修改 main.py。
"""
