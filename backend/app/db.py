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

v0.6 变更（全局模型预设）：
- sessions 不再保存 base_url / api_key / model / reasoning_effort（v3 迁移回填为
  model_presets 预设后删除旧列），改为 `model_preset_id` 引用全局预设；
- 新增 model_presets 表与 list/get/get_raw/create/update/delete 六个 DAO；
- `get_session_raw` 保留（v0.6 起会话已无敏感字段，与 get_session 返回值相同，
  仅为兼容既有内部调用）。

注意：本模块的 DAO 签名按 §5.1 冻结，backend-chat 会直接调用。
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

# v3（v0.6②③④）：全局模型预设。
#   1. 新建 model_presets 表（§5）；
#   2. sessions 增加 model_preset_id（NULL = 未选择；不建外键，删除预设时由
#      delete_model_preset 显式把引用会话置 NULL）；
#   3. 旧数据回填：存在模型配置的会话按 (base_url, api_key, model,
#      reasoning_effort) 去重生成预设，并把会话 model_preset_id 指向匹配预设；
#   4. 回填完成后删除 sessions 的 base_url / api_key / model / reasoning_effort
#      旧列（SQLite 3.35+ 的 ALTER TABLE DROP COLUMN，本机 3.53 实测可用）。
#   5. meta 记录 presets_backfilled=1（纯 SQL 回填条件基于旧列，旧列删除后本身
#      即幂等；标记仅作为迁移完成的显式证据，防止任何人工重复回填路径）。
# 注意：迁移脚本自身不得包含 BEGIN/COMMIT/ROLLBACK（事务由 init_db 包裹）。
# 时间：预设取同组会话的 MIN(created_at) / MAX(updated_at)（复用真实时间线，
# 避免全部预设时间相同导致列表排序不稳定）；空串 reasoning_effort 归一为 NULL
# （NULL = 不设置/不向上游发送，与 API 校验语义一致）。
MIGRATION_V3_MODEL_PRESETS = """
CREATE TABLE model_presets (
    id                TEXT PRIMARY KEY,          -- UUID
    name              TEXT NOT NULL DEFAULT '',   -- 预设名（非空由 API 层校验）
    base_url          TEXT NOT NULL DEFAULT '',   -- 模型 Base URL
    api_key           TEXT NOT NULL DEFAULT '',   -- 明文存储（§12），接口打码回显
    model             TEXT NOT NULL DEFAULT '',   -- 模型名
    reasoning_effort  TEXT,                       -- NULL = 不设置；low/medium/high
    created_at        TEXT NOT NULL,              -- ISO8601（UTC）
    updated_at        TEXT NOT NULL               -- ISO8601（UTC）
);

ALTER TABLE sessions ADD COLUMN model_preset_id TEXT;

INSERT INTO model_presets (id, name, base_url, api_key, model, reasoning_effort, created_at, updated_at)
SELECT
    lower(hex(randomblob(16))),
    COALESCE(NULLIF(COALESCE(model, ''), ''), '默认模型'),
    COALESCE(base_url, ''),
    COALESCE(api_key, ''),
    COALESCE(model, ''),
    NULLIF(COALESCE(reasoning_effort, ''), ''),
    MIN(created_at),
    MAX(updated_at)
FROM sessions
WHERE COALESCE(base_url, '') <> ''
   OR COALESCE(model, '') <> ''
   OR COALESCE(api_key, '') <> ''
GROUP BY COALESCE(base_url, ''), COALESCE(api_key, ''), COALESCE(model, ''),
         COALESCE(reasoning_effort, '');

UPDATE sessions
SET model_preset_id = (
    SELECT p.id FROM model_presets p
    WHERE p.base_url = COALESCE(sessions.base_url, '')
      AND p.api_key = COALESCE(sessions.api_key, '')
      AND p.model = COALESCE(sessions.model, '')
      AND COALESCE(p.reasoning_effort, '') = COALESCE(sessions.reasoning_effort, '')
)
WHERE COALESCE(base_url, '') <> ''
   OR COALESCE(model, '') <> ''
   OR COALESCE(api_key, '') <> '';

ALTER TABLE sessions DROP COLUMN base_url;
ALTER TABLE sessions DROP COLUMN api_key;
ALTER TABLE sessions DROP COLUMN model;
ALTER TABLE sessions DROP COLUMN reasoning_effort;

INSERT INTO meta(key, value) VALUES ('presets_backfilled', '1')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;
"""

# MIGRATIONS：(版本号, SQL 脚本)；新版本只追加到列表末尾，禁止修改历史条目。
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
    # v2（v0.5①）：会话新增推理强度 reasoning_effort（可 NULL，无默认值）。
    # NULL = 不设置/不向上游发送该字段；取值 low/medium/high 由 API 层校验。
    # 历史 v1 库启动时由 init_db 自动增量升级，既有数据 reasoning_effort 为 NULL。
    (2, "ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT;"),
    # v3（v0.6）：全局模型预设 + 会话引用 + 旧模型配置回填后删除旧列。
    (3, MIGRATION_V3_MODEL_PRESETS),
]

# 会话表可被 update_session 更新的列（§4.3 PATCH 契约 + §5 表结构，v0.6）。
# v0.6 起 base_url / api_key / model / reasoning_effort 已从 sessions 删除，
# 模型配置改由 model_preset_id 引用全局预设。
_SESSION_COLUMNS = {
    "name",
    "system_prompt",
    "scan_rounds",
    "worldbook_enabled",
    "model_preset_id",
    "temperature",
    "top_p",
    "max_tokens",
    "max_context_chars",
    "proxy_url",
}

# 允许显式置为 NULL（"清除配置"）的列；其余列传入 None 视为"未提供"跳过。
# 注意：proxy_url 为 NOT NULL DEFAULT ''（§5 "可空 = 直连" 指空串，非 NULL），
# 清除代理应 PATCH 空串，因此不在此列表。
# max_tokens（v0.5）：null = 清除配置（上游默认）；
# model_preset_id（v0.6③）：null = 清除预设选择（未选择状态）。
_SESSION_NULLABLE = {"max_tokens", "model_preset_id"}

# 模型预设表可被 update_model_preset 更新的列（§4.3 PATCH /api/model-presets/{id}）。
_MODEL_PRESET_COLUMNS = {"name", "base_url", "api_key", "model", "reasoning_effort"}

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
    """会话列表：按 updated_at 倒序，附 message_count（§5.1）。

    v0.6 起会话表已无 api_key（模型配置走预设），无需打码。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT s.*,"
            "  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
            "FROM sessions s ORDER BY s.updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_session(session_id: str) -> dict | None:
    """会话详情（v0.6：含 model_preset_id；会话已无敏感字段，无需打码）。"""
    conn = _connect()
    try:
        row = _select_session(conn, session_id)
    finally:
        conn.close()
    return row


def get_session_raw(session_id: str) -> dict | None:
    """会话详情（原始值）。

    v0.6 起会话表已无 api_key 等敏感字段，本函数与 get_session 返回值相同；
    保留函数仅为兼容既有内部调用（chat 路由等），HTTP 层优先使用 get_session。
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
        model_preset_id / max_tokens 为 NULL（v0.6：新建会话未选择预设，
        聊天时会提示先选择；max_tokens NULL = 上游默认）。
    """
    session_id = _new_uuid()
    now = _utcnow()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions ("
            " id, name, system_prompt, scan_rounds, worldbook_enabled,"
            " model_preset_id, temperature, top_p, max_tokens, max_context_chars,"
            " proxy_url, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                name,
                "",   # system_prompt 默认空
                4,    # scan_rounds 默认 4
                1,    # worldbook_enabled 默认 1
                None,  # model_preset_id：未选择预设
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
    """部分更新会话：仅更新传入字段；自动刷新 updated_at（§5.1）。

    规则（v0.6）：
        - 未知键忽略（白名单 _SESSION_COLUMNS；v0.6 起 base_url / api_key /
          model / reasoning_effort 已不在白名单，传入被忽略）；
        - 显式 null 一律视为"未提供 → 不更新"，唯一例外是 _SESSION_NULLABLE
          （max_tokens 用 null 清除配置；model_preset_id 用 null 清除预设选择）；
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
        if column == "worldbook_enabled":
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
# 模型预设 DAO（v0.6，§5.1）
# ---------------------------------------------------------------------------

def _select_model_preset(conn: sqlite3.Connection, preset_id: str) -> dict | None:
    """按 id 查询模型预设（内部工具，不做打码处理）。"""
    row = conn.execute(
        "SELECT * FROM model_presets WHERE id = ?", (preset_id,)
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def _mask_preset(preset: dict) -> dict:
    """模型预设打码：api_key 保留前 4 后 4（§5.1 mask_key）。"""
    preset["api_key"] = mask_key(preset.get("api_key"))
    return preset


def list_model_presets() -> list[dict]:
    """模型预设列表：按 created_at 升序，api_key 打码（§5.1，v0.6）。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM model_presets ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()
    return [_mask_preset(_row_to_dict(r)) for r in rows]


def get_model_preset(preset_id: str) -> dict | None:
    """模型预设详情：api_key 打码（§5.1，对外接口用）。"""
    conn = _connect()
    try:
        preset = _select_model_preset(conn, preset_id)
    finally:
        conn.close()
    return _mask_preset(preset) if preset is not None else None


def get_model_preset_raw(preset_id: str) -> dict | None:
    """模型预设详情（api_key **不**打码，仅 chat / test-connection 内部使用，§5.1）。"""
    conn = _connect()
    try:
        return _select_model_preset(conn, preset_id)
    finally:
        conn.close()


def create_model_preset(data: dict) -> dict:
    """新建模型预设，返回原始 dict（api_key 未打码；HTTP 层负责打码回显）。

    data 字段（§5.1 / §4.3）：name / base_url / api_key / model /
    reasoning_effort。name 非空与 reasoning_effort 取值校验由 Pydantic 层负责；
    空串 reasoning_effort 归一为 NULL（NULL = 不设置/不向上游发送）。
    """
    preset_id = _new_uuid()
    now = _utcnow()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO model_presets ("
            " id, name, base_url, api_key, model, reasoning_effort, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preset_id,
                data.get("name") or "",
                data.get("base_url") or "",
                data.get("api_key") or "",
                data.get("model") or "",
                data.get("reasoning_effort") or None,
                now,
                now,
            ),
        )
        conn.commit()
        return _select_model_preset(conn, preset_id) or {}
    finally:
        conn.close()


def update_model_preset(preset_id: str, fields: dict) -> dict | None:
    """部分更新模型预设：仅更新传入字段，自动刷新 updated_at（§5.1，v0.6）。

    规则：
        - 未知键忽略（白名单 _MODEL_PRESET_COLUMNS）；
        - api_key 传 None / 空串 → 跳过（§4.3：仅非空值更新，避免打码回显被当明文写回）；
        - 其余字段传 None 视为"未提供 → 不更新"；唯一例外 reasoning_effort
          传 null = 清除（置 NULL，不向上游发送）；
        - 不存在返回 None。
    """
    if not fields:
        return get_model_preset_raw(preset_id)

    sets: list[str] = []
    params: list[object] = []
    for column in _MODEL_PRESET_COLUMNS:
        if column not in fields:
            continue
        value = fields[column]
        if column == "api_key":
            # api_key 仅在传入非空值时更新（§4.3 契约）
            if value is None or str(value).strip() == "":
                continue
            sets.append("api_key = ?")
            params.append(str(value))
        elif column == "reasoning_effort":
            if value is None or str(value).strip() == "":
                sets.append("reasoning_effort = NULL")  # null = 清除
            else:
                sets.append("reasoning_effort = ?")
                params.append(value)
        else:
            if value is None:
                continue
            sets.append(f"{column} = ?")
            params.append(value)

    if not sets:
        return get_model_preset_raw(preset_id)

    sets.append("updated_at = ?")
    params.append(_utcnow())
    params.append(preset_id)  # WHERE id = ?

    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE model_presets SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return _select_model_preset(conn, preset_id)
    finally:
        conn.close()


def delete_model_preset(preset_id: str) -> bool:
    """删除模型预设，并把引用它的会话 model_preset_id 置 NULL（§5.1，v0.6）。

    返回是否真的删除了预设（不存在返回 False）。两步写操作在同一事务内，
    避免"预设已删、会话仍引用悬空 id"的中间态。
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM model_presets WHERE id = ?", (preset_id,))
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE sessions SET model_preset_id = NULL WHERE model_preset_id = ?",
            (preset_id,),
        )
        conn.commit()
        return True
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
