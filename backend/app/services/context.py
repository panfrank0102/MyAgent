"""Token 估算与上下文预算裁剪（纯函数，可独立单测）。

契约出处：《需求与方案指导.md》§6.3 / 《开发步骤分解.md》Step 6。

估算规则（指导 §6.3 冻结）：
    estimate_tokens(text) = 中文字符数 + ceil(ASCII 字符数 / 4) + 其他字符数
- 中文（CJK 统一表意文字 U+4E00~U+9FFF）：1 字符 ≈ 1 token；
- ASCII（ord < 128）：4 字符 ≈ 1 token（向上取整）；
- 其余字符（全角标点、emoji 等）：1 字符 ≈ 1 token。

预算规则：
- budget_for_history(max_context_chars, system_text, current_input)
    = max − est(system) − est(current_input) − 500（安全余量）。
  max_context_chars = 0 表示"不裁剪"，返回 None（注释见函数文档）。
- trim_history(history, budget)：
    budget 为 None → 原样返回（不裁剪）；
    budget ≤ 0 → 只保留最后一条消息（即使仍超预算也保留，见指导与任务要求）；
    budget > 0 → 从最旧整条消息开始丢弃，直到历史估算 ≤ 预算；
                  保底至少保留最后一条（与 0 预算规则一致，避免历史被清空）。
"""

from __future__ import annotations

import math
from typing import Any


def _is_cjk(ch: str) -> bool:
    """判断字符是否为 CJK 统一表意文字（"中文"的近似口径）。"""
    return "\u4e00" <= ch <= "\u9fff"


def estimate_tokens(text: str) -> int:
    """估算文本的 token 占用（保守估算，指导 §6.3）。

    中文 1 字符 ≈ 1 token；ASCII 4 字符 ≈ 1 token（向上取整）；
    其余字符 1 字符 ≈ 1 token。空串返回 0。
    """
    if not text:
        return 0
    ascii_count = 0
    other_count = 0
    for ch in text:
        code = ord(ch)
        if code < 128:
            ascii_count += 1  # ASCII：4 字符 ≈ 1 token，最后统一 ceil
        elif _is_cjk(ch):
            other_count += 1  # 中文：1 字符 ≈ 1 token
        else:
            other_count += 1  # 其余（全角标点/emoji 等）：1 字符 ≈ 1 token
    return other_count + math.ceil(ascii_count / 4)


def budget_for_history(max_context_chars: int, system_text: str, current_input: str) -> int | None:
    """计算历史消息可用的字符预算（指导 §6.1 第 5 点 / §6.3 动态预留）。

    = max_context_chars − est(system) − est(当前输入) − 500（安全余量）。

    - max_context_chars = 0 → 返回 None，表示"不裁剪"（指导 §6.3："0 = 不裁剪"）。
      任务说明允许返回 None 或大数，此处选择 None（语义更清晰，trim_history 同样识别）。
    - 计算结果允许为负数：负数同样传给 trim_history，由后者执行
      "≤0 只保留最后一条"的规则（负数防护）。
    """
    max_chars = int(max_context_chars or 0)
    if max_chars <= 0:
        # 契约：max=0 表示不裁剪；负数按不裁剪处理更安全（避免负数预算截断全部历史）。
        return None
    return max_chars - estimate_tokens(system_text) - estimate_tokens(current_input) - 500


def _history_estimate(history: list[dict[str, Any]]) -> int:
    """估算历史消息列表的总 token 数（只统计 content，契约未要求计入 role 等开销）。"""
    return sum(estimate_tokens(m.get("content") or "") for m in history)


def trim_history(history: list[dict[str, Any]], budget: int | None) -> list[dict[str, Any]]:
    """按预算裁剪历史消息：超限时从最旧整条消息开始丢弃。

    参数：
        history：消息 dict 列表（含 "content" 字段，旧 → 新）。
        budget：历史可用预算（budget_for_history 的结果）；
                None 表示不裁剪（max_context_chars = 0）。

    规则（指导 §6.3 + 任务边界要求）：
    - budget 为 None → 不裁剪，返回副本；
    - budget ≤ 0 → 只保留最后一条（即使最后一条估算仍超预算也保留）；
    - budget > 0 → 从最旧整条消息丢弃直至估算 ≤ 预算；
      若只剩最后一条仍超预算，保底保留最后一条（与 0 预算规则一致，
      避免把最近的上下文也裁掉）。

    注意：返回新列表，不修改入参。
    """
    if budget is None:
        return list(history)  # 不裁剪（max_context_chars = 0）

    if not history:
        return []

    if budget <= 0:
        # 契约：0/负数预算时只保留最后一条；仍超也保留最后一条。
        return history[-1:]

    kept = list(history)
    # 从最旧整条消息丢弃；至少保留最后一条（边界：单条消息本身超预算时不丢）。
    while len(kept) > 1 and _history_estimate(kept) > budget:
        kept.pop(0)
    return kept
