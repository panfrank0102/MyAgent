using System.Windows;
using MyAgent.App.Services;

namespace MyAgent.App;

/// <summary>
/// 应用入口（组合根，App.xaml 已去掉 StartupUri）：
/// 先读 exe 目录 config.json（AppSettingsService，§4.1/§8），按 mode 选择后端服务：
///   - "fake"（默认）：FakeBackendService，Step 9-11 假数据演示；
///   - "real"：RealApiBackendService，连接 http://127.0.0.1:{Port} 的真实 Python 后端
///     （需先手动启动后端；自动拉起属 Step 14 BackendManager）。
/// </summary>
public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var settings = new AppSettingsService().Load();

        IBackendService backend = settings.Mode.Equals("real", StringComparison.OrdinalIgnoreCase)
            ? new RealApiBackendService($"http://127.0.0.1:{settings.Port}")
            : new FakeBackendService();

        var window = new MainWindow(backend, new MessageBoxDialogService());
        MainWindow = window;
        window.Show();
    }
}
