// 【离线验证垫片 verify-shim/ —— 仅 UseLocalMvvmShim=true 时参与编译】
// 手写模拟 CommunityToolkit.Mvvm 源生成器产物：为 MainViewModel 的
// [ObservableProperty] 字段生成属性与 partial 方法声明（联网构建使用真实包）。
namespace MyAgent.App.ViewModels;

public partial class MainViewModel
{
    public SessionItemViewModel? SelectedSession
    {
        get => _selectedSession;
        set
        {
            if (!EqualityComparer<SessionItemViewModel?>.Default.Equals(_selectedSession, value))
            {
                _selectedSession = value;
                OnPropertyChanged();
                OnSelectedSessionChanged(value);
            }
        }
    }

    public string ErrorText
    {
        get => _errorText;
        set
        {
            if (!EqualityComparer<string>.Default.Equals(_errorText, value))
            {
                _errorText = value;
                OnPropertyChanged();
                OnErrorTextChanged(value);
            }
        }
    }

    partial void OnSelectedSessionChanged(SessionItemViewModel? value);
    partial void OnErrorTextChanged(string value);
}
