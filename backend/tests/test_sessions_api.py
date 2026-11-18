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


def test_patch_null_bool_not_updated_false_updates(client: TestClient) -> None:
    """PATCH 显式 null 的 bool 字段 = 未提供（不更新）；显式 false 必须生效。"""
    session = _create_session(client)
    assert session["worldbook_enabled"] is True

    resp = client.patch(
        f"/api/sessions/{session['id']}", json={"worldbook_enabled": None}
    )
    assert resp.status_code == 200
    assert resp.json()["worldbook_enabled"] is True  # null 不更新
    assert db.get_session_raw(session["id"])["worldbook_enabled"] == 1

    resp = client.patch(
        f"/api/sessions/{session['id']}", json={"worldbook_enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["worldbook_enabled"] is False  # 显式 false 正常更新
    assert db.get_session_raw(session["id"])["worldbook_enabled"] == 0

    resp = client.patch(
        f"/api/sessions/{session['id']}", json={"worldbook_enabled": True}
    )
    assert resp.status_code == 200
    assert resp.json()["worldbook_enabled"] is True


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


def test_patch_reasoning_effort_set_clear_and_echo(client: TestClient) -> None:
    """v0.5①：PATCH reasoning_effort 设置 low/medium/high → 回显；null 清除。"""
    session = _create_session(client)
    # 新会话默认 null，且列表/详情 JSON 均含该字段（契约冻结字段名）
    assert session["reasoning_effort"] is None
    assert "reasoning_effort" in client.get(f"/api/sessions/{session['id']}").json()
    listed = client.get("/api/sessions").json()
    assert "reasoning_effort" in listed[0]
    assert listed[0]["reasoning_effort"] is None

    for value in ("low", "medium", "high"):
        resp = client.patch(
            f"/api/sessions/{session['id']}", json={"reasoning_effort": value}
        )
        assert resp.status_code == 200, value
        assert resp.json()["reasoning_effort"] == value  # PATCH 响应回显
        assert db.get_session_raw(session["id"])["reasoning_effort"] == value  # 已落库
        assert (
            client.get(f"/api/sessions/{session['id']}").json()["reasoning_effort"] == value
        )  # GET 详情回显
        assert client.get("/api/sessions").json()[0]["reasoning_effort"] == value

    # 显式 null = 清除配置（置 NULL，之后不向上游发送该字段）
    resp = client.patch(f"/api/sessions/{session['id']}", json={"reasoning_effort": None})
    assert resp.status_code == 200
    assert resp.json()["reasoning_effort"] is None
    assert db.get_session_raw(session["id"])["reasoning_effort"] is None
    assert client.get(f"/api/sessions/{session['id']}").json()["reasoning_effort"] is None


def test_patch_reasoning_effort_invalid_422(client: TestClient) -> None:
    """v0.5①：reasoning_effort 仅接受 low/medium/high 或 null，否则 422 且不落库。"""
    session = _create_session(client)
    client.patch(f"/api/sessions/{session['id']}", json={"reasoning_effort": "high"})

    for bad in ("extreme", "HIGH", "low ", "", "medium\n", 123):
        resp = client.patch(
            f"/api/sessions/{session['id']}", json={"reasoning_effort": bad}
        )
        assert resp.status_code == 422, bad
        body = resp.json()
        assert set(body.keys()) == {"error"}
        # 走 main.py 全局 422 handler 的统一 code（不新造 code）
        assert body["error"]["code"] == "validation_error"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]
        # 校验失败不落库：原值保持不变
        assert db.get_session_raw(session["id"])["reasoning_effort"] == "high"


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


def test_messages_limit_upper_bound_422(client: TestClient) -> None:
    """limit 超过 10000 → 422（不再 500），沿用 invalid_param 统一错误格式。"""
    session = _create_session(client)
    db.append_message(session["id"], "user", "m")

    for limit in (10001, 10**19):
        resp = client.get(
            f"/api/sessions/{session['id']}/messages", params={"limit": limit}
        )
        assert resp.status_code == 422, limit
        body = resp.json()
        assert set(body.keys()) == {"error"}
        assert body["error"]["code"] == "invalid_param"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    # 上界本身仍可接受（宽松上界，不误伤合法值）
    resp = client.get(
        f"/api/sessions/{session['id']}/messages", params={"limit": 10000}
    )
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()] == [1]


def test_patch_numeric_upper_bound_422(client: TestClient) -> None:
    """scan_rounds / max_tokens / max_context_chars 超上界 → 422（不再 500）。"""
    session = _create_session(client)
    cases = [
        ("scan_rounds", 1001),
        ("scan_rounds", 10**19),
        ("max_tokens", 10_000_001),
        ("max_tokens", 10**19),
        ("max_context_chars", 100_000_001),
        ("max_context_chars", 10**19),
    ]
    for field, value in cases:
        resp = client.patch(f"/api/sessions/{session['id']}", json={field: value})
        assert resp.status_code == 422, (field, value)
        body = resp.json()
        assert set(body.keys()) == {"error"}
        # 走 main.py 全局 422 handler，沿用项目既有 code（不新造 code）
        assert body["error"]["code"] == "validation_error"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    # 超界请求不落库：原值保持不变
    raw = db.get_session_raw(session["id"])
    assert raw["scan_rounds"] == 4
    assert raw["max_tokens"] is None
    assert raw["max_context_chars"] == 80000

    # 精确上界仍可接受（宽松上界，不引入业务语义范围）
    resp = client.patch(
        f"/api/sessions/{session['id']}",
        json={
            "scan_rounds": 1000,
            "max_tokens": 10_000_000,
            "max_context_chars": 100_000_000,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_rounds"] == 1000
    assert body["max_tokens"] == 10_000_000
    assert body["max_context_chars"] == 100_000_000


def test_patch_max_tokens_lower_bound_422(client: TestClient) -> None:
    """max_tokens 低于 SQLite int64 下界 → 422（不再 500），且不落库。

    max_tokens 本身无业务下界，故下界仅拦截会触发 OverflowError 的
    溢出级负值；负值（含 int64 最小值）仍按既有行为接受。
    """
    session = _create_session(client)
    for value in (-(10**19), -(2**63) - 1):
        resp = client.patch(f"/api/sessions/{session['id']}", json={"max_tokens": value})
        assert resp.status_code == 422, value
        body = resp.json()
        assert set(body.keys()) == {"error"}
        assert body["error"]["code"] == "validation_error"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    # 超界请求不落库：max_tokens 仍为默认 None
    assert db.get_session_raw(session["id"])["max_tokens"] is None

    # int64 最小值与普通负值仍可接受（不收紧为 ge=0，避免超范围行为变更）
    for value in (-(2**63), -1):
        resp = client.patch(f"/api/sessions/{session['id']}", json={"max_tokens": value})
        assert resp.status_code == 200, value
        assert resp.json()["max_tokens"] == value


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
