using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;
using MyAgent.App.Services;

namespace MyAgent.App.ViewModels;

/// <summary>
/// 会话设置窗口视图模型（Step 12 / §8）：
///  - 打开时 GET /api/sessions/{id} 拉取最新配置回显（失败显示错误条，不弹裸异常）；
///  - 保存 = 整表提交 <see cref="SessionConfigPatch"/>（空字符串字段发 ""、max_tokens 空 → null 清除、
///    api_key 未改动/为空 → 不发送），再回显服务端返回的快照；
///  - 测试连接 = 收集表单当前值调用 TestConnectionAsync，结果以 Ok/Message 展示（服务层约定不抛异常）；
///  - 数值与代理地址做本地校验：非法时禁用“保存”并给出提示，避免产生未处理异常。
/// 注意：本类刻意不使用 [ObservableProperty] 源生成器特性——新增 VM 用 ObservableObject.SetProperty
/// 手写属性，这样离线 verify-shim（禁止修改）无需新增 GeneratedProps 文件也能 0 警告 0 错误编译。
/// </summary>
public partial class SettingsViewModel : ObservableObject
{
    /// <summary>后端 v0.4② 防溢出上界：scan_rounds ≤ 1000、max_tokens ≤ 10^7、max_context_chars ≤ 10^8。</summary>
    private const int MaxScanRounds = 1000;
    private const int MaxTokensUpperBound = 10_000_000;
    private const int MaxContextCharsUpperBound = 100_000_000;

    private readonly IBackendService _backend;
    private readonly string _sessionId;

    private bool _isLoading;
    private bool _loaded;
    private bool _isBusy;
    private bool _configSaved;
    private bool _apiKeyDirty;

    /// <summary>密码框当前内容（未修改时等于打码值 MaskedApiKey）。</summary>
    private string _apiKeyInput = "";
    private string _maskedApiKey = "";

    // —— 表单字段（数值一律用 string 承载，便于表达“空”与非法输入，本地校验后再转换） ——
    private string _name = "";
    private string _systemPrompt = "";
    private string _scanRoundsText = "4";
    private bool _worldbookEnabled = true;
    private string _baseUrl = "";
    private string _model = "";
    private string _temperatureText = "1";
    private string _topPText = "1";
    private string _maxTokensText = "";
    private string _maxContextCharsText = "80000";
    private string _proxyUrl = "";

    private string _errorText = "";
    private string _statusText = "";
    private string _validationText = "正在加载配置…";
    private string _testResultText = "";
    private bool _testResultOk;

    public SettingsViewModel(IBackendService backend, string sessionId)
    {
        _backend = backend;
        _sessionId = sessionId;

        // 用同步 RelayCommand 包 async 方法（与主窗口 SendOrStop 同理）：命令自身的可用性完全由
        // CanSave/CanTest 控制，异步过程不依赖 AsyncRelayCommand 的执行期禁用语义。
        SaveCommand = new RelayCommand(() => _ = SaveAsync(), () => CanSave);
        TestConnectionCommand = new RelayCommand(() => _ = TestConnectionAsync(), () => CanTest);
        DismissErrorCommand = new RelayCommand(() => ErrorText = "");
    }

    // ---------------- 只读状态 ----------------

    public string SessionId => _sessionId;

    /// <summary>是否已经成功保存过（主窗口据此决定关闭后是否刷新列表/名）。</summary>
    public bool ConfigSaved => _configSaved;

    /// <summary>打码后的 API Key（打开时回显到密码框；服务端只回显打码值）。</summary>
    public string MaskedApiKey => _maskedApiKey;

    /// <summary>用户是否改动过 API Key 密码框：未改动时保存不发送 api_key（§4.3 仅非空值更新）。</summary>
    public bool IsApiKeyModified => _apiKeyDirty;

    /// <summary>回显打码值变更通知：视图层据此刷新 PasswordBox（WPF 的 PasswordBox.Password 不可绑定）。</summary>
    public event EventHandler? MaskedApiKeyChanged;

    // ---------------- 表单属性 ----------------

    public string Name
    {
        get => _name;
        set
        {
            if (!SetProperty(ref _name, value)) return;
            Validate();
        }
    }

    public string SystemPrompt
    {
        get => _systemPrompt;
        set
        {
            if (!SetProperty(ref _systemPrompt, value)) return;
            Validate();
        }
    }

    public string ScanRoundsText
    {
        get => _scanRoundsText;
        set
        {
            if (!SetProperty(ref _scanRoundsText, value)) return;
            Validate();
        }
    }

    public bool WorldbookEnabled
    {
        get => _worldbookEnabled;
        set => SetProperty(ref _worldbookEnabled, value);
    }

    public string BaseUrl
    {
        get => _baseUrl;
        set
        {
            if (!SetProperty(ref _baseUrl, value)) return;
            Validate();
        }
    }

    public string Model
    {
        get => _model;
        set
        {
            if (!SetProperty(ref _model, value)) return;
            Validate();
        }
    }

    public string TemperatureText
    {
        get => _temperatureText;
        set
        {
            if (!SetProperty(ref _temperatureText, value)) return;
            Validate();
        }
    }

    public string TopPText
    {
        get => _topPText;
        set
        {
            if (!SetProperty(ref _topPText, value)) return;
            Validate();
        }
    }

    public string MaxTokensText
    {
        get => _maxTokensText;
        set
        {
            if (!SetProperty(ref _maxTokensText, value)) return;
            Validate();
        }
    }

    public string MaxContextCharsText
    {
        get => _maxContextCharsText;
        set
        {
            if (!SetProperty(ref _maxContextCharsText, value)) return;
            Validate();
        }
    }

    public string ProxyUrl
    {
        get => _proxyUrl;
        set
        {
            if (!SetProperty(ref _proxyUrl, value)) return;
            Validate();
        }
    }

    // ---------------- 命令 ----------------

    public RelayCommand SaveCommand { get; }
    public RelayCommand TestConnectionCommand { get; }
    public RelayCommand DismissErrorCommand { get; }

    /// <summary>保存可用：已成功加载、非忙碌、且表单本地校验通过。</summary>
    public bool CanSave => _loaded && !_isBusy && IsFormValid;

    /// <summary>测试连接可用：非忙碌即可（不强制先保存；表单值直接用于探测）。</summary>
    public bool CanTest => !_isBusy;

    // ---------------- 错误条 / 成功提示 / 校验提示 / 测试结果 ----------------

    public string ErrorText
    {
        get => _errorText;
        private set
        {
            if (!SetProperty(ref _errorText, value)) return;
            OnPropertyChanged(nameof(ShowError));
        }
    }

    public bool ShowError => _errorText.Length > 0;

    public string StatusText
    {
        get => _statusText;
        private set
        {
            if (!SetProperty(ref _statusText, value)) return;
            OnPropertyChanged(nameof(ShowStatus));
        }
    }

    public bool ShowStatus => _statusText.Length > 0;

    public string ValidationText
    {
        get => _validationText;
        private set
        {
            if (!SetProperty(ref _validationText, value)) return;
            OnPropertyChanged(nameof(ShowValidation));
        }
    }

    public bool ShowValidation => _validationText.Length > 0;

    public bool IsFormValid => _validationText.Length == 0;

    public string TestResultText
    {
        get => _testResultText;
        private set => SetProperty(ref _testResultText, value);
    }

    public bool TestResultOk
    {
        get => _testResultOk;
        private set => SetProperty(ref _testResultOk, value);
    }

    private bool IsBusy
    {
        get => _isBusy;
        set
        {
            if (!SetProperty(ref _isBusy, value)) return;
            OnPropertyChanged(nameof(CanSave));
            OnPropertyChanged(nameof(CanTest));
            RefreshCommands();
        }
    }

    // ---------------- 加载 / 保存 / 测试连接 ----------------

    /// <summary>打开窗口时拉取会话详情并回显；失败只设置错误条（保存保持禁用）。</summary>
    public async Task LoadAsync(CancellationToken ct = default)
    {
        _isLoading = true;
        _loaded = false;
        ErrorText = "";
        StatusText = "";
        TestResultText = "";
        Validate();
        IsBusy = true;
        try
        {
            var info = await _backend.GetSessionAsync(_sessionId, ct);
            if (info == null)
            {
                ErrorText = "加载会话配置失败：会话不存在或已被删除。";
                return;
            }

            ApplyInfo(info);
            _loaded = true;
        }
        catch (Exception ex)
        {
            ErrorText = "加载会话配置失败：" + ex.Message;
        }
        finally
        {
            _isLoading = false;
            IsBusy = false;
            Validate();
        }
    }

    /// <summary>
    /// 整表保存：构建 <see cref="SessionConfigPatch"/> 并 PATCH；成功回显服务端快照。
    /// 语义（契约冻结）：空字符串字段发 ""；max_tokens 空 → null（清除，上游默认）；
    /// max_context_chars 空 → null（未提供，不更新）；api_key 未改动/为空 → 不发送。
    /// </summary>
    public async Task SaveAsync(CancellationToken ct = default)
    {
        if (!CanSave) return;

        var patch = BuildPatch();
        IsBusy = true;
        ErrorText = "";
        StatusText = "正在保存…";
        try
        {
            var updated = await _backend.UpdateSessionConfigAsync(_sessionId, patch, ct);
            if (updated == null)
            {
                StatusText = "";
                ErrorText = "保存失败：会话不存在或已被删除。";
                return;
            }

            ApplyInfo(updated);
            _loaded = true;
            _configSaved = true;
            ErrorText = "";
            StatusText = "配置已保存（" + DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + "）。";
        }
        catch (Exception ex)
        {
            StatusText = "";
            ErrorText = "保存会话配置失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
            Validate();
        }
    }

    /// <summary>测试连接：收集表单当前值 → TestConnectionAsync → 展示 Ok/Message（失败不抛异常）。</summary>
    public async Task TestConnectionAsync(CancellationToken ct = default)
    {
        if (_isBusy) return;

        // 代理格式非法时先本地拦截，避免把明显错误的地址发到后端
        if (!IsProxyUrlValid(_proxyUrl))
        {
            TestResultOk = false;
            TestResultText = "代理地址格式不正确：需以 http:// 或 https:// 开头（留空 = 直连）。";
            return;
        }

        // 未改动 API Key 时密码框里只有打码值（不是可用的明文 Key）：测试请求改为发送空 key，
        // 本地模型/直连场景可用；需要验证鉴权时请重新输入完整 Key。
        var request = new TestConnectionRequest
        {
            BaseUrl = _baseUrl.Trim(),
            ApiKey = _apiKeyDirty ? _apiKeyInput : "",
            Model = _model.Trim(),
            ProxyUrl = _proxyUrl.Trim(),
        };

        IsBusy = true;
        ErrorText = "";
        TestResultOk = false;
        TestResultText = "正在测试连接…";
        try
        {
            var result = await _backend.TestConnectionAsync(request, ct);
            TestResultOk = result.Ok;
            if (result.Ok)
            {
                TestResultText = string.IsNullOrWhiteSpace(result.Message) ? "连接成功。" : "连接成功：" + result.Message;
            }
            else
            {
                var reason = !string.IsNullOrWhiteSpace(result.Message)
                    ? result.Message
                    : (!string.IsNullOrWhiteSpace(result.ErrorCode) ? result.ErrorCode! : "未知原因");
                TestResultText = "连接失败：" + reason;
            }
        }
        catch (Exception ex)
        {
            // 契约约定 TestConnectionAsync 不抛异常；这里兜底防止未观察异常（§10）
            TestResultOk = false;
            TestResultText = "测试连接失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    // ---------------- 视图层桥接（PasswordBox 不可绑定） ----------------

    /// <summary>PasswordBox 内容变化（用户输入）时调用：记录输入并标记是否与打码值不同。</summary>
    public void SetApiKeyInputFromView(string password)
    {
        password ??= "";
        if (string.Equals(_apiKeyInput, password, StringComparison.Ordinal)) return;
        _apiKeyInput = password;
        _apiKeyDirty = !string.Equals(password, _maskedApiKey, StringComparison.Ordinal);
        OnPropertyChanged(nameof(IsApiKeyModified));
    }

    // ---------------- 内部实现 ----------------

    private void ApplyInfo(SessionInfo info)
    {
        Name = info.Name;
        SystemPrompt = info.SystemPrompt;
        ScanRoundsText = info.ScanRounds.ToString(CultureInfo.InvariantCulture);
        WorldbookEnabled = info.WorldbookEnabled;
        BaseUrl = info.BaseUrl;
        Model = info.Model;
        TemperatureText = FormatNumber(info.Temperature);
        TopPText = FormatNumber(info.TopP);
        MaxTokensText = info.MaxTokens?.ToString(CultureInfo.InvariantCulture) ?? "";
        MaxContextCharsText = info.MaxContextChars.ToString(CultureInfo.InvariantCulture);
        ProxyUrl = info.ProxyUrl;
        SetMaskedApiKey(info.MaskedApiKey);
    }

    private void SetMaskedApiKey(string masked)
    {
        _maskedApiKey = masked ?? "";
        _apiKeyInput = _maskedApiKey;
        _apiKeyDirty = false;
        OnPropertyChanged(nameof(MaskedApiKey));
        OnPropertyChanged(nameof(IsApiKeyModified));
        MaskedApiKeyChanged?.Invoke(this, EventArgs.Empty);
    }

    private SessionConfigPatch BuildPatch()
    {
        return new SessionConfigPatch
        {
            Name = _name.Trim(),
            SystemPrompt = _systemPrompt,
            ScanRounds = TryParseInt(_scanRoundsText, out int scanRounds) ? Math.Max(0, scanRounds) : 0,
            WorldbookEnabled = _worldbookEnabled,
            BaseUrl = _baseUrl.Trim(),
            // 契约：仅非空值更新 API Key；未改动或空白 = 不发送
            ApiKey = _apiKeyDirty && _apiKeyInput.Length > 0 ? _apiKeyInput : null,
            Model = _model.Trim(),
            Temperature = TryParseDouble(_temperatureText, out double temperature) ? temperature : 1.0,
            TopP = TryParseDouble(_topPText, out double topP) ? topP : 1.0,
            // 空文本框 = 清除配置（null），对应上游默认
            MaxTokens = _maxTokensText.Trim().Length == 0
                ? null
                : (TryParseInt(_maxTokensText, out int maxTokens) ? maxTokens : null),
            // 空文本框 = 未提供、不更新（其余字段的 null 语义，§4.3）
            MaxContextChars = _maxContextCharsText.Trim().Length == 0
                ? null
                : (TryParseInt(_maxContextCharsText, out int maxContextChars) ? maxContextChars : null),
            ProxyUrl = _proxyUrl.Trim(),
        };
    }

    /// <summary>本地校验：更新校验提示与命令可用性（非法时保存按钮禁用 + 提示原因）。</summary>
    private void Validate()
    {
        string error;
        if (_isLoading)
        {
            error = "正在加载配置…";
        }
        else if (!_loaded)
        {
            error = "";
        }
        else if (string.IsNullOrWhiteSpace(_name))
        {
            error = "会话名不能为空。";
        }
        else if (!TryParseInt(_scanRoundsText, out int scanRounds) || scanRounds < 0 || scanRounds > MaxScanRounds)
        {
            error = $"世界书扫描轮数需为 0~{MaxScanRounds} 的整数。";
        }
        else if (!TryParseDouble(_temperatureText, out double temperature)
                 || !double.IsFinite(temperature) || temperature < 0 || temperature > 2)
        {
            error = "temperature 需为 0~2 之间的数字。";
        }
        else if (!TryParseDouble(_topPText, out double topP)
                 || !double.IsFinite(topP) || topP < 0 || topP > 2)
        {
            error = "top_p 需为 0~2 之间的数字。";
        }
        else if (_maxTokensText.Trim().Length > 0
                 && (!TryParseInt(_maxTokensText, out int maxTokens) || maxTokens < 0 || maxTokens > MaxTokensUpperBound))
        {
            error = "max_tokens 需为 ≥0 的整数（留空 = 上游默认）。";
        }
        else if (_maxContextCharsText.Trim().Length > 0
                 && (!TryParseInt(_maxContextCharsText, out int maxContextChars)
                     || maxContextChars < 0 || maxContextChars > MaxContextCharsUpperBound))
        {
            error = "max_context_chars 需为 ≥0 的整数（留空 = 不修改）。";
        }
        else if (!IsProxyUrlValid(_proxyUrl))
        {
            error = "代理地址需以 http:// 或 https:// 开头（留空 = 直连）。";
        }
        else
        {
            error = "";
        }

        ValidationText = error;
        OnPropertyChanged(nameof(IsFormValid));
        OnPropertyChanged(nameof(CanSave));
        OnPropertyChanged(nameof(CanTest));
        RefreshCommands();
    }

    private void RefreshCommands()
    {
        SaveCommand.NotifyCanExecuteChanged();
        TestConnectionCommand.NotifyCanExecuteChanged();
    }

    /// <summary>代理地址：空 = 直连；否则必须是带主机的 http/https 绝对地址。</summary>
    private static bool IsProxyUrlValid(string proxyUrl)
    {
        var text = proxyUrl.Trim();
        if (text.Length == 0) return true;
        return Uri.TryCreate(text, UriKind.Absolute, out var uri)
               && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps)
               && uri.Host.Length > 0;
    }

    private static bool TryParseInt(string text, out int value)
        => int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out value)
           || int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.CurrentCulture, out value);

    private static bool TryParseDouble(string text, out double value)
        => double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out value)
           || double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.CurrentCulture, out value);

    /// <summary>数值回显：去掉无意义的尾随 0（1.0 → "1"，0.7 → "0.7"）。</summary>
    private static string FormatNumber(double value) => value.ToString("0.####", CultureInfo.InvariantCulture);
}
