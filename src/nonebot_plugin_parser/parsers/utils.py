def fmt_duration(duration: float) -> str:
    """格式化媒体时长，超过 1 小时后显示为 h:mm:ss。"""
    total_seconds = max(int(duration), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


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
