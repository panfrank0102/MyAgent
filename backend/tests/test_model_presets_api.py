"""全局模型预设 API 测试（v0.6，§4.3 / §5 / §5.1）。

覆盖：预设 CRUD（列表 created_at 升序、api_key 打码、创建默认值）、PATCH 部分
更新与 api_key 空值不更新、reasoning_effort 校验（422）、删除级联清空会话引用、
404 统一错误格式、test-connection（使用预设存储值、成功/上游失败/预设不存在/
预设不完整）。

团队约定：fixture 写在本文件内（不创建 conftest.py / tests/__init__.py），
使用 TestClient + 临时数据库，不污染 data/app.db。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 保证在任意目录下运行 pytest 都能导入 backend/ 下的 app 包与 main 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.routers import model_presets as model_presets_mod
from app.services.provider import CODE_AUTH_FAILED, ProviderError
from main import create_app

# api_key 打码样例（21 位：前 4 后 4，中间 13 个 *）
RAW_KEY = "sk-secret-key-12345678"


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


def _create_preset(client: TestClient, **overrides) -> dict:
    """POST /api/model-presets 并断言成功，返回预设 dict（api_key 已打码）。"""
    body = {
        "name": "预设",
        "base_url": "https://upstream.example.com/v1",
        "api_key": RAW_KEY,
        "model": "gpt-test",
        "reasoning_effort": "medium",
    }
    body.update(overrides)
    resp = client.post("/api/model-presets", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_session(client: TestClient, name: str = "会话") -> dict:
    resp = client.post("/api/sessions", json={"name": name})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# 列表 / 新建 / 打码
# ---------------------------------------------------------------------------


def test_create_preset_returns_masked_and_defaults(client: TestClient) -> None:
    """POST 返回完整预设：UUID id、api_key 打码、时间 ISO8601；缺省字段为空串/NULL。"""
    preset = _create_preset(client)
    assert len(preset["id"]) == 36 and preset["id"].count("-") == 4
    assert preset["name"] == "预设"
    assert preset["base_url"] == "https://upstream.example.com/v1"
    assert preset["api_key"] == db.mask_key(RAW_KEY)  # 打码回显
    assert preset["api_key"] != RAW_KEY               # 明文不外泄
    assert preset["model"] == "gpt-test"
    assert preset["reasoning_effort"] == "medium"
    assert preset["created_at"] and preset["updated_at"]

    # 最小 body：其余字段默认空串 / reasoning NULL
    minimal = _create_preset(client, name="最小预设", base_url="", api_key="",
                             model="", reasoning_effort=None)
    assert minimal["base_url"] == ""
    assert minimal["api_key"] == ""
    assert minimal["model"] == ""
    assert minimal["reasoning_effort"] is None


def test_list_presets_masked_and_ordered_by_created_at(client: TestClient) -> None:
    """GET 列表：按 created_at 升序，api_key 打码，明文不外泄。"""
    p1 = _create_preset(client, name="甲", api_key="sk-aaaa1111bbbb2222")
    p2 = _create_preset(client, name="乙", api_key="sk-cccc3333dddd4444")
    # 显式控制 created_at，避免同微秒导致排序不确定
    conn = sqlite3.connect(str(db._db_path))
    try:
        conn.execute(
            "UPDATE model_presets SET created_at = ? WHERE id = ?",
            ("2026-01-02T00:00:00.000000Z", p1["id"]),
        )
        conn.execute(
            "UPDATE model_presets SET created_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000000Z", p2["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    listed = client.get("/api/model-presets").json()
    assert [p["name"] for p in listed] == ["乙", "甲"]  # created_at 升序
    by_name = {p["name"]: p for p in listed}
    assert by_name["甲"]["api_key"] == db.mask_key("sk-aaaa1111bbbb2222")
    assert by_name["乙"]["api_key"] == db.mask_key("sk-cccc3333dddd4444")
    assert "sk-aaaa1111bbbb2222" not in str(listed)
    assert "sk-cccc3333dddd4444" not in str(listed)


def test_create_preset_validation_422(client: TestClient) -> None:
    """name 缺失/空白、reasoning_effort 非法 → 422 统一 validation_error。"""
    bad_bodies = [
        {"base_url": ""},                                  # 缺 name
        {"name": ""},                                      # 空 name
        {"name": "   "},                                   # 纯空白 name
        {"name": 123},                                     # 类型错误
        {"name": "p", "reasoning_effort": "extreme"},      # 非法推理强度
        {"name": "p", "reasoning_effort": "HIGH"},
        {"name": "p", "reasoning_effort": 1},
    ]
    for body in bad_bodies:
        resp = client.post("/api/model-presets", json=body)
        assert resp.status_code == 422, body
        error = resp.json()["error"]
        assert error["code"] == "validation_error"
        assert isinstance(error["message"], str) and error["message"]

    # 校验失败不落库
    assert client.get("/api/model-presets").json() == []

    # reasoning_effort = null 合法（不设置）
    ok = _create_preset(client, reasoning_effort=None)
    assert ok["reasoning_effort"] is None


# ---------------------------------------------------------------------------
# PATCH / DELETE
# ---------------------------------------------------------------------------


def test_patch_preset_partial_and_api_key_empty_not_updated(client: TestClient) -> None:
    """PATCH 仅更新传入字段；api_key null/空 → 不更新；写回打码形状。"""
    preset = _create_preset(client)
    preset_id = preset["id"]

    resp = client.patch(
        f"/api/model-presets/{preset_id}",
        json={"name": "改名", "base_url": "http://new", "model": "new-model"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "改名"
    assert body["base_url"] == "http://new"
    assert body["model"] == "new-model"
    assert body["reasoning_effort"] == "medium"  # 未传字段不动
    # api_key 未传 → 已存 Key 保留（打码回显，且明文不泄漏）
    assert body["api_key"] == db.mask_key(RAW_KEY)

    # api_key 空串 / null → 不更新（避免把打码回显当明文写回）
    for blank in ("", None):
        resp = client.patch(f"/api/model-presets/{preset_id}", json={"api_key": blank})
        assert resp.status_code == 200
        assert resp.json()["api_key"] == db.mask_key(RAW_KEY)
    assert db.get_model_preset_raw(preset_id)["api_key"] == RAW_KEY  # 明文未被覆盖

    # 非空 api_key 才更新
    new_key = "sk-new-key-87654321"
    resp = client.patch(f"/api/model-presets/{preset_id}", json={"api_key": new_key})
    assert resp.status_code == 200
    assert resp.json()["api_key"] == db.mask_key(new_key)
    assert db.get_model_preset_raw(preset_id)["api_key"] == new_key


def test_patch_preset_reasoning_effort_set_clear_and_validate(client: TestClient) -> None:
    """PATCH reasoning_effort：low/medium/high 生效；null 清除；非法 → 422 且不落库。"""
    preset_id = _create_preset(client)["id"]

    for value in ("low", "medium", "high"):
        resp = client.patch(
            f"/api/model-presets/{preset_id}", json={"reasoning_effort": value}
        )
        assert resp.status_code == 200, value
        assert resp.json()["reasoning_effort"] == value
        assert db.get_model_preset_raw(preset_id)["reasoning_effort"] == value

    for bad in ("extreme", "HIGH", "low ", "medium\n", 123):
        resp = client.patch(
            f"/api/model-presets/{preset_id}", json={"reasoning_effort": bad}
        )
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation_error"
        # 校验失败不落库：原值保持不变
        assert db.get_model_preset_raw(preset_id)["reasoning_effort"] == "high"

    # 显式 null = 清除（不设置，不向上游发送）
    resp = client.patch(f"/api/model-presets/{preset_id}", json={"reasoning_effort": None})
    assert resp.status_code == 200
    assert resp.json()["reasoning_effort"] is None
    assert db.get_model_preset_raw(preset_id)["reasoning_effort"] is None


def test_patch_preset_not_found_404(client: TestClient) -> None:
    """PATCH 不存在的预设 → 404 统一错误格式（code not_found）。"""
    resp = client.patch("/api/model-presets/no-such", json={"name": "x"})
    assert resp.status_code == 404
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_delete_preset_clears_session_reference_and_404(client: TestClient) -> None:
    """DELETE 预设：返回 {"deleted": true}，引用会话 model_preset_id 置 NULL；再删 404。"""
    preset = _create_preset(client)
    session = _create_session(client)
    resp = client.patch(
        f"/api/sessions/{session['id']}", json={"model_preset_id": preset["id"]}
    )
    assert resp.status_code == 200
    assert client.get(f"/api/sessions/{session['id']}").json()["model_preset_id"] == preset["id"]

    resp = client.delete(f"/api/model-presets/{preset['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    # 引用会话自动置 NULL（未选择状态）
    assert db.get_session_raw(session["id"])["model_preset_id"] is None
    assert client.get(f"/api/sessions/{session['id']}").json()["model_preset_id"] is None
    assert client.get("/api/model-presets").json() == []

    # 再次删除 → 404
    resp = client.delete(f"/api/model-presets/{preset['id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# test-connection（使用预设存储值，不接收前端传来的 Key）
# ---------------------------------------------------------------------------


def test_preset_test_connection_uses_stored_values(client: TestClient, monkeypatch) -> None:
    """成功：用预设存储的 base_url/api_key/model 调 provider.test_connection → 200 ok。"""
    preset = _create_preset(client)
    captured: dict = {}

    async def fake_tc(base_url, api_key, model, proxy_url):
        captured.update({"base_url": base_url, "api_key": api_key,
                         "model": model, "proxy_url": proxy_url})
        return {"ok": True}

    monkeypatch.setattr(model_presets_mod, "test_connection", fake_tc)
    resp = client.post(f"/api/model-presets/{preset['id']}/test-connection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model"] == "gpt-test"
    assert isinstance(body["message"], str) and body["message"]
    # 服务端使用预设存储的明文 Key（不下发前端）
    assert captured == {
        "base_url": "https://upstream.example.com/v1",
        "api_key": RAW_KEY,
        "model": "gpt-test",
        "proxy_url": "",
    }


def test_preset_test_connection_upstream_failure_returns_200_error(
    client: TestClient, monkeypatch
) -> None:
    """上游失败：HTTP 200 + {"error": {...}}（与 /api/config/test-connection 一致）。"""
    preset = _create_preset(client)

    async def fake_tc(base_url, api_key, model, proxy_url):
        raise ProviderError(CODE_AUTH_FAILED, "上游返回 401/403，认证失败，请检查 API Key", 401)

    monkeypatch.setattr(model_presets_mod, "test_connection", fake_tc)
    resp = client.post(f"/api/model-presets/{preset['id']}/test-connection")
    assert resp.status_code == 200
    assert resp.json() == {
        "error": {"code": "auth_failed",
                  "message": "上游返回 401/403，认证失败，请检查 API Key"}
    }


def test_preset_test_connection_missing_preset_404(client: TestClient) -> None:
    """预设不存在 → 404 统一错误格式（前端据 ErrorCode=not_found 处理）。"""
    resp = client.post("/api/model-presets/no-such/test-connection")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert "message" in body["error"]


def test_preset_test_connection_incomplete_preset_200_error(client: TestClient) -> None:
    """预设的 Base URL / 模型名为空 → HTTP 200 + error(bad_request)，不调用上游。"""
    preset = _create_preset(client, base_url="", model="")
    resp = client.post(f"/api/model-presets/{preset['id']}/test-connection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == "bad_request"
    assert "Base URL" in body["error"]["message"]
