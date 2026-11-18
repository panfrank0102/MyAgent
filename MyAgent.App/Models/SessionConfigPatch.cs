namespace MyAgent.App.Models;

/// <summary>
/// PATCH /api/sessions/{id} 的整表配置载荷（Step 12 会话设置页保存，§4.3 / v0.4）。
/// 语义：null = 显式发送 null（v0.4①：base_url / model / max_tokens 表示清除配置，
/// 其余字段视为“未提供、不更新”）；ApiKey 为 null 或空串时不发送（§4.3：仅非空值更新）。
/// 设置页总是整表提交，每个字段都代表“用户当前意图”。
/// </summary>
public sealed class SessionConfigPatch
{
    public string? Name { get; set; }
    public string? SystemPrompt { get; set; }
    public int? ScanRounds { get; set; }
    public bool? WorldbookEnabled { get; set; }
    public string? BaseUrl { get; set; }
    public string? ApiKey { get; set; }
    public string? Model { get; set; }
    public double? Temperature { get; set; }
    public double? TopP { get; set; }
    public int? MaxTokens { get; set; }
    public int? MaxContextChars { get; set; }
    public string? ProxyUrl { get; set; }
}
