"""Step 8 测试：chat/regenerate 流式端点全流程（mock 上游）。

策略：
- 数据库：优先使用 backend-core 的真实 app/db.py（tmp_path 临时库）；
  若 db.py 尚未就绪（并行开发中），注入一个最小内存替身（下方
  _make_standin_db，函数签名与 §5.1 契约一致，仅供本测试驱动 chat 链路）。
- 上游：monkeypatch app.services.provider.stream_chat 为假生成器（mock 上游）。
- 应用：只挂 chat + config 两个路由，避免依赖 backend-core 尚未完成的其它路由。

覆盖（《开发步骤分解.md》Step 8 完成标准 + 任务要求）：
meta→delta→done 序列、meta 必发且未命中为空数组、世界书命中 meta、
error 映射（失败不落库 AI 回复）、regenerate 两分支、409 并发、
客户端断开/框架取消 → interrupted=1 落库、test-connection、404/400。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 临时内存替身：backend-core 的 app/db.py 未就绪时的最小实现。
# 仅实现 chat 链路用到的函数（§5.1 冻结签名）；一旦真实 db.py 出现即自动弃用。
# ---------------------------------------------------------------------------


def _make_standin_db() -> types.ModuleType:
    """构建 app.db 的最小内存替身（临时，正式 db.py 就绪后不再使用）。"""
    mod = types.ModuleType("app.db")
    state: dict[str, Any] = {"sessions": {}, "messages": {}, "worldbook": {}, "seq": 0}

    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def init_db(path=None) -> None:  # noqa: ANN001 —— 内存实现，忽略路径
        return None

    def create_session(name: str) -> dict:
        state["seq"] += 1
        sid = f"sid-{state['seq']}"
        row = {"id": sid, "name": name, "system_prompt": "", "scan_rounds": 4,
               "worldbook_enabled": 1, "base_url": None, "api_key": "",
               "model": None, "temperature": 1.0, "top_p": 1.0, "max_tokens": None,
               "max_context_chars": 80000, "proxy_url": "",
               "created_at": _now(), "updated_at": _now()}
        state["sessions"][sid] = row
        state["messages"][sid] = []
        state["worldbook"][sid] = []
        return dict(row)

    def update_session(session_id: str, fields: dict) -> dict | None:
        row = state["sessions"].get(session_id)
        if row is None:
            return None
        for k, v in (fields or {}).items():
            if k == "api_key" and not v:
                continue  # 契约：api_key 空值不更新
            row[k] = v
        row["updated_at"] = _now()
        return dict(row)

    def get_session_raw(session_id: str) -> dict | None:
        """返回含原始 api_key 的会话（聊天链路专用，backend-core 已确认提供）。"""
        row = state["sessions"].get(session_id)
        return dict(row) if row else None

    def append_message(session_id: str, role: str, content: str, interrupted: int = 0) -> dict:
        state["seq"] += 1
        msg = {"id": state["seq"], "session_id": session_id, "role": role,
               "content": content, "interrupted": interrupted, "created_at": _now()}
        state["messages"].setdefault(session_id, []).append(msg)
        s = state["sessions"].get(session_id)
        if s:
            s["updated_at"] = _now()  # 契约：同步更新会话 updated_at
        return dict(msg)

    def list_messages(session_id: str, limit: int = 200) -> list[dict]:
        msgs = state["messages"].get(session_id, [])
        return [dict(m) for m in msgs[-limit:]]  # 最新 limit 条，id 正序

    def delete_message_and_after(session_id: str, message_id: int) -> int:
        msgs = state["messages"].get(session_id, [])
        before = len(msgs)
        state["messages"][session_id] = [m for m in msgs if m["id"] < message_id]
        return before - len(state["messages"][session_id])

    def list_worldbook(session_id: str) -> list[dict]:
        rows = state["worldbook"].get(session_id, [])
        return sorted((dict(r) for r in rows), key=lambda r: r["sort_order"])

    def create_worldbook_entry(session_id: str, data: dict) -> dict:
        state["seq"] += 1
        default_order = max((e["sort_order"] for e in state["worldbook"].get(session_id, [])),
                            default=0) + 1
        entry = {"id": f"wb-{state['seq']}", "session_id": session_id,
                 "name": data.get("name", ""), "keywords": data.get("keywords", ""),
                 "content": data.get("content", ""), "enabled": data.get("enabled", 1),
                 "sort_order": data.get("sort_order", default_order)}
        state["worldbook"].setdefault(session_id, []).append(entry)
        return dict(entry)

    mod.init_db = init_db
    mod.create_session = create_session
    mod.update_session = update_session
    mod.get_session_raw = get_session_raw
    mod.append_message = append_message
    mod.list_messages = list_messages
    mod.delete_message_and_after = delete_message_and_after
    mod.list_worldbook = list_worldbook
    mod.create_worldbook_entry = create_worldbook_entry
    return mod


# 先尝试真实 db.py；不存在则注入内存替身（必须在导入 chat 路由之前完成）。
try:
    from app import db as _db_mod  # backend-core 的真实实现
except ImportError:
    _db_mod = _make_standin_db()
    sys.modules["app.db"] = _db_mod
    import app as _app_pkg
    _app_pkg.db = _db_mod

db = _db_mod

from app.routers import chat as chat_mod          # noqa: E402
from app.routers import config as config_mod      # noqa: E402
from app.services import provider as provider_mod  # noqa: E402
from app.services.provider import CODE_AUTH_FAILED, ProviderError  # noqa: E402

# ---------------------------------------------------------------------------
# 测试工具：mock 上游生成器、应用、fixture
# ---------------------------------------------------------------------------


def make_fake_stream(
    chunks: list[str],
    usage: dict | None = None,
    error: ProviderError | None = None,
    capture: dict | None = None,
) -> Any:
    """构造假的 provider.stream_chat（签名与真实函数一致）。

    capture 非空时把上游请求参数（messages/model/base_url/api_key）写入其中，
    便于断言提示词组装结果。
    """

    async def fake_stream(base_url, api_key, model, messages, temperature, top_p,
                          max_tokens, proxy_url, timeout=120.0, *, transport=None,
                          usage_out=None):
        if capture is not None:
            capture.update({"base_url": base_url, "api_key": api_key, "model": model,
                            "messages": messages, "temperature": temperature,
                            "top_p": top_p, "max_tokens": max_tokens, "proxy_url": proxy_url})
        if usage_out is not None and usage is not None:
            usage_out["usage"] = usage
        for c in chunks:
            yield c
        if error is not None:
            raise error

    return fake_stream


def _build_app() -> FastAPI:
    """只挂 chat + config 路由的最小应用（隔离 backend-core 的进行中路由）。"""
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.include_router(config_mod.router)
    return app


@pytest.fixture()
def env() -> SimpleNamespace:
    """每个测试独立临时库 + 已配置好模型参数的会话 + 应用。

    说明（与任务描述的偏离，已在最终报告标注）：本沙箱会话中 pytest 的
    tmp_path 夹具不可用——%TEMP% 下存在其他智能体进程创建的 pytest-of-*
    根目录，其 ACL 拒绝本会话进程枚举/删除（修复需授权脚本，本会话禁止
    授权），导致 tmp_path 夹具在任何测试上抛 PermissionError。因此改为在
    工作区 backend/.test-tmp 下创建临时目录并在测试结束清理；数据库文件
    绝不使用 data/app.db，满足"不污染生产库"的原意。
    """
    base = Path(__file__).resolve().parent.parent / ".test-tmp"
    base.mkdir(parents=True, exist_ok=True)
    # 注意：不能用 tempfile.mkdtemp——本沙箱下其创建的子目录会拒绝后续文件
    # 写入（PermissionError，与 tmp_path 同源），用 os.makedirs 则正常。
    tmpdir = str(base / f"chatflow-{uuid.uuid4().hex[:8]}")
    os.makedirs(tmpdir, exist_ok=False)
    try:
        db.init_db(Path(tmpdir) / "chat.db")
        session = db.create_session("测试会话")
        db.update_session(session["id"], {
            "base_url": "https://upstream.example.com/v1",
            "api_key": "sk-test-key",
            "model": "gpt-test",
            "system_prompt": "你是助手",
        })
        yield SimpleNamespace(session=db.get_session_raw(session["id"]), app=_build_app())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _events(resp) -> list[dict]:
    """解析 NDJSON 响应为事件列表。"""
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 基础序列与校验
# ---------------------------------------------------------------------------


def test_chat_meta_delta_done_sequence(env, monkeypatch) -> None:
    """完整 NDJSON 序列：meta → delta* → done，且内容与落库一致。"""
    monkeypatch.setattr(provider_mod, "stream_chat",
                        make_fake_stream(["你好", "，世界"], usage={"prompt_tokens": 5, "completion_tokens": 3}))
    client = TestClient(env.app)
    resp = client.post(f"/api/sessions/{env.session['id']}/chat", json={"content": "  你好呀  "})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    events = _events(resp)
    assert [e["type"] for e in events] == ["meta", "delta", "delta", "done"]
    assert events[0]["turn_id"] and isinstance(events[0]["turn_id"], str)
    assert events[0]["matched_entries"] == []  # meta 必发，未命中为空数组
    assert events[1]["text"] == "你好"
    assert events[2]["text"] == "，世界"
    assert events[3]["full_text"] == "你好，世界"
    assert events[3]["usage"] == {"prompt_tokens": 5, "completion_tokens": 3}

    msgs = db.list_messages(env.session["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "你好呀"  # 存库前 strip 首尾空白
    assert msgs[1]["content"] == "你好，世界"
    assert msgs[1]["interrupted"] == 0
    # 正常结束后生成权已释放，可再次发送
    assert env.session["id"] not in chat_mod._in_flight


def test_chat_done_usage_null_when_absent(env, monkeypatch) -> None:
    """上游未返回 usage 时 done 的 usage 为空（§4.2 usage 可空）。"""
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["好"]))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat", json={"content": "你好"})
    events = _events(resp)
    assert events[-1]["type"] == "done"
    assert events[-1]["usage"] is None


def test_chat_404_unknown_session(env) -> None:
    resp = TestClient(env.app).post("/api/sessions/nope/chat", json={"content": "hi"})
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "会话不存在"}}


def test_chat_400_empty_content(env) -> None:
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat", json={"content": "   \n "})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# 世界书命中与提示词组装
# ---------------------------------------------------------------------------


def test_chat_worldbook_hit_in_meta_and_system(env, monkeypatch) -> None:
    """命中条目出现在 meta 事件，且 system 按 §6.2 模板拼接世界书块。"""
    db.create_worldbook_entry(env.session["id"], {
        "name": "角色", "keywords": "林月如\n蜀山", "content": "蜀山派女侠",
        "enabled": 1, "sort_order": 1,
    })
    capture: dict = {}
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["收到"], capture=capture))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "林月如是谁？"})
    events = _events(resp)
    assert events[0]["matched_entries"] == [{"name": "角色", "hit_keywords": ["林月如"]}]

    system = capture["messages"][0]["content"]
    assert system.startswith("你是助手")
    assert '【以下为世界书设定' in system
    assert "[角色]" in system
    assert "蜀山派女侠" in system


def test_chat_worldbook_disabled_no_injection(env, monkeypatch) -> None:
    """worldbook_enabled=0 时不匹配、不注入。"""
    db.create_worldbook_entry(env.session["id"], {
        "name": "角色", "keywords": "林月如", "content": "蜀山派女侠", "enabled": 1, "sort_order": 1,
    })
    db.update_session(env.session["id"], {"worldbook_enabled": 0})
    capture: dict = {}
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["好"], capture=capture))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "林月如来了"})
    assert _events(resp)[0]["matched_entries"] == []
    assert "世界书" not in capture["messages"][0]["content"]


def test_chat_scan_rounds_zero_only_current_input(env, monkeypatch) -> None:
    """scan_rounds=0：历史中的关键词不触发，当前输入触发。"""
    db.update_session(env.session["id"], {"scan_rounds": 0})
    db.create_worldbook_entry(env.session["id"], {
        "name": "地点", "keywords": "蜀山", "content": "仙山", "enabled": 1, "sort_order": 1,
    })
    db.append_message(env.session["id"], "user", "我们去了蜀山")
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["好"]))
    # 当前输入不含关键词：不命中（尽管历史含"蜀山"）
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "然后呢？"})
    assert _events(resp)[0]["matched_entries"] == []


def test_chat_scan_window_only_recent_2x_rounds(env, monkeypatch) -> None:
    """扫描窗口 = 最近 2×scan_rounds 条平铺消息；窗口外的历史不参与匹配。"""
    db.update_session(env.session["id"], {"scan_rounds": 1})
    db.create_worldbook_entry(env.session["id"], {
        "name": "地点", "keywords": "蜀山", "content": "仙山", "enabled": 1, "sort_order": 1,
    })
    # 平铺历史：越窗的"蜀山" + 两条更新消息 → 窗口只取最近 2 条
    db.append_message(env.session["id"], "user", "很久以前去过蜀山")
    db.append_message(env.session["id"], "assistant", "嗯")
    db.append_message(env.session["id"], "user", "最近在聊别的")
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["好"]))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "继续"})
    assert _events(resp)[0]["matched_entries"] == []


def test_chat_scan_window_includes_recent_messages(env, monkeypatch) -> None:
    """窗口内的历史关键词仍参与匹配（scan_rounds=1 → 最近 2 条）。"""
    db.update_session(env.session["id"], {"scan_rounds": 1})
    db.create_worldbook_entry(env.session["id"], {
        "name": "地点", "keywords": "蜀山", "content": "仙山", "enabled": 1, "sort_order": 1,
    })
    db.append_message(env.session["id"], "user", "我们去了蜀山")
    db.append_message(env.session["id"], "assistant", "那里如何？")
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["好"]))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "继续"})
    assert _events(resp)[0]["matched_entries"] == [
        {"name": "地点", "hit_keywords": ["蜀山"]}]


def test_chat_max_context_chars_zero_keeps_full_history(env, monkeypatch) -> None:
    """max_context_chars=0 → 不裁剪（§6.3），全部历史进入上游 messages。"""
    db.update_session(env.session["id"], {"max_context_chars": 0})
    for i in range(6):
        db.append_message(env.session["id"], "user", f"历史{i}" + "啊" * 300)
    capture: dict = {}
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["ok"], capture=capture))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "最新问题"})
    assert _events(resp)[-1]["type"] == "done"
    msgs = capture["messages"]
    assert len(msgs) == 1 + 6 + 1  # system + 全部历史 + 当前输入
    assert msgs[0]["role"] == "system"
    assert "历史0" in msgs[1]["content"]  # 最旧一条保留
    assert msgs[-1] == {"role": "user", "content": "最新问题"}


def test_chat_history_trimmed_by_budget(env, monkeypatch) -> None:
    """预算裁剪：长历史下最旧消息被丢弃，且 system/当前输入不受影响。"""
    db.update_session(env.session["id"], {"max_context_chars": 2000})
    # 5 条各 400 汉字（≈400 token）的历史，总 2000 > 可用预算（2000 − system − input − 500）
    for i in range(5):
        db.append_message(env.session["id"], "user", f"历史问题{i}" + "啊" * 396)
        db.append_message(env.session["id"], "assistant", f"历史回答{i}" + "呀" * 396)
    capture: dict = {}
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["ok"], capture=capture))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "最新问题"})
    assert _events(resp)[-1]["type"] == "done"
    msgs = capture["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1] == {"role": "user", "content": "最新问题"}
    # 历史被裁剪（10 条旧消息没有全进），最旧的消息已丢弃
    history_contents = [m["content"] for m in msgs[1:-1]]
    assert "历史问题0" + "啊" * 396 not in history_contents
    assert len(history_contents) < 10


# ---------------------------------------------------------------------------
# 上游失败：error 事件、失败不落库 AI 回复
# ---------------------------------------------------------------------------


def test_chat_upstream_error_event(env, monkeypatch) -> None:
    """401 → error(auth_failed)；已产生的部分不落库；用户消息保留。"""
    err = ProviderError(CODE_AUTH_FAILED, "上游返回 401/403，认证失败，请检查 API Key", 401)
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["部分"], error=err))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "你好"})
    assert resp.status_code == 200
    events = _events(resp)
    assert [e["type"] for e in events] == ["meta", "delta", "error"]
    assert events[-1]["code"] == "auth_failed"
    assert "API Key" in events[-1]["message"]
    msgs = db.list_messages(env.session["id"])
    assert [m["role"] for m in msgs] == ["user"]  # 失败不存库 AI 回复（§6.1 第 10 点）
    assert msgs[0]["content"] == "你好"           # 用户消息不丢（§6.1 第 2 点）


def test_chat_missing_model_config_error_event(env, monkeypatch) -> None:
    """未配置 base_url/model → error(internal) 中文提示。"""
    db.update_session(env.session["id"], {"base_url": ""})
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "你好"})
    events = _events(resp)
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "internal"
    assert "Base URL" in events[-1]["message"]


@pytest.mark.parametrize("code", ["rate_limited", "timeout", "upstream_unreachable", "internal"])
def test_chat_provider_error_code_passthrough(env, monkeypatch, code: str) -> None:
    """上游各错误分类原样透传到 NDJSON error；无 delta 时序列为 meta→error。"""
    err = ProviderError(code, f"上游错误-{code}")
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream([], error=err))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/chat",
                                    json={"content": "你好"})
    events = _events(resp)
    assert [e["type"] for e in events] == ["meta", "error"]  # meta 必发且最先
    assert events[-1] == {"type": "error", "code": code, "message": f"上游错误-{code}"}
    assert env.session["id"] not in chat_mod._in_flight  # 出错后生成权释放


# ---------------------------------------------------------------------------
# regenerate 两分支与空历史
# ---------------------------------------------------------------------------


def test_regenerate_last_user_direct_retry(env, monkeypatch) -> None:
    """末条为 user → 直接重试：不新增用户消息、不删除任何消息。"""
    db.append_message(env.session["id"], "user", "上次的问题")
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["重试的回复"]))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/regenerate")
    events = _events(resp)
    assert [e["type"] for e in events] == ["meta", "delta", "done"]
    assert events[-1]["full_text"] == "重试的回复"
    msgs = db.list_messages(env.session["id"])
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "上次的问题"), ("assistant", "重试的回复")]


def test_regenerate_last_assistant_deletes_then_generates(env, monkeypatch) -> None:
    """末条为 assistant → 先删除该条再生成。"""
    db.append_message(env.session["id"], "user", "问题")
    db.append_message(env.session["id"], "assistant", "旧回复")
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["新回复"]))
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/regenerate")
    assert _events(resp)[-1] == {"type": "done", "full_text": "新回复", "usage": None}
    msgs = db.list_messages(env.session["id"])
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "问题"), ("assistant", "新回复")]  # 旧回复已被删除


def test_regenerate_no_messages_400(env) -> None:
    resp = TestClient(env.app).post(f"/api/sessions/{env.session['id']}/regenerate")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# 并发控制：同会话同时只允许一个生成（409）
# ---------------------------------------------------------------------------


def test_concurrent_generation_409(env, monkeypatch) -> None:
    """第一个生成进行中时，同会话第二次请求返回 409 统一错误格式。"""

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fake_stream(base_url, api_key, model, messages, temperature, top_p,
                              max_tokens, proxy_url, timeout=120.0, *, transport=None,
                              usage_out=None):
            entered.set()
            await release.wait()
            yield "慢慢生成"

        monkeypatch.setattr(provider_mod, "stream_chat", fake_stream)
        transport = httpx.ASGITransport(app=env.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            task1 = asyncio.create_task(
                client.post(f"/api/sessions/{env.session['id']}/chat", json={"content": "第一次"}))
            await asyncio.wait_for(entered.wait(), timeout=5)  # 等第一个生成真正开始
            resp2 = await client.post(f"/api/sessions/{env.session['id']}/chat",
                                      json={"content": "第二次"})
            assert resp2.status_code == 409
            assert resp2.json() == {"error": {
                "code": "generation_in_progress",
                "message": "该会话正在生成回复，请等待完成或停止后再试"}}
            release.set()
            resp1 = await task1
            assert resp1.status_code == 200
            assert _events(resp1)[-1]["type"] == "done"
        # 生成权已释放，可再次发送
        assert env.session["id"] not in chat_mod._in_flight

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 生成权释放：响应创建前失败也不得把会话永久锁死（409）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["chat", "regenerate"])
def test_prepare_failure_releases_generation_lock(env, monkeypatch, endpoint: str) -> None:
    """_prepare 抛异常（如数据库读取失败）→ 500，但生成权必须释放。"""
    db.append_message(env.session["id"], "user", "问题")

    def boom(session, content):
        raise RuntimeError("db read failed")

    monkeypatch.setattr(chat_mod, "_prepare", boom)
    client = TestClient(env.app, raise_server_exceptions=False)
    if endpoint == "chat":
        resp = client.post(f"/api/sessions/{env.session['id']}/chat", json={"content": "你好"})
    else:
        resp = client.post(f"/api/sessions/{env.session['id']}/regenerate")
    assert resp.status_code == 500
    assert env.session["id"] not in chat_mod._in_flight  # 未泄漏生成权


# ---------------------------------------------------------------------------
# 停止语义：客户端断开 → 中止上游、部分文本落库 interrupted=1
# ---------------------------------------------------------------------------


def test_disconnect_stores_partial_interrupted(env, monkeypatch) -> None:
    """is_disconnected 为真 → 停止转发、部分文本落库 interrupted=1。"""
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["你", "好", "世界"]))
    session = db.get_session_raw(env.session["id"])
    db.append_message(session["id"], "user", "你好呀")

    class StubRequest:
        """假请求：第 1 次检测（收到首个 delta 后）即判定客户端已断开。"""

        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls >= 1

    async def run() -> list[str]:
        gen = chat_mod._stream_events(StubRequest(), session, [],
                                      [{"role": "user", "content": "你好呀"}])
        return [line async for line in gen]

    lines = asyncio.run(run())
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["meta", "delta"]  # 断开后不再发 done/error
    assert events[-1] == {"type": "delta", "text": "你"}     # 只转发已收到的部分

    msgs = db.list_messages(session["id"])
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "你"
    assert msgs[-1]["interrupted"] == 1
    assert session["id"] not in chat_mod._in_flight  # 生成权已释放


def test_framework_cancel_stores_partial(env, monkeypatch) -> None:
    """框架级取消（aclose，客户端断开兜底路径）→ 部分文本落库 interrupted=1。"""
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream(["你", "好"]))

    class NeverDisconnect:
        async def is_disconnected(self) -> bool:
            return False

    session = db.get_session_raw(env.session["id"])
    db.append_message(session["id"], "user", "x")

    async def run() -> None:
        gen = chat_mod._stream_events(NeverDisconnect(), session, [],
                                      [{"role": "user", "content": "x"}])
        await gen.__anext__()   # meta
        await gen.__anext__()   # delta "你"
        await gen.aclose()      # 模拟客户端断开导致框架取消流式任务

    asyncio.run(run())
    msgs = db.list_messages(session["id"])
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "你"
    assert msgs[-1]["interrupted"] == 1


def test_cancel_before_any_delta_stores_nothing(env, monkeypatch) -> None:
    """断开时没有任何已生成文本 → 不落空气泡 assistant 消息。"""
    monkeypatch.setattr(provider_mod, "stream_chat", make_fake_stream([]))

    class NeverDisconnect:
        async def is_disconnected(self) -> bool:
            return False

    session = db.get_session_raw(env.session["id"])
    db.append_message(session["id"], "user", "x")

    async def run() -> None:
        gen = chat_mod._stream_events(NeverDisconnect(), session, [],
                                      [{"role": "user", "content": "x"}])
        await gen.__anext__()  # meta
        await gen.aclose()

    asyncio.run(run())
    msgs = db.list_messages(session["id"])
    assert [m["role"] for m in msgs] == ["user"]  # 无空 assistant 消息


# ---------------------------------------------------------------------------
# test-connection 端点
# ---------------------------------------------------------------------------


def test_config_test_connection_ok(env, monkeypatch) -> None:
    async def fake_tc(base_url, api_key, model, proxy_url):
        return {"ok": True}

    monkeypatch.setattr(config_mod, "test_connection", fake_tc)
    resp = TestClient(env.app).post("/api/config/test-connection",
                                    json={"base_url": "https://u.example.com/v1",
                                          "api_key": "k", "model": "m", "proxy_url": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_config_test_connection_auth_failed(env, monkeypatch) -> None:
    async def fake_tc(base_url, api_key, model, proxy_url):
        raise ProviderError(CODE_AUTH_FAILED, "上游返回 401/403，认证失败，请检查 API Key", 401)

    monkeypatch.setattr(config_mod, "test_connection", fake_tc)
    resp = TestClient(env.app).post("/api/config/test-connection",
                                    json={"base_url": "https://u.example.com/v1",
                                          "api_key": "bad", "model": "m", "proxy_url": ""})
    assert resp.status_code == 200
    assert resp.json() == {"error": {"code": "auth_failed",
                                     "message": "上游返回 401/403，认证失败，请检查 API Key"}}


def test_config_test_connection_empty_fields(env) -> None:
    resp = TestClient(env.app).post("/api/config/test-connection",
                                    json={"base_url": "  ", "api_key": "", "model": "", "proxy_url": ""})
    assert resp.json() == {"error": {"code": "bad_request", "message": "Base URL 与模型名不能为空"}}
