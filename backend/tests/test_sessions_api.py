"""会话与消息接口路由测试（开发步骤 Step 3 完成标准）。

重点：列表排序（updated_at 倒序）与 message_count、api_key 打码与非空更新、
删单条级联数量、清空、limit 语义、404/422 统一错误格式
{"error": {"code", "message"}}。

团队约定：fixture 写在本文件内（不创建 conftest.py / tests/__init__.py），
使用 TestClient + tmp_path 临时数据库，不污染 data/app.db。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 保证在任意目录下运行 pytest 都能导入 backend/ 下的 app 包与 main 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from main import create_app


@pytest.fixture()
def tmp_path() -> Path:
    """每个测试独立的临时目录（等效替换 pytest 内建 tmp_path，行为一致）。

    背景：pytest 内建 tmp_path 使用 mkdir(mode=0o700) 创建目录；在
    Python 3.14(Windows) 上 mode 会被映射为目录 DACL，在 DSH 沙箱令牌下
    刚创建的目录立刻无法访问（PermissionError）。本 fixture 改用默认权限
    mkdir 的全新目录，测试结束后递归清理；在普通环境下与内建 tmp_path
    等价（每测试独立、自动清理、不污染 data/app.db）。
    """
    base = Path(tempfile.gettempdir()) / f"myagent-test-{uuid.uuid4().hex}"
    os.mkdir(base)  # 注意：不可传 mode=0o700（见 docstring）
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """每个测试独立的临时数据库 + 由 main.create_app 组装的 TestClient。"""
    db.init_db(tmp_path / "t.db")
    return TestClient(create_app())


def _create_session(client: TestClient, name: str = "测试") -> dict:
    """POST /api/sessions 并断言成功，返回会话 dict。"""
    resp = client.post("/api/sessions", json={"name": name})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# 会话列表与新建（§4.3）
# ---------------------------------------------------------------------------


def test_create_session_defaults_and_list(client: TestClient) -> None:
    """新建会话默认值按 §5.1 create_session 契约；列表含 message_count。"""
    session = _create_session(client, "新会话")
    assert session["name"] == "新会话"
    assert session["temperature"] == 1.0
    assert session["top_p"] == 1.0
    assert session["scan_rounds"] == 4
    assert session["worldbook_enabled"] is True
    assert session["max_context_chars"] == 80000
    assert session["proxy_url"] == ""
    assert session["system_prompt"] == ""
    assert session["api_key"] == ""

    listed = client.get("/api/sessions").json()
    assert len(listed) == 1
    assert listed[0]["id"] == session["id"]
    assert listed[0]["message_count"] == 0


def test_list_sessions_order_by_updated_at_desc(client: TestClient) -> None:
    """列表按 updated_at 倒序：最后被触碰（新建/消息/更新）的会话排最前。"""
    a = _create_session(client, "A")
    b = _create_session(client, "B")
    ids = [x["id"] for x in client.get("/api/sessions").json()]
    assert ids == [b["id"], a["id"]]  # B 后创建 → 排前

    # 触碰 A（PATCH 自动刷新 updated_at）→ A 排到最前
    resp = client.patch(f"/api/sessions/{a['id']}", json={"name": "A改"})
    assert resp.status_code == 200
    ids = [x["id"] for x in client.get("/api/sessions").json()]
    assert ids == [a["id"], b["id"]]


def test_get_detail_and_patch_partial(client: TestClient) -> None:
    """GET 详情完整配置；PATCH 仅更新传入字段。"""
    session = _create_session(client)
    resp = client.patch(
        f"/api/sessions/{session['id']}",
        json={"name": "改", "temperature": 0.6, "system_prompt": "你是猫娘"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "改"
    assert body["temperature"] == 0.6
    assert body["system_prompt"] == "你是猫娘"
    assert body["top_p"] == 1.0       # 未传字段不动
    assert body["scan_rounds"] == 4

    detail = client.get(f"/api/sessions/{session['id']}").json()
    assert detail["name"] == "改"


def test_patch_api_key_masked_and_empty_not_update(client: TestClient) -> None:
    """api_key：非空才更新；读路径（详情/列表/回显）一律打码。"""
    session = _create_session(client)
    resp = client.patch(
        f"/api/sessions/{session['id']}", json={"api_key": "sk-secret-key-12345678"}
    )
    assert resp.status_code == 200
    assert resp.json()["api_key"] == db.mask_key("sk-secret-key-12345678")  # 打码回显

    # 空值不更新
    client.patch(f"/api/sessions/{session['id']}", json={"api_key": ""})
    assert db.get_session_raw(session["id"])["api_key"] == "sk-secret-key-12345678"

    # 详情与列表均打码
    assert (
        client.get(f"/api/sessions/{session['id']}").json()["api_key"]
        == db.mask_key("sk-secret-key-12345678")
    )
    listed = client.get("/api/sessions").json()
    assert listed[0]["api_key"] == db.mask_key("sk-secret-key-12345678")
    assert "sk-secret-key-12345678" not in str(listed)


def test_not_found_error_shape(client: TestClient) -> None:
    """会话不存在 → 404，错误体为统一的 {"error":{"code","message"}}。"""
    cases = [
        ("get", "/api/sessions/no-such", None),
        ("patch", "/api/sessions/no-such", {}),
        ("delete", "/api/sessions/no-such", None),
        ("get", "/api/sessions/no-such/messages", None),
        ("delete", "/api/sessions/no-such/messages", None),
    ]
    for method, url, payload in cases:
        if payload is not None:
            resp = getattr(client, method)(url, json=payload)
        else:
            resp = getattr(client, method)(url)
        assert resp.status_code == 404
        body = resp.json()
        assert set(body.keys()) == {"error"}
        assert body["error"]["code"] == "session_not_found"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_delete_session_cascades(client: TestClient) -> None:
    """DELETE 会话级联删除消息与世界书条目（§4.3）。"""
    session = _create_session(client)
    db.append_message(session["id"], "user", "m1")
    db.append_message(session["id"], "assistant", "m2")
    db.create_worldbook_entry(session["id"], {"name": "条目", "content": "内容"})

    resp = client.delete(f"/api/sessions/{session['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert client.get(f"/api/sessions/{session['id']}").status_code == 404
    assert db.list_messages(session["id"]) == []
    assert db.list_worldbook(session["id"]) == []


# ---------------------------------------------------------------------------
# 消息接口（§4.3）
# ---------------------------------------------------------------------------


def test_messages_list_limit_order_and_interrupted(client: TestClient) -> None:
    """GET messages：最新 limit 条、按 id 正序、含 interrupted 标记。"""
    session = _create_session(client)
    for i in range(5):
        db.append_message(session["id"], "user" if i % 2 == 0 else "assistant", f"m{i}")
    db.append_message(session["id"], "assistant", "被停止", interrupted=1)

    resp = client.get(f"/api/sessions/{session['id']}/messages")
    assert resp.status_code == 200
    msgs = resp.json()
    assert [m["id"] for m in msgs] == [1, 2, 3, 4, 5, 6]
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["interrupted"] is True
    assert msgs[0]["interrupted"] is False
    assert msgs[0]["session_id"] == session["id"]

    # limit=2：取最新 2 条，仍按 id 正序
    resp2 = client.get(f"/api/sessions/{session['id']}/messages", params={"limit": 2})
    assert [m["id"] for m in resp2.json()] == [5, 6]


def test_messages_invalid_limit_422(client: TestClient) -> None:
    """limit < 1 视为无效参数 → 422 统一错误格式。"""
    session = _create_session(client)
    resp = client.get(f"/api/sessions/{session['id']}/messages", params={"limit": 0})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "invalid_param"
    assert "message" in body["error"]


def test_clear_messages(client: TestClient) -> None:
    """DELETE messages 清空历史，返回删除条数。"""
    session = _create_session(client)
    for i in range(3):
        db.append_message(session["id"], "user", f"m{i}")
    resp = client.delete(f"/api/sessions/{session['id']}/messages")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 3}
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


def test_delete_message_and_after_cascade(client: TestClient) -> None:
    """删单条级联删除该条及其后所有消息，返回 {"deleted": N}（§4.3）。"""
    session = _create_session(client)
    for i in range(5):
        db.append_message(session["id"], "user", f"m{i}")

    resp = client.delete(f"/api/sessions/{session['id']}/messages/3")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 3}  # 3/4/5 被级联删除
    msgs = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [m["id"] for m in msgs] == [1, 2]


def test_delete_message_not_found_404(client: TestClient) -> None:
    """消息不存在 → 404，且不误删后续消息（防误删保护）。"""
    session = _create_session(client)
    db.append_message(session["id"], "user", "m0")

    resp = client.delete(f"/api/sessions/{session['id']}/messages/99")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "message_not_found"
    msgs = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [m["id"] for m in msgs] == [1]


def test_create_invalid_param_422(client: TestClient) -> None:
    """无效参数（name 类型错误）→ 422。"""
    resp = client.post("/api/sessions", json={"name": 123})
    assert resp.status_code == 422
