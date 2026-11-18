using System.Windows;
using MyAgent.App.Services;
using MyAgent.App.ViewModels;

namespace MyAgent.App.Views;

/// <summary>
/// 世界书编辑器窗口（Step 13，每会话独立）代码后置：只承担“视图层”职责——
///  1. 窗口装配：接收 IBackendService + 当前会话 sessionId，创建 WorldBookViewModel，
///     删除确认等对话框使用 MessageBoxDialogService（§8 / Step 13）；
///  2. 关键词文本框 LostFocus 时触发规范化（trim / 去空行 / 去重）回写，用户可见清洗结果；
///  3. 关闭按钮。
/// 世界书增删改查/排序/启停等业务逻辑全部在 WorldBookViewModel。
/// </summary>
public partial class WorldBookWindow : Window
{
    private readonly WorldBookViewModel _viewModel;

    public WorldBookWindow(IBackendService backend, string sessionId)
    {
        InitializeComponent();

        // 确认删除等交互统一走 MessageBoxDialogService（测试可替换 IDialogService）
        _viewModel = new WorldBookViewModel(backend, sessionId, new MessageBoxDialogService());
        DataContext = _viewModel;
    }

    /// <summary>当前会话 id（每会话独立世界书）。</summary>
    public string SessionId => _viewModel.SessionId;

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        await _viewModel.LoadAsync(); // 打开时 ListWorldbookAsync；失败显示错误条
    }

    private void KeywordsBox_LostFocus(object sender, RoutedEventArgs e)
    {
        // 点击“保存条目”时会先触发 LostFocus：先把关键词清洗结果回写，再保存。
        _viewModel.NormalizeKeywordsText();
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
