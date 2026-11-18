using System.IO;
using System.Text.Json;

namespace MyAgent.App.Services;

/// <summary>
/// 应用级本地配置（exe 目录 config.json，§4.1 / §8 / §12 便携免安装）。
/// </summary>
public sealed class AppSettings
{
    /// <summary>python 解释器全路径（供 Step 14 BackendManager 拉起后端；本批次仅记录）。</summary>
    public string PythonPath { get; set; } = @"D:\ProgramData\Conda_Data\envs\env_1\python.exe";

    /// <summary>后端目录（供 Step 14 使用；空 = 未配置，后端需手动启动）。</summary>
    public string BackendDir { get; set; } = "";

    /// <summary>后端 HTTP 端口（默认 8765，§4.1；仅绑定 127.0.0.1）。</summary>
    public int Port { get; set; } = 8765;

    /// <summary>
    /// 后端模式："fake" = 假数据演示（Step 9-11，默认）；"real" = 连接真实 Python 后端
    /// （http://127.0.0.1:{Port}，需先手动启动后端；自动拉起属 Step 14 BackendManager）。
    /// </summary>
    public string Mode { get; set; } = "fake";
}

/// <summary>
/// 应用级配置读写：exe 目录下 config.json，缺失时写默认值。读写失败回退默认、不阻塞启动。
/// </summary>
public sealed class AppSettingsService
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    private readonly string _path;

    public AppSettingsService(string? configPath = null)
    {
        _path = configPath ?? Path.Combine(AppContext.BaseDirectory, "config.json");
    }

    public AppSettings Load()
    {
        var defaults = new AppSettings();
        try
        {
            if (!File.Exists(_path))
            {
                Save(defaults); // 首次运行：落默认配置，便于用户手工修改
                return defaults;
            }
            var settings = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(_path));
            return settings ?? defaults;
        }
        catch
        {
            // 配置损坏/不可写：回退默认值，不阻塞启动（下次 Save 会覆盖）
            return defaults;
        }
    }

    public void Save(AppSettings settings)
    {
        File.WriteAllText(_path, JsonSerializer.Serialize(settings, JsonOptions));
    }
}
