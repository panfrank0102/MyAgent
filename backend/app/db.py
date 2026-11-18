"""SQLite 存储层与 DAO（开发步骤 Step 2）。

契约出处：《需求与方案指导.md》§5（表结构）与 §5.1（DAO 契约，签名冻结）。

设计要点：
- 标准库 sqlite3，不引入 ORM；连接使用 row_factory=sqlite3.Row；
- 线程安全策略：**每个操作独立连接**（用完即关），不存在跨线程共享的连接对象，
  配合 sqlite 自身的文件锁即可满足 FastAPI 线程池并发的需求；
- 时间字段一律存 **UTC ISO8601 字符串**（形如 2026-11-18T04:30:15.123456Z），
  前端负责本地化显示（§5.1）；
- 迁移框架：meta 表（key/value）记录 schema_version，MIGRATIONS 列表按版本号
  顺序增量执行；init_db 幂等（§5.1）；
- 所有 DAO 返回 dict（sqlite3.Row → dict），不泄漏连接对象。

注意：本模块的 DAO 签名按 §5.1 冻结，backend-chat 会直接调用；
除契约表列出的 16 个函数外，另提供 `get_session_raw`（返回未打码的 api_key，
供 backend-chat 的聊天流程读取真实 Key 使用，见其 docstring）。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import config

# ---------------------------------------------------------------------------
# 模块级状态：最近一次 init_db 使用的数据库路径（§5.1：连接函数使用该路径）。
# ---------------------------------------------------------------------------
_db_path: Path | None = None

# ---------------------------------------------------------------------------
# 迁移框架（§5.1：meta 表记录 schema_version，按 MIGRATIONS 顺序增量执行）
# ---------------------------------------------------------------------------

# 版本 1 的完整初始建表语句。表结构与《需求与方案指导.md》§5 逐字段对齐：
# - sessions：全部列、默认值严格照抄 §5 表格；
# - messages：id 自增（天然有序）、session_id 外键级联删除、interrupted 0/1，
#   索引 messages(session_id, id)；
# - worldbook_entries：每会话独立一套，session_id 外键级联删除，keywords 为
#   "一行一个"的 TEXT，索引 worldbook_entries(session_id, sort_order)。
SCHEMA_V1 = """
CREATE TABLE sessions (
    id                TEXT PRIMARY KEY,          -- UUID（§5）
    name              TEXT NOT NULL DEFAULT '',   -- 会话名
    system_prompt     TEXT NOT NULL DEFAULT '',   -- 前置提示词，默认空
    scan_rounds       INTEGER NOT NULL DEFAULT 4, -- 世界书扫描轮数，默认 4，最小 0
    worldbook_enabled INTEGER NOT NULL DEFAULT 1, -- 0/1，默认 1
    base_url          TEXT,                       -- 模型 Base URL（可空）
    api_key           TEXT NOT NULL DEFAULT '',   -- 明文存储（§12），默认空串
    model             TEXT,                       -- 模型名（可空）
    temperature       REAL NOT NULL DEFAULT 1.0,  -- 默认 1.0
    top_p             REAL NOT NULL DEFAULT 1.0,  -- 默认 1.0
    max_tokens        INTEGER,                    -- NULL = 上游默认
    max_context_chars INTEGER NOT NULL DEFAULT 80000, -- 上下文预算，默认 80000；0 = 不裁剪
    proxy_url         TEXT NOT NULL DEFAULT '',   -- HTTP 代理（空 = 直连）
    created_at        TEXT NOT NULL,              -- ISO8601（UTC）
    updated_at        TEXT NOT NULL               -- ISO8601（UTC）
);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT, -- 自增，天然有序
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                     -- 仅 user / assistant
    content     TEXT NOT NULL,                     -- interrupted 时为已生成部分
    interrupted INTEGER NOT NULL DEFAULT 0,        -- 0/1：该 AI 回复被手动停止
    created_at  TEXT NOT NULL                      -- ISO8601（UTC）
);

CREATE INDEX idx_messages_session ON messages(session_id, id);

CREATE TABLE worldbook_entries (
    id         TEXT PRIMARY KEY,                   -- UUID
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',           -- 条目名（注入时作为标题）
    keywords   TEXT NOT NULL DEFAULT '',           -- 一行一个；可为空 = 不参与匹配
    content    TEXT NOT NULL,                      -- 条目内容（必填，空禁止保存）
    enabled    INTEGER NOT NULL DEFAULT 1,         -- 0/1
    sort_order INTEGER NOT NULL                    -- 越小越靠前；新条目取 max+1
);

CREATE INDEX idx_worldbook_session_sort ON worldbook_entries(session_id, sort_order);
"""

# MIGRATIONS：(版本号, SQL 脚本)；新版本只追加到列表末尾，禁止修改历史条目。
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
    # v2（v0.5①）：会话新增推理强度 reasoning_effort（可 NULL，无默认值）。
    # NULL = 不设置/不向上游发送该字段；取值 low/medium/high 由 API 层校验。
    # 历史 v1 库启动时由 init_db 自动增量升级，既有数据 reasoning_effort 为 NULL。
    (2, "ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT;"),
]

# 会话表可被 update_session 更新的列（§4.3 PATCH 契约 + §5 表结构）。
_SESSION_COLUMNS = {
    "name",
    "system_prompt",
    "scan_rounds",
    "worldbook_enabled",
    "base_url",
    "api_key",
    "model",
    "temperature",
    "top_p",
    "max_tokens",
    "max_context_chars",
    "reasoning_effort",
    "proxy_url",
}

# 允许显式置为 NULL（"清除配置"）的列；其余列传入 None 视为"未提供"跳过。
# 注意：proxy_url 为 NOT NULL DEFAULT ''（§5 "可空 = 直连" 指空串，非 NULL），
# 清除代理应 PATCH 空串，因此不在此列表。
# reasoning_effort（v0.5①）为可空 TEXT：null = 清除配置（置 NULL，不向上游发送）。
_SESSION_NULLABLE = {"base_url", "model", "max_tokens", "reasoning_effort"}

# 世界书条目表可被 update_worldbook_entry 更新的列（§4.3 PATCH 契约）。
_WORLDBOOK_COLUMNS = {"name", "keywords", "content", "enabled", "sort_order"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串（带 Z 后缀，微秒精度）。

    §5.1：时间字段一律存 UTC ISO8601 字符串；同格式字符串按字典序比较
    即等于按时间先后比较，便于 SQL 直接 ORDER BY。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_uuid() -> str:
    """生成 UUID 字符串（sessions / worldbook_entries 主键，§5）。"""
    return str(uuid.uuid4())


def _connect() -> sqlite3.Connection:
    """打开一个指向最近一次 init_db 路径的独立连接。

    - row_factory=sqlite3.Row：查询结果可按列名取值；
    - PRAGMA foreign_keys=ON：SQLite 默认关闭外键，必须每连接显式开启，
      否则 messages / worldbook_entries 的 ON DELETE CASCADE 不生效；
    - timeout=10：等待写锁的秒数，缓解并发写时的 database is locked。
    """
    if _db_path is None:
        raise RuntimeError("数据库尚未初始化：请先调用 init_db(path)")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """sqlite3.Row → dict（DAO 契约：返回一律 dict）。"""
    return {key: row[key] for key in row.keys()}


# ---------------------------------------------------------------------------
# 初始化与迁移
# ---------------------------------------------------------------------------

def init_db(path: Path | None = None) -> None:
    """幂等建表并按 MIGRATIONS 增量迁移（§5.1 契约）。

    参数：
        path：数据库文件路径；None 时使用 app.config.DB_PATH（生产默认），
              测试传入 tmp_path 下的文件，避免污染 data/app.db。

    实现：
        1. 记录模块级 _db_path（后续所有 _connect 使用）；
        2. 确保父目录存在；
        3. 建 meta 表（key/value），读取当前 schema_version；
        4. 逐条执行版本号大于当前值的迁移，并在同一事务内更新 schema_version，
           保证"表结构 + 版本号"原子落盘；
        5. WAL 模式：提升并发读写稳定性（单机工具，写入量小，够用）。
    """
    global _db_path
    db_path = Path(path) if path is not None else config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = db_path

    # isolation_level=None（自动提交模式）：事务边界完全由我们显式控制，
    # 便于把整段迁移脚本（BEGIN...COMMIT）作为单个事务执行。
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # meta 表是迁移框架自身的载体，不属于任何版本，先确保存在。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "  key   TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row is not None else 0

        for version, script in MIGRATIONS:
            if version <= current:
                continue  # 已执行过的版本跳过（幂等）
            # executescript 会先提交挂起事务，因此把 BEGIN/COMMIT 写进脚本自身；
            # schema_version 的更新也放进同一个事务，使"整段 DDL + 版本号更新"
            # 原子落盘（若分两次提交，进程在 DDL 已提交、版本号未写入之间崩溃时，
            # 下次启动会重复执行建表而报 "table already exists"）。
            # executescript 不支持参数绑定，version 来自受信的 MIGRATIONS 常量，
            # 先 int() 归一后再拼接，无注入风险。
            # 约束：MIGRATIONS 中的脚本自身不得包含事务控制语句（BEGIN/COMMIT/ROLLBACK）。
            conn.executescript(
                f"BEGIN;\n{script}\n"
                f"INSERT INTO meta(key, value) VALUES('schema_version', '{int(version)}') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\nCOMMIT;"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 会话 DAO（§5.1）
# ---------------------------------------------------------------------------

def mask_key(key: str | None) -> str:
    """api_key 打码：保留前 4 后 4，中间以 * 填充；不足 8 位全打码（§5.1）。

    示例：
        "1234567890abcdef" → "1234********cdef"
        "12345678"          → "12345678"（恰好 8 位，前 4 后 4 即为全部，按契约字面执行）
        "abc"               → "***"（不足 8 位全打码）
        None / ""           → ""（空 Key 打码后仍为空）
    """
    if not key:
        return ""
    if len(key) < 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _select_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """按 id 查询会话（内部工具，不做打码处理）。"""
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_sessions() -> list[dict]:
    """会话列表：按 updated_at 倒序，附 message_count，api_key 已打码（§5.1）。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT s.*,"
            "  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
            "FROM sessions s ORDER BY s.updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    result = [_row_to_dict(r) for r in rows]
    for item in result:
        item["api_key"] = mask_key(item["api_key"])
    return result


def get_session(session_id: str) -> dict | None:
    """会话详情：api_key 打码（§5.1 契约，对外接口一律不暴露明文）。"""
    conn = _connect()
    try:
        row = _select_session(conn, session_id)
    finally:
        conn.close()
    if row is None:
        return None
    row["api_key"] = mask_key(row["api_key"])
    return row


def get_session_raw(session_id: str) -> dict | None:
    """会话详情（api_key **不**打码，原始值）。

    这是对 §5.1 契约的补充工具函数（契约表未列出）：§5.1 冻结了
    get_session 返回打码 Key，但 backend-chat 的聊天流程需要真实 Key
    调用上游 API，因此提供本函数供其内部读取；HTTP 层不得直接使用。
    """
    conn = _connect()
    try:
        row = _select_session(conn, session_id)
    finally:
        conn.close()
    return row


def create_session(name: str) -> dict:
    """新建会话，返回完整会话 dict（§5.1 默认值契约）。

    默认值（严格照抄 §5.1 / §4.3）：
        temperature 1.0、top_p 1.0、scan_rounds 4、worldbook_enabled 1、
        max_context_chars 80000、proxy_url 空、system_prompt 空；
        base_url / model / max_tokens / reasoning_effort 为 NULL（可空，
        上游默认；reasoning_effort 为 NULL = 不设置/不发送，v0.5①）。
    """
    session_id = _new_uuid()
    now = _utcnow()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions ("
            " id, name, system_prompt, scan_rounds, worldbook_enabled, base_url,"
            " api_key, model, temperature, top_p, max_tokens, max_context_chars,"
            " proxy_url, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                name,
                "",   # system_prompt 默认空
                4,    # scan_rounds 默认 4
                1,    # worldbook_enabled 默认 1
                None,  # base_url 可空
                "",   # api_key 默认空串
                None,  # model 可空
                1.0,  # temperature 默认 1.0
                1.0,  # top_p 默认 1.0
                None,  # max_tokens：NULL = 上游默认
                80000,  # max_context_chars 默认 80000
                "",   # proxy_url 默认空 = 直连
                now,
                now,
            ),
        )
        conn.commit()
        return _select_session(conn, session_id) or {}
    finally:
        conn.close()


def update_session(session_id: str, fields: dict) -> dict | None:
    """部分更新会话：仅更新传入字段；api_key 空值不更新；自动刷新 updated_at（§5.1）。

    规则：
        - 未知键忽略（白名单 _SESSION_COLUMNS）；
        - api_key 传 None / 空串 → 跳过（§4.3：仅非空值更新）；
        - 显式 null 一律视为"未提供 → 不更新"，唯一例外是 _SESSION_NULLABLE
          （base_url / model / max_tokens）用 null 表示"清除配置"置 NULL；
        - proxy_url 为 NOT NULL 列，传 None 视为"未提供"（清除代理用空串）；
        - worldbook_enabled 统一存 0/1；显式 false 正常写入 0。
    """
    if not fields:
        # 空更新也算"会话存在"，直接返回当前值（updated_at 不动，契约未要求）。
        return get_session_raw(session_id)

    sets: list[str] = []
    params: list[object] = []
    for column in _SESSION_COLUMNS:
        if column not in fields:
            continue
        value = fields[column]
        # 显式 null 统一按"未提供 → 不更新"处理（PATCH 语义，所有列一致）：
        # 可空配置列例外，null 表示"清除配置 → 置 NULL"。必须先判 None，
        # 否则 worldbook_enabled 分支的 `1 if value else 0` 会把 null 误写成 0。
        if value is None:
            if column in _SESSION_NULLABLE:
                sets.append(f"{column} = NULL")
            continue
        if column == "api_key":
            # api_key 仅在传入非空值时更新（§4.3 PATCH 语义）
            if str(value).strip() == "":
                continue
            sets.append("api_key = ?")
            params.append(str(value))
        elif column == "worldbook_enabled":
            # 0/1 整型存储（表结构 §5）；显式 false 正常写入 0
            sets.append("worldbook_enabled = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{column} = ?")
            params.append(value)

    if not sets:
        return get_session_raw(session_id)

    sets.append("updated_at = ?")
    params.append(_utcnow())
    params.append(session_id)  # WHERE id = ?

    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return _select_session(conn, session_id)
    finally:
        conn.close()


def delete_session(session_id: str) -> bool:
    """删除会话：级联删除其消息与世界书条目（§5.1 / §4.3）。

    依赖外键 ON DELETE CASCADE（_connect 已开启 PRAGMA foreign_keys=ON）。
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 消息 DAO（§5.1）
# ---------------------------------------------------------------------------

def append_message(session_id: str, role: str, content: str, interrupted: int = 0) -> dict:
    """追加一条消息；仅接受 role ∈ {user, assistant}；内部同步更新会话 updated_at。

    会话不存在时由外键约束抛出 sqlite3.IntegrityError（调用方需先校验会话存在，
    与 §6.1 的流程一致：先校验会话再存用户消息）。
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"非法 role：{role!r}（仅允许 'user' / 'assistant'，§5）")
    now = _utcnow()
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, interrupted, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, 1 if interrupted else 0, now),
        )
        # §5.1：内部同步更新会话 updated_at（消息追加属于会话活动）
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        conn.commit()
        message_id = cur.lastrowid
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_messages(session_id: str, limit: int = 200) -> list[dict]:
    """取该会话最新 limit 条消息，返回按 id 正序（§5.1）。

    实现：先按 id 倒序取 limit 条，再正序排回，保证"最新的 N 条、顺序不变"。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (session_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def delete_message_and_after(session_id: str, message_id: int) -> int:
    """删除该条消息及其后所有消息，返回删除条数（§5.1 / §4.3 删单条级联）。

    防误删保护：先确认锚点消息存在；若不存在（已被删/不属于本会话）返回 0 且
    **不删除任何消息**，避免"删除不存在的第 N 条却把 N+1 之后全删掉"的坑。
    """
    conn = _connect()
    try:
        anchor = conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND id = ?",
            (session_id, message_id),
        ).fetchone()
        if anchor is None:
            return 0
        cur = conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND id >= ?",
            (session_id, message_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_messages(session_id: str) -> int:
    """清空该会话全部消息，返回删除条数（§5.1 / §4.3）。"""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 世界书条目 DAO（§5.1，每会话独立一套）
# ---------------------------------------------------------------------------

def _normalize_keywords_value(keywords: object) -> str:
    """DAO 层的关键词轻量归一：list → 换行分隔 TEXT（§5：一行一个）。

    路由层已完成 trim/去空行/去重，这里只负责 list → str 的形态转换；
    传入字符串时原样保存，传入 None/其他类型时按空处理。
    """
    if isinstance(keywords, list):
        return "\n".join(str(k) for k in keywords)
    if isinstance(keywords, str):
        return keywords
    return ""


def list_worldbook(session_id: str) -> list[dict]:
    """该会话世界书列表，按 sort_order 升序（§5.1）。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM worldbook_entries WHERE session_id = ? ORDER BY sort_order ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_worldbook_entry(session_id: str, entry_id: str) -> dict | None:
    """按 id 取条目；跨会话访问返回 None（会话隔离，§5.1）。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM worldbook_entries WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None


def create_worldbook_entry(session_id: str, data: dict) -> dict:
    """新建世界书条目；sort_order 缺省 = 本会话 max+1（§5.1）。

    data 字段：name / keywords（list 或已清洗的换行文本）/ content /
    enabled / sort_order。content 非空校验由路由层（Pydantic）负责，
    DAO 保持契约最小行为。
    """
    entry_id = _new_uuid()
    keywords = _normalize_keywords_value(data.get("keywords", []))
    sort_order = data.get("sort_order")

    conn = _connect()
    try:
        if sort_order is None:
            # 本会话 max+1；无条目时 COALESCE 兜底为 0，首条取 1。
            # BEGIN IMMEDIATE 保证"查 max → 插入"之间不被其他写入穿插，
            # 避免并发生成重复 sort_order。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) AS mx"
                " FROM worldbook_entries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            sort_order = (row["mx"] if row else 0) + 1

        conn.execute(
            "INSERT INTO worldbook_entries (id, session_id, name, keywords, content,"
            " enabled, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                session_id,
                data.get("name", ""),
                keywords,
                data.get("content", ""),
                1 if data.get("enabled", 1) else 0,
                int(sort_order),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM worldbook_entries WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_worldbook_entry(session_id: str, entry_id: str, fields: dict) -> dict | None:
    """部分更新世界书条目，仅更新传入字段；跨会话/不存在返回 None（§5.1）。"""
    if not fields:
        return get_worldbook_entry(session_id, entry_id)

    sets: list[str] = []
    params: list[object] = []
    for column in _WORLDBOOK_COLUMNS:
        if column not in fields:
            continue
        value = fields[column]
        # 显式 null 统一按"未提供 → 不更新"处理（PATCH 语义，所有列一致）。
        # 清空 keywords 请传 []（空列表是显式值）。此判断必须先于 enabled
        # 分支，否则 `1 if value else 0` 会把 null 误写成 0。
        if value is None:
            continue
        if column == "keywords":
            sets.append("keywords = ?")
            params.append(_normalize_keywords_value(value))
        elif column == "enabled":
            sets.append("enabled = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{column} = ?")
            params.append(value)
    if not sets:
        return get_worldbook_entry(session_id, entry_id)

    params.extend([session_id, entry_id])
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE worldbook_entries SET {', '.join(sets)}"
            " WHERE session_id = ? AND id = ?",
            tuple(params),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM worldbook_entries WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_worldbook_entry(session_id: str, entry_id: str) -> bool:
    """删除条目；跨会话删除返回 False（会话隔离，§5.1）。"""
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM worldbook_entries WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
