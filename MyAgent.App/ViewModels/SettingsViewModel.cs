using System.Collections.ObjectModel;
using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;
using MyAgent.App.Services;

namespace MyAgent.App.ViewModels;

/// <summary>
/// “模型预设”下拉选项（v0.6 会话设置）：Id 为空串表示“不选择”，其余为全局预设 id。
/// 下拉显示 Name（预设名），SelectedItem 即本类实例；保存时取 Id 构造 patch.ModelPresetId。
/// </summary>
public sealed class ModelPresetOption
{
    public ModelPresetOption(string id, string name)
    {
        Id = id ?? "";
        Name = name ?? "";
    }

    /// <summary>预设 id；空串 = 不选择（保存时 patch.ModelPresetId = null，清除选择）。</summary>
    public string Id { get; }

    /// <summary>下拉显示名（“不选择”或预设名）。</summary>
    public string Name { get; }
}

/// <summary>
/// 会话设置窗口视图模型（Step 12 / §8，v0.6 改造）：
///  - v0.6 起会话不再持有 Base URL / API Key / 模型名 / 推理强度（统一由“全局设置 → 模型预设”管理），
///    本页改为“模型预设”下拉（不选择 / 各预设），保存时 patch.ModelPresetId = 未选择 ? null : id；
///    会话级“测试连接”按钮随之移除（连通性测试在全局设置的预设编辑中）；
///  - 打开时先 GET /api/model-presets 填充下拉，再 GET /api/sessions/{id} 回显（失败显示错误条，不弹裸异常）；
///  - 保存 = 整表提交 <see cref="SessionConfigPatch"/>（空字符串字段发 ""、max_tokens 空 → null 清除、
///    max_context_chars 空 → null 不更新、模型预设未选择 → null 清除选择），再回显服务端返回的快照；
///  - 数值与代理地址做本地校验：非法时禁用“保存”并给出提示，避免产生未处理异常；
///  - “管理模型预设…”按钮由视图层打开 GlobalSettingsWindow，关闭后调用 <see cref="ReloadModelPresetsAsync"/> 刷新下拉。
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

    /// <summary>“不选择”选项的 Id（空串）。</summary>
    private const string NoPresetId = "";

    // —— 表单字段（数值一律用 string 承载，便于表达“空”与非法输入，本地校验后再转换） ——
    private string _name = "";
    private string _systemPrompt = "";
    private string _scanRoundsText = "4";
    private bool _worldbookEnabled = true;
    private string _temperatureText = "1";
    private string _topPText = "1";
    private string _maxTokensText = "";
    private string _maxContextCharsText = "80000";
    private string _proxyUrl = "";

    // —— 模型预设下拉（v0.6） ——
    private ModelPresetOption? _selectedModelPreset;

    private string _errorText = "";
    private string _statusText = "";
    private string _validationText = "正在加载配置…";

    public SettingsViewModel(IBackendService backend, string sessionId)
    {
        _backend = backend;
        _sessionId = sessionId;

        // 用同步 RelayCommand 包 async 方法（与主窗口 SendOrStop 同理）：命令自身的可用性完全由
        // CanSave 控制，异步过程不依赖 AsyncRelayCommand 的执行期禁用语义。
        SaveCommand = new RelayCommand(() => _ = SaveAsync(), () => CanSave);
        DismissErrorCommand = new RelayCommand(() => ErrorText = "");
    }

    // ---------------- 只读状态 ----------------

    public string SessionId => _sessionId;

    /// <summary>是否已经成功保存过（主窗口据此决定关闭后是否刷新列表/名）。</summary>
    public bool ConfigSaved => _configSaved;

    // ---------------- 表单属性 ----------------

    /// <summary>模型预设下拉选项：「不选择」+ 全部全局预设（打开时/管理窗口关闭后刷新）。</summary>
    public ObservableCollection<ModelPresetOption> ModelPresetOptions { get; } = new();

    /// <summary>
    /// 当前选中的模型预设（SelectedItem 双向绑定）：Id 空串 = 不选择（保存时 patch.ModelPresetId = null）。
    /// 打开时按会话的 ModelPresetId 回显；所选预设已被删除/列表暂不可用时保留一个占位项，避免误清除。
    /// </summary>
    public ModelPresetOption? SelectedModelPreset
    {
        get => _selectedModelPreset;
        set
        {
            if (!SetProperty(ref _selectedModelPreset, value)) return;
            OnPropertyChanged(nameof(ModelPresetHint));
        }
    }

    /// <summary>模型预设字段下方提示：未选择时说明聊天会被提示先选择预设。</summary>
    public string ModelPresetHint => _selectedModelPreset is { Id.Length: > 0 }
        ? "聊天时使用该预设的 Base URL / API Key / 模型名 / 推理强度；可随时切换。"
        : "未选择模型预设：聊天时会提示先选择模型预设（可点右侧“管理模型预设…”新建）。";

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
    public RelayCommand DismissErrorCommand { get; }

    /// <summary>保存可用：已成功加载、非忙碌、且表单本地校验通过。</summary>
    public bool CanSave => _loaded && !_isBusy && IsFormValid;

    // ---------------- 错误条 / 成功提示 / 校验提示 ----------------

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

    private bool IsBusy
    {
        get => _isBusy;
        set
        {
            if (!SetProperty(ref _isBusy, value)) return;
            OnPropertyChanged(nameof(CanSave));
            RefreshCommands();
        }
    }

    // ---------------- 加载 / 保存 ----------------

    /// <summary>
    /// 打开窗口时加载：先 ListModelPresetsAsync 填充模型预设下拉，再 GetSessionAsync 回显会话配置。
    /// 两者失败都只设置错误条（不崩）；预设列表失败不阻塞会话字段编辑/保存。
    /// </summary>
    public async Task LoadAsync(CancellationToken ct = default)
    {
        _isLoading = true;
        _loaded = false;
        ErrorText = "";
        StatusText = "";
        Validate();
        IsBusy = true;
        try
        {
            // 顺序要求：先预设下拉，再会话回显（回显时才能按 ModelPresetId 选中对应项）
            try
            {
                await LoadModelPresetOptionsAsync(NoPresetId, ct);
            }
            catch (Exception ex)
            {
                ErrorText = "加载模型预设失败：" + ex.Message;
            }

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
    /// 重新加载模型预设下拉（全局设置窗口关闭后由视图层调用）：保留当前选择（按 id）；
    /// 当前预设已被删除时回退到“不选择”。失败只显示错误条（§10）。
    /// </summary>
    public async Task ReloadModelPresetsAsync(CancellationToken ct = default)
    {
        try
        {
            await LoadModelPresetOptionsAsync(null, ct);
            // 若已加载会话、且服务端会话的 ModelPresetId 与当前选择不一致（例如别处切换过），以当前会话为准
            ErrorText = "";
        }
        catch (Exception ex)
        {
            ErrorText = "刷新模型预设列表失败：" + ex.Message;
        }
    }

    /// <summary>
    /// 整表保存：构建 <see cref="SessionConfigPatch"/> 并 PATCH；成功回显服务端快照。
    /// 语义（契约冻结，§4.3）：
    ///  - 会话名剔空白后提交；系统提示词原样；扫描轮数/温度/top_p 已通过本地校验；
    ///  - max_tokens 空 → null（清除，上游默认）；max_context_chars 空 → null（未提供，不更新）；
    ///  - model_preset_id：未选择 → null（v0.6③：显式 null = 清除选择），选择 → 预设 id；
    ///  - v0.6 起不再提交 base_url / api_key / model / reasoning_effort（会话已无这些字段）。
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

    // ---------------- 内部实现 ----------------

    /// <summary>
    /// 加载预设下拉：首项固定为“不选择”，其后为全部预设（显示 Name）。
    /// preferPresetId 为 null 时保留当前选择（按 id）；找不到时回退到“不选择”。
    /// </summary>
    private async Task LoadModelPresetOptionsAsync(string? preferPresetId, CancellationToken ct)
    {
        var presets = await _backend.ListModelPresetsAsync(ct);

        string targetId = preferPresetId ?? _selectedModelPreset?.Id ?? NoPresetId;
        var options = new List<ModelPresetOption> { new(NoPresetId, "不选择") };
        options.AddRange(presets.Select(p => new ModelPresetOption(p.Id, p.Name)));

        ModelPresetOptions.Clear();
        foreach (var option in options) ModelPresetOptions.Add(option);

        SelectedModelPreset = options.FirstOrDefault(o => o.Id == targetId)
                              ?? options.FirstOrDefault(o => o.Id == NoPresetId)
                              ?? options[0];
    }

    private void ApplyInfo(SessionInfo info)
    {
        Name = info.Name;
        SystemPrompt = info.SystemPrompt;
        ScanRoundsText = info.ScanRounds.ToString(CultureInfo.InvariantCulture);
        WorldbookEnabled = info.WorldbookEnabled;
        TemperatureText = FormatNumber(info.Temperature);
        TopPText = FormatNumber(info.TopP);
        MaxTokensText = info.MaxTokens?.ToString(CultureInfo.InvariantCulture) ?? "";
        MaxContextCharsText = info.MaxContextChars.ToString(CultureInfo.InvariantCulture);
        ProxyUrl = info.ProxyUrl;
        SelectModelPreset(info.ModelPresetId);
    }

    /// <summary>
    /// 按会话的 model_preset_id 回显下拉选择。
    /// 若下拉里没有该 id（预设列表加载失败/数据不一致），补一个占位项并保留 id：
    /// 避免用户仅保存其它字段时把会话的预设选择误清除（占位项仍提交原 id）。
    /// </summary>
    private void SelectModelPreset(string? presetId)
    {
        string id = presetId ?? NoPresetId;
        var option = ModelPresetOptions.FirstOrDefault(o => o.Id == id);
        if (option == null && id.Length > 0)
        {
            option = new ModelPresetOption(id, "（无法获取该预设信息）");
            ModelPresetOptions.Add(option);
        }

        SelectedModelPreset = option
                              ?? ModelPresetOptions.FirstOrDefault(o => o.Id == NoPresetId)
                              ?? (ModelPresetOptions.Count > 0 ? ModelPresetOptions[0] : null);
    }

    private SessionConfigPatch BuildPatch()
    {
        return new SessionConfigPatch
        {
            Name = _name.Trim(),
            SystemPrompt = _systemPrompt,
            ScanRounds = TryParseInt(_scanRoundsText, out int scanRounds) ? Math.Max(0, scanRounds) : 0,
            WorldbookEnabled = _worldbookEnabled,
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
            // v0.6③：null = 清除选择；字符串 = 选择该全局模型预设
            ModelPresetId = _selectedModelPreset is { Id.Length: > 0 } selected ? selected.Id : null,
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
        RefreshCommands();
    }

    private void RefreshCommands()
    {
        SaveCommand.NotifyCanExecuteChanged();
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
