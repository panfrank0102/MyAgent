using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MyAgent.App.Models;
using MyAgent.App.Services;

namespace MyAgent.App.ViewModels;

/// <summary>
/// 世界书编辑器视图模型（Step 13，每会话独立）：
///  - 左列表：条目按 sort_order 升序 + 名称/关键词子串搜索过滤 + 启用勾选 + 上移/下移 + 新建/删除；
///  - 右编辑区：名称、关键词（一行一个，实时 trim/去空行/去重）、内容（多行，内容为空禁止保存）；
///  - 数据流：打开时 ListWorldbookAsync，每次变更后重新拉取列表；失败统一错误条（§10）。
/// 会话间数据隔离由服务层按 sessionId 保证，UI 只持有当前 sessionId，不做跨会话缓存。
/// 与 SettingsViewModel 同样刻意不使用 [ObservableProperty]（保持离线 verify-shim 无需改动）。
/// </summary>
public partial class WorldBookViewModel : ObservableObject
{
    private readonly IBackendService _backend;
    private readonly IDialogService _dialogs;
    private readonly string _sessionId;

    /// <summary>当前会话的全部条目（服务端返回，已按 sort_order 升序）。</summary>
    private readonly List<WorldBookItemViewModel> _allEntries = new();

    /// <summary>重建 <see cref="Entries"/> 期间抑制“列表选中项被清空”引起的编辑器重载。</summary>
    private bool _rebuildingEntries;

    /// <summary>恢复列表选中项期间抑制编辑器重载（搜索过滤时保留正在编辑的未保存内容）。</summary>
    private bool _suppressEditorReload;

    private WorldBookItemViewModel? _selectedEntry;
    private string _searchText = "";
    private bool _isBusy;
    private string _errorText = "";
    private string _statusText = "";

    // —— 右编辑区 ——
    private string _editingEntryId = ""; // "" = 新建
    private string _nameText = "";
    private string _keywordsText = "";
    private string _contentText = "";
    private List<string> _normalizedKeywords = new();
    private string _editorValidationText = "";

    public WorldBookViewModel(IBackendService backend, string sessionId, IDialogService dialogs)
    {
        _backend = backend;
        _sessionId = sessionId;
        _dialogs = dialogs;

        NewEntryCommand = new RelayCommand(BeginNewEntry, () => !_isBusy);
        SaveEntryCommand = new RelayCommand(() => _ = SaveEntryAsync(), () => CanSaveEntry);
        DeleteEntryCommand = new RelayCommand(() => _ = DeleteSelectedEntryAsync(), () => CanDeleteEntry);
        MoveUpCommand = new RelayCommand(() => _ = MoveUpAsync(), () => CanMoveUp);
        MoveDownCommand = new RelayCommand(() => _ = MoveDownAsync(), () => CanMoveDown);
        DismissErrorCommand = new RelayCommand(() => ErrorText = "");
    }

    // ---------------- 集合与选中 ----------------

    /// <summary>左列表（已按搜索条件过滤，保持 sort_order 顺序；实例不替换）。</summary>
    public ObservableCollection<WorldBookItemViewModel> Entries { get; } = new();

    public string SessionId => _sessionId;

    public WorldBookItemViewModel? SelectedEntry
    {
        get => _selectedEntry;
        set
        {
            if (!SetProperty(ref _selectedEntry, value)) return;
            // 集合重建期间 ListBox 会把选中项置空并回写，这里先忽略，稍后由 RebuildEntries 显式恢复
            if (_rebuildingEntries) return;
            if (value != null && !_suppressEditorReload) LoadEditorFromEntry(value);
            RefreshCommands();
        }
    }

    /// <summary>搜索过滤文本：匹配名称或关键词子串（ASCII 忽略大小写），空 = 全部。</summary>
    public string SearchText
    {
        get => _searchText;
        set
        {
            if (!SetProperty(ref _searchText, value)) return;
            RebuildEntries();
        }
    }

    // ---------------- 错误条 / 成功提示 ----------------

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

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            if (!SetProperty(ref _isBusy, value)) return;
            OnPropertyChanged(nameof(CanSaveEntry));
            OnPropertyChanged(nameof(CanDeleteEntry));
            OnPropertyChanged(nameof(CanMoveUp));
            OnPropertyChanged(nameof(CanMoveDown));
            RefreshCommands();
        }
    }

    // ---------------- 右编辑区属性 ----------------

    /// <summary>编辑标题：新建 / 编辑。</summary>
    public string EditorTitle => IsNewEntry ? "新建条目" : "编辑条目";

    public bool IsNewEntry => _editingEntryId.Length == 0;

    public string NameText
    {
        get => _nameText;
        set
        {
            if (!SetProperty(ref _nameText, value)) return;
            ValidateEditor();
        }
    }

    /// <summary>关键词文本框原始内容（一行一个）；保存/预览使用实时清洗后的 <see cref="_normalizedKeywords"/>。</summary>
    public string KeywordsText
    {
        get => _keywordsText;
        set
        {
            if (!SetProperty(ref _keywordsText, value)) return;
            _normalizedKeywords = SplitKeywords(value);
            OnPropertyChanged(nameof(KeywordCountText));
            OnPropertyChanged(nameof(KeywordsPreview));
        }
    }

    public string ContentText
    {
        get => _contentText;
        set
        {
            if (!SetProperty(ref _contentText, value)) return;
            ValidateEditor();
        }
    }

    /// <summary>实时清洗结果提示（trim / 去空行 / 去重）。</summary>
    public string KeywordCountText => _normalizedKeywords.Count == 0
        ? "未填写关键词（允许）：本条目不参与关键词匹配。"
        : $"已识别 {_normalizedKeywords.Count} 个关键词（自动 trim / 去空行 / 去重）。";

    /// <summary>实时清洗后的关键词预览。</summary>
    public string KeywordsPreview => _normalizedKeywords.Count == 0
        ? ""
        : "关键词：" + string.Join("、", _normalizedKeywords);

    public string EditorValidationText
    {
        get => _editorValidationText;
        private set
        {
            if (!SetProperty(ref _editorValidationText, value)) return;
            OnPropertyChanged(nameof(ShowEditorValidation));
        }
    }

    public bool ShowEditorValidation => _editorValidationText.Length > 0;

    public bool CanSaveEntry => !_isBusy && _contentText.Trim().Length > 0;

    public bool CanDeleteEntry => !_isBusy && _selectedEntry != null;

    public bool CanMoveUp => !_isBusy && SelectedIndex > 0;

    public bool CanMoveDown => !_isBusy && SelectedIndex >= 0 && SelectedIndex < _allEntries.Count - 1;

    private int SelectedIndex => _selectedEntry == null ? -1 : _allEntries.IndexOf(_selectedEntry);

    // ---------------- 命令 ----------------

    public RelayCommand NewEntryCommand { get; }
    public RelayCommand SaveEntryCommand { get; }
    public RelayCommand DeleteEntryCommand { get; }
    public RelayCommand MoveUpCommand { get; }
    public RelayCommand MoveDownCommand { get; }
    public RelayCommand DismissErrorCommand { get; }

    // ---------------- 加载 / 刷新 ----------------

    /// <summary>打开窗口时加载该会话的世界书；无条目时直接进入“新建条目”状态。</summary>
    public async Task LoadAsync(CancellationToken ct = default)
    {
        IsBusy = true;
        ErrorText = "";
        StatusText = "";
        try
        {
            var list = await _backend.ListWorldbookAsync(_sessionId, ct);
            _allEntries.Clear();
            foreach (var entry in list) _allEntries.Add(new WorldBookItemViewModel(this, entry));
            _editingEntryId = "";
            OnPropertyChanged(nameof(EditorTitle));
            OnPropertyChanged(nameof(IsNewEntry));
            RebuildEntries();
            if (_allEntries.Count > 0)
            {
                SelectedEntry = _allEntries[0]; // 默认选中第一条并载入右侧编辑区
            }
            else
            {
                ClearEditorFields();
                EditorValidationText = "";
            }
        }
        catch (Exception ex)
        {
            ErrorText = "加载世界书失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
            ValidateEditor();
        }
    }

    /// <summary>
    /// 从服务端重新拉取列表（每次变更后调用，保证 UI 与服务端一致）。
    /// selectId 指定刷新后要选中的条目 id（如刚新建/刚移动的条目）；为空则尽量保持当前选中。
    /// </summary>
    public async Task ReloadAsync(string? selectId = null, CancellationToken ct = default)
    {
        string? preferred = selectId
                            ?? _selectedEntry?.Id
                            ?? (_editingEntryId.Length == 0 ? null : _editingEntryId);
        try
        {
            var list = await _backend.ListWorldbookAsync(_sessionId, ct);
            _allEntries.Clear();
            foreach (var entry in list) _allEntries.Add(new WorldBookItemViewModel(this, entry));
            RebuildEntries();

            var target = preferred == null ? null : _allEntries.FirstOrDefault(x => x.Id == preferred);
            SelectedEntry = target;
            if (target == null && _allEntries.Count == 0)
            {
                // 条目被删光：清空编辑区，进入新建状态
                _editingEntryId = "";
                OnPropertyChanged(nameof(EditorTitle));
                OnPropertyChanged(nameof(IsNewEntry));
                ClearEditorFields();
            }
        }
        catch (Exception ex)
        {
            ErrorText = "刷新世界书列表失败：" + ex.Message;
        }
        finally
        {
            RefreshCommands();
        }
    }

    // ---------------- 新建 / 保存 / 删除 ----------------

    /// <summary>进入“新建条目”状态：清空右侧编辑区。</summary>
    public void BeginNewEntry()
    {
        _editingEntryId = "";
        OnPropertyChanged(nameof(EditorTitle));
        OnPropertyChanged(nameof(IsNewEntry));
        SelectedEntry = null; // 仅清除列表选中，不触发编辑器重载
        ClearEditorFields();
        ErrorText = "";
        StatusText = "";
        ValidateEditor();
        RefreshCommands();
    }

    /// <summary>
    /// 保存右编辑区：新建走 CreateWorldbookEntryAsync，编辑走 UpdateWorldbookEntryAsync；
    /// 内容为空时按钮禁用（此处双保险直接返回），服务端拒绝的异常转错误条。
    /// </summary>
    public async Task SaveEntryAsync(CancellationToken ct = default)
    {
        if (!CanSaveEntry) return;

        bool isNew = IsNewEntry;
        var existing = _allEntries.FirstOrDefault(x => x.Id == _editingEntryId);
        var entry = new WorldBookEntry
        {
            SessionId = _sessionId,
            Name = _nameText.Trim(),
            Keywords = new List<string>(_normalizedKeywords),
            Content = _contentText.Trim(),
            // 编辑时保留服务端的启用状态与排序；新建由后端取 max(sort_order)+1
            Enabled = existing?.Enabled ?? true,
            SortOrder = existing?.SortOrder ?? 0,
        };

        IsBusy = true;
        ErrorText = "";
        StatusText = isNew ? "正在新建…" : "正在保存…";
        try
        {
            WorldBookEntry? saved;
            if (isNew)
                saved = await _backend.CreateWorldbookEntryAsync(_sessionId, entry, ct);
            else
                saved = await _backend.UpdateWorldbookEntryAsync(_sessionId, _editingEntryId, entry, ct);

            if (saved == null)
            {
                StatusText = "";
                ErrorText = "保存失败：条目不存在或已被删除。";
                await ReloadAsync(ct: ct);
                return;
            }

            await ReloadAsync(saved.Id, ct);
            StatusText = isNew ? "条目已新建。" : "条目已保存。";
        }
        catch (Exception ex)
        {
            StatusText = "";
            ErrorText = (isNew ? "新建" : "保存") + "世界书条目失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
            ValidateEditor();
        }
    }

    /// <summary>删除当前选中条目（需确认）；删除后刷新列表。</summary>
    public async Task DeleteSelectedEntryAsync(CancellationToken ct = default)
    {
        var item = _selectedEntry;
        if (item == null || _isBusy) return;

        string name = string.IsNullOrWhiteSpace(item.Name) ? "(未命名条目)" : item.Name;
        if (!_dialogs.Confirm($"确定删除世界书条目“{name}”吗？删除后不可恢复。", "删除世界书条目")) return;

        IsBusy = true;
        ErrorText = "";
        StatusText = "";
        try
        {
            bool deleted = await _backend.DeleteWorldbookEntryAsync(_sessionId, item.Id, ct);
            if (deleted)
            {
                StatusText = "条目已删除。";
            }
            else
            {
                ErrorText = "删除失败：条目不存在或已被删除。";
            }

            _editingEntryId = "";
            OnPropertyChanged(nameof(EditorTitle));
            OnPropertyChanged(nameof(IsNewEntry));
            await ReloadAsync(ct: ct);

            if (_allEntries.Count > 0)
                SelectedEntry = _allEntries[0];
            else
                BeginNewEntry();
        }
        catch (Exception ex)
        {
            ErrorText = "删除世界书条目失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
            ValidateEditor();
        }
    }

    // ---------------- 排序 ----------------

    public Task MoveUpAsync(CancellationToken ct = default) => MoveAsync(-1, ct);

    public Task MoveDownAsync(CancellationToken ct = default) => MoveAsync(1, ct);

    /// <summary>上移/下移：与相邻条目交换 sort_order 并逐个保存，随后刷新列表。</summary>
    private async Task MoveAsync(int direction, CancellationToken ct = default)
    {
        int index = SelectedIndex;
        int target = index + direction;
        if (_isBusy || index < 0 || target < 0 || target >= _allEntries.Count) return;

        var current = _allEntries[index];
        var other = _allEntries[target];

        IsBusy = true;
        ErrorText = "";
        StatusText = "";
        try
        {
            var first = await UpdateSortOrderAsync(current, other.SortOrder, ct);
            var second = first == null ? null : await UpdateSortOrderAsync(other, current.SortOrder, ct);

            if (second == null)
            {
                ErrorText = "移动失败：条目不存在或已被删除。";
            }
            else
            {
                StatusText = direction < 0 ? "已上移。" : "已下移。";
            }

            await ReloadAsync(current.Id, ct);
        }
        catch (Exception ex)
        {
            ErrorText = "移动条目失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
            RefreshCommands();
        }
    }

    private Task<WorldBookEntry?> UpdateSortOrderAsync(WorldBookItemViewModel item, int sortOrder, CancellationToken ct)
        => _backend.UpdateWorldbookEntryAsync(_sessionId, item.Id, item.ToModel(sortOrder: sortOrder), ct);

    // ---------------- 启停（列表勾选框直接 PATCH） ----------------

    /// <summary>勾选/取消勾选启用状态：直接 PATCH enabled；失败回滚勾选并显示错误条。</summary>
    public async Task SetEntryEnabledAsync(WorldBookItemViewModel item, bool enabled)
    {
        if (_isBusy)
        {
            item.SetEnabledSilently(!enabled); // 繁忙时拒绝并回滚
            return;
        }

        IsBusy = true;
        ErrorText = "";
        try
        {
            var updated = await _backend.UpdateWorldbookEntryAsync(
                _sessionId, item.Id, item.ToModel(enabled: enabled));
            if (updated == null)
            {
                item.SetEnabledSilently(!enabled);
                ErrorText = "更新启用状态失败：条目不存在或已被删除。";
            }
            else
            {
                StatusText = enabled ? $"已启用条目“{item.DisplayName}”。" : $"已停用条目“{item.DisplayName}”。";
                await ReloadAsync(_selectedEntry?.Id);
            }
        }
        catch (Exception ex)
        {
            item.SetEnabledSilently(!enabled);
            ErrorText = "更新启用状态失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
            RefreshCommands();
        }
    }

    // ---------------- 关键词实时清洗 ----------------

    /// <summary>
    /// 把关键词文本框内容规范化（trim / 去空行 / 去重）后回写。
    /// 由视图在 LostFocus 时调用：点击“保存条目”会先触发 LostFocus，用户可见清洗结果。
    /// </summary>
    public void NormalizeKeywordsText()
    {
        var canonical = string.Join(Environment.NewLine, _normalizedKeywords);
        if (!string.Equals(canonical, _keywordsText, StringComparison.Ordinal))
            KeywordsText = canonical;
    }

    // ---------------- 内部实现 ----------------

    private void LoadEditorFromEntry(WorldBookItemViewModel item)
    {
        _editingEntryId = item.Id;
        OnPropertyChanged(nameof(EditorTitle));
        OnPropertyChanged(nameof(IsNewEntry));
        NameText = item.Name;
        KeywordsText = string.Join(Environment.NewLine, item.Keywords);
        ContentText = item.Content;
        ValidateEditor();
    }

    private void ClearEditorFields()
    {
        NameText = "";
        KeywordsText = "";
        ContentText = "";
    }

    /// <summary>按搜索词重建左列表；尽量保持选中项（不因过滤而丢未保存编辑内容）。</summary>
    private void RebuildEntries()
    {
        string query = _searchText.Trim();
        var previous = _selectedEntry;

        _rebuildingEntries = true;
        try
        {
            Entries.Clear();
            foreach (var item in _allEntries)
            {
                if (item.Matches(query)) Entries.Add(item);
            }
        }
        finally
        {
            _rebuildingEntries = false;
        }

        // Entries.Clear() 会让 ListBox 把 SelectedItem 置空并回写 SelectedEntry=null；
        // 这里把仍然可见的选中项恢复回去，并抑制编辑器重载（保留未保存的编辑内容）。
        if (previous != null && Entries.Contains(previous))
        {
            _suppressEditorReload = true;
            try
            {
                SelectedEntry = previous;
            }
            finally
            {
                _suppressEditorReload = false;
            }
        }

        OnPropertyChanged(nameof(CanMoveUp));
        OnPropertyChanged(nameof(CanMoveDown));
        RefreshCommands();
    }

    private void ValidateEditor()
    {
        EditorValidationText = _contentText.Trim().Length == 0 ? "内容不能为空，禁止保存。" : "";
        OnPropertyChanged(nameof(CanSaveEntry));
        RefreshCommands();
    }

    private void RefreshCommands()
    {
        NewEntryCommand.NotifyCanExecuteChanged();
        SaveEntryCommand.NotifyCanExecuteChanged();
        DeleteEntryCommand.NotifyCanExecuteChanged();
        MoveUpCommand.NotifyCanExecuteChanged();
        MoveDownCommand.NotifyCanExecuteChanged();
    }

    /// <summary>关键词清洗（§5）：trim、去空行、去重，保持输入顺序。</summary>
    private static List<string> SplitKeywords(string text)
    {
        var result = new List<string>();
        foreach (var line in text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n'))
        {
            string keyword = line.Trim();
            if (keyword.Length == 0) continue;
            if (result.Contains(keyword, StringComparer.Ordinal)) continue;
            result.Add(keyword);
        }
        return result;
    }
}

/// <summary>
/// 世界书列表条目 VM：包装 <see cref="WorldBookEntry"/>，承载“启用勾选”的交互与列表展示文本。
/// 条目字段本身不可在列表内编辑（编辑走右侧编辑区），因此除 Enabled 外均为只读。
/// </summary>
public sealed class WorldBookItemViewModel : ObservableObject
{
    private readonly WorldBookViewModel _owner;
    private readonly WorldBookEntry _model;
    private bool _enabled;
    private bool _suppressEnabledWrite;

    internal WorldBookItemViewModel(WorldBookViewModel owner, WorldBookEntry model)
    {
        _owner = owner;
        _model = model;
        _enabled = model.Enabled;
    }

    public string Id => _model.Id;

    public string Name => _model.Name;

    public IReadOnlyList<string> Keywords => _model.Keywords;

    public string Content => _model.Content;

    /// <summary>排序权重：越小越靠前（列表已按此排序）。</summary>
    public int SortOrder => _model.SortOrder;

    public string DisplayName => string.IsNullOrWhiteSpace(_model.Name) ? "(未命名条目)" : _model.Name;

    public string KeywordSummary => _model.Keywords.Count == 0
        ? "（无关键词：不参与匹配）"
        : "关键词：" + string.Join("、", _model.Keywords);

    /// <summary>启用状态：勾选/取消时直接 PATCH（由父 VM 负责服务调用与失败回滚）。</summary>
    public bool Enabled
    {
        get => _enabled;
        set
        {
            if (!SetProperty(ref _enabled, value)) return;
            if (!_suppressEnabledWrite) _ = _owner.SetEntryEnabledAsync(this, value);
        }
    }

    /// <summary>程序性回滚/初始同步勾选状态（不触发 PATCH）。</summary>
    internal void SetEnabledSilently(bool value)
    {
        _suppressEnabledWrite = true;
        try
        {
            Enabled = value;
        }
        finally
        {
            _suppressEnabledWrite = false;
        }
    }

    /// <summary>构造更新/排序用的传输对象（保留未修改字段，避免 PATCH 丢字段）。</summary>
    internal WorldBookEntry ToModel(bool? enabled = null, int? sortOrder = null) => new()
    {
        Id = _model.Id,
        SessionId = _model.SessionId,
        Name = _model.Name,
        Keywords = new List<string>(_model.Keywords),
        Content = _model.Content,
        Enabled = enabled ?? _enabled,
        SortOrder = sortOrder ?? _model.SortOrder,
    };

    /// <summary>搜索匹配：名称或任一关键词包含搜索词（ASCII 忽略大小写）。</summary>
    public bool Matches(string query)
    {
        if (query.Length == 0) return true;
        if (_model.Name.Contains(query, StringComparison.OrdinalIgnoreCase)) return true;
        return _model.Keywords.Any(keyword => keyword.Contains(query, StringComparison.OrdinalIgnoreCase));
    }
}
