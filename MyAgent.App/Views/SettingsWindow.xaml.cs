using System.Windows;
using MyAgent.App.Services;
using MyAgent.App.ViewModels;

namespace MyAgent.App.Views;

/// <summary>
/// 会话设置窗口（Step 12）代码后置：只承担“视图层”职责——
///  1. 窗口装配：接收 IBackendService + 当前会话 Id，创建 SettingsViewModel；
///  2. PasswordBox 桥接：WPF 的 PasswordBox.Password 不可绑定，这里在打开/保存回显时同步打码值，
///     并把用户输入转交 VM（未修改时不发送 api_key）；
///  3. 打开世界书编辑器窗口（每会话，传入同一 backend 与 sessionId），以及关闭按钮。
/// 配置读写/校验/测试连接等业务逻辑全部在 SettingsViewModel。
/// </summary>
public partial class SettingsWindow : Window
{
    private readonly IBackendService _backend;
    private readonly string _sessionId;
    private readonly SettingsViewModel _viewModel;

    /// <summary>正在把打码值写回 PasswordBox：抑制 PasswordChanged，避免被当成用户输入。</summary>
    private bool _syncingPassword;

    public SettingsWindow(IBackendService backend, string sessionId)
    {
        InitializeComponent();

        _backend = backend;
        _sessionId = sessionId;
        _viewModel = new SettingsViewModel(backend, sessionId);
        _viewModel.MaskedApiKeyChanged += (_, _) => SyncApiKeyBox();
        DataContext = _viewModel;
    }

    /// <summary>是否成功保存过：主窗口在窗口关闭后据此刷新会话列表/名。</summary>
    public bool ConfigSaved => _viewModel.ConfigSaved;

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        await _viewModel.LoadAsync(); // 打开时拉取最新配置；失败显示错误条
        SyncApiKeyBox();
    }

    private void SyncApiKeyBox()
    {
        _syncingPassword = true;
        try
        {
            // 回显打码值（或保存后服务端返回的新打码值）
            ApiKeyBox.Password = _viewModel.MaskedApiKey;
        }
        finally
        {
            _syncingPassword = false;
        }
    }

    private void ApiKeyBox_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingPassword) return;
        _viewModel.SetApiKeyInputFromView(ApiKeyBox.Password);
    }

    private void OpenWorldBook_Click(object sender, RoutedEventArgs e)
    {
        // 世界书每会话独立：只传当前 sessionId 与 backend，UI 不跨会话共享状态
        var window = new WorldBookWindow(_backend, _sessionId) { Owner = this };
        window.ShowDialog();
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
