using System.Text;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;

namespace MyAgent.App.ViewModels;

/// <summary>
/// 聊天消息条目 VM：包装 ChatMessage，附加渲染派生状态与消息级命令
/// （删除 / 重新生成 / 重试），并承载流式增量的 30ms 节流缓冲。
///
/// 流式节流设计（§8）：delta 到达频率远高于 UI 刷新需求，因此先把增量文本累积到
/// PendingText，由 MainViewModel 的共享 DispatcherTimer（30ms）统一冲刷进 Content，
/// 每次冲刷只触发一次属性变更，从而把高频 delta 合并为低频 UI 刷新。
/// </summary>
public partial class MessageItemViewModel : ObservableObject
{
    private readonly MainViewModel _owner;

    /// <summary>流式 delta 暂存缓冲（由 MainViewModel 的 30ms 定时器统一冲刷）。</summary>
    internal readonly StringBuilder PendingText = new();

    /// <summary>底层数据模型（含服务端 id，用于级联删除等操作）。</summary>
    public ChatMessage Model { get; }

    /// <summary>消息正文（AI 消息流式增长时实时更新）。</summary>
    [ObservableProperty]
    private string _content;

    /// <summary>是否被手动停止（尾部标注“（已停止）”）。</summary>
    [ObservableProperty]
    private bool _interrupted;

    /// <summary>是否为列表最后一条（决定“重新生成/重试”按钮是否出现）。</summary>
    [ObservableProperty]
    private bool _isLast;

    [ObservableProperty]
    private bool _canDelete;

    [ObservableProperty]
    private bool _canRegenerate;

    [ObservableProperty]
    private bool _canRetry;

    /// <summary>本轮命中的世界书条目（来自 meta 事件）。</summary>
    [ObservableProperty]
    private IReadOnlyList<MatchedEntry> _matchedEntries = Array.Empty<MatchedEntry>();

    /// <summary>“世界书命中：…”小字文本；无命中为 null（XAML 据此隐藏）。</summary>
    [ObservableProperty]
    private string? _hitText;

    public MessageItemViewModel(MainViewModel owner, ChatMessage model)
    {
        _owner = owner;
        Model = model;
        _content = model.Content;
        _interrupted = model.Interrupted;
        _matchedEntries = model.MatchedEntries;
        _hitText = BuildHitText(model.MatchedEntries);

        // 消息级命令全部委托父级执行（父级负责确认框与服务调用）
        DeleteCommand = new AsyncRelayCommand(() => _owner.DeleteMessageAsync(this), () => CanDelete);
        RegenerateCommand = new AsyncRelayCommand(() => _owner.RegenerateMessageAsync(this), () => CanRegenerate);
        RetryCommand = new AsyncRelayCommand(() => _owner.RegenerateMessageAsync(this), () => CanRetry);
    }

    public MessageRole Role => Model.Role;
    public bool IsUser => Model.Role == MessageRole.User;
    public bool IsAssistant => Model.Role == MessageRole.Assistant;

    /// <summary>展示文本：interrupted 消息尾部标注“（已停止）”（§4.2 / §8）。</summary>
    public string DisplayText => Interrupted ? Content + "（已停止）" : Content;

    public AsyncRelayCommand DeleteCommand { get; }
    public AsyncRelayCommand RegenerateCommand { get; }
    public AsyncRelayCommand RetryCommand { get; }

    partial void OnContentChanged(string value) => OnPropertyChanged(nameof(DisplayText));
    partial void OnInterruptedChanged(bool value) => OnPropertyChanged(nameof(DisplayText));
    partial void OnMatchedEntriesChanged(IReadOnlyList<MatchedEntry> value) => HitText = BuildHitText(value);
    partial void OnCanDeleteChanged(bool value) => DeleteCommand.NotifyCanExecuteChanged();
    partial void OnCanRegenerateChanged(bool value) => RegenerateCommand.NotifyCanExecuteChanged();
    partial void OnCanRetryChanged(bool value) => RetryCommand.NotifyCanExecuteChanged();

    // ---- 流式节流缓冲 ----

    internal int PendingLength => PendingText.Length;

    internal void AppendDelta(string text) => PendingText.Append(text);

    /// <summary>把 30ms 内累积的增量一次性写入 Content（只触发一次属性变更）。</summary>
    internal void FlushPending()
    {
        if (PendingText.Length == 0) return;
        Content += PendingText.ToString();
        PendingText.Clear();
    }

    internal void SetMatchedEntries(IReadOnlyList<MatchedEntry> entries) => MatchedEntries = entries;

    /// <summary>
    /// 格式化命中小字（§8）：世界书命中：条目A（关键词：xx、yy）、条目B（关键词：zz）…
    /// 无命中返回 null，XAML 用 DataTrigger 隐藏整行。
    /// </summary>
    private static string? BuildHitText(IReadOnlyList<MatchedEntry> entries)
        => entries.Count == 0
            ? null
            : "世界书命中：" + string.Join("、", entries.Select(e => $"{e.Name}（关键词：{string.Join("、", e.HitKeywords)}）"));
}
