using System.IO;
using System.Net.Http;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using MyAgent.App.Models;

namespace MyAgent.App.Services;

/// <summary>
/// 后端 API 调用失败（REST 非 2xx / NDJSON 流式接口返回 JSON 错误 / 后端不可达）。
///
/// <para><see cref="Code"/> 对齐《需求与方案指导.md》§4.3 的统一错误格式
/// <c>{"error":{"code","message"}}</c>（not_found / bad_request / generation_in_progress /
/// validation_error / auth_failed / timeout / backend_unreachable / ...）；
/// <see cref="Message"/> 为可直接展示给用户的中文提示（后端返回的中文 message 原样透传）。</para>
///
/// <para>约定：ApiClient 的所有 REST 方法在非 2xx 时抛本异常；连接失败/超时也包装成本异常
/// （code=backend_unreachable / timeout），让上层只处理一种异常类型。调用方主动取消
/// （CancellationToken）时保持标准 <see cref="OperationCanceledException"/> 语义，不包装。</para>
/// </summary>
public sealed class BackendApiException : Exception
{
    /// <summary>机器可读错误码（与后端 §4.3 错误码一致；网络层错误为 backend_unreachable / timeout）。</summary>
    public string Code { get; }

    /// <summary>HTTP 状态码；网络层错误（后端未启动等）为 null。</summary>
    public int? StatusCode { get; }

    public BackendApiException(
        string code,
        string message,
        int? statusCode = null,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        StatusCode = statusCode;
    }
}

/// <summary>
/// 后端 HTTP 客户端（Step 14）：REST JSON + NDJSON 流式解析的统一入口。
///
/// <para>职责：</para>
/// <list type="number">
/// <item>HttpClient 绑定 baseUrl（默认 http://127.0.0.1:8765），所有请求走相对路径；</item>
/// <item>统一 JSON 选项：**snake_case 命名策略 + 大小写不敏感**——后端字段为 snake_case
/// （如 message_count / api_key），C# 模型/DTO 为 PascalCase；</item>
/// <item>GET/POST/PATCH/DELETE 通用 JSON 方法，非 2xx 统一解析 <c>{"error":{...}}</c>
/// 抛 <see cref="BackendApiException"/>（解析失败用状态码兜底中文文案）；</item>
/// <item>NDJSON 流式方法（§4.2）：POST + ResponseHeadersRead，逐行解析
/// <c>{"type":"meta|delta|done|error"}</c> 为 <see cref="ChatEvent"/>；未知事件类型忽略（向前兼容）；
/// 响应不是 NDJSON（如 404/409 的 JSON 错误体）时读 body 抛异常；</item>
/// <item>取消 = 释放请求/响应：异步迭代器被 Dispose 或 CancellationToken 触发时关闭 HTTP 连接，
/// 后端据此感知客户端断开（停止生成 → interrupted=1 落库）。</item>
/// </list>
/// </summary>
public sealed class ApiClient
{
    /// <summary>§4.2 冻结的流式响应 Content-Type。</summary>
    public const string NdjsonMediaType = "application/x-ndjson";

    /// <summary>前后端统一 JSON 选项：snake_case 命名 + 大小写不敏感（后端 snake_case，模型 PascalCase）。</summary>
    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    private readonly HttpClient _http;

    /// <summary>规范化后的 Base URL（无尾斜杠）。</summary>
    public string BaseUrl { get; }

    public ApiClient(string baseUrl)
    {
        BaseUrl = string.IsNullOrWhiteSpace(baseUrl)
            ? "http://127.0.0.1:8765"
            : baseUrl.Trim().TrimEnd('/');
        _http = new HttpClient
        {
            BaseAddress = new Uri(BaseUrl + "/"),
        };
    }

    // ------------------------------------------------------------------
    // REST 通用方法（非 2xx 抛 BackendApiException）
    // ------------------------------------------------------------------

    public async Task<T> GetAsync<T>(string path, CancellationToken ct = default)
        where T : class
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, path);
        return await SendJsonAsync<T>(request, ct).ConfigureAwait(false);
    }

    public async Task<T> PostAsync<T>(string path, object? body, CancellationToken ct = default)
        where T : class
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, path);
        if (body is not null)
        {
            request.Content = CreateJsonContent(body);
        }
        return await SendJsonAsync<T>(request, ct).ConfigureAwait(false);
    }

    public async Task<T> PatchAsync<T>(string path, object? body, CancellationToken ct = default)
        where T : class
    {
        using var request = new HttpRequestMessage(HttpMethod.Patch, path);
        if (body is not null)
        {
            request.Content = CreateJsonContent(body);
        }
        return await SendJsonAsync<T>(request, ct).ConfigureAwait(false);
    }

    public async Task<T> DeleteAsync<T>(string path, CancellationToken ct = default)
        where T : class
    {
        using var request = new HttpRequestMessage(HttpMethod.Delete, path);
        return await SendJsonAsync<T>(request, ct).ConfigureAwait(false);
    }

    // ------------------------------------------------------------------
    // NDJSON 流式方法（§4.2）
    // ------------------------------------------------------------------

    /// <summary>
    /// POST 一个流式端点并逐行解析 NDJSON 事件（meta → delta* → done|error）。
    ///
    /// <para>取消语义：调用方取消 <paramref name="ct"/>、或提前 Dispose 枚举器（如界面“停止生成”
    /// 后 break），迭代器内的 using 会释放 HttpResponseMessage → 关闭 HTTP 连接；
    /// 后端感知断开后中止上游请求并把已生成部分以 interrupted=1 落库（§4.2 / §6.1 第 9 点）。</para>
    ///
    /// <para>错误：非 2xx（如 chat 的 400/404/409 JSON 错误体）→ 解析统一错误格式抛
    /// <see cref="BackendApiException"/>；2xx 但不是 NDJSON → 抛 invalid_response；
    /// 流在收到 done/error 之前意外结束 → 抛 stream_interrupted（避免界面永远停在“生成中”）。</para>
    /// </summary>
    /// <param name="method">HTTP 方法（chat / regenerate 均为 POST）。</param>
    /// <param name="path">相对路径（如 api/sessions/{id}/chat）。</param>
    /// <param name="body">请求体（regenerate 为 null）。</param>
    /// <param name="ct">取消令牌。</param>
    public async IAsyncEnumerable<ChatEvent> StreamEventsAsync(
        HttpMethod method,
        string path,
        object? body,
        [EnumeratorCancellation] CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(method, path);
        if (body is not null)
        {
            request.Content = CreateJsonContent(body);
        }

        // ResponseHeadersRead：拿到响应头即返回，随后逐行读取流式 body。
        using var response = await SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct)
            .ConfigureAwait(false);

        var mediaType = response.Content.Headers.ContentType?.MediaType;
        if (!string.Equals(mediaType, NdjsonMediaType, StringComparison.OrdinalIgnoreCase))
        {
            // 理论上非 2xx 已在 SendAsync 拦截；走到这里说明后端返回了 2xx 的非流式响应。
            throw new BackendApiException(
                "invalid_response",
                "后端返回的响应不是 NDJSON 流（Content-Type 应为 application/x-ndjson）",
                (int)response.StatusCode);
        }

        await using var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        using var reader = new StreamReader(
            stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true, bufferSize: 4096, leaveOpen: false);

        bool gotTerminalEvent = false;
        while (true)
        {
            ct.ThrowIfCancellationRequested();

            string? line;
            try
            {
                line = await reader.ReadLineAsync(ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw; // 调用方主动取消（停止生成）：保持取消语义
            }
            catch (Exception ex) when (ex is IOException or HttpRequestException or ObjectDisposedException)
            {
                throw new BackendApiException(
                    "stream_interrupted", "流式连接中断，回复未完整接收", null, ex);
            }

            if (line is null)
            {
                break; // 流结束
            }
            if (line.Length == 0)
            {
                continue;
            }

            var chatEvent = ParseEvent(line);
            if (chatEvent is null)
            {
                continue; // 未知事件类型 / 非 JSON 行：向前兼容，忽略（§4.2）
            }
            if (chatEvent.Type is ChatEventType.Done or ChatEventType.Error)
            {
                gotTerminalEvent = true;
            }
            yield return chatEvent;
        }

        if (!gotTerminalEvent && !ct.IsCancellationRequested)
        {
            throw new BackendApiException(
                "stream_interrupted", "流式响应意外结束（未收到 done/error 事件），回复可能不完整");
        }
    }

    // ------------------------------------------------------------------
    // 内部实现
    // ------------------------------------------------------------------

    /// <summary>
    /// 发送请求：非 2xx 时读取响应体并抛 <see cref="BackendApiException"/>；
    /// 连接失败/超时包装为 backend_unreachable / timeout（调用方主动取消原样抛出）。
    /// 调用方负责 Dispose 返回的响应。
    /// </summary>
    private async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        HttpCompletionOption completionOption,
        CancellationToken ct)
    {
        HttpResponseMessage response;
        try
        {
            response = await _http.SendAsync(request, completionOption, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw; // 调用方取消（停止生成 / 关闭窗口）
        }
        catch (OperationCanceledException ex)
        {
            // 未主动取消却收到取消：HttpClient.Timeout 触发（后端长时间无响应）
            throw new BackendApiException("timeout", "请求后端超时（后端长时间无响应）", null, ex);
        }
        catch (HttpRequestException ex)
        {
            throw new BackendApiException("backend_unreachable", "后端未连接或网络错误", null, ex);
        }

        if (!response.IsSuccessStatusCode)
        {
            string body;
            try
            {
                body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            }
            catch (Exception)
            {
                body = ""; // 错误体读取失败不覆盖原始状态码信息
            }
            var statusCode = (int)response.StatusCode;
            response.Dispose();
            throw CreateError(statusCode, body);
        }

        return response;
    }

    /// <summary>发送 JSON 请求并反序列化响应体；JSON 解析失败抛 invalid_response。</summary>
    private async Task<T> SendJsonAsync<T>(HttpRequestMessage request, CancellationToken ct)
        where T : class
    {
        using var response = await SendAsync(request, HttpCompletionOption.ResponseContentRead, ct)
            .ConfigureAwait(false);
        await using var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        try
        {
            var value = await JsonSerializer.DeserializeAsync<T>(stream, JsonOptions, ct).ConfigureAwait(false);
            if (value is null)
            {
                throw new BackendApiException(
                    "invalid_response",
                    "后端响应内容为空或格式不正确（期望 JSON）",
                    (int)response.StatusCode);
            }
            return value;
        }
        catch (JsonException ex)
        {
            throw new BackendApiException(
                "invalid_response", "后端响应 JSON 解析失败", (int)response.StatusCode, ex);
        }
    }

    /// <summary>把请求体序列化为 application/json（使用统一 snake_case 选项）。</summary>
    private static HttpContent CreateJsonContent(object body)
    {
        var json = JsonSerializer.Serialize(body, body.GetType(), JsonOptions);
        return new StringContent(json, Encoding.UTF8, "application/json");
    }

    /// <summary>
    /// 解析后端统一错误体 <c>{"error":{"code","message"}}</c>（§4.3）；
    /// 解析失败时用 HTTP 状态码生成兜底 code/中文 message。
    /// </summary>
    private static BackendApiException CreateError(int statusCode, string? body)
    {
        string? code = null;
        string? message = null;

        if (!string.IsNullOrWhiteSpace(body))
        {
            try
            {
                using var document = JsonDocument.Parse(body);
                var root = document.RootElement;
                if (root.ValueKind == JsonValueKind.Object)
                {
                    if (root.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.Object)
                    {
                        code = ReadString(error, "code");
                        message = ReadString(error, "message");
                    }
                    else
                    {
                        // 兼容 FastAPI 默认 {"detail": ...}（虽然契约要求 error 结构）
                        message = ReadString(root, "detail") ?? ReadString(root, "message");
                    }
                }
            }
            catch (JsonException)
            {
                // 非 JSON 错误体（如代理返回 HTML）→ 走兜底文案
            }
        }

        code ??= DefaultCode(statusCode);
        message ??= $"后端请求失败（HTTP {statusCode}）";
        return new BackendApiException(code, message, statusCode);
    }

    /// <summary>状态码 → 兜底错误码（与 §4.3 / 后端冻结的错误码枚举对齐）。</summary>
    private static string DefaultCode(int statusCode) => statusCode switch
    {
        400 => "bad_request",
        401 or 403 => "auth_failed",
        404 => "not_found",
        409 => "generation_in_progress",
        422 => "validation_error",
        429 => "rate_limited",
        >= 500 => "internal",
        _ => "http_error",
    };

    /// <summary>把单行 NDJSON 解析为 <see cref="ChatEvent"/>；未知类型/非法行返回 null（忽略）。</summary>
    private static ChatEvent? ParseEvent(string line)
    {
        try
        {
            using var document = JsonDocument.Parse(line);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return null;
            }

            var type = ReadString(root, "type");
            switch (type)
            {
                case "meta":
                {
                    var matched = new List<MatchedEntry>();
                    if (root.TryGetProperty("matched_entries", out var entries)
                        && entries.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var item in entries.EnumerateArray())
                        {
                            if (item.ValueKind != JsonValueKind.Object)
                            {
                                continue;
                            }
                            var name = ReadString(item, "name") ?? "";
                            var keywords = new List<string>();
                            if (item.TryGetProperty("hit_keywords", out var hitKeywords)
                                && hitKeywords.ValueKind == JsonValueKind.Array)
                            {
                                foreach (var keyword in hitKeywords.EnumerateArray())
                                {
                                    if (keyword.ValueKind == JsonValueKind.String)
                                    {
                                        var text = keyword.GetString();
                                        if (!string.IsNullOrEmpty(text))
                                        {
                                            keywords.Add(text);
                                        }
                                    }
                                }
                            }
                            matched.Add(new MatchedEntry(name, keywords));
                        }
                    }
                    return ChatEvent.CreateMeta(matched);
                }

                case "delta":
                    return ChatEvent.CreateDelta(ReadString(root, "text") ?? "");

                case "done":
                {
                    ChatUsage? usage = null;
                    if (root.TryGetProperty("usage", out var usageElement)
                        && usageElement.ValueKind == JsonValueKind.Object)
                    {
                        usage = new ChatUsage(
                            ReadLong(usageElement, "prompt_tokens"),
                            ReadLong(usageElement, "completion_tokens"));
                    }
                    return ChatEvent.CreateDone(ReadString(root, "full_text") ?? "", usage);
                }

                case "error":
                    return ChatEvent.CreateError(
                        ReadString(root, "code") ?? "internal",
                        ReadString(root, "message") ?? "生成失败，请稍后重试");

                default:
                    return null; // 未知事件类型：向前兼容，忽略（§4.2）
            }
        }
        catch (JsonException)
        {
            return null; // 非法行：忽略，不中断整个流
        }
    }

    /// <summary>读取 JSON 对象的字符串属性；不存在/类型不符返回 null。</summary>
    private static string? ReadString(JsonElement element, string propertyName)
    {
        if (!element.TryGetProperty(propertyName, out var value))
        {
            return null;
        }
        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Number => value.GetRawText(),
            _ => null,
        };
    }

    /// <summary>读取 JSON 对象的数字属性为 long?；不存在/类型不符返回 null。</summary>
    private static long? ReadLong(JsonElement element, string propertyName)
    {
        if (!element.TryGetProperty(propertyName, out var value))
        {
            return null;
        }
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var number))
        {
            return number;
        }
        return null;
    }
}
