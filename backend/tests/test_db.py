"""db.py 存储层测试（开发步骤 Step 2 完成标准）。

覆盖：建表与迁移（schema_version）、会话 CRUD 与默认值、api_key 打码、
消息追加/查询/级联删除/清空、世界书 CRUD/排序/会话隔离、外键级联删除。

团队约定：fixture 写在本文件内（不创建 conftest.py / tests/__init__.py），
一律使用 tmp_path 临时数据库（init_db 显式传入路径），不污染 data/app.db。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# 保证在任意目录下运行 pytest 都能导入 backend/ 下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db

# UTC ISO8601 时间格式（带微秒与 Z 后缀，§5.1）
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


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
def dbfile(tmp_path: Path) -> Path:
    """每个测试独立的临时数据库文件（并成为模块级 _db_path）。"""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


# ---------------------------------------------------------------------------
# 建表与迁移
# ---------------------------------------------------------------------------


def test_init_db_idempotent_and_schema_version(dbfile: Path) -> None:
    """建表 + meta 记录 schema_version；重复 init 幂等。"""
    db.init_db(dbfile)  # 再次调用不报错（幂等）
    conn = sqlite3.connect(str(dbfile))
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"sessions", "messages", "worldbook_entries", "meta"} <= tables
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row is not None
        assert row[0] == str(db.MIGRATIONS[-1][0])  # 已执行到最新版本
        # §5 要求的两条索引
        indexes = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_messages_session" in indexes
        assert "idx_worldbook_session_sort" in indexes
    finally:
        conn.close()


def test_init_db_incremental_migration(dbfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MIGRATIONS 追加新版本：只执行未应用版本，schema_version 递增且可重入。"""
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [
            (1, db.SCHEMA_V1),
            (2, "CREATE TABLE demo_incremental (id INTEGER PRIMARY KEY);"),
        ],
    )
    db.init_db(dbfile)  # 当前已是 v1，只应执行 v2
    db.init_db(dbfile)  # 再次调用幂等
    conn = sqlite3.connect(str(dbfile))
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert version == "2"
    assert "demo_incremental" in tables
    assert {"sessions", "messages", "worldbook_entries"} <= tables  # 旧版本对象保留


def test_init_db_failed_migration_rolls_back(
    dbfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移脚本中途失败：DDL 与 schema_version 同一事务回滚，修正后可继续升级。"""
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [
            (1, db.SCHEMA_V1),
            (2, "CREATE TABLE half_migrated (id INTEGER);\nINSERT INTO no_such_table VALUES (1);"),
        ],
    )
    with pytest.raises(sqlite3.OperationalError):
        db.init_db(dbfile)

    conn = sqlite3.connect(str(dbfile))
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert version == "1"                 # 版本号未前进（回滚）
    assert "half_migrated" not in tables  # 半成品 DDL 已回滚
    assert {"sessions", "messages", "worldbook_entries"} <= tables

    # 修正迁移脚本后重新初始化：正常升到 v2，说明失败未留下不可恢复状态
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [
            (1, db.SCHEMA_V1),
            (2, "CREATE TABLE half_migrated (id INTEGER PRIMARY KEY);"),
        ],
    )
    db.init_db(dbfile)
    conn = sqlite3.connect(str(dbfile))
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert version == "2"
    assert "half_migrated" in tables


# ---------------------------------------------------------------------------
# api_key 打码（§5.1 mask_key）
# ---------------------------------------------------------------------------


def test_mask_key() -> None:
    """前 4 后 4、中间 *；不足 8 位全打码。"""
    assert db.mask_key("1234567890abcdef") == "1234********cdef"  # 16 位：前4后4中间8个*
    assert db.mask_key("12345678") == "12345678"  # 恰好 8 位：前 4 后 4 即全部（按契约字面执行）
    assert db.mask_key("abc") == "***"            # 不足 8 位全打码
    assert db.mask_key("") == ""
    assert db.mask_key(None) == ""


# ---------------------------------------------------------------------------
# 会话 CRUD（§5.1）
# ---------------------------------------------------------------------------


def test_create_session_defaults(dbfile: Path) -> None:
    """create_session 默认值严格照抄 §5.1：temperature/top_p 1.0、scan_rounds 4、
    worldbook_enabled 1、max_context_chars 80000、proxy_url/system_prompt/api_key 空。"""
    session = db.create_session("测试会话")
    assert session["name"] == "测试会话"
    assert session["temperature"] == 1.0
    assert session["top_p"] == 1.0
    assert session["scan_rounds"] == 4
    assert session["worldbook_enabled"] == 1
    assert session["max_context_chars"] == 80000
    assert session["proxy_url"] == ""
    assert session["system_prompt"] == ""
    assert session["api_key"] == ""
    assert session["base_url"] is None
    assert session["model"] is None
    assert session["max_tokens"] is None
    # id 为 UUID 形态（TEXT PK，§5）
    assert len(session["id"]) == 36 and session["id"].count("-") == 4
    # 时间字段为 UTC ISO8601
    assert ISO_RE.match(session["created_at"])
    assert ISO_RE.match(session["updated_at"])


def test_list_sessions_order_count_and_mask(dbfile: Path) -> None:
    """列表按 updated_at 倒序、附 message_count、api_key 打码。"""
    s1 = db.create_session("会话一")
    s2 = db.create_session("会话二")
    db.append_message(s1["id"], "user", "你好")
    db.append_message(s1["id"], "assistant", "在的")
    db.append_message(s2["id"], "user", "hi")
    db.update_session(s1["id"], {"api_key": "sk-secret-key-12345678"})
    db.update_session(s2["id"], {"name": "会话二改"})  # s2 最后被触碰 → 排最前

    sessions = db.list_sessions()
    assert [x["id"] for x in sessions] == [s2["id"], s1["id"]]
    by_id = {x["id"]: x for x in sessions}
    assert by_id[s1["id"]]["message_count"] == 2
    assert by_id[s2["id"]]["message_count"] == 1
    assert by_id[s1["id"]]["api_key"] == db.mask_key("sk-secret-key-12345678")
    assert by_id[s1["id"]]["api_key"] != "sk-secret-key-12345678"  # 明文不外泄


def test_get_session_masked_and_raw(dbfile: Path) -> None:
    """get_session 打码（§5.1 契约）；get_session_raw 供内部读取明文。"""
    session = db.create_session("s")
    db.update_session(session["id"], {"api_key": "sk-raw-key-abcdef1234"})

    masked = db.get_session(session["id"])
    assert masked is not None
    assert masked["api_key"] == db.mask_key("sk-raw-key-abcdef1234")
    assert masked["api_key"] != "sk-raw-key-abcdef1234"

    raw = db.get_session_raw(session["id"])
    assert raw is not None
    assert raw["api_key"] == "sk-raw-key-abcdef1234"  # 原始值（仅供内部）

    assert db.get_session("不存在") is None


def test_update_session_partial_and_updated_at(dbfile: Path) -> None:
    """仅更新传入字段，自动刷新 updated_at；未知/未传字段不动。"""
    session = db.create_session("原名")
    old_updated = session["updated_at"]
    updated = db.update_session(session["id"], {"name": "新名", "temperature": 0.7})
    assert updated is not None
    assert updated["name"] == "新名"
    assert updated["temperature"] == 0.7
    assert updated["top_p"] == 1.0        # 未传字段不动
    assert updated["scan_rounds"] == 4
    assert updated["updated_at"] != old_updated  # 自动刷新
    assert db.update_session("missing", {"name": "x"}) is None  # 不存在返回 None


def test_update_session_api_key_empty_not_updated(dbfile: Path) -> None:
    """api_key 空值不更新（§4.3 PATCH 语义，DAO 兜底）。"""
    session = db.create_session("s")
    db.update_session(session["id"], {"api_key": "sk-keep-me-12345678"})
    db.update_session(session["id"], {"api_key": ""})       # 空串 → 跳过
    assert db.get_session_raw(session["id"])["api_key"] == "sk-keep-me-12345678"
    db.update_session(session["id"], {"api_key": None})     # None → 跳过
    assert db.get_session_raw(session["id"])["api_key"] == "sk-keep-me-12345678"
    db.update_session(session["id"], {"api_key": "sk-new-key-87654321"})  # 非空才更新
    assert db.get_session_raw(session["id"])["api_key"] == "sk-new-key-87654321"


def test_update_session_nullable_fields(dbfile: Path) -> None:
    """可空配置列（base_url/model/max_tokens）可显式置 NULL 清除；
    proxy_url 为 NOT NULL 默认空串：传 None 视为未提供，清除用空串。"""
    session = db.create_session("s")
    db.update_session(session["id"], {
        "base_url": "http://x", "model": "gpt", "max_tokens": 100, "proxy_url": "http://p",
    })
    updated = db.update_session(session["id"], {
        "base_url": None, "model": None, "max_tokens": None, "proxy_url": None,
    })
    assert updated["base_url"] is None
    assert updated["model"] is None
    assert updated["max_tokens"] is None
    assert updated["proxy_url"] == "http://p"  # 非空列：None 视为未提供，值不变
    # 清除代理用空串（§5：空 = 直连）
    cleared = db.update_session(session["id"], {"proxy_url": ""})
    assert cleared["proxy_url"] == ""
    # worldbook_enabled 存 0/1 整型
    assert db.update_session(session["id"], {"worldbook_enabled": False})["worldbook_enabled"] == 0


def test_delete_session_cascades(dbfile: Path) -> None:
    """delete_session 级联删除消息与世界书条目；不存在返回 False。"""
    session = db.create_session("s")
    db.append_message(session["id"], "user", "m1")
    db.append_message(session["id"], "assistant", "m2")
    db.create_worldbook_entry(session["id"], {"name": "条目", "content": "内容"})

    assert db.delete_session(session["id"]) is True
    assert db.get_session(session["id"]) is None
    assert db.list_messages(session["id"]) == []
    assert db.list_worldbook(session["id"]) == []
    assert db.delete_session(session["id"]) is False  # 再删返回 False


# ---------------------------------------------------------------------------
# 消息 DAO（§5.1）
# ---------------------------------------------------------------------------


def test_append_message_and_session_updated_at(dbfile: Path) -> None:
    """追加消息返回 dict；非法 role 抛 ValueError；内部同步更新会话 updated_at。"""
    session = db.create_session("s")
    before = session["updated_at"]

    msg = db.append_message(session["id"], "user", "你好")
    assert msg["id"] == 1
    assert msg["role"] == "user"
    assert msg["content"] == "你好"
    assert msg["interrupted"] == 0
    assert ISO_RE.match(msg["created_at"])

    msg2 = db.append_message(session["id"], "assistant", "hi", interrupted=1)
    assert msg2["id"] == 2
    assert msg2["interrupted"] == 1

    # 消息追加属于会话活动 → updated_at 同步刷新（§5.1）
    assert db.get_session_raw(session["id"])["updated_at"] != before

    with pytest.raises(ValueError):
        db.append_message(session["id"], "system", "x")  # 非法 role


def test_append_message_missing_session_fk(dbfile: Path) -> None:
    """会话不存在时外键约束拦截（PRAGMA foreign_keys 生效）。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.append_message("no-such-session", "user", "x")


def test_list_messages_latest_limit_ascending(dbfile: Path) -> None:
    """取最新 limit 条，按 id 正序返回（§5.1）。"""
    session = db.create_session("s")
    for i in range(5):
        db.append_message(session["id"], "user" if i % 2 == 0 else "assistant", f"m{i}")

    assert [m["id"] for m in db.list_messages(session["id"])] == [1, 2, 3, 4, 5]
    assert [m["id"] for m in db.list_messages(session["id"], limit=3)] == [3, 4, 5]


def test_delete_message_and_after(dbfile: Path) -> None:
    """删单条级联其后消息，返回删除条数；锚点不存在返回 0 且不误删。"""
    session = db.create_session("s")
    for i in range(5):
        db.append_message(session["id"], "user", f"m{i}")

    assert db.delete_message_and_after(session["id"], 3) == 3  # 3/4/5 被删
    assert [m["id"] for m in db.list_messages(session["id"])] == [1, 2]

    # 锚点已不存在：返回 0 且不删任何消息（防误删保护）
    assert db.delete_message_and_after(session["id"], 3) == 0
    assert [m["id"] for m in db.list_messages(session["id"])] == [1, 2]
    assert db.delete_message_and_after(session["id"], 99) == 0

    # 会话隔离：messages.id 是全局自增（§5），删除只作用于本会话，
    # 其他会话的消息 id 不影响本会话的删除判定
    other = db.create_session("s2")
    other_msg = db.append_message(other["id"], "user", "x")
    assert db.delete_message_and_after(session["id"], other_msg["id"]) == 0  # 该 id 不属于本会话
    assert [m["id"] for m in db.list_messages(session["id"])] == [1, 2]
    assert db.delete_message_and_after(other["id"], other_msg["id"]) == 1
    assert db.list_messages(other["id"]) == []


def test_clear_messages(dbfile: Path) -> None:
    """清空返回删除条数；空表清空返回 0。"""
    session = db.create_session("s")
    for i in range(3):
        db.append_message(session["id"], "user", f"m{i}")
    assert db.clear_messages(session["id"]) == 3
    assert db.list_messages(session["id"]) == []
    assert db.clear_messages(session["id"]) == 0


# ---------------------------------------------------------------------------
# 世界书 DAO（§5.1，每会话独立一套）
# ---------------------------------------------------------------------------


def test_worldbook_create_auto_sort_order_per_session(dbfile: Path) -> None:
    """sort_order 缺省 = 本会话 max+1；各会话独立计数。"""
    s1 = db.create_session("s1")
    s2 = db.create_session("s2")
    e1 = db.create_worldbook_entry(s1["id"], {"name": "A", "content": "c"})
    e2 = db.create_worldbook_entry(s1["id"], {"name": "B", "content": "c"})
    e3 = db.create_worldbook_entry(s1["id"], {"name": "C", "content": "c", "sort_order": 10})
    e4 = db.create_worldbook_entry(s1["id"], {"name": "D", "content": "c"})
    assert (e1["sort_order"], e2["sort_order"], e3["sort_order"], e4["sort_order"]) == (1, 2, 10, 11)

    f1 = db.create_worldbook_entry(s2["id"], {"name": "F", "content": "c"})
    assert f1["sort_order"] == 1  # 每会话独立一套，从 1 开始
    assert len(e1["id"]) == 36   # UUID 主键
    assert e1["enabled"] == 1
    assert e1["keywords"] == ""


def test_worldbook_keywords_list_stored_joined(dbfile: Path) -> None:
    """DAO 接收 list 关键词时以换行分隔存 TEXT（§5：一行一个）。"""
    session = db.create_session("s")
    entry = db.create_worldbook_entry(
        session["id"], {"name": "n", "keywords": ["猫耳", "尾巴"], "content": "c"}
    )
    stored = db.get_worldbook_entry(session["id"], entry["id"])
    assert stored["keywords"] == "猫耳\n尾巴"


def test_worldbook_list_sorted_and_update_partial(dbfile: Path) -> None:
    """列表按 sort_order 升序；更新仅作用于传入字段。"""
    session = db.create_session("s")
    ids = [
        db.create_worldbook_entry(
            session["id"], {"name": f"n{i}", "content": "c", "sort_order": order}
        )["id"]
        for i, order in enumerate([5, 2, 9])
    ]
    assert [x["sort_order"] for x in db.list_worldbook(session["id"])] == [2, 5, 9]

    updated = db.update_worldbook_entry(session["id"], ids[0], {"enabled": 0, "content": "新内容"})
    assert updated is not None
    assert updated["enabled"] == 0
    assert updated["content"] == "新内容"
    assert updated["name"] == "n0"       # 未传字段不动
    assert updated["sort_order"] == 5

    # 关键词更新：list → 换行 TEXT
    updated2 = db.update_worldbook_entry(session["id"], ids[0], {"keywords": ["a", "b"]})
    assert updated2 is not None and updated2["keywords"] == "a\nb"


def test_worldbook_cross_session_isolation(dbfile: Path) -> None:
    """跨会话读取/更新/删除分别返回 None / None / False（§5.1 会话隔离）。"""
    s1 = db.create_session("s1")
    s2 = db.create_session("s2")
    entry = db.create_worldbook_entry(s1["id"], {"name": "n", "content": "c"})

    assert db.get_worldbook_entry(s2["id"], entry["id"]) is None
    assert db.update_worldbook_entry(s2["id"], entry["id"], {"name": "x"}) is None
    assert db.delete_worldbook_entry(s2["id"], entry["id"]) is False

    # 本会话不受影响
    assert db.get_worldbook_entry(s1["id"], entry["id"]) is not None
    assert db.delete_worldbook_entry(s1["id"], entry["id"]) is True
    assert db.delete_worldbook_entry(s1["id"], entry["id"]) is False
