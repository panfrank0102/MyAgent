"""Step 6 测试：token 估算与上下文裁剪（app/services/context.py 纯函数）。

覆盖（《开发步骤分解.md》Step 6 完成标准）：
空历史、刚好超限、整条丢弃粒度、0/负数预算只保最新一条、
负数防护（budget_for_history 允许负数并交给 trim 处理）、max=0 不裁剪。
"""

from __future__ import annotations

from app.services.context import budget_for_history, estimate_tokens, trim_history


# ---------- estimate_tokens ----------

def test_estimate_empty() -> None:
    assert estimate_tokens("") == 0


def test_estimate_ascii_4_chars_per_token() -> None:
    """ASCII 4 字符 ≈ 1 token（向上取整）。"""
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("a") == 1


def test_estimate_chinese_one_per_token() -> None:
    """中文 1 字符 ≈ 1 token。"""
    assert estimate_tokens("你好世界") == 4


def test_estimate_mixed() -> None:
    """混合：中文 1/字、ASCII 1/4字、其余 1/字。"""
    # 4 个汉字(4) + 8 个 ASCII(2) + 2 个全角标点(2) = 8
    text = "你好啊，World!。"  # 汉字:你好啊 → 3；ASCII:World! → 6；全角：，。→ 2
    # 3 + ceil(6/4)=2 + 2 = 7
    assert estimate_tokens(text) == 7


def test_estimate_emoji_and_newline() -> None:
    """emoji/换行：换行属 ASCII（1/4），emoji 属其余（1 个）。"""
    # "hi\n🙂" = 2 ASCII(→1) + 1 换行(ASCII) + 1 emoji(1) → ceil(3/4)=1 + 1 = 2
    assert estimate_tokens("hi\n🙂") == 2


# ---------- budget_for_history ----------

def test_budget_normal() -> None:
    """budget = max − est(system) − est(input) − 500。"""
    # system 全中文 100 字 → 100；input 全中文 50 字 → 50
    budget = budget_for_history(10_000, "系" * 100, "入" * 50)
    assert budget == 10_000 - 100 - 50 - 500


def test_budget_max_zero_means_unlimited() -> None:
    """max_context_chars=0 → 返回 None 表示不裁剪（§6.3）。"""
    assert budget_for_history(0, "x" * 100, "y" * 100) is None


def test_budget_can_be_negative() -> None:
    """负数防护：预算允许为负（system+input 已超限），由 trim_history 兜底。"""
    assert budget_for_history(1_000, "x" * 10_000, "") < 0


def test_budget_negative_max_treated_as_unlimited() -> None:
    """负数 max 按不裁剪处理（更安全的防御）。"""
    assert budget_for_history(-1, "x", "y") is None


# ---------- trim_history ----------

def _msg(content: str) -> dict:
    return {"role": "user", "content": content}


def test_trim_empty_history() -> None:
    assert trim_history([], 100) == []
    assert trim_history([], None) == []


def test_trim_none_budget_returns_copy() -> None:
    """budget=None（max=0 不裁剪）→ 原样返回副本。"""
    history = [_msg("你好")]
    out = trim_history(history, None)
    assert out == history
    assert out is not history  # 返回新列表，不修改入参


def test_trim_fits_budget_keeps_all() -> None:
    history = [_msg("aaaa"), _msg("你好")]  # 1 + 2 = 3 tokens
    out = trim_history(history, 3)
    assert out == history


def test_trim_drops_oldest_whole_messages() -> None:
    """整条丢弃粒度：只丢最旧，绝不截断半条。"""
    history = [_msg("a" * 4), _msg("b" * 4), _msg("c" * 4)]  # 各 1 token，共 3
    out = trim_history(history, 2)
    assert len(out) == 2
    assert out[0]["content"] == "b" * 4  # 最旧的 a 被整条丢弃
    assert out[1]["content"] == "c" * 4


def test_trim_just_over_budget() -> None:
    """刚好超限：丢最旧一条后满足。"""
    history = [_msg("aaaa"), _msg("bbbb")]  # 2 tokens
    out = trim_history(history, 1)          # 预算 1：丢最旧
    assert len(out) == 1
    assert out[0]["content"] == "bbbb"


def test_trim_zero_budget_keeps_only_last() -> None:
    """0 预算：只保留最后一条。"""
    history = [_msg("a"), _msg("b"), _msg("c")]
    assert trim_history(history, 0) == [history[-1]]


def test_trim_negative_budget_keeps_only_last() -> None:
    """负数预算：只保留最后一条（负数防护）。"""
    history = [_msg("a"), _msg("b"), _msg("c")]
    assert trim_history(history, -50) == [history[-1]]


def test_trim_last_message_over_budget_still_kept() -> None:
    """边界：只剩最后一条且仍超预算（正预算）→ 保底保留最后一条。"""
    history = [_msg("a" * 8), _msg("b" * 8)]  # 各 2 tokens
    out = trim_history(history, 1)            # 丢最旧后剩 1 条（2 tokens 仍超）
    assert out == [history[-1]]               # 仍超也保留最后一条


def test_trim_zero_budget_last_message_still_kept_even_over() -> None:
    """0 预算且最后一条本身超预算 → 仍保留最后一条（任务边界要求）。"""
    history = [_msg("x" * 100)]
    assert trim_history(history, 0) == history


def test_trim_does_not_mutate_input() -> None:
    history = [_msg("aaaa"), _msg("bbbb")]
    trim_history(history, 1)
    assert len(history) == 2  # 入参未被修改
