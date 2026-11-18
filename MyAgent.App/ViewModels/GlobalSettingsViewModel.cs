using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;
using MyAgent.App.Services;

namespace MyAgent.App.ViewModels;

/// <summary>
/// 全局设置左侧子设置栏条目（v0.6）：首版仅“模型预设”一项，结构预留后续扩展
/// （新增子设置 = 往 <see cref="GlobalSettingsViewModel.Sections"/> 增加一项，右侧按 Key 切换内容区）。
/// </summary>
public sealed class GlobalSettingsSectionViewModel
{
    public GlobalSettingsSectionViewModel(string key, string name)
    {
        Key = key;
        Name = name;
    }

    /// <summary>稳定标识（右侧内容区据此切换，不依赖显示文案）。</summary>
    public string Key { get; }

    /// <summary>侧栏显示名。</summary>
    public string Name { get; }
}

/// <summary>
/// 全局设置窗口视图模型（v0.6 / §8）：左侧子设置栏 + 右侧“模型预设”管理。
/// 职责（全部网络操作 async/await，失败统一错误条，不弹裸异常，§10）：
///  - 加载：GET /api/model-presets 填充预设列表（按 created_at 升序，服务端已排序）；
///  - 新建 = 清空表单（无 id，保存走 CreateModelPresetAsync）；
///  - 保存 = 有 id 走 UpdateModelPresetAsync（API Key 未改动时提交空值 = 服务端不更新），
///    无 id 走 CreateModelPresetAsync；保存后重新拉取列表并保持选中；
///  - 删除 = 先经 IDialogService 确认（提示“引用该预设的会话将变为未选择”），删除后刷新列表；
///  - 测试连接：已有 id 且 Key 未改动 → TestModelPresetAsync(id)（用服务端存储的 Key）；
///    新建或用户改了 Key → TestConnectionAsync(表单明文 Key)，结果以 Ok/Message 展示；
///  - PasswordBox 回显：与 SettingsWindow 同款桥接——回显打码值、PasswordChanged 抑制回写，
///    打码值绝不作为明文提交（见 <see cref="SetApiKeyInputFromView"/>）。
/// 注意：本类刻意不使用 [ObservableProperty] 源生成器特性（手写属性 + SetProperty），
/// 与 SettingsViewModel 一致：离线 verify-shim 无需新增 GeneratedProps 文件即可 0 警告 0 错误编译。
/// </summary>
public sealed class GlobalSettingsViewModel : ObservableObject
{
    /// <summary>“模型预设”子设置页的稳定标识（右侧内容区/侧栏项共用）。</summary>
    public const string ModelPresetsSectionKey = "model-presets";

    private readonly IBackendService _backend;
    private readonly IDialogService _dialogs;

    /// <summary>左侧子设置栏（首版仅“模型预设”，后续可扩展）。</summary>
    public ObservableCollection<GlobalSettingsSectionViewModel> Sections { get; } = new();

    /// <summary>预设列表（服务端返回 ModelPreset 快照；刷新时整体重建）。</summary>
    public ObservableCollection<ModelPreset> Presets { get; } = new();

    // —— 侧栏选择 / 预设表单状态 ——
    private GlobalSettingsSectionViewModel? _selectedSection;
    private ModelPreset? _selectedPreset;

    private bool _isBusy;
    private bool _apiKeyDirty;

    /// <summary>密码框当前内容（未改动时等于打码值 MaskedApiKey）。</summary>
    private string _apiKeyInput = "";
    private string _maskedApiKey = "";

    // —— 表单字段 ——
    private string _presetName = "";
    private string _baseUrl = "";
    private string _model = "";
    private string _reasoningEffortSelected = "";

    // —— 错误条 / 成功提示 / 测试结果 ——
    private string _errorText = "";
    private string _statusText = "";
    private string _testResultText = "";
    private bool _testResultOk;

    public GlobalSettingsViewModel(IBackendService backend, IDialogService dialogs)
    {
        _backend = backend ?? throw new ArgumentNullException(nameof(backend));
        _dialogs = dialogs ?? throw new ArgumentNullException(nameof(dialogs));

        // 侧栏用集合绑定（不硬编码到 XAML），首版仅一项；后续子设置页直接 Add 即可扩展。
        Sections.Add(new GlobalSettingsSectionViewModel(ModelPresetsSectionKey, "模型预设"));
        _selectedSection = Sections[0];

        // 与 SettingsViewModel 同款：同步 RelayCommand 包 async 方法，命令可用性由 CanXxx 控制，
        // 避免 AsyncRelayCommand 执行期间自禁用导致的重复点击问题。
        NewCommand = new RelayCommand(EnterNewMode, () => CanNew);
        DeleteCommand = new RelayCommand(() => _ = DeleteAsync(), () => CanDelete);
        SaveCommand = new RelayCommand(() => _ = SaveAsync(), () => CanSave);
        TestConnectionCommand = new RelayCommand(() => _ = TestConnectionAsync(), () => CanTest);
        DismissErrorCommand = new RelayCommand(() => ErrorText = "");
    }

    // ---------------- 侧栏 ----------------

    /// <summary>当前选中的子设置页（ListBox.SelectedItem 双向绑定）。</summary>
    public GlobalSettingsSectionViewModel? SelectedSection
    {
        get => _selectedSection;
        set
        {
            if (!SetProperty(ref _selectedSection, value)) return;
            OnPropertyChanged(nameof(IsModelPresetsSection));
        }
    }

    /// <summary>右侧是否显示“模型预设”面板（首版右侧固定为它；后续子设置按 Key 增加对应面板）。</summary>
    public bool IsModelPresetsSection => _selectedSection?.Key == ModelPresetsSectionKey;

    // ---------------- 预设列表 / 表单状态 ----------------

    /// <summary>当前选中的预设：null = 新建模式（表单为空、保存走创建）。</summary>
    public ModelPreset? SelectedPreset
    {
        get => _selectedPreset;
        set
        {
            if (!SetProperty(ref _selectedPreset, value)) return;

            OnPropertyChanged(nameof(FormTitle));
            OnPropertyChanged(nameof(CanDelete));
            OnPropertyChanged(nameof(CanSave));

            if (value != null) LoadForm(value);
            else ClearForm();

            TestResultText = "";
            RefreshCommands();
        }
    }

    /// <summary>表单头部标题：新建 / 编辑。</summary>
    public string FormTitle => _selectedPreset == null ? "新建模型预设" : "编辑模型预设";

    /// <summary>预设名称（必填；允许重名）。</summary>
    public string PresetName
    {
        get => _presetName;
        set
        {
            if (!SetProperty(ref _presetName, value ?? "")) return;
            RefreshCommands();
        }
    }

    /// <summary>模型 Base URL（OpenAI 兼容接口地址）。</summary>
    public string BaseUrl
    {
        get => _baseUrl;
        set => SetProperty(ref _baseUrl, value ?? "");
    }

    /// <summary>模型名（如 deepseek-chat）。</summary>
    public string Model
    {
        get => _model;
        set => SetProperty(ref _model, value ?? "");
    }

    /// <summary>推理强度："" = 不设置（不向上游发送）/ "low" / "medium" / "high"。</summary>
    public string ReasoningEffortSelected
    {
        get => _reasoningEffortSelected;
        set => SetProperty(ref _reasoningEffortSelected, value ?? "");
    }

    /// <summary>打码后的 API Key（打开/切换预设时回显到密码框；服务端只回显打码值）。</summary>
    public string MaskedApiKey => _maskedApiKey;

    /// <summary>回显打码值变更通知：视图层据此刷新 PasswordBox（WPF 的 PasswordBox.Password 不可绑定）。</summary>
    public event EventHandler? MaskedApiKeyChanged;

    // ---------------- 命令与可用性 ----------------

    public RelayCommand NewCommand { get; }
    public RelayCommand DeleteCommand { get; }
    public RelayCommand SaveCommand { get; }
    public RelayCommand TestConnectionCommand { get; }
    public RelayCommand DismissErrorCommand { get; }

    /// <summary>新建可用：非忙碌。</summary>
    public bool CanNew => !_isBusy;

    /// <summary>删除可用：非忙碌且当前选中了已保存的预设。</summary>
    public bool CanDelete => !_isBusy && _selectedPreset != null;

    /// <summary>保存可用：非忙碌且预设名称非空白。</summary>
    public bool CanSave => !_isBusy && _presetName.Trim().Length > 0;

    /// <summary>测试连接可用：非忙碌（新建/编辑均可测）。</summary>
    public bool CanTest => !_isBusy;

    private bool IsBusy
    {
        get => _isBusy;
        set
        {
            if (!SetProperty(ref _isBusy, value)) return;
            RefreshCommands();
        }
    }

    private void RefreshCommands()
    {
        OnPropertyChanged(nameof(CanNew));
        OnPropertyChanged(nameof(CanDelete));
        OnPropertyChanged(nameof(CanSave));
        OnPropertyChanged(nameof(CanTest));
        NewCommand.NotifyCanExecuteChanged();
        DeleteCommand.NotifyCanExecuteChanged();
        SaveCommand.NotifyCanExecuteChanged();
        TestConnectionCommand.NotifyCanExecuteChanged();
    }

    // ---------------- 错误条 / 成功提示 / 测试结果 ----------------

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

    // ---------------- 加载 / 新建 / 保存 / 删除 ----------------

    /// <summary>打开窗口时加载预设列表：失败只设置错误条，界面仍可尝试新建（不崩）。</summary>
    public async Task LoadAsync(CancellationToken ct = default)
    {
        ErrorText = "";
        StatusText = "";
        TestResultText = "";
        TestResultOk = false;
        IsBusy = true;
        try
        {
            var list = await _backend.ListModelPresetsAsync(ct);
            ApplyPresets(list, preferPresetId: null);
        }
        catch (Exception ex)
        {
            // 后端未就绪/网络失败：显示错误条；列表置空并进入新建模式（保存/测试仍会走服务层并给出错误）
            ErrorText = "加载模型预设失败：" + ex.Message;
            ApplyPresets(Array.Empty<ModelPreset>(), preferPresetId: null);
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>新建：清空表单（ApiKey 可输入；无 id，保存走 CreateModelPresetAsync）。</summary>
    public void EnterNewMode()
    {
        if (_selectedPreset != null) SelectedPreset = null; // 触发取消列表选中 + 清空表单
        else ClearForm();                                  // 已是新建状态：再次点击也恢复干净表单
    }

    /// <summary>
    /// 保存：无 id → CreateModelPresetAsync；有 id → UpdateModelPresetAsync（API Key 未改动提交空值，
    /// 服务端按契约“空值不更新”）；成功后重新 ListModelPresetsAsync 并保持选中。
    /// </summary>
    public async Task SaveAsync(CancellationToken ct = default)
    {
        if (!CanSave) return;

        var id = _selectedPreset?.Id;
        var preset = new ModelPreset
        {
            Id = id ?? "",
            Name = _presetName.Trim(),
            BaseUrl = _baseUrl.Trim(),
            // 打码值绝不作为明文提交：未改动 → 空（不更新）；改动 → 用户输入的明文
            ApiKeyInput = _apiKeyDirty ? _apiKeyInput : "",
            Model = _model.Trim(),
            ReasoningEffort = _reasoningEffortSelected,
        };

        IsBusy = true;
        ErrorText = "";
        StatusText = "正在保存…";
        try
        {
            if (string.IsNullOrEmpty(id))
            {
                var created = await _backend.CreateModelPresetAsync(preset, ct);
                StatusText = "模型预设已创建。";
                await ReloadPresetsAfterWriteAsync(created.Id, ct);
            }
            else
            {
                var updated = await _backend.UpdateModelPresetAsync(id, preset, ct);
                if (updated == null)
                {
                    // 预设已被删除（例如另一个窗口删掉了）：提示并刷新列表，不崩
                    StatusText = "";
                    ErrorText = "保存失败：预设不存在或已被删除。";
                    await ReloadPresetsAfterWriteAsync(null, ct);
                    return;
                }

                StatusText = "模型预设已保存。";
                await ReloadPresetsAfterWriteAsync(updated.Id, ct);
            }
        }
        catch (Exception ex)
        {
            StatusText = "";
            ErrorText = "保存模型预设失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>删除：确认（引用该预设的会话将变为未选择）→ DeleteModelPresetAsync → 刷新列表并选中剩余第一项。</summary>
    public async Task DeleteAsync(CancellationToken ct = default)
    {
        if (!CanDelete) return;

        var preset = _selectedPreset!;
        // §8：破坏性操作需确认；明确提示引用会话会变为“未选择”（聊天前需重新选择预设）
        if (!_dialogs.Confirm(
                $"确定删除模型预设“{preset.Name}”吗？引用该预设的会话将变为未选择（聊天前需重新选择模型预设）。",
                "删除模型预设"))
        {
            return;
        }

        IsBusy = true;
        ErrorText = "";
        StatusText = "正在删除…";
        try
        {
            var deleted = await _backend.DeleteModelPresetAsync(preset.Id, ct);
            if (!deleted)
            {
                StatusText = "";
                ErrorText = "删除失败：预设不存在或已被删除。";
            }
            else
            {
                StatusText = "模型预设已删除。";
            }

            await ReloadPresetsAfterWriteAsync(null, ct);
        }
        catch (Exception ex)
        {
            StatusText = "";
            ErrorText = "删除模型预设失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    // ---------------- 测试连接 ----------------

    /// <summary>
    /// 测试连接（结果以 Ok/Message 展示，服务层约定不抛异常；这里仍兜底 catch 防止裸异常）：
    ///  - 已有 id 且 Key 未改动 → TestModelPresetAsync(id)：使用服务端存储的 Key；
    ///  - 新建或用户改了 Key → TestConnectionAsync：使用表单里用户输入的明文 Key，
    ///    ProxyUrl 固定空串（代理属于会话配置，全局预设不含代理）。
    /// </summary>
    public async Task TestConnectionAsync(CancellationToken ct = default)
    {
        if (!CanTest) return;

        var id = _selectedPreset?.Id;
        bool useSavedPreset = !string.IsNullOrEmpty(id) && !_apiKeyDirty;

        IsBusy = true;
        ErrorText = "";
        TestResultOk = false;
        TestResultText = "正在测试连接…";
        try
        {
            TestConnectionResult result;
            if (useSavedPreset)
            {
                result = await _backend.TestModelPresetAsync(id!, ct);
            }
            else
            {
                result = await _backend.TestConnectionAsync(new TestConnectionRequest
                {
                    BaseUrl = _baseUrl.Trim(),
                    // 未改动/新建未输入 = 空 Key（本地模型/直连可用；需验证鉴权请重新输入完整 Key）
                    ApiKey = _apiKeyDirty ? _apiKeyInput : "",
                    Model = _model.Trim(),
                    ProxyUrl = "",
                }, ct);
            }

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
            // 契约约定不抛异常；兜底防止未观察异常（§10）
            TestResultOk = false;
            TestResultText = "测试连接失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    // ---------------- 视图层桥接（PasswordBox 不可绑定） ----------------

    /// <summary>
    /// PasswordBox 内容变化（用户输入）时调用：记录输入并标记是否与打码值不同。
    /// 与 SettingsWindow 的桥接同款：视图在回显打码值时抑制回调，故这里拿到的只会是用户输入。
    /// </summary>
    public void SetApiKeyInputFromView(string password)
    {
        password ??= "";
        if (string.Equals(_apiKeyInput, password, StringComparison.Ordinal)) return;
        _apiKeyInput = password;
        _apiKeyDirty = !string.Equals(password, _maskedApiKey, StringComparison.Ordinal);
    }

    // ---------------- 内部实现 ----------------

    /// <summary>把服务端预设列表写入集合，并保持（或回退到）合理选中项。</summary>
    private void ApplyPresets(IReadOnlyList<ModelPreset> list, string? preferPresetId)
    {
        Presets.Clear();
        foreach (var preset in list) Presets.Add(preset);

        ModelPreset? target = null;
        if (!string.IsNullOrEmpty(preferPresetId))
            target = Presets.FirstOrDefault(p => p.Id == preferPresetId);

        // 无明确偏好/偏好已不存在时：选中第一项；没有任何预设则进入新建模式
        target ??= Presets.Count > 0 ? Presets[0] : null;

        if (target != null) SelectedPreset = target;
        else EnterNewMode();
    }

    /// <summary>
    /// 写操作成功后刷新列表（保存/删除）。刷新失败单独提示“已保存/已删除但刷新失败”，
    /// 不把成功操作显示成失败（§10 错误信息尽量准确）。
    /// </summary>
    private async Task ReloadPresetsAfterWriteAsync(string? preferPresetId, CancellationToken ct)
    {
        try
        {
            var list = await _backend.ListModelPresetsAsync(ct);
            ApplyPresets(list, preferPresetId);
        }
        catch (Exception ex)
        {
            ErrorText = "刷新模型预设列表失败：" + ex.Message;
        }
    }

    private void LoadForm(ModelPreset preset)
    {
        PresetName = preset.Name;
        BaseUrl = preset.BaseUrl;
        Model = preset.Model;
        ReasoningEffortSelected = string.IsNullOrEmpty(preset.ReasoningEffort) ? "" : preset.ReasoningEffort;
        SetMaskedApiKey(preset.MaskedApiKey);
    }

    private void ClearForm()
    {
        PresetName = "";
        BaseUrl = "";
        Model = "";
        ReasoningEffortSelected = "";
        SetMaskedApiKey("");
    }

    private void SetMaskedApiKey(string masked)
    {
        _maskedApiKey = masked ?? "";
        _apiKeyInput = _maskedApiKey;
        _apiKeyDirty = false;
        OnPropertyChanged(nameof(MaskedApiKey));
        MaskedApiKeyChanged?.Invoke(this, EventArgs.Empty);
    }
}
