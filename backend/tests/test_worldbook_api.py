"""每会话世界书接口路由测试（开发步骤 Step 4 完成标准）。

重点：关键词清洗（trim/去空行/去重后存换行 TEXT）、content 空白 422、
sort_order 缺省 = 本会话 max+1、排序、启停、删除、会话隔离（跨会话 404）、
会话不存在 404 与统一错误格式。

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


def _make_session(client: TestClient, name: str = "s") -> str:
    """新建会话并返回其 id。"""
    resp = client.post("/api/sessions", json={"name": name})
    assert resp.status_code == 200
    return resp.json()["id"]


def _post_entry(client: TestClient, sid: str, **overrides) -> dict:
    """POST 世界书条目（带默认字段，可覆盖），返回 resp.json()。"""
    payload = {"name": "条目", "keywords": ["关键词"], "content": "内容"}
    payload.update(overrides)
    return client.post(f"/api/sessions/{sid}/worldbook", json=payload).json()


# ---------------------------------------------------------------------------
# 新建与校验（§4.3 / §5）
# ---------------------------------------------------------------------------


def test_create_entry_keyword_cleaning(client: TestClient) -> None:
    """keywords 清洗：trim → 去空行 → 去重（保持顺序），存换行 TEXT、响应拆回列表。"""
    sid = _make_session(client)
    resp = client.post(
        f"/api/sessions/{sid}/worldbook",
        json={
            "name": "猫娘",
            "keywords": [" 猫耳 ", "猫耳", "", "   ", "尾巴", "猫耳", "\n"],
            "content": "内容",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["keywords"] == ["猫耳", "尾巴"]  # trim + 去空 + 去重
    assert body["name"] == "猫娘"
    assert body["content"] == "内容"
    assert body["enabled"] is True
    assert body["sort_order"] == 1          # 缺省 = 本会话 max+1（首条为 1）
    assert body["session_id"] == sid
    assert len(body["id"]) == 36            # UUID 主键

    # GET 回读一致（DB 存换行 TEXT，响应拆回列表）
    listed = client.get(f"/api/sessions/{sid}/worldbook").json()
    assert listed[0]["keywords"] == ["猫耳", "尾巴"]


def test_create_entry_empty_content_422(client: TestClient) -> None:
    """content 去空白后为空（含缺失）→ 422，且不落库。"""
    sid = _make_session(client)
    for content in ("", "   ", "\n\t "):
        resp = client.post(
            f"/api/sessions/{sid}/worldbook",
            json={"name": "x", "keywords": [], "content": content},
        )
        assert resp.status_code == 422
    # 缺少 content 字段同样 422（必填）
    resp = client.post(f"/api/sessions/{sid}/worldbook", json={"name": "x", "keywords": []})
    assert resp.status_code == 422
    # 全部未保存
    assert client.get(f"/api/sessions/{sid}/worldbook").json() == []


def test_entry_without_keywords_allowed(client: TestClient) -> None:
    """keywords 为空 = 不参与匹配（§5），允许保存。"""
    sid = _make_session(client)
    body = _post_entry(client, sid, keywords=[])
    assert body["keywords"] == []
    # 完全省略 keywords 字段也允许
    resp = client.post(
        f"/api/sessions/{sid}/worldbook", json={"name": "n", "content": "c"}
    )
    assert resp.status_code == 200
    assert resp.json()["keywords"] == []


# ---------------------------------------------------------------------------
# sort_order 与排序（§5 / §5.1）
# ---------------------------------------------------------------------------


def test_sort_order_auto_max_plus_one(client: TestClient) -> None:
    """sort_order 缺省 = 本会话 max+1；显式指定按显式值；列表按 sort_order 升序。"""
    sid = _make_session(client)
    orders = [
        _post_entry(client, sid, name="a")["sort_order"],
        _post_entry(client, sid, name="b")["sort_order"],
        _post_entry(client, sid, name="c", sort_order=10)["sort_order"],
        _post_entry(client, sid, name="d")["sort_order"],
    ]
    assert orders == [1, 2, 10, 11]
    listed = client.get(f"/api/sessions/{sid}/worldbook").json()
    assert [x["sort_order"] for x in listed] == [1, 2, 10, 11]
    assert [x["name"] for x in listed] == ["a", "b", "c", "d"]


def test_list_sorted_by_sort_order(client: TestClient) -> None:
    """乱序创建，列表仍按 sort_order 升序。"""
    sid = _make_session(client)
    for name, order in (("n1", 5), ("n2", 2), ("n3", 9)):
        _post_entry(client, sid, name=name, sort_order=order)
    listed = client.get(f"/api/sessions/{sid}/worldbook").json()
    assert [x["sort_order"] for x in listed] == [2, 5, 9]


def test_sort_order_isolated_per_session(client: TestClient) -> None:
    """max+1 只统计本会话（每会话独立一套，§5）。"""
    sid_a = _make_session(client, "A")
    sid_b = _make_session(client, "B")
    assert _post_entry(client, sid_a)["sort_order"] == 1
    assert _post_entry(client, sid_a)["sort_order"] == 2
    assert _post_entry(client, sid_b)["sort_order"] == 1  # B 独立计数


# ---------------------------------------------------------------------------
# 会话隔离与 404（§4.3 / §5.1）
# ---------------------------------------------------------------------------


def test_session_isolation(client: TestClient) -> None:
    """两会话各建条目互不可见；跨会话 PATCH/DELETE 一律 404 且不生效。"""
    sid_a = _make_session(client, "A")
    sid_b = _make_session(client, "B")
    entry = _post_entry(client, sid_a)

    # B 的列表看不到 A 的条目
    assert client.get(f"/api/sessions/{sid_b}/worldbook").json() == []

    # 跨会话更新 → 404
    resp = client.patch(f"/api/sessions/{sid_b}/worldbook/{entry['id']}", json={"name": "hack"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "worldbook_entry_not_found"

    # 跨会话删除 → 404
    resp = client.delete(f"/api/sessions/{sid_b}/worldbook/{entry['id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "worldbook_entry_not_found"

    # A 的条目未被篡改/删除
    listed = client.get(f"/api/sessions/{sid_a}/worldbook").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "条目"


def test_nonexistent_session_404(client: TestClient) -> None:
    """会话不存在 → 全部端点 404 统一错误格式。"""
    assert client.get("/api/sessions/nope/worldbook").status_code == 404
    resp = client.post("/api/sessions/nope/worldbook", json={"name": "x", "content": "c"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"
    assert (
        client.patch("/api/sessions/nope/worldbook/e1", json={"name": "x"}).status_code == 404
    )
    assert client.delete("/api/sessions/nope/worldbook/e1").status_code == 404


# ---------------------------------------------------------------------------
# 更新与删除（§4.3）
# ---------------------------------------------------------------------------


def test_patch_entry_partial_update(client: TestClient) -> None:
    """PATCH 字段可选：更新传入字段并清洗关键词；未传字段不动。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)
    resp = client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}",
        json={
            "content": "新内容",
            "enabled": False,
            "keywords": [" 新词 ", "新词", "", "词二"],
            "sort_order": 7,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "新内容"
    assert body["enabled"] is False
    assert body["keywords"] == ["新词", "词二"]
    assert body["sort_order"] == 7
    assert body["name"] == "条目"  # 未传字段不动


def test_patch_entry_null_fields_not_updated_false_updates(client: TestClient) -> None:
    """PATCH 显式 null 一律不更新（含 bool enabled）；显式 false 必须生效。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)
    assert entry["enabled"] is True

    resp = client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}",
        json={"enabled": None, "keywords": None, "name": None, "content": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True  # null 不更新
    assert body["keywords"] == ["关键词"]  # null ≠ 清空（清空传 []）
    assert body["name"] == "条目"
    assert body["content"] == "内容"
    assert db.get_worldbook_entry(sid, entry["id"])["enabled"] == 1

    resp = client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}", json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False  # 显式 false 正常更新
    assert db.get_worldbook_entry(sid, entry["id"])["enabled"] == 0


def test_patch_entry_sort_order_upper_bound_422(client: TestClient) -> None:
    """sort_order 超上界 → 422（不再 500），统一错误格式。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)

    for value in (10_000_001, 10**19):
        resp = client.patch(
            f"/api/sessions/{sid}/worldbook/{entry['id']}", json={"sort_order": value}
        )
        assert resp.status_code == 422, value
        body = resp.json()
        assert set(body.keys()) == {"error"}
        # 走 main.py 全局 422 handler，沿用项目既有 code（不新造 code）
        assert body["error"]["code"] == "validation_error"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    # 超界请求不落库；上界本身仍可接受（宽松上界，不误伤合法值）
    assert client.get(f"/api/sessions/{sid}/worldbook").json()[0]["sort_order"] == 1
    resp = client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}",
        json={"sort_order": 10_000_000},
    )
    assert resp.status_code == 200
    assert resp.json()["sort_order"] == 10_000_000


def test_create_entry_sort_order_out_of_int64_range_422(client: TestClient) -> None:
    """POST sort_order 超出 SQLite int64 范围 → 422（不再 500），统一错误格式。

    Create 模型此前无任何边界，10**19 会直接触发 INTEGER 绑定 OverflowError
    → 500；边界仅为防溢出，不引入业务语义范围（负值仍接受）。
    """
    sid = _make_session(client)
    for value in (10**19, -(10**19)):
        resp = client.post(
            f"/api/sessions/{sid}/worldbook",
            json={"content": "内容", "sort_order": value},
        )
        assert resp.status_code == 422, value
        body = resp.json()
        assert set(body.keys()) == {"error"}
        # 走 main.py 全局 422 handler，沿用项目既有 code（不新造 code）
        assert body["error"]["code"] == "validation_error"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    # 超界请求不落库
    assert client.get(f"/api/sessions/{sid}/worldbook").json() == []

    # 上界与 int64 最小值仍可接受（宽松防溢出，不误伤合法值）
    for value in (10_000_000, -(2**63)):
        resp = client.post(
            f"/api/sessions/{sid}/worldbook",
            json={"content": "内容", "sort_order": value},
        )
        assert resp.status_code == 200, value
        assert resp.json()["sort_order"] == value


def test_patch_entry_sort_order_lower_bound_422(client: TestClient) -> None:
    """PATCH sort_order 低于 SQLite int64 下界 → 422（不再 500），且不落库。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)

    for value in (-(10**19), -(2**63) - 1):
        resp = client.patch(
            f"/api/sessions/{sid}/worldbook/{entry['id']}", json={"sort_order": value}
        )
        assert resp.status_code == 422, value
        body = resp.json()
        assert set(body.keys()) == {"error"}
        assert body["error"]["code"] == "validation_error"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    # 超界不落库；int64 最小值本身仍可接受（不收紧为 ge=0）
    assert client.get(f"/api/sessions/{sid}/worldbook").json()[0]["sort_order"] == 1
    resp = client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}",
        json={"sort_order": -(2**63)},
    )
    assert resp.status_code == 200
    assert resp.json()["sort_order"] == -(2**63)


def test_patch_entry_blank_content_422(client: TestClient) -> None:
    """PATCH 传入空白 content → 422。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)
    resp = client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}", json={"content": "  "}
    )
    assert resp.status_code == 422


def test_patch_entry_not_found_404(client: TestClient) -> None:
    """条目不存在 → 404 统一错误格式。"""
    sid = _make_session(client)
    resp = client.patch(f"/api/sessions/{sid}/worldbook/missing", json={"name": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "worldbook_entry_not_found"


def test_delete_entry(client: TestClient) -> None:
    """DELETE 条目 → {"deleted": true}；再次删除 404。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)
    resp = client.delete(f"/api/sessions/{sid}/worldbook/{entry['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert client.get(f"/api/sessions/{sid}/worldbook").json() == []

    resp2 = client.delete(f"/api/sessions/{sid}/worldbook/{entry['id']}")
    assert resp2.status_code == 404


def test_enabled_toggle_roundtrip(client: TestClient) -> None:
    """enabled 0/1 在 DB 中为整型，响应输出 bool（启停开关）。"""
    sid = _make_session(client)
    entry = _post_entry(client, sid)
    assert client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}", json={"enabled": False}
    ).json()["enabled"] is False
    assert client.get(f"/api/sessions/{sid}/worldbook").json()[0]["enabled"] is False
    assert client.patch(
        f"/api/sessions/{sid}/worldbook/{entry['id']}", json={"enabled": True}
    ).json()["enabled"] is True
