namespace MyAgent.App.Models;

/// <summary>POST /api/config/test-connection 请求体（§4.3）：非流式最小请求。</summary>
public sealed class TestConnectionRequest
{
    public string BaseUrl { get; set; } = "";
    public string ApiKey { get; set; } = "";
    public string Model { get; set; } = "";
    public string ProxyUrl { get; set; } = "";
}

/// <summary>
/// test-connection 的统一结果：服务层约定不抛异常——
/// 成功（Ok=true）或失败（Ok=false，含后端未连接/网络失败/校验失败）都由此对象表达。
/// </summary>
public sealed class TestConnectionResult
{
    /// <summary>是否连通成功。</summary>
    public bool Ok { get; set; }

    /// <summary>成功提示或失败原因（人类可读中文）。</summary>
    public string Message { get; set; } = "";

    /// <summary>失败时的后端错误 code（如 auth_failed / upstream_unreachable），成功为 null。</summary>
    public string? ErrorCode { get; set; }
}
