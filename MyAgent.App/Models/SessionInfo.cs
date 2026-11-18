namespace MyAgent.App.Models;

/// <summary>
/// 会话信息（对应《需求与方案指导.md》§5 sessions 表 + §4.3 GET /api/sessions 返回形状）。
/// v0.6：会话不再持有模型配置（Base URL / API Key / 模型名 / 推理强度），
/// 改为通过 <see cref="ModelPresetId"/> 引用全局模型预设（见 ModelPreset）。
/// 假数据阶段由 FakeBackendService 填充；真实模式由 RealApiBackendService 解析后端 JSON 得到同一模型。
/// </summary>
public class SessionInfo
{
    /// <summary>会话唯一标识（后端为 UUID）。</summary>
    public string Id { get; set; } = Guid.NewGuid().ToString();

    /// <summary>会话名（默认“新会话”）。</summary>
    public string Name { get; set; } = "新会话";

    /// <summary>前置系统提示词（多行文本，默认空）。</summary>
    public string SystemPrompt { get; set; } = "";

    /// <summary>世界书扫描最近 N 轮，默认 4，最小 0（只扫当前输入）。</summary>
    public int ScanRounds { get; set; } = 4;

    /// <summary>世界书开关（默认开启）。</summary>
    public bool WorldbookEnabled { get; set; } = true;

    /// <summary>采样温度，默认 1.0。</summary>
    public double Temperature { get; set; } = 1.0;

    /// <summary>核采样 top_p，默认 1.0。</summary>
    public double TopP { get; set; } = 1.0;

    /// <summary>max_tokens，null = 上游默认。</summary>
    public int? MaxTokens { get; set; }

    /// <summary>所选全局模型预设 id（v0.6）：空串 = 未选择；聊天需先选择预设。</summary>
    public string ModelPresetId { get; set; } = "";

    /// <summary>上下文总预算（字符），默认 80000；0 = 不裁剪（§6.3）。</summary>
    public int MaxContextChars { get; set; } = 80000;

    /// <summary>每会话 HTTP 代理地址（空 = 直连；仅支持 http/https，§2 第 20 条）。</summary>
    public string ProxyUrl { get; set; } = "";

    /// <summary>创建时间（UTC ISO8601 语义，界面负责本地化显示）。</summary>
    public DateTimeOffset CreatedAt { get; set; }

    /// <summary>更新时间：列表按此字段倒序排列（§4.3）。</summary>
    public DateTimeOffset UpdatedAt { get; set; }

    /// <summary>消息数（来自列表接口的 message_count 字段）。</summary>
    public int MessageCount { get; set; }

    /// <summary>浅拷贝：服务层返回快照，避免调用方改动服务内部状态。</summary>
    public SessionInfo Clone() => new()
    {
        Id = Id,
        Name = Name,
        SystemPrompt = SystemPrompt,
        ScanRounds = ScanRounds,
        WorldbookEnabled = WorldbookEnabled,
        Temperature = Temperature,
        TopP = TopP,
        MaxTokens = MaxTokens,
        ModelPresetId = ModelPresetId,
        MaxContextChars = MaxContextChars,
        ProxyUrl = ProxyUrl,
        CreatedAt = CreatedAt,
        UpdatedAt = UpdatedAt,
        MessageCount = MessageCount,
    };
}
