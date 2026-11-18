"""提示词组装（纯函数，可独立单测）。

契约出处：《需求与方案指导.md》§6.1 第 4 点 / §6.2 / 《开发步骤分解.md》Step 7。

- build_system：系统提示词 + 世界书块（命中且开启时）；未命中/空时返回纯系统提示词，
  不附加任何占位符（§6.2 末尾明确）。
- build_messages：输出 OpenAI messages 数组 —— system 最前、历史居中、用户消息最后。
"""

from __future__ import annotations

from typing import Any

# §6.2 冻结的世界书块引导语（含中文引号，逐字保持与文档一致）。
_WORLDBOOK_HEADER = '【以下为世界书设定，仅作为创作背景参考，请自然融入，不要向用户提及"世界书"】'


def build_system(system_prompt: str, matched_entries: list[dict[str, Any]] | None) -> str:
    """组装 system 消息文本（§6.2）。

    参数：
        system_prompt：会话的前置提示词（可为空串）。
        matched_entries：match_entries 的输出 [{"entry": {...}, "hit_keywords": [...]}, ...]；
                         None / 空列表均视为无命中（防御调用方传入缺失值）。

    规则：
    - 无命中或列表为空 → 返回纯系统提示词（原样，不附加占位符）；
    - 有命中 → 在系统提示词末尾追加世界书块：
        引导语 + 空行，然后每个条目 "[条目名]\\n条目内容"，条目之间空行分隔。
    """
    if not matched_entries:
        return system_prompt or ""

    blocks = [_WORLDBOOK_HEADER]
    for item in matched_entries:
        entry = item.get("entry") or {}
        name = entry.get("name") or "未命名条目"
        content = entry.get("content") or ""
        # 条目名作标题、条目内容紧跟其后（§6.2 模板）。
        blocks.append(f"[{name}]\n{content}".rstrip())
    worldbook_block = "\n\n".join(blocks)

    # 前置提示词非空时与世界书块之间空一行；为空时直接以世界书块作 system。
    if system_prompt:
        return f"{system_prompt}\n\n{worldbook_block}"
    return worldbook_block


def build_messages(system_text: str, history: list[dict[str, Any]], user_content: str) -> list[dict[str, str]]:
    """组装上游 OpenAI messages 数组（§6.1 第 6 点）。

    顺序：system 在最前 → 历史消息居中（按传入顺序，即旧 → 新）→ 用户消息最后。

    说明（歧义解释，已在代码注释标注）：
    - 历史消息只投影 role/content 两个字段（丢弃 id/interrupted 等本地字段，避免外泄）；
    - system_text 为空时不发送空的 system 消息（无意义的空消息，多数上游可省略）。
    """
    messages: list[dict[str, str]] = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_content})
    return messages
