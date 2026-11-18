using MyAgent.App.Models;

namespace MyAgent.App.Services;

/// <summary>
/// 后端服务抽象：方法形状对齐《需求与方案指导.md》§4.3 REST API 契约。
/// 假数据阶段由 FakeBackendService 实现；集成阶段由 ApiClient（REST JSON + NDJSON 流式解析，
/// 配合 BackendManager 进程管理）按同一接口替换，界面层（ViewModels）不感知底层来源。
/// 接口只表达业务语义，不写死任何 HTTP/进程细节。
/// </summary>
public interface IBackendService
{
    /// <summary>GET /api/sessions：会话列表，按 updated_at 倒序，含消息数；api_key 打码。</summary>
    Task<IReadOnlyList<SessionInfo>> ListSessionsAsync(CancellationToken ct = default);

    /// <summary>POST /api/sessions：新建会话（默认名“新会话”，其余配置取后端默认值）。</summary>
    Task<SessionInfo> CreateSessionAsync(string name, CancellationToken ct = default);

    /// <summary>
    /// PATCH /api/sessions/{id}：更新会话字段。主窗口阶段只用它改名；
    /// 集成阶段可在实现内扩展为完整字段集（system_prompt / base_url / model / 预算 / 代理等）。
    /// </summary>
    Task<SessionInfo?> UpdateSessionAsync(string id, string newName, CancellationToken ct = default);

    /// <summary>DELETE /api/sessions/{id}：删除会话（级联删除其消息与世界书条目）。</summary>
    Task<bool> DeleteSessionAsync(string id, CancellationToken ct = default);

    /// <summary>GET /api/sessions/{id}/messages?limit=200：取最新 limit 条，按 id 正序返回（含 interrupted 标记）。</summary>
    Task<IReadOnlyList<ChatMessage>> GetMessagesAsync(string sessionId, int limit = 200, CancellationToken ct = default);

    /// <summary>DELETE /api/sessions/{id}/messages：清空历史，返回删除条数。</summary>
    Task<int> ClearMessagesAsync(string sessionId, CancellationToken ct = default);

    /// <summary>
    /// DELETE /api/sessions/{id}/messages/{msg_id}：删除该条及其后所有消息（级联，§4.3 / §5.1），
    /// 返回删除条数（对应响应 {"deleted": N}）。
    /// </summary>
    Task<int> DeleteMessageAndAfterAsync(string sessionId, long messageId, CancellationToken ct = default);

    /// <summary>
    /// POST /api/sessions/{id}/chat：发送消息，返回 NDJSON 流式事件序列（§4.2：meta→delta*→done|error）。
    /// 取消（CancellationToken）即“停止生成”：后端感知断开后中止上游请求，
    /// 并把已生成部分以 interrupted=1 落库。
    /// </summary>
    IAsyncEnumerable<ChatEvent> SendMessageAsync(string sessionId, string content, CancellationToken ct = default);

    /// <summary>
    /// POST /api/sessions/{id}/regenerate：重新生成。末条为 assistant → 先删除该条再以现有历史生成；
    /// 末条为 user → 直接重试（不新增输入）。事件协议同 chat。
    /// </summary>
    IAsyncEnumerable<ChatEvent> RegenerateAsync(string sessionId, CancellationToken ct = default);
}
