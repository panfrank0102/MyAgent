using System.Windows;
using MyAgent.App.Services;
using MyAgent.App.ViewModels;

namespace MyAgent.App.Views;

/// <summary>
/// 全局设置窗口（v0.6 / §8）代码后置：只承担“视图层”职责——
///  1. 窗口装配：接收 IBackendService（+ 可替换的 IDialogService），创建 GlobalSettingsViewModel；
///  2. PasswordBox 桥接：WPF 的 PasswordBox.Password 不可绑定，这里在打开/切换预设时同步打码值，
///     并把用户输入转交 VM（打码值不会被当作明文提交）；
///  3. 关闭按钮。
/// 预设增删改查/测试连接等业务逻辑全部在 GlobalSettingsViewModel。
/// </summary>
public partial class GlobalSettingsWindow : Window
{
    private readonly GlobalSettingsViewModel _viewModel;

    /// <summary>正在把打码值写回 PasswordBox：抑制 PasswordChanged，避免被当成用户输入。</summary>
    private bool _syncingPassword;

    /// <summary>便捷构造：对话框使用默认 MessageBox 实现（主窗口/会话设置窗口直接使用）。</summary>
    public GlobalSettingsWindow(IBackendService backend)
        : this(backend, new MessageBoxDialogService())
    {
    }

    public GlobalSettingsWindow(IBackendService backend, IDialogService dialogs)
    {
        InitializeComponent();

        _viewModel = new GlobalSettingsViewModel(backend, dialogs);
        _viewModel.MaskedApiKeyChanged += (_, _) => SyncApiKeyBox();
        DataContext = _viewModel;
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        await _viewModel.LoadAsync(); // 打开时拉取预设列表；失败显示错误条
        SyncApiKeyBox();
    }

    private void SyncApiKeyBox()
    {
        _syncingPassword = true;
        try
        {
            // 回显打码值（切换预设/保存后服务端返回的新打码值）
            PresetApiKeyBox.Password = _viewModel.MaskedApiKey;
        }
        finally
        {
            _syncingPassword = false;
        }
    }

    private void PresetApiKeyBox_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingPassword) return;
        _viewModel.SetApiKeyInputFromView(PresetApiKeyBox.Password);
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
