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
/// <item>“设置页整表保存”语义：所有字段显式发送（含 null）；v0.6 起会话移除旧模型字段
/// （base_url / api_key / model / reasoning_effort 后端不再返回/接受），改为 model_preset_id
/// （null = 清除预设选择）；模型预设的 api_key 为 null/空串时不发送（= 不更新已存 Key）；</item>
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
    // 全局模型预设（v0.6，全局设置 → 模型预设，§4.3）
    // ------------------------------------------------------------------

    /// <summary>GET /api/model-presets：按 created_at 升序，api_key 打码（后端仅回显打码值）。</summary>
    public async Task<IReadOnlyList<ModelPreset>> ListModelPresetsAsync(CancellationToken ct = default)
    {
        var items = await _api
            .GetAsync<List<ApiModelPreset>>(ModelPresetsPath(), ct)
            .ConfigureAwait(false);
        return items.Select(ToModelPreset).ToList();
    }

    /// <summary>
    /// POST /api/model-presets：新建预设（§4.3：name 非空；api_key 明文提交，返回打码快照）。
    /// 校验失败（空名/非法 reasoning_effort）由后端返回 422/400，本方法抛 <see cref="BackendApiException"/>。
    /// </summary>
    public async Task<ModelPreset> CreateModelPresetAsync(ModelPreset preset, CancellationToken ct = default)
    {
        var body = new ModelPresetCreateBody
        {
            Name = preset.Name ?? "",
            BaseUrl = preset.BaseUrl ?? "",
            // 新建时 api_key 允许为空串（尚未填写 Key 的预设也可先保存）
            ApiKey = preset.ApiKeyInput ?? "",
            Model = preset.Model ?? "",
            // 推理强度：空 = 不设置，显式发送 null（§5：NULL = 不设置/不向上游发送）
            ReasoningEffort = string.IsNullOrEmpty(preset.ReasoningEffort) ? null : preset.ReasoningEffort,
        };
        var created = await _api
            .PostAsync<ApiModelPreset>(ModelPresetsPath(), body, ct)
            .ConfigureAwait(false);
        return ToModelPreset(created);
    }

    /// <summary>
    /// PATCH /api/model-presets/{id}：整表更新预设（§4.3：api_key 为 null/空串不更新已存 Key）。
    /// 预设不存在返回 null（404）；name 为空等校验失败抛 <see cref="BackendApiException"/>。
    /// </summary>
    public async Task<ModelPreset?> UpdateModelPresetAsync(string id, ModelPreset preset, CancellationToken ct = default)
    {
        var body = new ModelPresetUpdateBody
        {
            Name = preset.Name ?? "",
            BaseUrl = preset.BaseUrl ?? "",
            // api_key 为空时不发送该字段（[JsonIgnore] WhenWritingNull），沿用服务端已存 Key
            ApiKey = string.IsNullOrWhiteSpace(preset.ApiKeyInput) ? null : preset.ApiKeyInput,
            Model = preset.Model ?? "",
            // 推理强度 null 必须显式发送（= 清除/不设置），因此该属性不加 JsonIgnore
            ReasoningEffort = string.IsNullOrEmpty(preset.ReasoningEffort) ? null : preset.ReasoningEffort,
        };
        try
        {
            var updated = await _api
                .PatchAsync<ApiModelPreset>(ModelPresetPath(id), body, ct)
                .ConfigureAwait(false);
            return ToModelPreset(updated);
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return null; // 预设不存在（与 Fake 语义一致）
        }
    }

    /// <summary>DELETE /api/model-presets/{id}：{"deleted":true}；不存在（404）→ false（与 Fake 一致）。</summary>
    public async Task<bool> DeleteModelPresetAsync(string id, CancellationToken ct = default)
    {
        try
        {
            var result = await _api
                .DeleteAsync<DeletedFlag>(ModelPresetPath(id), ct)
                .ConfigureAwait(false);
            return result.Deleted;
        }
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return false;
        }
    }

    /// <summary>
    /// POST /api/model-presets/{id}/test-connection：用服务端保存的 Key 测试连通性（§4.3）。
    /// 本方法约定不抛异常：200+{"ok":true} / 200+{"error":{...}} / 404 / 网络失败都返回
    /// <see cref="TestConnectionResult"/>（失败时 Ok=false，404 的 ErrorCode=not_found）。
    /// </summary>
    public async Task<TestConnectionResult> TestModelPresetAsync(string id, CancellationToken ct = default)
    {
        try
        {
            var response = await _api
                .PostAsync<ApiTestConnectionResponse>(ModelPresetTestPath(id), body: null, ct)
                .ConfigureAwait(false);

            if (response.Error is not null)
            {
                // 200 + {"error":{"code","message"}}：上游/参数失败（§4.3：失败也返回 HTTP 200）
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
        catch (BackendApiException ex) when (ex.StatusCode == 404)
        {
            return new TestConnectionResult
            {
                Ok = false,
                ErrorCode = "not_found",
                Message = string.IsNullOrWhiteSpace(ex.Message) ? "所选模型预设不存在或已被删除" : ex.Message,
            };
        }
        catch (BackendApiException ex)
        {
            // 网络层错误（backend_unreachable / timeout）与其它 HTTP 错误：返回失败，不抛异常
            return new TestConnectionResult
            {
                Ok = false,
                ErrorCode = ex.Code,
                Message = ex.Message,
            };
        }
        catch (Exception)
        {
            // 兜底：未知异常一律按“后端未连接”（本方法不抛）
            return new TestConnectionResult
            {
                Ok = false,
                ErrorCode = "backend_unreachable",
                Message = "后端未连接或网络错误",
            };
        }
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
    /// PATCH /api/sessions/{id}：整表保存会话配置（v0.4① / v0.6③ 语义）。
    /// 除已废弃的旧模型字段外，所有字段显式发送（null 也发）：max_tokens 的 null = 清除，
    /// model_preset_id 的 null = 清除预设选择（未选择状态），其余 null 后端按“未提供”忽略。
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
            Temperature = patch.Temperature,
            TopP = patch.TopP,
            MaxTokens = patch.MaxTokens,
            // v0.6③：null 必须显式序列化（= 清除预设选择），因此该属性不加 JsonIgnore
            ModelPresetId = patch.ModelPresetId,
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

    private static string ModelPresetsPath() => "api/model-presets";

    private static string ModelPresetPath(string id) => ModelPresetsPath() + "/" + Uri.EscapeDataString(id);

    private static string ModelPresetTestPath(string id) => ModelPresetPath(id) + "/test-connection";

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
        Temperature = dto.Temperature,
        TopP = dto.TopP,
        MaxTokens = dto.MaxTokens,
        // v0.6：会话不再持有 Base URL / API Key / 模型名 / 推理强度（后端不再返回这些字段）
        MaxContextChars = dto.MaxContextChars,
        ProxyUrl = dto.ProxyUrl ?? "",
        // v0.6③：后端 NULL = 未选择预设，C# 模型用空串表达“未选择”
        ModelPresetId = dto.ModelPresetId ?? "",
        CreatedAt = ParseTimestamp(dto.CreatedAt),
        UpdatedAt = ParseTimestamp(dto.UpdatedAt),
        MessageCount = dto.MessageCount,
    };

    /// <summary>GET / POST / PATCH 模型预设响应（ModelPresetOut；api_key 为打码值）。</summary>
    private static ModelPreset ToModelPreset(ApiModelPreset dto) => new()
    {
        Id = dto.Id ?? "",
        Name = dto.Name ?? "",
        BaseUrl = dto.BaseUrl ?? "",
        // 接口仅回显打码值（§5.1 mask_key）；明文只由调用方在创建/更新时提供
        MaskedApiKey = dto.ApiKey ?? "",
        ApiKeyInput = "",
        Model = dto.Model ?? "",
        // 后端 NULL = 不设置，C# 模型用空串表达“不设置/不向上游发送”
        ReasoningEffort = dto.ReasoningEffort ?? "",
        CreatedAt = ParseTimestamp(dto.CreatedAt),
        UpdatedAt = ParseTimestamp(dto.UpdatedAt),
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

    /// <summary>GET / POST / PATCH 会话响应（SessionOut / SessionListOut；v0.6 不含旧模型字段）。</summary>
    private sealed class ApiSession
    {
        public string? Id { get; set; }
        public string? Name { get; set; }
        public string? SystemPrompt { get; set; }
        public int ScanRounds { get; set; }
        public bool WorldbookEnabled { get; set; }
        public double Temperature { get; set; }
        public double TopP { get; set; }
        public int? MaxTokens { get; set; }
        public string? ModelPresetId { get; set; }
        public int MaxContextChars { get; set; }
        public string? ProxyUrl { get; set; }
        public string? CreatedAt { get; set; }
        public string? UpdatedAt { get; set; }
        public int MessageCount { get; set; }
    }

    /// <summary>PATCH /api/sessions/{id} 整表配置载荷（v0.6：旧模型字段已移除，改用 model_preset_id）。</summary>
    private sealed class SessionConfigPatchBody
    {
        public string? Name { get; set; }
        public string? SystemPrompt { get; set; }
        public int? ScanRounds { get; set; }
        public bool? WorldbookEnabled { get; set; }
        public double? Temperature { get; set; }
        public double? TopP { get; set; }
        public int? MaxTokens { get; set; }

        /// <summary>
        /// 所选全局模型预设 id（v0.6③）：null 必须显式发送（= 清除选择，未选择状态），
        /// 因此这里<b>不能</b>加 JsonIgnore；非 null 时原样发送该 id。
        /// </summary>
        public string? ModelPresetId { get; set; }

        public int? MaxContextChars { get; set; }
        public string? ProxyUrl { get; set; }
    }

    /// <summary>GET / POST / PATCH /api/model-presets 响应项（ModelPresetOut；api_key 打码）。</summary>
    private sealed class ApiModelPreset
    {
        public string? Id { get; set; }
        public string? Name { get; set; }
        public string? BaseUrl { get; set; }
        public string? ApiKey { get; set; }
        public string? Model { get; set; }
        public string? ReasoningEffort { get; set; }
        public string? CreatedAt { get; set; }
        public string? UpdatedAt { get; set; }
    }

    /// <summary>POST /api/model-presets 请求体（§4.3：name/base_url/api_key/model/reasoning_effort）。</summary>
    private sealed class ModelPresetCreateBody
    {
        public string Name { get; set; } = "";
        public string BaseUrl { get; set; } = "";
        public string ApiKey { get; set; } = "";
        public string Model { get; set; } = "";

        /// <summary>推理强度：null = 不设置（后端存 NULL）。</summary>
        public string? ReasoningEffort { get; set; }
    }

    /// <summary>PATCH /api/model-presets/{id} 请求体（api_key 为空时不发送，不更新已存 Key）。</summary>
    private sealed class ModelPresetUpdateBody
    {
        public string Name { get; set; } = "";
        public string BaseUrl { get; set; } = "";

        /// <summary>api_key 为 null/空串时不发送该字段（§4.3：仅非空值更新）。</summary>
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? ApiKey { get; set; }

        public string Model { get; set; } = "";

        /// <summary>null 必须显式发送（= 清除推理强度，不向上游发送），因此不加 JsonIgnore。</summary>
        public string? ReasoningEffort { get; set; }
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
