using System.Runtime.CompilerServices;
using System.Text;
using MyAgent.App.Models;

namespace MyAgent.App.Services;

/// <summary>
/// 假数据后端：在内存中模拟 §4.3 REST API 与 §4.2 NDJSON 流式协议的行为，
/// 供前端在真实 Python 后端就绪前完成 Step 9-11 的界面开发与演示。
///
/// 模拟内容：
///  1. 会话/消息 CRUD（含删单条级联、清空历史、按 updated_at 倒序列表、api_key 打码）；
///  2. 流式生成：meta（世界书命中）→ delta*（随机分块 + 随机延迟，模拟逐 token 到达）→ done|error；
///  3. 停止语义：CancellationToken 取消后，把已生成部分以 interrupted=1 落库（§4.2 / §6.1 第 9 步）；
///  4. 世界书匹配：纯子串匹配、ASCII 忽略大小写、按 sort_order 顺序返回、禁用条目跳过（§6.1 第 3 点）；
///  5. 失败演示：用户输入包含“模拟失败”时返回 error 事件（用户消息保留，可点“重试”恢复）。
///
/// 集成阶段由 ApiClient 按 IBackendService 同一接口替换本类，界面层无需改动。
/// 所有“网络延迟”用 Task.Delay 模拟，便于演示流式 UI 与 30ms 节流。
/// </summary>
public sealed class FakeBackendService : IBackendService
{
    /// <summary>内存存储统一加锁（模拟 SQLite 串行写，避免多会话并行生成时数据竞争）。</summary>
    private readonly object _lock = new();

    private readonly List<SessionInfo> _sessions = new();
    private readonly Dictionary<string, List<ChatMessage>> _messages = new();
    private readonly Dictionary<string, List<WorldBookEntry>> _worldbooks = new();
    private readonly Dictionary<string, int> _generationCounts = new();

    /// <summary>固定种子：演示行为可复现，便于验收对照。</summary>
    private readonly Random _random = new(20261118);
    private long _nextMessageId = 1;

    public FakeBackendService() => Seed();

    // ---------------- 会话 ----------------

    public async Task<IReadOnlyList<SessionInfo>> ListSessionsAsync(CancellationToken ct = default)
    {
        await Task.Delay(30, ct); // 模拟网络往返
        lock (_lock)
        {
            // §4.3：按 updated_at 倒序，附 message_count；api_key 打码
            return _sessions.OrderByDescending(s => s.UpdatedAt).Select(CloneWithCount).ToList();
        }
    }

    public async Task<SessionInfo> CreateSessionAsync(string name, CancellationToken ct = default)
    {
        await Task.Delay(30, ct);
        lock (_lock)
        {
            var now = DateTimeOffset.UtcNow;
            var session = new SessionInfo
            {
                Id = Guid.NewGuid().ToString(),
                Name = string.IsNullOrWhiteSpace(name) ? "新会话" : name.Trim(),
                CreatedAt = now,
                UpdatedAt = now,
                // 其余字段使用 SessionInfo 默认值，与 §4.3 POST /api/sessions 默认配置一致
            };
            _sessions.Add(session);
            _messages[session.Id] = new List<ChatMessage>();
            _worldbooks[session.Id] = new List<WorldBookEntry>();
            return session.Clone();
        }
    }

    public async Task<SessionInfo?> UpdateSessionAsync(string id, string newName, CancellationToken ct = default)
    {
        await Task.Delay(30, ct);
        lock (_lock)
        {
            var session = _sessions.FirstOrDefault(s => s.Id == id);
            if (session == null) return null;
            if (!string.IsNullOrWhiteSpace(newName)) session.Name = newName.Trim();
            session.UpdatedAt = DateTimeOffset.UtcNow; // §5.1：更新自动刷新 updated_at
            return CloneWithCount(session);
        }
    }

    public async Task<bool> DeleteSessionAsync(string id, CancellationToken ct = default)
    {
        await Task.Delay(30, ct);
        lock (_lock)
        {
            // §4.3：级联删除其消息与世界书条目
            bool removed = _sessions.RemoveAll(s => s.Id == id) > 0;
            if (removed)
            {
                _messages.Remove(id);
                _worldbooks.Remove(id);
                _generationCounts.Remove(id);
            }
            return removed;
        }
    }

    // ---------------- 消息 ----------------

    public async Task<IReadOnlyList<ChatMessage>> GetMessagesAsync(string sessionId, int limit = 200, CancellationToken ct = default)
    {
        await Task.Delay(20, ct);
        lock (_lock)
        {
            if (!_messages.TryGetValue(sessionId, out var msgs)) return Array.Empty<ChatMessage>();
            // §5.1：取最新 limit 条，返回按 id 正序
            return msgs.Skip(Math.Max(0, msgs.Count - limit)).Select(m => m.Clone()).ToList();
        }
    }

    public async Task<int> ClearMessagesAsync(string sessionId, CancellationToken ct = default)
    {
        await Task.Delay(20, ct);
        lock (_lock)
        {
            if (!_messages.TryGetValue(sessionId, out var msgs)) return 0;
            int deleted = msgs.Count;
            msgs.Clear();
            TouchSession(sessionId);
            return deleted;
        }
    }

    public async Task<int> DeleteMessageAndAfterAsync(string sessionId, long messageId, CancellationToken ct = default)
    {
        await Task.Delay(20, ct);
        lock (_lock)
        {
            if (!_messages.TryGetValue(sessionId, out var msgs)) return 0;
            int index = msgs.FindIndex(m => m.Id == messageId);
            if (index < 0) return 0;
            // §4.3 / §5.1：删除该条及其后所有消息，返回删除条数
            int deleted = msgs.Count - index;
            msgs.RemoveRange(index, deleted);
            TouchSession(sessionId);
            return deleted;
        }
    }

    // ---------------- 会话配置（Step 12） ----------------

    public async Task<SessionInfo?> GetSessionAsync(string id, CancellationToken ct = default)
    {
        await Task.Delay(20, ct);
        lock (_lock)
        {
            var session = _sessions.FirstOrDefault(s => s.Id == id);
            return session == null ? null : CloneWithCount(session);
        }
    }

    public async Task<SessionInfo?> UpdateSessionConfigAsync(string id, SessionConfigPatch patch, CancellationToken ct = default)
    {
        await Task.Delay(30, ct);
        lock (_lock)
        {
            var session = _sessions.FirstOrDefault(s => s.Id == id);
            if (session == null) return null;

            // v0.4① 语义：null = 未提供（跳过）；例外——base_url/model/max_tokens 的 null 表示清除配置。
            // 设置页总是整表提交，max_tokens 为 null 即“清除，上游默认”。
            if (patch.Name != null) session.Name = patch.Name.Trim();
            if (patch.SystemPrompt != null) session.SystemPrompt = patch.SystemPrompt;
            if (patch.ScanRounds.HasValue) session.ScanRounds = Math.Max(0, patch.ScanRounds.Value);
            if (patch.WorldbookEnabled.HasValue) session.WorldbookEnabled = patch.WorldbookEnabled.Value;
            if (patch.BaseUrl != null) session.BaseUrl = patch.BaseUrl;
            if (patch.Model != null) session.Model = patch.Model;
            session.MaxTokens = patch.MaxTokens; // null = 清除（上游默认）
            // v0.5①：推理强度整表保存；null = 清除（Fake 用空串表示“不设置/不向上游发送”），值原样存
            session.ReasoningEffort = patch.ReasoningEffort ?? "";
            if (patch.Temperature.HasValue) session.Temperature = patch.Temperature.Value;
            if (patch.TopP.HasValue) session.TopP = patch.TopP.Value;
            if (patch.MaxContextChars.HasValue) session.MaxContextChars = Math.Max(0, patch.MaxContextChars.Value);
            if (patch.ProxyUrl != null) session.ProxyUrl = patch.ProxyUrl;
            if (!string.IsNullOrWhiteSpace(patch.ApiKey)) session.MaskedApiKey = MaskKey(patch.ApiKey);

            session.UpdatedAt = DateTimeOffset.UtcNow; // §5.1：更新自动刷新 updated_at
            return CloneWithCount(session);
        }
    }

    // ---------------- 世界书 CRUD（Step 13，每会话独立，§4.3） ----------------

    public async Task<IReadOnlyList<WorldBookEntry>> ListWorldbookAsync(string sessionId, CancellationToken ct = default)
    {
        await Task.Delay(20, ct);
        lock (_lock)
        {
            if (!_worldbooks.TryGetValue(sessionId, out var entries)) return Array.Empty<WorldBookEntry>();
            // §5.1：按 sort_order 升序
            return entries.OrderBy(e => e.SortOrder).Select(CloneEntry).ToList();
        }
    }

    public async Task<WorldBookEntry> CreateWorldbookEntryAsync(string sessionId, WorldBookEntry entry, CancellationToken ct = default)
    {
        await Task.Delay(30, ct);
        lock (_lock)
        {
            EnsureSession(sessionId);
            var content = (entry.Content ?? "").Trim();
            if (content.Length == 0) throw new InvalidOperationException("条目内容不能为空（§5/§10：空禁止保存）");
            var entries = _worldbooks[sessionId];
            var created = new WorldBookEntry
            {
                Id = Guid.NewGuid().ToString(),
                SessionId = sessionId,
                Name = (entry.Name ?? "").Trim(),
                Keywords = NormalizeKeywords(entry.Keywords),
                Content = content,
                Enabled = entry.Enabled,
                // §5.1：sort_order 缺省 = 本会话 max+1（假数据约定：SortOrder <= 0 视为缺省）
                SortOrder = entry.SortOrder > 0
                    ? entry.SortOrder
                    : (entries.Count == 0 ? 1 : entries.Max(w => w.SortOrder) + 1),
            };
            entries.Add(created);
            TouchSession(sessionId);
            return CloneEntry(created);
        }
    }

    public async Task<WorldBookEntry?> UpdateWorldbookEntryAsync(string sessionId, string entryId, WorldBookEntry entry, CancellationToken ct = default)
    {
        await Task.Delay(30, ct);
        lock (_lock)
        {
            if (!_worldbooks.TryGetValue(sessionId, out var entries)) return null;
            var existing = entries.FirstOrDefault(w => w.Id == entryId);
            if (existing == null) return null; // 跨会话/不存在：null（对应后端 404，§5.1 会话隔离）

            var content = (entry.Content ?? "").Trim();
            if (content.Length == 0) throw new InvalidOperationException("条目内容不能为空（§5/§10：空禁止保存）");

            existing.Name = (entry.Name ?? "").Trim();
            existing.Keywords = NormalizeKeywords(entry.Keywords);
            existing.Content = content;
            existing.Enabled = entry.Enabled;
            existing.SortOrder = entry.SortOrder;
            TouchSession(sessionId);
            return CloneEntry(existing);
        }
    }

    public async Task<bool> DeleteWorldbookEntryAsync(string sessionId, string entryId, CancellationToken ct = default)
    {
        await Task.Delay(20, ct);
        lock (_lock)
        {
            if (!_worldbooks.TryGetValue(sessionId, out var entries)) return false;
            bool removed = entries.RemoveAll(w => w.Id == entryId) > 0;
            if (removed) TouchSession(sessionId);
            return removed;
        }
    }

    // ---------------- 连通性测试（Step 12） ----------------

    public async Task<TestConnectionResult> TestConnectionAsync(TestConnectionRequest request, CancellationToken ct = default)
    {
        await Task.Delay(150, ct); // 模拟连通性探测
        if (string.IsNullOrWhiteSpace(request.BaseUrl) || string.IsNullOrWhiteSpace(request.Model))
            return new TestConnectionResult
            {
                Ok = false,
                Message = "请先填写 Base URL 与模型名（假数据模式仅做本地校验，未真实请求）",
            };
        return new TestConnectionResult
        {
            Ok = true,
            Message = "连接成功（假数据模式模拟结果，未发起真实请求）",
        };
    }

    // ---------------- 流式生成 ----------------

    public async IAsyncEnumerable<ChatEvent> SendMessageAsync(string sessionId, string content, [EnumeratorCancellation] CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(content))
            throw new ArgumentException("消息内容不能为空（前端已拦截，此处双保险）", nameof(content));

        bool failDemo;
        lock (_lock)
        {
            EnsureSession(sessionId);
            // §6.1 第 2 步：立即存库用户消息——后续生成失败也不丢用户输入
            AppendMessageCore(sessionId, MessageRole.User, content.Trim(), interrupted: false, matched: null);
            // 演示触发器：输入包含“模拟失败”时本轮返回 error（重新生成/重试时不触发）
            failDemo = content.Contains("模拟失败", StringComparison.Ordinal);
        }

        await foreach (var ev in StreamReplyCoreAsync(sessionId, content.Trim(), failDemo, ct))
            yield return ev;
    }

    public async IAsyncEnumerable<ChatEvent> RegenerateAsync(string sessionId, [EnumeratorCancellation] CancellationToken ct = default)
    {
        string userContent;
        lock (_lock)
        {
            EnsureSession(sessionId);
            var msgs = _messages[sessionId];
            if (msgs.Count == 0)
                throw new InvalidOperationException("没有可重新生成的消息");
            // §4.3：末条为 assistant → 先删除该条再以现有历史重新生成；末条为 user → 直接重试（不新增输入）
            if (msgs[^1].Role == MessageRole.Assistant) msgs.RemoveAt(msgs.Count - 1);
            if (msgs.Count == 0 || msgs[^1].Role != MessageRole.User)
                throw new InvalidOperationException("当前会话状态不支持重新生成");
            userContent = msgs[^1].Content;
        }

        // 重新生成时不触发“模拟失败”，保证点“重试”可以演示恢复成功
        await foreach (var ev in StreamReplyCoreAsync(sessionId, userContent, failDemo: false, ct))
            yield return ev;
    }

    /// <summary>
    /// 共用的流式回复核心：meta → delta* → done|error（§4.2 协议顺序）。
    /// 注意：不要在持有 _lock 的同时 await（生成期间不阻塞其它会话）。
    /// </summary>
    private async IAsyncEnumerable<ChatEvent> StreamReplyCoreAsync(
        string sessionId, string userContent, bool failDemo, [EnumeratorCancellation] CancellationToken ct)
    {
        // §6.1 第 3 点：候选文本 = 最近 N 轮历史 + 当前输入，命中启用条目（简化：全部历史 + 当前输入）
        var matched = MatchWorldbook(sessionId, userContent);

        await Task.Delay(200 + _random.Next(300), ct); // 模拟上游首包延迟

        yield return ChatEvent.CreateMeta(matched); // meta 必发（§4.2），未命中时为空数组

        if (failDemo)
        {
            // 演示失败路径：error 为最后一行，用户消息保留（§6.1 第 10 点）
            yield return ChatEvent.CreateError(
                "upstream_unreachable",
                "模拟的上游连接失败（假数据演示：输入包含“模拟失败”即触发；点击消息上的“重试”可恢复）");
            yield break;
        }

        var fullText = BuildReplyText(sessionId, userContent, matched);
        var partial = new StringBuilder();
        bool completed = false;
        try
        {
            int index = 0;
            while (index < fullText.Length)
            {
                ct.ThrowIfCancellationRequested();
                // 随机 1-3 字符一块、20-60ms 一块，模拟逐 token 到达（足够慢，便于演示 30ms 节流与“停止”）
                int length = Math.Min(_random.Next(1, 4), fullText.Length - index);
                string chunk = fullText.Substring(index, length);
                partial.Append(chunk);
                index += length;
                await Task.Delay(_random.Next(20, 60), ct);
                yield return ChatEvent.CreateDelta(chunk);
            }

            completed = true;
            lock (_lock)
            {
                // §6.1 第 8 步：完成时存库 assistant 完整回复
                AppendMessageCore(sessionId, MessageRole.Assistant, partial.ToString(), interrupted: false, matched);
            }
            yield return ChatEvent.CreateDone(partial.ToString());
        }
        finally
        {
            // 客户端断开（用户点了“停止”）→ 中止生成，把已生成部分落库（interrupted=1），
            // 对应 §4.2 / §6.1 第 9 步。注意：finally 在 CancellationToken 触发后先于异常传播执行，
            // 因此调用方随后刷新消息列表时一定能看到 interrupted 消息。
            if (!completed && partial.Length > 0)
            {
                lock (_lock)
                {
                    AppendMessageCore(sessionId, MessageRole.Assistant, partial.ToString(), interrupted: true, matched);
                }
            }
        }
    }

    // ---------------- 世界书匹配（假数据简化版 §6.1 第 3 点） ----------------

    private IReadOnlyList<MatchedEntry> MatchWorldbook(string sessionId, string currentInput)
    {
        lock (_lock)
        {
            if (!_worldbooks.TryGetValue(sessionId, out var entries)) return Array.Empty<MatchedEntry>();

            // 候选文本：最近 scan_rounds 轮（user+assistant 成对）+ 当前用户输入；
            // 假数据阶段简化为“全部历史 + 当前输入”，匹配语义（子串、忽略大小写、sort_order 顺序）与文档一致
            var historyText = _messages.TryGetValue(sessionId, out var msgs)
                ? string.Join("\n", msgs.Select(m => m.Content))
                : "";
            var candidate = historyText + "\n" + currentInput;

            var result = new List<MatchedEntry>();
            foreach (var entry in entries.Where(e => e.Enabled).OrderBy(e => e.SortOrder))
            {
                // 纯子串匹配；ASCII 双方忽略大小写（OrdinalIgnoreCase 对中文即精确匹配）
                var hits = entry.Keywords
                    .Where(k => !string.IsNullOrWhiteSpace(k) && candidate.Contains(k, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                if (hits.Count > 0) result.Add(new MatchedEntry(entry.Name, hits)); // 同条目每轮只注入一次
            }
            return result;
        }
    }

    /// <summary>拼一段可读的假回复：引用用户输入、命中条目与演示说明，长度足够演示流式与停止。</summary>
    private string BuildReplyText(string sessionId, string userContent, IReadOnlyList<MatchedEntry> matched)
    {
        int generation;
        lock (_lock)
        {
            generation = _generationCounts.TryGetValue(sessionId, out var count) ? count + 1 : 1;
            _generationCounts[sessionId] = generation;
        }

        string hitNote = matched.Count > 0
            ? $"本次生成命中了世界书条目：{string.Join("、", matched.Select(m => m.Name))}。注意观察气泡下方的小字命中提示。"
            : "本次生成没有命中任何世界书条目，气泡下方不会出现命中提示。";

        string[] openings =
        {
            "你话音落下，",
            "故事继续推进，",
            "四周安静了片刻，",
            "一阵夜风吹过，",
        };

        return $"{openings[generation % openings.Length]}世界对你的话做出了回应：\n\n“{userContent}”\n\n{hitNote}\n\n"
             + $"这是 FakeBackendService 模拟的流式回复（本会话第 {generation} 次生成）。"
             + "生成过程中点击“停止”会保留已生成部分并标注“（已停止）”；"
             + "在输入中包含“模拟失败”四个字可演示失败与“重试”按钮。";
    }

    // ---------------- 内部存储辅助（调用方须持有 _lock） ----------------

    private void EnsureSession(string sessionId)
    {
        if (!_sessions.Any(s => s.Id == sessionId))
            throw new KeyNotFoundException($"会话不存在：{sessionId}");
    }

    private void AppendMessageCore(string sessionId, MessageRole role, string content, bool interrupted, IReadOnlyList<MatchedEntry>? matched)
    {
        _messages[sessionId].Add(new ChatMessage
        {
            Id = _nextMessageId++,
            SessionId = sessionId,
            Role = role,
            Content = content,
            Interrupted = interrupted,
            // 假数据附加字段：让命中提示在切换会话/刷新后仍然可见（见 ChatMessage 类注释）
            MatchedEntries = matched ?? Array.Empty<MatchedEntry>(),
            CreatedAt = DateTimeOffset.UtcNow,
        });
        TouchSession(sessionId); // §5.1：append_message 同步更新会话 updated_at
    }

    private void TouchSession(string sessionId)
    {
        var session = _sessions.FirstOrDefault(s => s.Id == sessionId);
        if (session != null) session.UpdatedAt = DateTimeOffset.UtcNow;
    }

    private SessionInfo CloneWithCount(SessionInfo session)
    {
        var clone = session.Clone();
        clone.MessageCount = _messages.TryGetValue(session.Id, out var msgs) ? msgs.Count : 0;
        return clone;
    }

    // ---------------- 演示种子数据 ----------------

    private void Seed()
    {
        var now = DateTimeOffset.UtcNow;

        // 会话 A：包含世界书命中小字与一条“（已停止）”消息，启动即演示渲染效果
        var a = new SessionInfo
        {
            Id = "demo-session-01",
            Name = "示例：王城历险",
            SystemPrompt = "你是一个奇幻世界观的叙事助手，请沉浸式地推进故事。",
            Model = "gpt-4o-mini（示例）",
            BaseUrl = "https://api.openai.com/v1",
            MaskedApiKey = "sk-12****abcd",
            CreatedAt = now.AddMinutes(-120),
            UpdatedAt = now.AddMinutes(-10),
        };

        // 会话 B：演示 ASCII 关键词忽略大小写命中（Arthur / arthur 均可）
        var b = new SessionInfo
        {
            Id = "demo-session-02",
            Name = "示例：魔法学院",
            SystemPrompt = "你扮演魔法学院的教务助理。",
            Model = "gpt-4o-mini（示例）",
            BaseUrl = "https://api.openai.com/v1",
            MaskedApiKey = "sk-34****ef56",
            CreatedAt = now.AddMinutes(-90),
            UpdatedAt = now.AddMinutes(-30),
        };

        // 会话 C：空会话（演示空状态提示与新建/删除操作）
        var c = new SessionInfo
        {
            Id = "demo-session-03",
            Name = "新会话",
            CreatedAt = now.AddMinutes(-60),
            UpdatedAt = now.AddMinutes(-60),
        };

        _sessions.Add(a);
        _sessions.Add(b);
        _sessions.Add(c);

        // 会话 A 的世界书：两条启用条目 + 一条禁用条目（演示 Enabled 字段与 sort_order 注入顺序）
        _worldbooks[a.Id] = new List<WorldBookEntry>
        {
            new()
            {
                Id = "wb-a1", SessionId = a.Id, Name = "霜月剑",
                Keywords = new List<string> { "剑", "霜月剑", "神剑" },
                Content = "霜月剑是艾尔登王城传说中的神器，剑身泛着月光般的冷辉。",
                Enabled = true, SortOrder = 1,
            },
            new()
            {
                Id = "wb-a2", SessionId = a.Id, Name = "艾尔登王城",
                Keywords = new List<string> { "王城", "帝都", "城门" },
                Content = "艾尔登王城位于大陆中央，城墙高耸，卫兵盘查森严。",
                Enabled = true, SortOrder = 2,
            },
            new()
            {
                Id = "wb-a3", SessionId = a.Id, Name = "无面者",
                Keywords = new List<string> { "无面者", "面具" },
                Content = "（演示用：本条目处于禁用状态，不参与匹配）无面者是城中的神秘情报贩子。",
                Enabled = false, SortOrder = 3,
            },
        };

        // 会话 B 的世界书
        _worldbooks[b.Id] = new List<WorldBookEntry>
        {
            new()
            {
                Id = "wb-b1", SessionId = b.Id, Name = "亚瑟教授",
                Keywords = new List<string> { "Arthur", "亚瑟", "教授" },
                Content = "亚瑟教授是学院最年长的占星学导师，性情古怪但学识渊博。",
                Enabled = true, SortOrder = 1,
            },
        };
        _worldbooks[c.Id] = new List<WorldBookEntry>();

        // 会话 A 的历史消息：第 2/4 条带命中提示，第 4 条为 interrupted（演示“（已停止）”标注）
        _messages[a.Id] = new List<ChatMessage>
        {
            SeedMessage(1, a.Id, MessageRole.User, "我听说王城里藏着一把传说中的剑，是真的吗？", now.AddMinutes(-9)),
            SeedMessage(2, a.Id, MessageRole.Assistant,
                "是的，旅人。艾尔登王城的旧城区一直流传着“霜月剑”的传说……不过先别急着出发，城门口的卫兵最近盘查得很严。",
                now.AddMinutes(-8),
                hits: new[] { ("艾尔登王城", new[] { "王城" }), ("霜月剑", new[] { "剑" }) }),
            SeedMessage(3, a.Id, MessageRole.User, "那卫兵盘查什么？我该怎么混进去？", now.AddMinutes(-7)),
            SeedMessage(4, a.Id, MessageRole.Assistant,
                "北门的暗渠在王城修城墙时被遗弃，每到午夜会有",
                now.AddMinutes(-6),
                interrupted: true,
                hits: new[] { ("艾尔登王城", new[] { "王城" }) }),
        };

        // 会话 B 的历史消息：ASCII 关键词命中演示
        _messages[b.Id] = new List<ChatMessage>
        {
            SeedMessage(5, b.Id, MessageRole.User, "Arthur 教授今天会来上占星课吗？", now.AddMinutes(-31)),
            SeedMessage(6, b.Id, MessageRole.Assistant,
                "Arthur 教授今天请了假，据说是去北塔观星台了。这条消息用来演示关键词命中：ASCII 关键词忽略大小写，输入 arthur 也能命中。",
                now.AddMinutes(-30),
                hits: new[] { ("亚瑟教授", new[] { "Arthur" }) }),
        };
        _messages[c.Id] = new List<ChatMessage>();

        _nextMessageId = 10;
    }

    private static WorldBookEntry CloneEntry(WorldBookEntry entry) => new()
    {
        Id = entry.Id,
        SessionId = entry.SessionId,
        Name = entry.Name,
        Keywords = new List<string>(entry.Keywords),
        Content = entry.Content,
        Enabled = entry.Enabled,
        SortOrder = entry.SortOrder,
    };

    /// <summary>关键词清洗（§5）：trim、去空行、去重，保持传入顺序。</summary>
    private static List<string> NormalizeKeywords(IEnumerable<string>? keywords)
        => keywords?.Select(k => k.Trim()).Where(k => k.Length > 0).Distinct().ToList() ?? new List<string>();

    /// <summary>api_key 打码（§5.1 mask_key）：保留前 4 后 4，中间 *；不足 8 位全打码。</summary>
    private static string MaskKey(string key)
        => key.Length < 8 ? new string('*', key.Length) : key[..4] + new string('*', key.Length - 8) + key[^4..];

    private static ChatMessage SeedMessage(
        long id, string sessionId, MessageRole role, string content, DateTimeOffset createdAt,
        bool interrupted = false, (string Name, string[] Keywords)[]? hits = null)
        => new()
        {
            Id = id,
            SessionId = sessionId,
            Role = role,
            Content = content,
            Interrupted = interrupted,
            MatchedEntries = hits == null
                ? Array.Empty<MatchedEntry>()
                : hits.Select(h => new MatchedEntry(h.Name, h.Keywords)).ToList(),
            CreatedAt = createdAt,
        };
}
