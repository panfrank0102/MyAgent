using System.Windows;
using System.Windows.Controls;

namespace MyAgent.App.Views;

/// <summary>
/// 轻量视图层辅助行为：附加属性 FocusOnVisible=true 时，元素变为可见自动获得焦点
/// （TextBox 额外全选文本）。用于会话列表的内联重命名编辑框：点“改名”后编辑框直接可输入，
/// 无需用户再点一次。只做视图级交互，不含业务逻辑。
/// </summary>
public static class FocusBehavior
{
    public static readonly DependencyProperty FocusOnVisibleProperty =
        DependencyProperty.RegisterAttached(
            "FocusOnVisible",
            typeof(bool),
            typeof(FocusBehavior),
            new PropertyMetadata(false, OnFocusOnVisibleChanged));

    public static void SetFocusOnVisible(DependencyObject element, bool value)
        => element.SetValue(FocusOnVisibleProperty, value);

    public static bool GetFocusOnVisible(DependencyObject element)
        => (bool)element.GetValue(FocusOnVisibleProperty);

    private static void OnFocusOnVisibleChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not UIElement element) return;
        element.IsVisibleChanged -= OnVisibleChanged; // 防重复订阅
        if (e.NewValue is true) element.IsVisibleChanged += OnVisibleChanged;
    }

    private static void OnVisibleChanged(object sender, DependencyPropertyChangedEventArgs e)
    {
        if (sender is not UIElement element || e.NewValue is not true) return;
        if (!GetFocusOnVisible(element)) return;
        element.Focus();
        if (element is TextBox textBox) textBox.SelectAll();
    }
}
