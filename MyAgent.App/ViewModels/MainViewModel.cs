using System.Collections.ObjectModel;
using System.Windows.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;
using MyAgent.App.Services;

namespace MyAgent.App.ViewModels;

/// <summary>
/// 主窗口视图模型：会话列表 + 聊天区 + 输入区的一站式状态与命令。
/// 数据一律来自 IBackendService（假数据阶段 = FakeBackendService，集成阶段换 ApiClient），
/// 本类不感知 HTTP/进程细节，保持 MVVM 分层（View 代码后置只做手势与滚底）。
///
/// 关键设计：
///  - Sessions / Messages 两个集合实例不整体替换（只 Clear/Add/Move），保证 View 订阅稳定；
///  - 每会话独立的 CancellationTokenSource：同会话同时仅一个生成，不同会话可并行（§8）；
///  - 流式 delta 先累积到各 MessageItemViewModel.PendingText，共享 30ms DispatcherTimer 统一冲刷；
///  - 生成结束后从服务重新拉取消息，使界面与服务端（含 interrupted 落库）保持一致；
///  - 应用退出时由 View 调用 <see cref="Shutdown"/>：逐个取消进行中的生成并停止节流定时器（幂等）。
/// </summary>
public partial class MainViewModel : ObservableObject
{
    private readonly IBackendService _backend;
    private readonly IDialogService _dialogs;

    /// <summary>会话列表（实例不替换，仅增删/重排）。</summary>
    public ObservableCollection<SessionItemViewModel> Sessions { get; } = new();

    /// <summary>当前会话的消息列表（同上，实例不替换）。</summary>
    public ObservableCollection<MessageItemViewModel> Messages { get; } = new();

    /// <summary>当前选中会话（与左侧 ListBox.SelectedItem 双向绑定）。</summary>
    [ObservableProperty]
    private SessionItemViewModel? _selectedSession;

    /// <summary>消息级错误条文案（§10：请求失败统一错误条，不弹裸异常）。</summary>
    [ObservableProperty]
    private string _errorText = "";

    // —— 按会话维护的生成状态 ——
    private readonly Dictionary<string, SessionItemViewModel> _sessionVms = new();
    private readonly Dictionary<string, CancellationTokenSource> _generationCts = new();
    private readonly Dictionary<string, MessageItemViewModel> _liveMessages = new();

    /// <summary>上一次选中的会话：源生成器只回调新值，旧值由本类自行跟踪（用于退订属性订阅）。</summary>
    private SessionItemViewModel? _previousSession;

    /// <summary>30ms 节流定时器：所有正在生成的会话共用，冲刷各自的 pending 缓冲（§8）。</summary>
    private DispatcherTimer? _flushTimer;

    /// <summary>是否已执行退出收尾（Shutdown 的幂等标记，避免重复取消/重复停表）。</summary>
    private bool _isShutdown;

    public MainViewModel(IBackendService backend, IDialogService dialogs)
    {
        _backend = backend;
        _dialogs = dialogs;
        NewSessionCommand = new AsyncRelayCommand(NewSessionAsync);
        // 发送/停止刻意用 RelayCommand（同步返回）而非 AsyncRelayCommand：
        // CommunityToolkit.Mvvm 的 AsyncRelayCommand 默认禁止并发执行，命令执行期间 CanExecute 恒为 false，
        // 会让“生成中变为‘停止’”的按钮在生成期间被禁用、无法点击停止（离线垫片不模拟该行为，故不易察觉）。
        // 因此生成流程脱离命令执行（见 SendOrStop），按钮可用性完全由 CanSendOrStop 控制。
        SendOrStopCommand = new RelayCommand(SendOrStop, () => CanSendOrStop);
        DismissErrorCommand = new RelayCommand(() => ErrorText = "");
        // 消息增删会影响“空状态提示”的显隐
        Messages.CollectionChanged += (_, _) => OnPropertyChanged(nameof(ShowEmptyChatHint));
    }

    // —— 派生状态（XAML 直接绑定） ——

    /// <summary>是否已选择会话（未选择时禁用输入框、显示空状态引导）。</summary>
    public bool HasSession => SelectedSession != null;

    /// <summary>空状态提示：未选择会话（或会话被删光），以及已选会话还没有消息时显示。</summary>
    public bool ShowEmptyChatHint => !HasSession || Messages.Count == 0;

    /// <summary>错误条是否可见。</summary>
    public bool ShowError => !string.IsNullOrWhiteSpace(ErrorText);

    /// <summary>发送按钮可用性：生成中（此时按钮为“停止”）或草稿非空白（全空白禁用发送，§8）。</summary>
    private bool CanSendOrStop => SelectedSession is { } s && (s.IsGenerating || !string.IsNullOrWhiteSpace(s.Draft));

    // —— 命令 ——

    public AsyncRelayCommand NewSessionCommand { get; }

    /// <summary>
    /// 发送/停止按钮：生成中显示“停止”，否则“发送”。
    /// 用 <see cref="RelayCommand"/> 是因为 AsyncRelayCommand 在命令执行期间会禁用自身（见构造函数注释）。
    /// </summary>
    public RelayCommand SendOrStopCommand { get; }

    public RelayCommand DismissErrorCommand { get; }

    partial void OnSelectedSessionChanged(SessionItemViewModel? value)
    {
        // 注意：[ObservableProperty] 源生成器的 OnXxxChanged 只回调新值（value）；
        // 需要“旧会话”时用 _previousSession 自行跟踪（此处用于退订旧会话的属性订阅）
        if (_previousSession != null) _previousSession.PropertyChanged -= OnSessionPropertyChanged;
        _previousSession = value;
        if (value != null) value.PropertyChanged += OnSessionPropertyChanged;

        OnPropertyChanged(nameof(HasSession));
        OnPropertyChanged(nameof(ShowEmptyChatHint));
        SendOrStopCommand.NotifyCanExecuteChanged();

        // 切换会话 → 加载该会话的消息（异步，结果到达时若已切走则丢弃）
        if (value != null) _ = LoadMessagesAsync(value.Id);
        else Messages.Clear();
    }

    private void OnSessionPropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(SessionItemViewModel.Draft) or nameof(SessionItemViewModel.IsGenerating))
            SendOrStopCommand.NotifyCanExecuteChanged();
    }

    partial void OnErrorTextChanged(string value) => OnPropertyChanged(nameof(ShowError));

    /// <summary>启动初始化：加载会话列表并选中第一个会话。</summary>
    public async Task InitializeAsync()
    {
        await RefreshSessionsAsync();
        if (SelectedSession == null) SelectedSession = Sessions.FirstOrDefault();
    }

    // ---------------- 会话列表 ----------------

    /// <summary>
    /// 从服务端刷新会话列表：按 id 复用 VM 实例（保留草稿/生成状态），
    /// 移除已不存在的、按 updated_at 倒序重排，不整体替换集合。
    /// </summary>
    private async Task RefreshSessionsAsync()
    {
        try
        {
            var list = await _backend.ListSessionsAsync();
            var ids = new HashSet<string>(list.Select(s => s.Id));

            // 1) 移除已删除的会话
            for (int i = Sessions.Count - 1; i >= 0; i--)
            {
                if (!ids.Contains(Sessions[i].Id))
                {
                    _sessionVms.Remove(Sessions[i].Id);
                    Sessions.RemoveAt(i);
                }
            }

            // 2) 更新已有 / 创建新条目
            foreach (var info in list)
            {
                if (!_sessionVms.TryGetValue(info.Id, out var vm))
                {
                    vm = new SessionItemViewModel(this, info);
                    _sessionVms[info.Id] = vm;
                }
                vm.Apply(info);
            }

            // 3) 与后端一致：按 updated_at 倒序重排（§4.3）
            for (int i = 0; i < list.Count; i++)
            {
                var vm = _sessionVms[list[i].Id];
                int current = Sessions.IndexOf(vm);
                if (current < 0) Sessions.Insert(i, vm);
                else if (current != i) Sessions.Move(current, i);
            }
        }
        catch (Exception ex)
        {
            ErrorText = "刷新会话列表失败：" + ex.Message;
        }
    }

    /// <summary>加载指定会话的消息；若生成尚未结束，把流式占位气泡接到列表末尾。</summary>
    private async Task LoadMessagesAsync(string sessionId)
    {
        try
        {
            var list = await _backend.GetMessagesAsync(sessionId);
            if (SelectedSession?.Id != sessionId) return; // 用户已切走，丢弃过期结果

            Messages.Clear();
            foreach (var m in list) Messages.Add(new MessageItemViewModel(this, m));
            if (_liveMessages.TryGetValue(sessionId, out var live)) Messages.Add(live);
            RefreshMessageCommands();
        }
        catch (Exception ex)
        {
            if (SelectedSession?.Id == sessionId) ErrorText = "加载历史消息失败：" + ex.Message;
        }
    }

    /// <summary>按 §8 规则刷新每条消息的操作按钮状态。</summary>
    private void RefreshMessageCommands()
    {
        var session = SelectedSession;
        bool generating = session?.IsGenerating == true;
        for (int i = 0; i < Messages.Count; i++)
        {
            var m = Messages[i];
            bool isLast = i == Messages.Count - 1;
            m.IsLast = isLast;
            m.CanDelete = !generating;                                                  // 生成中禁止消息操作
            m.CanRegenerate = isLast && m.IsAssistant && !generating;                   // 仅最后一条 AI 消息可“重新生成”
            m.CanRetry = isLast && m.IsUser && session is { LastGenFailed: true } && !generating; // 最后一条 user 且上次失败 → “重试”
        }
    }

    // ---------------- 会话操作（Step 11） ----------------

    private async Task NewSessionAsync()
    {
        try
        {
            var info = await _backend.CreateSessionAsync("新会话"); // §4.3：默认名“新会话”
            await RefreshSessionsAsync();
            // 防御：极端情况下列表里没有新会话（接口返回与列表不一致）也不抛 KeyNotFoundException
            if (_sessionVms.TryGetValue(info.Id, out var createdVm)) SelectedSession = createdVm;
        }
        catch (Exception ex)
        {
            ErrorText = "新建会话失败：" + ex.Message;
        }
    }

    public async Task RenameSessionAsync(SessionItemViewModel item)
    {
        var newName = item.EditName.Trim();
        item.IsRenaming = false;
        if (newName.Length == 0 || newName == item.Name) return; // 空白/未变化：不请求后端

        try
        {
            await _backend.UpdateSessionAsync(item.Id, newName);
            await RefreshSessionsAsync(); // updated_at 变化后按最新排序重排
        }
        catch (Exception ex)
        {
            ErrorText = "重命名失败：" + ex.Message;
        }
    }

    public async Task DeleteSessionAsync(SessionItemViewModel item)
    {
        // Step 11：删除带确认框（§4.3：级联删除其消息与世界书条目）
        if (!_dialogs.Confirm($"确定删除会话“{item.Name}”吗？其全部消息与世界书条目将一并删除，且不可恢复。", "删除会话"))
            return;

        try
        {
            await _backend.DeleteSessionAsync(item.Id);
            _sessionVms.Remove(item.Id);
            Sessions.Remove(item);
            if (ReferenceEquals(SelectedSession, item))
                SelectedSession = Sessions.FirstOrDefault(); // 无会话时显示空状态
            await RefreshSessionsAsync();
            OnPropertyChanged(nameof(HasSession));
            OnPropertyChanged(nameof(ShowEmptyChatHint));
        }
        catch (Exception ex)
        {
            ErrorText = "删除会话失败：" + ex.Message;
        }
    }

    public async Task ClearHistoryAsync(SessionItemViewModel item)
    {
        // Step 11：清空历史带确认框
        if (!_dialogs.Confirm($"确定清空会话“{item.Name}”的全部历史消息吗？此操作不可恢复。", "清空历史"))
            return;

        try
        {
            await _backend.ClearMessagesAsync(item.Id);
            if (SelectedSession == item) await LoadMessagesAsync(item.Id);
            await RefreshSessionsAsync();
        }
        catch (Exception ex)
        {
            ErrorText = "清空历史失败：" + ex.Message;
        }
    }

    // ---------------- 消息操作（Step 11） ----------------

    public async Task DeleteMessageAsync(MessageItemViewModel msg)
    {
        var session = SelectedSession;
        if (session == null) return;

        int index = Messages.IndexOf(msg);
        if (index < 0) return;

        // §10 / Step 11：确认框明确“将删除此消息及其后 N 条消息”（级联删除）
        int cascade = Messages.Count - index;
        if (!_dialogs.Confirm($"将删除此消息及其后 {cascade} 条消息，确定继续吗？", "删除消息"))
            return;

        try
        {
            await _backend.DeleteMessageAndAfterAsync(session.Id, msg.Model.Id);
            await LoadMessagesAsync(session.Id);
        }
        catch (Exception ex)
        {
            ErrorText = "删除消息失败：" + ex.Message;
        }
    }

    /// <summary>
    /// “重新生成”与“重试”共用入口（§4.3 regenerate 端点本身覆盖两个分支）：
    /// 末条为 assistant → 先删该条再以现有历史生成；末条为 user（上次失败）→ 直接重试。
    /// 服务端内部同样处理删除分支，这里同步移除本地 VM 以对齐界面。
    /// </summary>
    public async Task RegenerateMessageAsync(MessageItemViewModel msg)
    {
        var session = SelectedSession;
        if (session == null || session.IsGenerating) return;

        if (msg.IsAssistant && Messages.Count > 0 && ReferenceEquals(Messages[^1], msg))
            Messages.Remove(msg);

        ErrorText = "";
        session.LastGenFailed = false;
        await RunGenerationSafeAsync(session, isRegenerate: true);
    }

    // ---------------- 发送 / 停止 / 流式消费（Step 10） ----------------

    private void SendOrStop()
    {
        var session = SelectedSession;
        if (session == null) return;

        // 生成中：按钮是“停止”
        if (session.IsGenerating)
        {
            StopGeneration(session);
            return;
        }

        var content = session.Draft?.Trim() ?? "";
        if (content.Length == 0) return; // 全空白禁用发送（CanExecute 已挡，这里双保险）

        session.Draft = "";
        ErrorText = "";
        session.LastGenFailed = false;

        // §6.1 第 2 步语义：用户消息立即上屏（服务端同时立即存库，失败也不丢）
        Messages.Add(new MessageItemViewModel(this, new ChatMessage
        {
            Id = -1, // 占位 id：生成结束后 LoadMessagesAsync 会用服务端真实数据重建列表
            SessionId = session.Id,
            Role = MessageRole.User,
            Content = content,
            CreatedAt = DateTimeOffset.UtcNow,
        }));
        RefreshMessageCommands();

        // 生成流程不 await：命令立即返回，按钮在生成期间保持可点击（文案=“停止”），见构造函数注释
        _ = RunGenerationSafeAsync(session, isRegenerate: false, userContent: content);
    }

    /// <summary>
    /// 生成入口的异常兜底：RunGenerationAsync 内部已按 §10 处理业务异常，
    /// 这里再包一层，避免 fire-and-forget 任务在极端路径（如 finally 内的意外异常）
    /// 变成未观察异常，或让 AsyncRelayCommand 把异常抛回 UI 线程导致崩溃。
    /// </summary>
    private async Task RunGenerationSafeAsync(SessionItemViewModel session, bool isRegenerate, string? userContent = null)
    {
        try
        {
            await RunGenerationAsync(session, isRegenerate, userContent);
        }
        catch (Exception ex)
        {
            session.IsGenerating = false;
            ErrorText = "生成异常：" + ex.Message;
        }
    }

    /// <summary>发起一次生成（发送或重新生成），消费流式事件直到 done/error/取消。</summary>
    private async Task RunGenerationAsync(SessionItemViewModel session, bool isRegenerate, string? userContent = null)
    {
        session.IsGenerating = true;
        RefreshMessageCommands();

        // 流式占位气泡：进入生成即出现在列表末尾，随 delta 增长；切走再切回也能看到
        var live = new MessageItemViewModel(this, new ChatMessage
        {
            Id = -1,
            SessionId = session.Id,
            Role = MessageRole.Assistant,
            Content = "",
            CreatedAt = DateTimeOffset.UtcNow,
        });
        _liveMessages[session.Id] = live;
        if (SelectedSession == session) Messages.Add(live);

        var cts = new CancellationTokenSource();
        _generationCts[session.Id] = cts;
        try
        {
            IAsyncEnumerable<ChatEvent> events = isRegenerate
                ? _backend.RegenerateAsync(session.Id, cts.Token)
                : _backend.SendMessageAsync(session.Id, userContent ?? "", cts.Token);

            await foreach (var ev in events)
                HandleEvent(ev, live, session);
        }
        catch (OperationCanceledException) when (cts.IsCancellationRequested)
        {
            // 用户点击“停止”：保留已生成部分并标注；服务端 finally 已把部分文本落库 interrupted=1。
            // 先把 30ms 缓冲里未冲刷的增量写入 Content，避免随后重拉历史失败时丢掉尾部文本。
            FlushAllPending();
            if (!string.IsNullOrEmpty(live.Content)) live.Interrupted = true;
        }
        catch (Exception ex)
        {
            FlushAllPending();
            ErrorText = "生成异常（后端不可达/网络错误）：" + ex.Message;
            session.LastGenFailed = true;
            // §10：生成中网络断开 → 保留已收部分并标注
            if (!string.IsNullOrEmpty(live.Content)) live.Interrupted = true;
        }
        finally
        {
            session.IsGenerating = false;
            cts.Dispose();
            _generationCts.Remove(session.Id);
            _liveMessages.Remove(session.Id);
            // 重新拉取消息，与服务端（含 interrupted 落库 / 用户消息 id）保持一致；
            // 若用户已切到别的会话，则跳过（切回时自然会加载最新数据）
            if (SelectedSession == session) await LoadMessagesAsync(session.Id);
            await RefreshSessionsAsync(); // 更新消息数与列表排序
            RefreshMessageCommands();
        }
    }

    /// <summary>处理单个流式事件（§4.2：meta → delta* → done | error）。</summary>
    private void HandleEvent(ChatEvent ev, MessageItemViewModel live, SessionItemViewModel session)
    {
        switch (ev.Type)
        {
            case ChatEventType.Meta:
                live.SetMatchedEntries(ev.MatchedEntries); // 命中提示立即显示（meta 必发）
                break;

            case ChatEventType.Delta:
                AppendDelta(live, ev.DeltaText); // 累积到 pending，30ms 定时器统一冲刷
                break;

            case ChatEventType.Done:
                FlushAllPending();
                // 以完整文本兜底（delta 累积结果与之等价，这里防丢失）
                if (!string.IsNullOrEmpty(ev.FullText)) live.Content = ev.FullText;
                session.LastGenFailed = false;
                break;

            case ChatEventType.Error:
                session.LastGenFailed = true;
                ErrorText = "生成失败：" + ev.ErrorMessage;
                // §6.1：失败不存库 AI 回复（interrupted 除外）——移除占位气泡，用户消息保留
                if (SelectedSession == session) Messages.Remove(live);
                break;

            default:
                break; // 未知事件类型忽略（§4.2 向前兼容）
        }
    }

    private void AppendDelta(MessageItemViewModel live, string text)
    {
        live.AppendDelta(text);
        // 已退出收尾（窗口已关闭）：不再创建/启动定时器。此时迟到的 delta 只进缓冲，
        // 不再驱动 UI 刷新，避免退出后仍有 DispatcherTimer 在跑（正式后端下也不会再产生 UI 工作）。
        if (_isShutdown) return;
        if (_flushTimer == null)
        {
            // 30ms 节流（§8）：多个会话的 delta 各攒各的缓冲，共享一个 UI 定时器合并刷新
            _flushTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(30) };
            _flushTimer.Tick += OnFlushTimerTick;
        }
        if (!_flushTimer.IsEnabled) _flushTimer.Start();
    }

    /// <summary>节流定时器 Tick：统一冲刷各会话的 pending 缓冲。</summary>
    private void OnFlushTimerTick(object? sender, EventArgs e) => FlushAllPending();

    /// <summary>把所有会话 pending 缓冲一次性写入 Content；全部清空后停表省资源。</summary>
    private void FlushAllPending()
    {
        bool any = false;
        foreach (var live in _liveMessages.Values)
        {
            if (live.PendingLength > 0)
            {
                live.FlushPending();
                any = true;
            }
        }
        if (!any) _flushTimer?.Stop();
    }

    /// <summary>
    /// “停止生成”= 取消本次请求（§4.2 / §7 停止语义）：
    /// 后端感知断开后中止上游请求，并把已生成部分以 interrupted=1 落库，前端保留部分文本。
    /// </summary>
    private void StopGeneration(SessionItemViewModel session)
    {
        if (_generationCts.TryGetValue(session.Id, out var cts)) cts.Cancel();
    }

    /// <summary>
    /// 应用退出收尾（幂等、不抛异常）：逐个取消所有进行中的生成，停止并解绑 30ms 节流定时器。
    /// 由 MainWindow.Closed 调用；必须在 UI 线程执行（Closed 即 UI 线程，DispatcherTimer 只能在该线程停表）。
    /// 刻意保持同步、不 await 任何生成任务：退出路径等待异步流程才容易死锁；取消后各生成流程
    /// 在自己的 catch/finally 里收尾（保留部分文本、刷新列表），即使窗口已关闭也不会阻塞退出。
    /// </summary>
    public void Shutdown()
    {
        if (_isShutdown) return; // 幂等：Closed 与其它退出路径重复调用直接返回（也避免取消回调重入）
        _isShutdown = true;

        // 1) 逐个取消各会话的 CTS（不同会话可并行生成，故每个会话各有一个令牌源）。
        //    先取快照再 Cancel：Cancel 会同步触发已注册的取消回调，避免枚举期间字典被改动。
        foreach (var cts in _generationCts.Values.ToArray())
        {
            try
            {
                cts.Cancel();
            }
            catch (Exception)
            {
                // ObjectDisposedException（生成恰在此刻结束并释放）或取消回调抛出的 AggregateException：
                // 退出路径吞掉，保证窗口正常关闭；不改变任何面向用户的行为。
            }
        }

        // 2) 停表、解绑 Tick 并置空引用：Shutdown 之后 AppendDelta 会因 _isShutdown 短路，不再重新建表。
        var timer = _flushTimer;
        _flushTimer = null;
        if (timer == null) return;
        try
        {
            timer.Stop();
        }
        catch (Exception)
        {
            // 理论上仅当非 UI 线程调用时抛（DispatcherTimer.Stop 要求同线程）；退出路径忽略。
        }
        timer.Tick -= OnFlushTimerTick;
    }
}
