using System.Runtime.CompilerServices;
using MyAgent.App.Models;

namespace MyAgent.App.Services;

/// <summary>
/// 真实后端服务（组合 ApiClient 实现，Step 12/13 前置基础设施 + Step 14 的 ApiClient 部分）：
/// REST JSON 全部端点 + NDJSON 流式 chat/regenerate + test-connection。
/// 【骨架占位】本批次由 integration 智能体实现全部方法；
/// 当前仅保证接口编译通过，所有方法抛 NotImplementedException。
/// 实现要求见开发步骤分解 Step 14 与指导 §4.2/§4.3，错误统一 BackendApiException（ApiClient 内定义）。
/// </summary>
public sealed class RealApiBackendService : IBackendService
{
    private readonly string _baseUrl;

    public RealApiBackendService(string baseUrl)
    {
        _baseUrl = string.IsNullOrWhiteSpace(baseUrl) ? "http://127.0.0.1:8765" : baseUrl.TrimEnd('/');
    }

    private static NotImplementedException Todo() =>
        new("RealApiBackendService 尚未实现（integration 智能体本批次落地）");

    public Task<IReadOnlyList<SessionInfo>> ListSessionsAsync(CancellationToken ct = default) => throw Todo();
    public Task<SessionInfo> CreateSessionAsync(string name, CancellationToken ct = default) => throw Todo();
    public Task<SessionInfo?> UpdateSessionAsync(string id, string newName, CancellationToken ct = default) => throw Todo();
    public Task<bool> DeleteSessionAsync(string id, CancellationToken ct = default) => throw Todo();
    public Task<IReadOnlyList<ChatMessage>> GetMessagesAsync(string sessionId, int limit = 200, CancellationToken ct = default) => throw Todo();
    public Task<int> ClearMessagesAsync(string sessionId, CancellationToken ct = default) => throw Todo();
    public Task<int> DeleteMessageAndAfterAsync(string sessionId, long messageId, CancellationToken ct = default) => throw Todo();
    public Task<SessionInfo?> GetSessionAsync(string id, CancellationToken ct = default) => throw Todo();
    public Task<SessionInfo?> UpdateSessionConfigAsync(string id, SessionConfigPatch patch, CancellationToken ct = default) => throw Todo();
    public Task<IReadOnlyList<WorldBookEntry>> ListWorldbookAsync(string sessionId, CancellationToken ct = default) => throw Todo();
    public Task<WorldBookEntry> CreateWorldbookEntryAsync(string sessionId, WorldBookEntry entry, CancellationToken ct = default) => throw Todo();
    public Task<WorldBookEntry?> UpdateWorldbookEntryAsync(string sessionId, string entryId, WorldBookEntry entry, CancellationToken ct = default) => throw Todo();
    public Task<bool> DeleteWorldbookEntryAsync(string sessionId, string entryId, CancellationToken ct = default) => throw Todo();
    public Task<TestConnectionResult> TestConnectionAsync(TestConnectionRequest request, CancellationToken ct = default) => throw Todo();

    public IAsyncEnumerable<ChatEvent> SendMessageAsync(string sessionId, string content, CancellationToken ct = default)
    {
        throw Todo();
    }

    public IAsyncEnumerable<ChatEvent> RegenerateAsync(string sessionId, CancellationToken ct = default)
    {
        throw Todo();
    }
}
