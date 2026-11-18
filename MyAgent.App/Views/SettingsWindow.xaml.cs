using System.Windows;
using MyAgent.App.Services;
using MyAgent.App.ViewModels;

namespace MyAgent.App.Views;

/// <summary>
/// 会话设置窗口（Step 12 / v0.6 改造）代码后置：只承担“视图层”职责——
///  1. 窗口装配：接收 IBackendService + 当前会话 Id，创建 SettingsViewModel；
///  2. 打开全局设置窗口（“管理模型预设…”按钮），关闭后刷新模型预设下拉；
///  3. 打开世界书编辑器窗口（每会话，传入同一 backend 与 sessionId），以及关闭按钮。
/// 配置读写/校验/模型预设选择等业务逻辑全部在 SettingsViewModel。
/// v0.6：会话不再有 Base URL / API Key / 模型名 / 推理强度与“测试连接”，故 PasswordBox 桥接一并移除。
/// </summary>
public partial class SettingsWindow : Window
{
    private readonly IBackendService _backend;
    private readonly string _sessionId;
    private readonly SettingsViewModel _viewModel;

    public SettingsWindow(IBackendService backend, string sessionId)
    {
        InitializeComponent();

        _backend = backend;
        _sessionId = sessionId;
        _viewModel = new SettingsViewModel(backend, sessionId);
        DataContext = _viewModel;
    }

    /// <summary>是否成功保存过：主窗口在窗口关闭后据此刷新会话列表/名。</summary>
    public bool ConfigSaved => _viewModel.ConfigSaved;

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        await _viewModel.LoadAsync(); // 打开时先拉预设下拉、再拉会话配置；失败显示错误条
    }

    /// <summary>
    /// “管理模型预设…”：打开全局设置窗口（模态，Owner=this）；关闭后刷新本页预设下拉，
    /// 使新建/改名/删除的预设立即反映到下拉中（保留当前选择）。
    /// </summary>
    private async void ManageModelPresets_Click(object sender, RoutedEventArgs e)
    {
        var window = new GlobalSettingsWindow(_backend) { Owner = this };
        window.ShowDialog();
        await _viewModel.ReloadModelPresetsAsync();
    }

    private void OpenWorldBook_Click(object sender, RoutedEventArgs e)
    {
        // 世界书每会话独立：只传当前 sessionId 与 backend，UI 不跨会话共享状态
        var window = new WorldBookWindow(_backend, _sessionId) { Owner = this };
        window.ShowDialog();
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
