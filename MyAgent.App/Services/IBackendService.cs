using MyAgent.App.Models;

namespace MyAgent.App.Services;

/// <summary>
/// 后端服务抽象：方法形状对齐《需求与方案指导.md》§4.3 REST API 契约。
/// 假数据阶段由 FakeBackendService 实现；真实模式由 RealApiBackendService（内部组合 ApiClient：
/// REST JSON + NDJSON 流式解析）按同一接口替换，界面层（ViewModels）不感知底层来源。
/// 接口只表达业务语义，不写死任何 HTTP/进程细节。
/// </summary>
public interface IBackendService
{
    /// <summary>GET /api/sessions：会话列表，按 updated_at 倒序，含消息数；api_key 打码。</summary>
    Task<IReadOnlyList<SessionInfo>> ListSessionsAsync(CancellationToken ct = default);

    /// <summary>POST /api/sessions：新建会话（默认名“新会话”，其余配置取后端默认值）。</summary>
    Task<SessionInfo> CreateSessionAsync(string name, CancellationToken ct = default);

    /// <summary>
    /// PATCH /api/sessions/{id}：仅更新会话名（主窗口重命名快捷路径）。
    /// 完整配置保存走 <see cref="UpdateSessionConfigAsync"/>。
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
    /// POST /api/sessions/{id}/regenerate：重新生成。末条为 assistant → 先校验存在上一条 user 再删除该条生成
    /// （v0.4③：缺少上一条 user 时 400 且不删除）；末条为 user → 直接重试（不新增输入）。事件协议同 chat。
    /// </summary>
    IAsyncEnumerable<ChatEvent> RegenerateAsync(string sessionId, CancellationToken ct = default);

    // ---------------- 会话配置（Step 12，wpf-config） ----------------

    /// <summary>GET /api/sessions/{id}：会话详情（完整配置字段，api_key 打码）。会话不存在返回 null。</summary>
    Task<SessionInfo?> GetSessionAsync(string id, CancellationToken ct = default);

    /// <summary>
    /// PATCH /api/sessions/{id}：整表保存会话配置（§4.3 / v0.4 语义）。
    /// patch 语义：null 字段 = 显式发送 null（v0.4①：base_url/model/max_tokens 表示清除配置，
    /// 其余字段视为“未提供、不更新”）；ApiKey 为 null/空串时不发送（仅非空值更新）。
    /// 设置页总是整表提交。返回更新后的会话快照；会话不存在返回 null。
    /// </summary>
    Task<SessionInfo?> UpdateSessionConfigAsync(string id, SessionConfigPatch patch, CancellationToken ct = default);

    // ---------------- 世界书（Step 13，每会话独立，§4.3） ----------------

    /// <summary>GET /api/sessions/{sid}/worldbook：条目列表，按 sort_order 升序。</summary>
    Task<IReadOnlyList<WorldBookEntry>> ListWorldbookAsync(string sessionId, CancellationToken ct = default);

    /// <summary>
    /// POST /api/sessions/{sid}/worldbook：新建条目。内容为空由后端拒绝（抛异常，界面转错误条）；
    /// sort_order ≤ 0 时后端取本会话 max+1。返回已创建条目（含 id）。
    /// </summary>
    Task<WorldBookEntry> CreateWorldbookEntryAsync(string sessionId, WorldBookEntry entry, CancellationToken ct = default);

    /// <summary>PATCH /api/sessions/{sid}/worldbook/{eid}：更新条目；条目不存在/跨会话返回 null。</summary>
    Task<WorldBookEntry?> UpdateWorldbookEntryAsync(string sessionId, string entryId, WorldBookEntry entry, CancellationToken ct = default);

    /// <summary>DELETE /api/sessions/{sid}/worldbook/{eid}：删除条目。</summary>
    Task<bool> DeleteWorldbookEntryAsync(string sessionId, string entryId, CancellationToken ct = default);

    // ---------------- 连通性测试（Step 12） ----------------

    /// <summary>
    /// POST /api/config/test-connection：测试模型连通性（§4.3：失败也返回 HTTP 200 + error）。
    /// 约定：本方法不抛异常——后端未连接/网络失败/校验失败一律返回 Ok=false 与中文原因。
    /// </summary>
    Task<TestConnectionResult> TestConnectionAsync(TestConnectionRequest request, CancellationToken ct = default);
}
