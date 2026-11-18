namespace MyAgent.App.Models;

/// <summary>
/// PATCH /api/sessions/{id} 的整表配置载荷（会话设置页保存，§4.3 / v0.6）。
/// 语义：null = 显式发送 null（v0.4①：max_tokens 表示清除配置；v0.6：
/// model_preset_id 表示清除预设选择；其余字段视为“未提供、不更新”）。
/// 设置页总是整表提交，每个字段都代表“用户当前意图”。
/// 模型 Base URL / API Key / 模型名 / 推理强度已移至全局模型预设（v0.6）。
/// </summary>
public sealed class SessionConfigPatch
{
    public string? Name { get; set; }
    public string? SystemPrompt { get; set; }
    public int? ScanRounds { get; set; }
    public bool? WorldbookEnabled { get; set; }
    public double? Temperature { get; set; }
    public double? TopP { get; set; }
    public int? MaxTokens { get; set; }

    /// <summary>所选全局模型预设 id（v0.6）：null = 清除选择（未选择）；字符串 = 选择该预设。</summary>
    public string? ModelPresetId { get; set; }
    public int? MaxContextChars { get; set; }
    public string? ProxyUrl { get; set; }
}
