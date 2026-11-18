using System.Collections.Specialized;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using MyAgent.App.Services;
using MyAgent.App.ViewModels;

namespace MyAgent.App;

/// <summary>
/// 主窗口代码后置：只承担“视图层”职责——
///  1. 组合根：装配 FakeBackendService + MessageBoxDialogService（集成阶段换成 ApiClient/BackendManager）；
///  2. 输入手势：Enter 发送 / Shift+Enter 换行（业务判断在 MainViewModel.SendOrStopCommand）；
///  3. 聊天区自动滚底：内容增长时贴底跟随，用户上滚历史后暂停跟随。
/// 全部业务逻辑位于 MainViewModel（MVVM 分层要求：后置文件不放业务逻辑）。
/// </summary>
public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel;

    /// <summary>是否贴底跟随：用户上滚查看历史时暂停自动滚底，滚回底部后恢复。</summary>
    private bool _stickToBottom = true;

    public MainWindow()
    {
        InitializeComponent();

        // 组合根：假数据阶段注入 FakeBackendService；集成阶段替换为 ApiClient（配合 BackendManager）
        _viewModel = new MainViewModel(new FakeBackendService(), new MessageBoxDialogService());
        DataContext = _viewModel;

        // 消息集合变化（增删/清空/重建）后滚动到底部。
        // Messages 实例不会整体替换（见 MainViewModel 注释），订阅一次即可。
        _viewModel.Messages.CollectionChanged += OnMessagesCollectionChanged;
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        await _viewModel.InitializeAsync(); // 加载假数据会话并选中第一个
    }

    private void OnMessagesCollectionChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        // 集合变化时新元素尚未完成布局，推迟到布局优先级再滚动
        Dispatcher.BeginInvoke(ScrollToEndIfSticky, DispatcherPriority.Background);
    }

    private void ScrollToEndIfSticky()
    {
        if (_stickToBottom) MessageScroll.ScrollToEnd();
    }

    private void MessageScroll_ScrollChanged(object sender, ScrollChangedEventArgs e)
    {
        const double threshold = 24; // 距底部 24px 内视为“在底部”
        // 内容增长（新消息/流式增量）：原本贴底则保持跟随
        if (e.ExtentHeightChange > 0 && _stickToBottom)
        {
            MessageScroll.ScrollToEnd();
            return;
        }
        // 记录当前位置是否在底部：主动上滚历史 → 暂停跟随；滚回底部 → 恢复跟随
        _stickToBottom = MessageScroll.VerticalOffset >= MessageScroll.ScrollableHeight - threshold;
    }

    private void InputBox_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        // Enter 发送；Shift+Enter 走 TextBox 默认换行（AcceptsReturn=True）。
        // 中文输入法组合态下回车键是 Key.ImeProcessed，不会误触发发送。
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.None)
        {
            e.Handled = true; // 阻止 Enter 在输入框内产生换行
            if (_viewModel.SendOrStopCommand.CanExecute(null))
                _viewModel.SendOrStopCommand.Execute(null);
        }
    }
}
