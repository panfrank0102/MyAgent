using System.Windows;

namespace MyAgent.App;

/// <summary>
/// 应用入口（App.xaml 中 StartupUri=MainWindow.xaml）。
/// 假数据阶段无需额外初始化；集成阶段在此装配 BackendManager / ApiClient 等真实服务。
/// </summary>
public partial class App : Application
{
}
