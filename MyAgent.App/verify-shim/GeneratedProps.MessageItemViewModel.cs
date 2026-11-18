// 【离线验证垫片 verify-shim/ —— 仅 UseLocalMvvmShim=true 时参与编译】
// 手写模拟 CommunityToolkit.Mvvm 源生成器产物：为 MessageItemViewModel 的
// [ObservableProperty] 字段生成属性与 partial 方法声明（联网构建使用真实包）。
using MyAgent.App.Models;

namespace MyAgent.App.ViewModels;

public partial class MessageItemViewModel
{
    public string Content
    {
        get => _content;
        set
        {
            if (!EqualityComparer<string>.Default.Equals(_content, value))
            {
                _content = value;
                OnPropertyChanged();
                OnContentChanged(value);
            }
        }
    }

    public bool Interrupted
    {
        get => _interrupted;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_interrupted, value))
            {
                _interrupted = value;
                OnPropertyChanged();
                OnInterruptedChanged(value);
            }
        }
    }

    public bool IsLast
    {
        get => _isLast;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_isLast, value))
            {
                _isLast = value;
                OnPropertyChanged();
                OnIsLastChanged(value);
            }
        }
    }

    public bool CanDelete
    {
        get => _canDelete;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_canDelete, value))
            {
                _canDelete = value;
                OnPropertyChanged();
                OnCanDeleteChanged(value);
            }
        }
    }

    public bool CanRegenerate
    {
        get => _canRegenerate;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_canRegenerate, value))
            {
                _canRegenerate = value;
                OnPropertyChanged();
                OnCanRegenerateChanged(value);
            }
        }
    }

    public bool CanRetry
    {
        get => _canRetry;
        set
        {
            if (!EqualityComparer<bool>.Default.Equals(_canRetry, value))
            {
                _canRetry = value;
                OnPropertyChanged();
                OnCanRetryChanged(value);
            }
        }
    }

    public IReadOnlyList<MatchedEntry> MatchedEntries
    {
        get => _matchedEntries;
        set
        {
            if (!EqualityComparer<IReadOnlyList<MatchedEntry>>.Default.Equals(_matchedEntries, value))
            {
                _matchedEntries = value;
                OnPropertyChanged();
                OnMatchedEntriesChanged(value);
            }
        }
    }

    public string? HitText
    {
        get => _hitText;
        set
        {
            if (!EqualityComparer<string?>.Default.Equals(_hitText, value))
            {
                _hitText = value;
                OnPropertyChanged();
                OnHitTextChanged(value);
            }
        }
    }

    partial void OnContentChanged(string value);
    partial void OnInterruptedChanged(bool value);
    partial void OnIsLastChanged(bool value);
    partial void OnCanDeleteChanged(bool value);
    partial void OnCanRegenerateChanged(bool value);
    partial void OnCanRetryChanged(bool value);
    partial void OnMatchedEntriesChanged(IReadOnlyList<MatchedEntry> value);
    partial void OnHitTextChanged(string? value);
}
