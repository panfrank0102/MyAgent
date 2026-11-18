"""世界书匹配引擎（纯函数，可独立单测）。

契约出处：《需求与方案指导.md》§2 第 12 行 / §6.1 第 3 点 / 《开发步骤分解.md》Step 5。
- 纯子串匹配：任一关键词出现在候选文本中即命中；
- 大小写：仅 ASCII 双方折叠为小写后比较（指导原文："ASCII 双方 .lower() 比较"）；
- 同一条目每轮只注入一次（一个条目即使多个关键词命中也只出现一次，
  但会记录全部命中关键词，供 meta 事件展示）；
- 输出按 sort_order 升序（不依赖调用方传入顺序）；
- enabled=0 的条目不参与匹配；条目缺 enabled 字段视为启用（兼容直接构造的 dict）。
"""

from __future__ import annotations

import string
from typing import Any

# 仅映射 ASCII 大写 → 小写；str.translate 在 C 层完成替换，
# 比逐字符条件拼接快（候选文本每轮可达数万字符）。
_ASCII_LOWER_TABLE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _fold_ascii(text: str) -> str:
    """仅将 ASCII 大写字母折叠为小写，其余字符原样保留。

    契约只要求 ASCII 忽略大小写，因此不做全量 Unicode .lower()，
    避免对个别非 ASCII 字符产生契约外的折叠行为。
    """
    return text.translate(_ASCII_LOWER_TABLE)


def _keywords_of(entry: dict[str, Any]) -> list[str]:
    """从条目中取出关键词列表（纯函数辅助）。

    契约 §5：keywords 以"一行一个"的 TEXT 形式存储；此处兼容两种输入：
    - str：按行切分、strip、去空行；
    - list：逐个 strip、去空字符串。
    关键词为空（清洗后无剩余）时条目不参与匹配（指导 §10 错误表）。
    """
    raw = entry.get("keywords") or ""
    if isinstance(raw, str):
        parts = raw.splitlines()
    else:
        parts = [str(p) for p in raw]
    return [p.strip() for p in parts if p and p.strip()]


def match_entries(text: str | None, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在候选文本中匹配世界书条目。

    参数：
        text：候选文本（最近 scan_rounds 轮消息 + 当前用户输入拼接，§6.1 第 3 点）；
              None 按空串处理（永不命中，防御调用方传入缺失值）。
        entries：本会话世界书条目列表（db.list_worldbook 返回的 dict 列表）。

    返回：
        [{"entry": 原始条目 dict, "hit_keywords": ["命中的关键词", ...]}, ...]
        按条目的 sort_order 升序；每个条目最多出现一次；
        该形状方便调用方直接序列化为 meta 事件：
        [{"name": e["entry"]["name"], "hit_keywords": e["hit_keywords"]} for e in matched]
    """
    # 防御：text 为 None 时视为空串（永不命中）。
    folded_text = _fold_ascii(text or "")
    matched: list[dict[str, Any]] = []

    # 契约：输出按 sort_order 升序；这里显式排序，不依赖调用方传入顺序。
    ordered = sorted(entries, key=lambda e: e.get("sort_order") if isinstance(e.get("sort_order"), int) else 0)

    for entry in ordered:
        # 契约：仅 enabled=1 的条目参与匹配；缺字段视为启用（兼容测试构造的 dict）。
        if not entry.get("enabled", 1):
            continue
        keywords = _keywords_of(entry)
        if not keywords:
            continue  # 无有效关键词 = 不参与匹配（指导 §10）
        hit_keywords = [kw for kw in keywords if _fold_ascii(kw) in folded_text]
        if hit_keywords:
            matched.append({"entry": entry, "hit_keywords": hit_keywords})

    return matched
