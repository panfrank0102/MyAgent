namespace MyAgent.App.Models;

/// <summary>
/// 世界书条目（对应 §5 worldbook_entries 表）。世界书每会话独立一套（§2 第 13 条）。
/// Keywords 一行一个（保存前 trim、去空行、去重）；可为空 = 不参与匹配。
/// 内容必填，空禁止保存。主窗口阶段仅为模型定义，编辑器窗口由 wpf-config 实现（Step 13）。
/// </summary>
public class WorldBookEntry
{
    /// <summary>条目唯一标识（后端为 UUID）。</summary>
    public string Id { get; set; } = Guid.NewGuid().ToString();

    /// <summary>所属会话 id（外键级联删除）。</summary>
    public string SessionId { get; set; } = "";

    /// <summary>条目名（注入时作为标题）。</summary>
    public string Name { get; set; } = "";

    /// <summary>触发关键词（纯子串匹配，ASCII 忽略大小写；一行一个）。</summary>
    public List<string> Keywords { get; set; } = new();

    /// <summary>条目内容（必填，空禁止保存）。</summary>
    public string Content { get; set; } = "";

    /// <summary>启用状态；禁用条目不参与匹配。</summary>
    public bool Enabled { get; set; } = true;

    /// <summary>排序权重：越小越靠前；命中后按此顺序注入（新条目取 max+1）。</summary>
    public int SortOrder { get; set; }
}
