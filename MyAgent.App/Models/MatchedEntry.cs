namespace MyAgent.App.Models;

/// <summary>
/// 世界书命中条目（对应 §4.2 meta 事件 matched_entries 数组的元素）：
/// 同一轮生成中同一条目只出现一次；HitKeywords 记录该条目实际命中的关键词。
/// 界面据此在 AI 回复下方渲染小字“世界书命中：条目A（关键词：xx）…”。
/// </summary>
public sealed class MatchedEntry
{
    /// <summary>条目名（注入时作为标题）。</summary>
    public string Name { get; }

    /// <summary>实际命中的关键词列表（可能多个）。</summary>
    public IReadOnlyList<string> HitKeywords { get; }

    public MatchedEntry(string name, IEnumerable<string> hitKeywords)
    {
        Name = name;
        HitKeywords = hitKeywords.ToList();
    }
}
