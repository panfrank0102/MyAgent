namespace MyAgent.App.Models;

/// <summary>
/// 全局模型预设（v0.6，对应 §5 model_presets 表 + §4.3 /api/model-presets）。
/// 模型 Base URL / API Key / 模型名 / 推理强度统一存放在预设中；会话通过
/// <see cref="SessionInfo.ModelPresetId"/> 引用，可在会话设置中随时切换预设。
/// api_key 接口仅回显打码值（MaskedApiKey）；用户新输入的明文只用于创建/更新（ApiKeyInput）。
/// </summary>
public sealed class ModelPreset
{
    /// <summary>预设唯一标识（后端为 UUID）。</summary>
    public string Id { get; set; } = Guid.NewGuid().ToString();

    /// <summary>预设名（会话设置中显示；非空，允许重名）。</summary>
    public string Name { get; set; } = "";

    /// <summary>模型 Base URL（OpenAI 兼容接口）。</summary>
    public string BaseUrl { get; set; } = "";

    /// <summary>打码后的 API Key（列表/详情接口仅回显打码值，§5.1 mask_key）。</summary>
    public string MaskedApiKey { get; set; } = "";

    /// <summary>
    /// 用户新输入的明文 API Key：仅创建/更新时发送；null/空 = 不更新（沿用已存值）。
    /// 显示/编辑用 MaskedApiKey，不要把打码值当明文提交。
    /// </summary>
    public string ApiKeyInput { get; set; } = "";

    /// <summary>模型名（如 deepseek-chat）。</summary>
    public string Model { get; set; } = "";

    /// <summary>推理强度：空串 = 不设置（不向上游发送）；可选 low / medium / high。</summary>
    public string ReasoningEffort { get; set; } = "";

    /// <summary>创建时间（UTC ISO8601 语义，界面负责本地化显示）。</summary>
    public DateTimeOffset CreatedAt { get; set; }

    /// <summary>更新时间。</summary>
    public DateTimeOffset UpdatedAt { get; set; }

    /// <summary>浅拷贝：服务层返回快照，避免调用方改动服务内部状态。</summary>
    public ModelPreset Clone() => new()
    {
        Id = Id,
        Name = Name,
        BaseUrl = BaseUrl,
        MaskedApiKey = MaskedApiKey,
        ApiKeyInput = ApiKeyInput,
        Model = Model,
        ReasoningEffort = ReasoningEffort,
        CreatedAt = CreatedAt,
        UpdatedAt = UpdatedAt,
    };
}
