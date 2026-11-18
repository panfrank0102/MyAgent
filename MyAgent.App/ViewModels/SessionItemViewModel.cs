using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;

namespace MyAgent.App.ViewModels;

/// <summary>
/// 会话列表条目 VM：包装 SessionInfo，附加列表交互状态——
/// 输入草稿（按会话保留，切回时恢复）、生成中标记、上次生成是否失败、内联重命名状态。
/// 业务动作（改名/删除/清空）委托给父级 MainViewModel，这里只承载展示状态与命令转发。
/// </summary>
public partial class SessionItemViewModel : ObservableObject
{
    private readonly MainViewModel _owner;

    /// <summary>会话 id（稳定键，列表刷新时按 id 复用本实例以保留草稿等 UI 状态）。</summary>
    public string Id { get; }

    [ObservableProperty]
    private string _name;

    [ObservableProperty]
    private DateTimeOffset _updatedAt;

    [ObservableProperty]
    private int _messageCount;

    /// <summary>本会话是否正在生成（同会话同时仅一个生成；不同会话可并行，§8）。</summary>
    [ObservableProperty]
    private bool _isGenerating;

    /// <summary>输入框草稿：按会话保存，切换会话后恢复各自未发送的内容。</summary>
    [ObservableProperty]
    private string _draft = "";

    /// <summary>是否处于内联重命名状态（列表项内显示编辑框）。</summary>
    [ObservableProperty]
    private bool _isRenaming;

    /// <summary>重命名编辑框的临时文本。</summary>
    [ObservableProperty]
    private string _editName = "";

    /// <summary>上次生成是否失败：决定最后一条 user 消息是否显示“重试”按钮（§8 / Step 11）。</summary>
    [ObservableProperty]
    private bool _lastGenFailed;

    /// <summary>更新时间（本地时区展示，存储侧为 UTC ISO8601，前端负责本地化，§5.1）。</summary>
    public string TimeDisplay => UpdatedAt.ToLocalTime().ToString("MM-dd HH:mm");

    /// <summary>消息条数展示。</summary>
    public string CountDisplay => $"{MessageCount} 条消息";

    public SessionItemViewModel(MainViewModel owner, SessionInfo info)
    {
        _owner = owner;
        Id = info.Id;
        _name = info.Name;
        _updatedAt = info.UpdatedAt;
        _messageCount = info.MessageCount;

        // 进入内联重命名：把当前名复制到编辑框
        StartRenameCommand = new RelayCommand(() => { EditName = Name; IsRenaming = true; });
        // Enter / 失焦提交（校验在父级：空白名不生效）
        CommitRenameCommand = new AsyncRelayCommand(() => _owner.RenameSessionAsync(this));
        CancelRenameCommand = new RelayCommand(() => IsRenaming = false);
        // 生成中禁止删除/清空，避免与后端的生成流程冲突
        DeleteCommand = new AsyncRelayCommand(() => _owner.DeleteSessionAsync(this), () => !IsGenerating);
        ClearCommand = new AsyncRelayCommand(() => _owner.ClearHistoryAsync(this), () => !IsGenerating);
    }

    public RelayCommand StartRenameCommand { get; }
    public AsyncRelayCommand CommitRenameCommand { get; }
    public RelayCommand CancelRenameCommand { get; }
    public AsyncRelayCommand DeleteCommand { get; }
    public AsyncRelayCommand ClearCommand { get; }

    partial void OnUpdatedAtChanged(DateTimeOffset value) => OnPropertyChanged(nameof(TimeDisplay));
    partial void OnMessageCountChanged(int value) => OnPropertyChanged(nameof(CountDisplay));
    partial void OnIsGeneratingChanged(bool value)
    {
        DeleteCommand.NotifyCanExecuteChanged();
        ClearCommand.NotifyCanExecuteChanged();
    }

    /// <summary>
    /// 用服务端最新数据刷新展示字段；不触碰 Draft / IsGenerating / LastGenFailed 等 UI 状态，
    /// 保证列表刷新（排序/计数更新）时用户未发送的草稿与生成过程不被打断。
    /// </summary>
    internal void Apply(SessionInfo info)
    {
        Name = info.Name;
        UpdatedAt = info.UpdatedAt;
        MessageCount = info.MessageCount;
    }
}
