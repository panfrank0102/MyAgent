using System.Globalization;
using System.Net.Http;
using System.Text.Json.Serialization;
using MyAgent.App.Models;

namespace MyAgent.App.Services;

/// <summary>
/// 真实后端服务（Step 14 集成实现）：组合 <see cref="ApiClient"/>，按
/// <see cref="IBackendService"/> 契约把界面层调用翻译为《需求与方案指导.md》§4.3 REST 请求
/// 与 §4.2 NDJSON 流式事件。
///
/// <para>要点：</para>
/// <list type="bullet">
/// <item>后端 JSON 为 snake_case、布尔以 JSON bool 输出（schemas.py 已把 DB 的 0/1 转 bool），
/// 本类用私有 DTO + 统一 snake_case 选项解析后映射到 PascalCase 模型；</item>
/// <item>REST 非 2xx → <see cref="BackendApiException"/>（Code/中文 Message 透传）；
/// 语义上“资源不存在”的方法按 FakeBackendService 约定返回 null/false/0，而不是抛异常；</item>
/// <item>“设置页整表保存”语义：除 api_key 为 null/空串不发送外，所有字段显式发送（含 null），
/// 与 v0.4① 的 base_url/model/max_tokens 的 null=清除 对齐；</item>
/// <item>流式 chat/regenerate：取消令牌直接透传给 ApiClient（界面“停止生成”= 断开请求）；</item>
/// <item>test-connection 约定不抛异常：成功/失败/后端不可达都返回 <see cref="TestConnectionResult"/>。</item>
/// </list>
/// </summary>
public sealed class RealApiBackendService : IBackendService
{
    private const string DefaultBaseUrl = "http://127.0.0.1:8765";

    private readonly ApiClient _api;

    public RealApiBackendService(string baseUrl)
    {
        _api = new ApiClient(string.IsNullOrWhiteSpace(baseUrl) ? DefaultBaseUrl : baseUrl);
    }

    // ------------------------------------------------------------------
    // 会话
    // ------------------------------------------------------------------

    /// <summary>GET /api/sessions：按 updated_at 倒序、含 message_count、api_key 打码。</summary>
    public async Task<IReadOnlyList<SessionInfo>> ListSessionsAsync(CancellationToken ct = default)
    {
        var items = await _api.GetAsync<List<ApiSession>>("api/sessions", ct).ConfigureAwait(false);
        return items.Select(ToSessionInfo).ToList();
    }

    /// <summary>POST /api/sessions：新建会话（空白名与 Fake 一致回退为“新会话”）。</summary>
    public async Task<SessionInfo> CreateSessionAsync(string name, CancellationToken ct = default)
    {
        var effectiveName = string.IsNullOrWhiteSpace(name) ? "新会话" : name.Trim();
        var body = new Dictionary<string, object?> { ["name"] = effectiveName };
        var created = await _api.PostAsync<ApiSession>("api/sessions", body, ct).ConfigureAwait(false);
        return ToSessionInfo(created);
    }

    /// <summary>
    /// PATCH /api/sessions/{id}：仅更新会话名（主窗口重命名快捷路径）。
    /// 空白新名与 Fake 一致“不更新名字，但仍返回当前会话快照”。
    /// </summary>
    public async Task<SessionInfo?> UpdateSessionAsync(string id, string newName, CancellationToken ct = default)
    {
        // 只发 name 字段：PATCH 其余字段不发送，避免把 null 当成“清除配置”。
        var body = new Dictionary<string, object?>();
        if (!string.IsNullOrWhiteSpace(newName))
        {
            body["name"] = newName.Trim();
        }

        try
        {
            var updated = await _api.PatchAsync<ApiSession>(SessionPath(id), body, ct).ConfigureAwait(false);
            return ToSessionInfo(updated);
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return null; // 会话不存在（与 Fake 语义一致）
        }
    }

    /// <summary>DELETE /api/sessions/{id}：200→true；404→false（不存在）；其余错误抛异常。</summary>
    public async Task<bool> DeleteSessionAsync(string id, CancellationToken ct = default)
    {
        try
        {
            var result = await _api.DeleteAsync<DeletedFlag>(SessionPath(id), ct).ConfigureAwait(false);
            return result.Deleted;
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return false; // 删除不存在的会话：与 Fake 一致返回 false
        }
    }

    // ------------------------------------------------------------------
    // 消息
    // ------------------------------------------------------------------

    /// <summary>GET /api/sessions/{id}/messages?limit=N：最新 N 条，按 id 正序（含 interrupted）。</summary>
    public async Task<IReadOnlyList<ChatMessage>> GetMessagesAsync(
        string sessionId, int limit = 200, CancellationToken ct = default)
    {
        var path = $"{SessionPath(sessionId)}/messages?limit={limit.ToString(CultureInfo.InvariantCulture)}";
        var items = await _api.GetAsync<List<ApiMessage>>(path, ct).ConfigureAwait(false);
        return items.Select(ToChatMessage).ToList();
    }

    /// <summary>DELETE /api/sessions/{id}/messages：清空历史，返回删除条数。</summary>
    public async Task<int> ClearMessagesAsync(string sessionId, CancellationToken ct = default)
    {
        var result = await _api
            .DeleteAsync<DeletedCount>($"{SessionPath(sessionId)}/messages", ct)
            .ConfigureAwait(false);
        return result.Deleted;
    }

    /// <summary>DELETE /api/sessions/{id}/messages/{msg_id}：删该条及其后，返回删除条数。</summary>
    public async Task<int> DeleteMessageAndAfterAsync(
        string sessionId, long messageId, CancellationToken ct = default)
    {
        var path = $"{SessionPath(sessionId)}/messages/{messageId.ToString(CultureInfo.InvariantCulture)}";
        try
        {
            var result = await _api.DeleteAsync<DeletedCount>(path, ct).ConfigureAwait(false);
            return result.Deleted;
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return 0; // 消息不存在：与 Fake 一致返回 0（后端 404 message_not_found）
        }
    }

    // ------------------------------------------------------------------
    // 流式生成（§4.2）
    // ------------------------------------------------------------------

    /// <summary>POST /api/sessions/{id}/chat：NDJSON 流式 meta→delta*→done|error。</summary>
    public IAsyncEnumerable<ChatEvent> SendMessageAsync(
        string sessionId, string content, CancellationToken ct = default)
    {
        var body = new ChatBody { Content = content };
        return _api.StreamEventsAsync(
            HttpMethod.Post, $"{SessionPath(sessionId)}/chat", body, ct);
    }

    /// <summary>POST /api/sessions/{id}/regenerate：协议同 chat，无请求体。</summary>
    public IAsyncEnumerable<ChatEvent> RegenerateAsync(string sessionId, CancellationToken ct = default)
    {
        return _api.StreamEventsAsync(
            HttpMethod.Post, $"{SessionPath(sessionId)}/regenerate", body: null, ct);
    }

    // ------------------------------------------------------------------
    // 会话配置（Step 12）
    // ------------------------------------------------------------------

    /// <summary>GET /api/sessions/{id}：会话详情；不存在返回 null（404）。</summary>
    public async Task<SessionInfo?> GetSessionAsync(string id, CancellationToken ct = default)
    {
        try
        {
            var session = await _api.GetAsync<ApiSession>(SessionPath(id), ct).ConfigureAwait(false);
            return ToSessionInfo(session);
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return null;
        }
    }

    /// <summary>
    /// PATCH /api/sessions/{id}：整表保存会话配置（v0.4 语义）。
    /// api_key 为 null/空串时不发送；其余字段全部显式发送（null 也发）——
    /// base_url/model/max_tokens 的 null = 清除配置，其余 null 后端按“未提供”忽略。
    /// 会话不存在返回 null；数值越界（422）等校验失败抛 <see cref="BackendApiException"/>。
    /// </summary>
    public async Task<SessionInfo?> UpdateSessionConfigAsync(
        string id, SessionConfigPatch patch, CancellationToken ct = default)
    {
        var body = new SessionConfigPatchBody
        {
            Name = patch.Name,
            SystemPrompt = patch.SystemPrompt,
            ScanRounds = patch.ScanRounds,
            WorldbookEnabled = patch.WorldbookEnabled,
            BaseUrl = patch.BaseUrl,
            // api_key 为 null/空串时不发送（§4.3：仅非空值更新；[JsonIgnore] 保证不序列化）
            ApiKey = string.IsNullOrWhiteSpace(patch.ApiKey) ? null : patch.ApiKey,
            Model = patch.Model,
            Temperature = patch.Temperature,
            TopP = patch.TopP,
            MaxTokens = patch.MaxTokens,
            MaxContextChars = patch.MaxContextChars,
            ProxyUrl = patch.ProxyUrl,
        };

        try
        {
            var updated = await _api
                .PatchAsync<ApiSession>(SessionPath(id), body, ct)
                .ConfigureAwait(false);
            return ToSessionInfo(updated);
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return null;
        }
    }

    // ------------------------------------------------------------------
    // 世界书（Step 13，每会话独立，§4.3）
    // ------------------------------------------------------------------

    /// <summary>GET /api/sessions/{sid}/worldbook：按 sort_order 升序。</summary>
    public async Task<IReadOnlyList<WorldBookEntry>> ListWorldbookAsync(
        string sessionId, CancellationToken ct = default)
    {
        var items = await _api
            .GetAsync<List<ApiWorldbookEntry>>(WorldbookPath(sessionId), ct)
            .ConfigureAwait(false);
        return items.Select(ToWorldbookEntry).ToList();
    }

    /// <summary>
    /// POST /api/sessions/{sid}/worldbook：新建条目。
    /// <para>sort_order 语义对齐后端/Fake：SortOrder &lt;= 0 视为“未指定”，发送 null，
    /// 由后端取本会话 max+1（首条为 1，与 Fake 一致）；SortOrder &gt; 0 按原值提交。</para>
    /// 内容为空由后端拒绝（422 validation_error），本方法原样抛 <see cref="BackendApiException"/>。
    /// </summary>
    public async Task<WorldBookEntry> CreateWorldbookEntryAsync(
        string sessionId, WorldBookEntry entry, CancellationToken ct = default)
    {
        var body = new WorldbookCreateBody
        {
            Name = entry.Name ?? "",
            Keywords = entry.Keywords?.ToList() ?? new List<string>(),
            Content = entry.Content ?? "",
            Enabled = entry.Enabled,
            SortOrder = entry.SortOrder > 0 ? entry.SortOrder : null,
        };
        var created = await _api
            .PostAsync<ApiWorldbookEntry>(WorldbookPath(sessionId), body, ct)
            .ConfigureAwait(false);
        return ToWorldbookEntry(created);
    }

    /// <summary>PATCH /api/sessions/{sid}/worldbook/{eid}：更新条目；不存在/跨会话返回 null（404）。</summary>
    public async Task<WorldBookEntry?> UpdateWorldbookEntryAsync(
        string sessionId, string entryId, WorldBookEntry entry, CancellationToken ct = default)
    {
        var body = new WorldbookUpdateBody
        {
            Name = entry.Name ?? "",
            Keywords = entry.Keywords?.ToList() ?? new List<string>(),
            Content = entry.Content ?? "",
            Enabled = entry.Enabled,
            // 更新路径与 Fake 对齐：sort_order 原值提交（不再走 max+1 缺省逻辑）
            SortOrder = entry.SortOrder,
        };
        try
        {
            var updated = await _api
                .PatchAsync<ApiWorldbookEntry>(WorldbookEntryPath(sessionId, entryId), body, ct)
                .ConfigureAwait(false);
            return ToWorldbookEntry(updated);
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return null; // 条目不存在 / 跨会话（会话隔离，后端 404）
        }
    }

    /// <summary>DELETE /api/sessions/{sid}/worldbook/{eid}：200→true；不存在/跨会话→false。</summary>
    public async Task<bool> DeleteWorldbookEntryAsync(
        string sessionId, string entryId, CancellationToken ct = default)
    {
        try
        {
            var result = await _api
                .DeleteAsync<DeletedFlag>(WorldbookEntryPath(sessionId, entryId), ct)
                .ConfigureAwait(false);
            return result.Deleted;
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return false;
        }
    }

    // ------------------------------------------------------------------
    // 连通性测试（Step 12）
    // ------------------------------------------------------------------

    /// <summary>
    /// POST /api/config/test-connection：失败也返回 HTTP 200 + {"error":{...}}（§4.3）；
    /// 本方法约定不抛异常——校验失败/上游失败/后端未连接都返回 Ok=false 与中文原因。
    /// </summary>
    public async Task<TestConnectionResult> TestConnectionAsync(
        TestConnectionRequest request, CancellationToken ct = default)
    {
        try
        {
            var response = await _api
                .PostAsync<ApiTestConnectionResponse>("api/config/test-connection", request, ct)
                .ConfigureAwait(false);

            if (response.Error is not null)
            {
                // 后端 200 + {"error":{"code","message"}}：上游/参数失败
                return new TestConnectionResult
                {
                    Ok = false,
                    ErrorCode = string.IsNullOrWhiteSpace(response.Error.Code) ? "internal" : response.Error.Code,
                    Message = string.IsNullOrWhiteSpace(response.Error.Message)
                        ? "连接测试失败"
                        : response.Error.Message,
                };
            }

            if (response.Ok == true)
            {
                return new TestConnectionResult
                {
                    Ok = true,
                    Message = string.IsNullOrWhiteSpace(response.Message)
                        ? "连接成功，模型可用"
                        : response.Message,
                };
            }

            return new TestConnectionResult
            {
                Ok = false,
                ErrorCode = "invalid_response",
                Message = "后端返回了无法识别的连接测试结果",
            };
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw; // 调用方主动取消（窗口关闭）：保持取消语义
        }
        catch (BackendApiException ex)
        {
            return new TestConnectionResult
            {
                Ok = false,
                ErrorCode = ex.Code,
                Message = ex.Message,
            };
        }
        catch (Exception)
        {
            // 兜底：网络异常/未知异常一律“后端未连接”（本方法不抛）
            return new TestConnectionResult
            {
                Ok = false,
                ErrorCode = "backend_unreachable",
                Message = "后端未连接或网络错误",
            };
        }
    }

    // ------------------------------------------------------------------
    // 路径拼接与模型映射
    // ------------------------------------------------------------------

    private static string SessionPath(string id) => "api/sessions/" + Uri.EscapeDataString(id);

    private static string WorldbookPath(string sessionId) => SessionPath(sessionId) + "/worldbook";

    private static string WorldbookEntryPath(string sessionId, string entryId)
        => WorldbookPath(sessionId) + "/" + Uri.EscapeDataString(entryId);

    private static SessionInfo ToSessionInfo(ApiSession dto) => new()
    {
        Id = dto.Id ?? "",
        Name = dto.Name ?? "",
        SystemPrompt = dto.SystemPrompt ?? "",
        ScanRounds = dto.ScanRounds,
        WorldbookEnabled = dto.WorldbookEnabled,
        // 后端 base_url / model 可为 null（未配置），C# 模型用空串表达“未配置”
        BaseUrl = dto.BaseUrl ?? "",
        MaskedApiKey = dto.ApiKey ?? "",
        Model = dto.Model ?? "",
        Temperature = dto.Temperature,
        TopP = dto.TopP,
        MaxTokens = dto.MaxTokens,
        MaxContextChars = dto.MaxContextChars,
        ProxyUrl = dto.ProxyUrl ?? "",
        CreatedAt = ParseTimestamp(dto.CreatedAt),
        UpdatedAt = ParseTimestamp(dto.UpdatedAt),
        MessageCount = dto.MessageCount,
    };

    private static ChatMessage ToChatMessage(ApiMessage dto) => new()
    {
        Id = dto.Id,
        SessionId = dto.SessionId ?? "",
        Role = string.Equals(dto.Role, "assistant", StringComparison.OrdinalIgnoreCase)
            ? MessageRole.Assistant
            : MessageRole.User,
        Content = dto.Content ?? "",
        Interrupted = dto.Interrupted,
        // 真实后端不回传世界书命中信息（仅 meta 事件实时传递，v0.4⑨ / §5 决策）
        MatchedEntries = Array.Empty<MatchedEntry>(),
        CreatedAt = ParseTimestamp(dto.CreatedAt),
    };

    private static WorldBookEntry ToWorldbookEntry(ApiWorldbookEntry dto) => new()
    {
        Id = dto.Id ?? "",
        SessionId = dto.SessionId ?? "",
        Name = dto.Name ?? "",
        Keywords = dto.Keywords?.ToList() ?? new List<string>(),
        Content = dto.Content ?? "",
        Enabled = dto.Enabled,
        SortOrder = dto.SortOrder,
    };

    /// <summary>后端时间为 UTC ISO8601 字符串（如 2026-11-18T04:30:15.123456Z）→ DateTimeOffset。</summary>
    private static DateTimeOffset ParseTimestamp(string? value)
        => DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed)
            ? parsed
            : DateTimeOffset.MinValue;

    // ------------------------------------------------------------------
    // 后端 JSON DTO（snake_case 由 ApiClient.JsonOptions 统一处理）
    // ------------------------------------------------------------------

    /// <summary>GET / POST / PATCH 会话响应（SessionOut / SessionListOut）。</summary>
    private sealed class ApiSession
    {
        public string? Id { get; set; }
        public string? Name { get; set; }
        public string? SystemPrompt { get; set; }
        public int ScanRounds { get; set; }
        public bool WorldbookEnabled { get; set; }
        public string? BaseUrl { get; set; }
        public string? ApiKey { get; set; }
        public string? Model { get; set; }
        public double Temperature { get; set; }
        public double TopP { get; set; }
        public int? MaxTokens { get; set; }
        public int MaxContextChars { get; set; }
        public string? ProxyUrl { get; set; }
        public string? CreatedAt { get; set; }
        public string? UpdatedAt { get; set; }
        public int MessageCount { get; set; }
    }

    /// <summary>PATCH /api/sessions/{id} 整表配置载荷。</summary>
    private sealed class SessionConfigPatchBody
    {
        public string? Name { get; set; }
        public string? SystemPrompt { get; set; }
        public int? ScanRounds { get; set; }
        public bool? WorldbookEnabled { get; set; }
        public string? BaseUrl { get; set; }

        /// <summary>api_key 为 null/空串时不发送：仅在非空时序列化该字段。</summary>
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? ApiKey { get; set; }

        public string? Model { get; set; }
        public double? Temperature { get; set; }
        public double? TopP { get; set; }
        public int? MaxTokens { get; set; }
        public int? MaxContextChars { get; set; }
        public string? ProxyUrl { get; set; }
    }

    /// <summary>GET /api/sessions/{id}/messages 响应项（MessageOut）。</summary>
    private sealed class ApiMessage
    {
        public long Id { get; set; }
        public string? SessionId { get; set; }
        public string? Role { get; set; }
        public string? Content { get; set; }
        public bool Interrupted { get; set; }
        public string? CreatedAt { get; set; }
    }

    /// <summary>世界书条目响应（WorldbookEntryOut；keywords 后端为字符串数组）。</summary>
    private sealed class ApiWorldbookEntry
    {
        public string? Id { get; set; }
        public string? SessionId { get; set; }
        public string? Name { get; set; }
        public List<string>? Keywords { get; set; }
        public string? Content { get; set; }
        public bool Enabled { get; set; }
        public int SortOrder { get; set; }
    }

    /// <summary>POST 世界书条目请求体；sort_order 为 null 时后端取 max+1。</summary>
    private sealed class WorldbookCreateBody
    {
        public string Name { get; set; } = "";
        public List<string> Keywords { get; set; } = new();
        public string Content { get; set; } = "";
        public bool Enabled { get; set; } = true;
        public int? SortOrder { get; set; }
    }

    /// <summary>PATCH 世界书条目请求体（字段全部携带，与 Fake 整表更新语义一致）。</summary>
    private sealed class WorldbookUpdateBody
    {
        public string Name { get; set; } = "";
        public List<string> Keywords { get; set; } = new();
        public string Content { get; set; } = "";
        public bool Enabled { get; set; } = true;
        public int SortOrder { get; set; }
    }

    /// <summary>POST /api/sessions/{id}/chat 请求体。</summary>
    private sealed class ChatBody
    {
        public string Content { get; set; } = "";
    }

    /// <summary>DELETE 类端点响应 {"deleted": true}（会话/世界书删除）。</summary>
    private sealed class DeletedFlag
    {
        public bool Deleted { get; set; }
    }

    /// <summary>DELETE 类端点响应 {"deleted": N}（清空历史/删消息）。</summary>
    private sealed class DeletedCount
    {
        public int Deleted { get; set; }
    }

    /// <summary>POST /api/config/test-connection 响应：成功 {"ok":true,...} 或失败 {"error":{...}}（均 HTTP 200）。</summary>
    private sealed class ApiTestConnectionResponse
    {
        public bool? Ok { get; set; }
        public string? Message { get; set; }
        public string? Model { get; set; }
        public ApiErrorBody? Error { get; set; }
    }

    /// <summary>统一错误体 {"error":{"code","message"}}。</summary>
    private sealed class ApiErrorBody
    {
        public string? Code { get; set; }
        public string? Message { get; set; }
    }
}
