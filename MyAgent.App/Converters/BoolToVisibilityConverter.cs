using System.Globalization;
using System.Windows;
using System.Windows.Data;

namespace MyAgent.App.Converters;

/// <summary>
/// bool → Visibility 转换器（true=Visible，false=Collapsed）。
/// 参数传 "Invert"（不区分大小写）时反转结果；null 视为 false，非 bool 非 null 视为“存在”。
/// 注意：绑定源为 UnsetValue（如 SelectedSession 为 null 时的路径中断）时 WPF 不会调用转换器，
/// 此时请在该绑定上提供 FallbackValue（例如 FallbackValue=Collapsed），已在 XAML 中统一处理。
/// </summary>
public sealed class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
    {
        bool flag = value switch
        {
            bool b => b,
            null => false,
            _ => true,
        };
        if (string.Equals(parameter as string, "Invert", StringComparison.OrdinalIgnoreCase))
            flag = !flag;
        return flag ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => throw new NotSupportedException("BoolToVisibilityConverter 仅支持单向转换。");
}
