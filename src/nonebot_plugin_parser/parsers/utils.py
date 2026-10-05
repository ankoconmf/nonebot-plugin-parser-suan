def fmt_duration(duration: float) -> str:
    """格式化媒体时长，超过 1 小时后显示为 h:mm:ss。"""
    total_seconds = max(int(duration), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def followers_extra(count: int | str | None, label: str = "粉丝") -> dict[str, str]:
    """粉丝(订阅)数 extra: 模板渲染在作者名下方、时间前面。

    - int / 纯数字字符串按 `fmt_stat` 格式化为 万/亿
    - 平台已格式化的文本(如 "41.6万" / "8.19K")原样使用
    - 取不到或为 0 时返回空字典, 不渲染
    """
    if count is None:
        return {}

    text = fmt_stat(count)
    if not text or text == "0":
        return {}

    # subscribers_label 决定单位文案: YouTube 用 "订阅", 其它平台用 "粉丝"
    return {"subscribers": text, "subscribers_label": label}


def fmt_stat(count: int | str | None) -> str:
    """格式化统计数字，超过 1 万显示为 x.x万，超过 1 亿显示为 x.x亿。"""
    try:
        n = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(count) if count else "0"
    if n < 0:
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)
