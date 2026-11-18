namespace MyAgent.App.Models;

/// <summary>消息角色（对应 §5 messages.role，仅 user / assistant）。</summary>
public enum MessageRole
{
    User,
    Assistant,
}

/// <summary>
/// 聊天消息（对应 §5 messages 表 + §4.3 GET /api/sessions/{id}/messages 返回形状）。
/// interrupted=true 表示该 AI 回复被手动停止：内容为已生成部分，界面尾部标注“（已停止）”，
/// 刷新/重启后仍可见（后端已落库 interrupted=1）。
/// 说明：§5 冻结的 messages 表没有世界书命中列，MatchedEntries 属于假数据阶段为演示
/// “命中提示持久显示”而附加的字段；真实后端下该信息由 meta 事件实时提供（§4.2），
/// 界面逻辑按“MatchedEntries 为空则不显示”编写，两种来源都能正确工作。
/// </summary>
public class ChatMessage
{
    /// <summary>自增 id，天然有序（假数据阶段同样使用自增 long，与后端语义一致）。</summary>
    public long Id { get; set; }

    /// <summary>所属会话 id（外键语义）。</summary>
    public string SessionId { get; set; } = "";

    /// <summary>消息角色：user / assistant。</summary>
    public MessageRole Role { get; set; }

    /// <summary>消息全文（interrupted 时为已生成部分）。</summary>
    public string Content { get; set; } = "";

    /// <summary>该 AI 回复是否被手动停止（0/1 → bool）。</summary>
    public bool Interrupted { get; set; }

    /// <summary>本轮生成命中的世界书条目（来自 meta 事件；见类注释）。</summary>
    public IReadOnlyList<MatchedEntry> MatchedEntries { get; set; } = Array.Empty<MatchedEntry>();

    /// <summary>创建时间（UTC ISO8601 语义，界面本地化显示）。</summary>
    public DateTimeOffset CreatedAt { get; set; }

    /// <summary>浅拷贝：服务层返回快照，避免调用方改动服务内部状态。</summary>
    public ChatMessage Clone() => new()
    {
        Id = Id,
        SessionId = SessionId,
        Role = Role,
        Content = Content,
        Interrupted = Interrupted,
        MatchedEntries = MatchedEntries.ToList(),
        CreatedAt = CreatedAt,
    };
}
