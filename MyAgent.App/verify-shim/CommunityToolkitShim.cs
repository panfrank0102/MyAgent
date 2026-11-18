// ====================================================================
// 【离线验证垫片 verify-shim/ —— 仅在无 NuGet 网络的沙箱中使用】
// 本目录为“无 NuGet 网络（TLS 凭证缺失）”的沙箱提供离线编译/演示能力：
// 以手写最小实现补齐 CommunityToolkit.Mvvm 的 API 子集（ObservableObject /
// RelayCommand / AsyncRelayCommand / [ObservableProperty] 特性），
// 并在 GeneratedProps.*.cs 中手写“源生成器产物”（各 VM 的生成属性与 partial 方法声明）。
//
// 用法：
//   沙箱（离线）：dotnet build -p:UseLocalMvvmShim=true   → 使用本垫片编译，0 警告 0 错误
//   正常环境：   dotnet restore && dotnet build           → 使用真实 NuGet 包（垫片被排除）
//
// 注意：垫片必须与 ViewModels 中的 [ObservableProperty] 字段保持同步；
// 若新增字段需在对应 GeneratedProps.*.cs 中补写生成属性，或直接在联网环境验证。
// 联网环境可整体删除本目录（csproj 中的排除条件随即失效，无副作用）。
// ====================================================================
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Input;

namespace CommunityToolkit.Mvvm.ComponentModel
{
    /// <summary>临时垫片：[ObservableProperty] 特性（真实包中由源生成器消费）。</summary>
    [AttributeUsage(AttributeTargets.Field, AllowMultiple = false, Inherited = false)]
    public sealed class ObservablePropertyAttribute : Attribute
    {
    }

    /// <summary>临时垫片：ObservableObject（仅验证编译所需的 API 子集）。</summary>
    public abstract class ObservableObject : INotifyPropertyChanged
    {
        public event PropertyChangedEventHandler? PropertyChanged;

        protected void OnPropertyChanged([CallerMemberName] string? propertyName = null)
            => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));

        protected bool SetProperty<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
        {
            if (EqualityComparer<T>.Default.Equals(field, value)) return false;
            field = value;
            OnPropertyChanged(propertyName);
            return true;
        }
    }
}

namespace CommunityToolkit.Mvvm.Input
{
    /// <summary>临时垫片：IRelayCommand（真实包同名接口的最小形态）。</summary>
    public interface IRelayCommand : ICommand
    {
        void NotifyCanExecuteChanged();
    }

    /// <summary>临时垫片：RelayCommand。</summary>
    public sealed class RelayCommand : IRelayCommand
    {
        private readonly Action _execute;
        private readonly Func<bool>? _canExecute;

        public RelayCommand(Action execute) : this(execute, null) { }

        public RelayCommand(Action execute, Func<bool>? canExecute)
        {
            _execute = execute;
            _canExecute = canExecute;
        }

        public bool CanExecute(object? parameter) => _canExecute?.Invoke() ?? true;

        public void Execute(object? parameter) => _execute();

        public event EventHandler? CanExecuteChanged;

        public void NotifyCanExecuteChanged() => CanExecuteChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>临时垫片：AsyncRelayCommand。</summary>
    public sealed class AsyncRelayCommand : IRelayCommand
    {
        private readonly Func<Task> _execute;
        private readonly Func<bool>? _canExecute;

        public AsyncRelayCommand(Func<Task> execute) : this(execute, null) { }

        public AsyncRelayCommand(Func<Task> execute, Func<bool>? canExecute)
        {
            _execute = execute;
            _canExecute = canExecute;
        }

        public bool CanExecute(object? parameter) => _canExecute?.Invoke() ?? true;

        public void Execute(object? parameter) => _ = _execute();

        public event EventHandler? CanExecuteChanged;

        public void NotifyCanExecuteChanged() => CanExecuteChanged?.Invoke(this, EventArgs.Empty);
    }
}
