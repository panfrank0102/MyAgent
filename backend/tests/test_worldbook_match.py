"""Step 5 测试：世界书匹配引擎（app/services/worldbook.py 纯函数）。

覆盖（《开发步骤分解.md》Step 5 完成标准）：
中文命中/不命中、英文大小写、多关键词、空关键词、多条排序、
同条目多词命中只出现一次、disabled 跳过、meta 序列化形状。
"""

from __future__ import annotations

from app.services.worldbook import _fold_ascii, _keywords_of, match_entries


def test_chinese_hit_and_miss() -> None:
    """中文关键词命中/不命中。"""
    entries = [
        {"id": "e1", "name": "角色", "keywords": "林月如\n李逍遥", "content": "……", "enabled": 1, "sort_order": 1},
        {"id": "e2", "name": "地点", "keywords": "蜀山", "content": "……", "enabled": 1, "sort_order": 2},
    ]
    text = "林月如和李逍遥去了蜀山"
    matched = match_entries(text, entries)
    assert len(matched) == 2
    assert matched[0]["entry"]["name"] == "角色"
    assert matched[0]["hit_keywords"] == ["林月如", "李逍遥"]
    assert matched[1]["entry"]["name"] == "地点"
    assert matched[1]["hit_keywords"] == ["蜀山"]

    # 不命中：无任何关键词出现
    assert match_entries("今天天气不错", entries) == []


def test_ascii_case_insensitive() -> None:
    """英文（ASCII）忽略大小写匹配。"""
    entries = [
        {"id": "e1", "name": "组织", "keywords": "Umbrella", "content": "……", "enabled": 1, "sort_order": 1},
    ]
    # 关键词大写、文本小写
    assert len(match_entries("the umbrella corp", entries)) == 1
    # 关键词小写、文本大写
    assert len(match_entries("THE UMBRELLA CORP", entries)) == 1
    # 大小写混合
    assert len(match_entries("uMbReLlA", entries)) == 1


def test_fold_ascii_only_folds_ascii() -> None:
    """_fold_ascii 仅折叠 ASCII 大写字母（契约："ASCII 双方 .lower() 比较"）。"""
    assert _fold_ascii("AbC 中文 İ") == "abc 中文 İ"  # İ(U+0130) 非 ASCII，不折叠


def test_keywords_one_per_line_and_empty() -> None:
    """keywords 按"一行一个"解析；空关键词/空行被清洗，全空则不参与匹配。"""
    entries = [
        {"id": "e1", "name": "A", "keywords": "  苹果\n\n香蕉  \n", "content": "……", "enabled": 1, "sort_order": 1},
        {"id": "e2", "name": "B", "keywords": "\n   \n", "content": "……", "enabled": 1, "sort_order": 2},
        {"id": "e3", "name": "C", "keywords": "", "content": "……", "enabled": 1, "sort_order": 3},
    ]
    text = "我喜欢吃苹果"
    matched = match_entries(text, entries)
    assert len(matched) == 1
    assert matched[0]["entry"]["name"] == "A"
    assert matched[0]["hit_keywords"] == ["苹果"]


def test_keywords_list_input() -> None:
    """兼容 keywords 为 list 的输入（防御性设计）。"""
    entries = [{"id": "e1", "name": "A", "keywords": [" 苹果 ", "", "香蕉"], "content": "……", "enabled": 1, "sort_order": 1}]
    matched = match_entries("苹果香蕉", entries)
    assert matched[0]["hit_keywords"] == ["苹果", "香蕉"]


def test_sort_by_sort_order_regardless_of_input_order() -> None:
    """输出按 sort_order 升序（不依赖输入顺序）。"""
    entries = [
        {"id": "e3", "name": "C", "keywords": "猫", "content": "……", "enabled": 1, "sort_order": 30},
        {"id": "e1", "name": "A", "keywords": "狗", "content": "……", "enabled": 1, "sort_order": 10},
        {"id": "e2", "name": "B", "keywords": "鸟", "content": "……", "enabled": 1, "sort_order": 20},
    ]
    names = [m["entry"]["name"] for m in match_entries("猫狗鸟", entries)]
    assert names == ["A", "B", "C"]


def test_same_entry_injected_once_with_all_hits() -> None:
    """同条目多关键词命中：只出现一次，但 hit_keywords 记录全部命中词。"""
    entries = [
        {"id": "e1", "name": "组织", "keywords": "Umbrella\n浣熊市\nT病毒", "content": "……", "enabled": 1, "sort_order": 1},
    ]
    matched = match_entries("Umbrella 在浣熊市投放 T病毒", entries)
    assert len(matched) == 1
    assert matched[0]["hit_keywords"] == ["Umbrella", "浣熊市", "T病毒"]


def test_disabled_entries_skipped() -> None:
    """enabled=0 的条目不参与匹配（§6.1 第 3 点）；缺 enabled 字段视为启用。"""
    entries = [
        {"id": "e1", "name": "关", "keywords": "苹果", "content": "……", "enabled": 0, "sort_order": 1},
        {"id": "e2", "name": "开", "keywords": "香蕉", "content": "……", "sort_order": 2},  # 缺 enabled
    ]
    matched = match_entries("苹果和香蕉", entries)
    assert len(matched) == 1
    assert matched[0]["entry"]["name"] == "开"


def test_meta_serialization_shape() -> None:
    """返回形状可直接投影为 meta 事件的 matched_entries（§4.2 示例）。"""
    entries = [{"id": "e1", "name": "角色", "keywords": "林月如", "content": "……", "enabled": 1, "sort_order": 1}]
    matched = match_entries("林月如来了", entries)
    meta = [{"name": item["entry"]["name"], "hit_keywords": item["hit_keywords"]} for item in matched]
    assert meta == [{"name": "角色", "hit_keywords": ["林月如"]}]


def test_empty_text_never_matches() -> None:
    """空文本永不命中。"""
    entries = [{"id": "e1", "name": "A", "keywords": "a", "content": "……", "enabled": 1, "sort_order": 1}]
    assert match_entries("", entries) == []
    assert match_entries(None, entries) == []  # type: ignore[arg-type]
