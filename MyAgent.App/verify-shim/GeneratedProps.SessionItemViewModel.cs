// 【离线验证垫片 verify-shim/ —— 仅 UseLocalMvvmShim=true 时参与编译】
// 手写模拟 CommunityToolkit.Mvvm 源生成器产物：为 SessionItemViewModel 的
// [ObservableProperty] 字段生成属性与 partial 方法声明（联网构建使用真实包）。
using MyAgent.App.Models;

namespace MyAgent.App.ViewModels;

public partial class SessionItemViewModel
{
    public string Name
    {
        get => _name;
        set
        {
            if (!EqualityComparer<string>.Default.Equals(_name, value))
            {
                _name = value;
                OnPropertyChanged();
                OnNameChanged(value);
            }
        }
    }

    public DateTimeOffset UpdatedAt
    {
        get => _updatedAt;
        set
        {
            if (!EqualityComparer<DateTimeOffset>.Default.Equals(_updatedAt, value))
            {
                _updatedAt = value;
                OnPropertyChanged();
                OnUpdatedAtChanged(value);
            }
        }
    }

    public int MessageCount
    {
        get => _messageCount;
        set
        {
            if (!EqualityComparer<int>.Default.Equals(_messageCount, value))
            {
                _messageCount = value;
                OnPropertyChanged();
                OnMessageCountChanged(value);
            }
        }
    }

    public bool IsGenerating
    {
        get => _isGenerating;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_isGenerating, value))
            {
                _isGenerating = value;
                OnPropertyChanged();
                OnIsGeneratingChanged(value);
            }
        }
    }

    public string Draft
    {
        get => _draft;
        set
        {
            if (!EqualityComparer<string>.Default.Equals(_draft, value))
            {
                _draft = value;
                OnPropertyChanged();
                OnDraftChanged(value);
            }
        }
    }

    public bool IsRenaming
    {
        get => _isRenaming;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_isRenaming, value))
            {
                _isRenaming = value;
                OnPropertyChanged();
                OnIsRenamingChanged(value);
            }
        }
    }

    public string EditName
    {
        get => _editName;
        set
        {
            if (!EqualityComparer<string>.Default.Equals(_editName, value))
            {
                _editName = value;
                OnPropertyChanged();
                OnEditNameChanged(value);
            }
        }
    }

    public bool LastGenFailed
    {
        get => _lastGenFailed;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_lastGenFailed, value))
            {
                _lastGenFailed = value;
                OnPropertyChanged();
                OnLastGenFailedChanged(value);
            }
        }
    }

    partial void OnNameChanged(string value);
    partial void OnUpdatedAtChanged(DateTimeOffset value);
    partial void OnMessageCountChanged(int value);
    partial void OnIsGeneratingChanged(bool value);
    partial void OnDraftChanged(string value);
    partial void OnIsRenamingChanged(bool value);
    partial void OnEditNameChanged(string value);
    partial void OnLastGenFailedChanged(bool value);
}
