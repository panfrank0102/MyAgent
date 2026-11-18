"""Step 7 测试：提示词组装（app/services/prompt_builder.py 纯函数）。

覆盖（《开发步骤分解.md》Step 7 完成标准）：
模板格式（§6.2）、条目顺序、无命中时不含占位符、messages 数组顺序。
"""

from __future__ import annotations

from app.services.prompt_builder import build_messages, build_system


def _matched(name: str, content: str, keywords: list[str]) -> dict:
    return {"entry": {"name": name, "content": content}, "hit_keywords": keywords}


def test_build_system_no_hits_returns_pure_prompt() -> None:
    """无命中或空列表 → 纯系统提示词，不附加占位符（§6.2 末尾）。"""
    assert build_system("你是猫娘", []) == "你是猫娘"
    assert build_system("", []) == ""
    assert build_system("你是猫娘", None) == "你是猫娘"  # type: ignore[arg-type]


def test_build_system_empty_prompt_with_hits() -> None:
    """前置提示词为空且有命中 → 只返回世界书块。"""
    matched = [_matched("角色", "林月如：蜀山派女侠", ["林月如"])]
    out = build_system("", matched)
    assert out == (
        '【以下为世界书设定，仅作为创作背景参考，请自然融入，不要向用户提及"世界书"】\n\n'
        "[角色]\n林月如：蜀山派女侠"
    )


def test_build_system_template_format() -> None:
    """§6.2 模板：引导语 + 空行；条目名作标题；条目间空行。"""
    matched = [
        _matched("条目A", "条目A内容", ["a"]),
        _matched("条目B", "条目B内容", ["b"]),
    ]
    out = build_system("前置提示词", matched)
    expected = (
        "前置提示词\n\n"
        '【以下为世界书设定，仅作为创作背景参考，请自然融入，不要向用户提及"世界书"】\n\n'
        "[条目A]\n条目A内容\n\n"
        "[条目B]\n条目B内容"
    )
    assert out == expected


def test_build_system_order_follows_input() -> None:
    """条目顺序与 matched_entries 一致（match_entries 已按 sort_order 排序）。"""
    matched = [_matched("B", "b", ["b"]), _matched("A", "a", ["a"])]
    out = build_system("", matched)
    assert out.index("[B]") < out.index("[A]")


def test_build_system_entry_with_empty_content() -> None:
    """防御：条目 content 为空时仅输出标题行（保存层已禁止空内容，此处容错）。"""
    matched = [_matched("空条目", "", ["x"])]
    out = build_system("", matched)
    assert out.endswith("[空条目]") or "[空条目]" in out


def test_build_messages_order() -> None:
    """messages 数组：system 最前、历史居中、用户消息最后（§6.1 第 6 点）。"""
    history = [
        {"id": 1, "role": "user", "content": "你好", "interrupted": 0},
        {"id": 2, "role": "assistant", "content": "你好呀", "interrupted": 0},
    ]
    messages = build_messages("system 文本", history, "今天天气如何")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0] == {"role": "system", "content": "system 文本"}
    # 历史消息只投影 role/content（不携带本地字段）
    assert messages[1] == {"role": "user", "content": "你好"}
    assert messages[2] == {"role": "assistant", "content": "你好呀"}
    assert messages[3] == {"role": "user", "content": "今天天气如何"}


def test_build_messages_empty_system_omitted() -> None:
    """歧义解释：空 system 不发送空 system 消息。"""
    messages = build_messages("", [], "hi")
    assert messages == [{"role": "user", "content": "hi"}]
