using System.Windows;

namespace MyAgent.App.Services;

/// <summary>
/// 确认/提示对话框抽象：让 ViewModel 不直接依赖 MessageBox，便于测试与替换。
/// 所有“带确认框”的破坏性操作（删除会话、清空历史、删除消息）都经由这里（§8 / Step 11），
/// 确认文案由调用方给出（如“将删除此消息及其后 N 条消息”）。
/// </summary>
public interface IDialogService
{
    /// <summary>确认框（是/否），返回用户是否确认。</summary>
    bool Confirm(string message, string title);

    /// <summary>信息提示框。</summary>
    void Info(string message, string title);
}

/// <summary>
/// MessageBox 实现：仅承载“弹框”这一 UI 职责，不包含业务判断。
/// 按钮文案由操作系统本地化（中文系统显示“是/否”）。
/// </summary>
public sealed class MessageBoxDialogService : IDialogService
{
    public bool Confirm(string message, string title)
        => MessageBox.Show(
            Application.Current?.MainWindow,
            message,
            title,
            MessageBoxButton.YesNo,
            MessageBoxImage.Question) == MessageBoxResult.Yes;

    public void Info(string message, string title)
        => MessageBox.Show(
            Application.Current?.MainWindow,
            message,
            title,
            MessageBoxButton.OK,
            MessageBoxImage.Information);
}
