namespace MyAgent.App.Models;

/// <summary>NDJSON 流式事件类型（§4.2：meta 必发；delta 可多次；done/error 恰一次且为最后一行）。</summary>
public enum ChatEventType
{
    Meta,
    Delta,
    Done,
    Error,
}

/// <summary>done 事件附带的 token 用量（§4.2，usage 可空）。</summary>
public sealed record ChatUsage(long? PromptTokens, long? CompletionTokens);

/// <summary>
/// 一次生成请求的流式事件：§4.2 NDJSON 协议的强类型化。
/// 事件类型是保留字段；消费方忽略未知事件类型（向前兼容）。
/// 由服务层（FakeBackendService / 将来的 ApiClient）产生，界面层只消费。
/// </summary>
public sealed class ChatEvent
{
    public ChatEventType Type { get; }

    /// <summary>meta：本轮命中的世界书条目（未命中时为空数组，meta 事件必发）。</summary>
    public IReadOnlyList<MatchedEntry> MatchedEntries { get; }

    /// <summary>delta：部分文本。多个 delta 由界面端 30ms 节流合并刷新。</summary>
    public string DeltaText { get; }

    /// <summary>done：完整回复文本。</summary>
    public string FullText { get; }

    /// <summary>done：token 用量（可空）。</summary>
    public ChatUsage? Usage { get; }

    /// <summary>error：机器可读错误码（auth_failed / rate_limited / timeout / upstream_unreachable / internal 等）。</summary>
    public string ErrorCode { get; }

    /// <summary>error：人类可读中文提示（§6.1 第 10 点）。</summary>
    public string ErrorMessage { get; }

    private ChatEvent(
        ChatEventType type,
        IReadOnlyList<MatchedEntry>? matchedEntries,
        string? deltaText,
        string? fullText,
        ChatUsage? usage,
        string? errorCode,
        string? errorMessage)
    {
        Type = type;
        MatchedEntries = matchedEntries ?? Array.Empty<MatchedEntry>();
        DeltaText = deltaText ?? "";
        FullText = fullText ?? "";
        Usage = usage;
        ErrorCode = errorCode ?? "";
        ErrorMessage = errorMessage ?? "";
    }

    public static ChatEvent CreateMeta(IReadOnlyList<MatchedEntry> matchedEntries)
        => new(ChatEventType.Meta, matchedEntries, null, null, null, null, null);

    public static ChatEvent CreateDelta(string text)
        => new(ChatEventType.Delta, null, text, null, null, null, null);

    public static ChatEvent CreateDone(string fullText, ChatUsage? usage = null)
        => new(ChatEventType.Done, null, null, fullText, usage, null, null);

    public static ChatEvent CreateError(string code, string message)
        => new(ChatEventType.Error, null, null, null, null, code, message);
}
