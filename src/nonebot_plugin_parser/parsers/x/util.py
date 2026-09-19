"""X 卡片解析

X 的卡片有几种形态:
- 新版 `unified_card`: 卡片内容是一段 JSON 字符串, 放在 binding_values 的 unified_card 里;
- 旧版 `summary` / `summary_large_image` / `player` 等: 直接从 binding_values 取字段;
- 投票卡 `poll*`: 选项为 choice1_label / choice1_count 这种成对字段, 由 `parse_poll` 解析。
"""

from dataclasses import dataclass

from msgspec import DecodeError
from msgspec.json import decode

from .model import CardValue, Poll, PollChoice, TweetCard, UnifiedCard, UnifiedText


@dataclass(frozen=True, slots=True)
class LinkCardData:
    url: str
    title: str
    site_name: str | None = None
    description: str | None = None
    preview_url: str | None = None


def _binding_values(card: TweetCard) -> dict[str, CardValue]:
    if card.legacy is None:
        return {}
    return {item.key: item.value for item in card.legacy.binding_values if item.key}


def is_poll_card(card: TweetCard | None) -> bool:
    """是否是投票卡 (name 形如 poll4choice_text_only)"""
    return bool(card and card.legacy and card.legacy.name.startswith("poll"))


def _string_value(values: dict[str, CardValue], key: str) -> str | None:
    value = values.get(key)
    text = value.string_value if value else None
    return text if isinstance(text, str) and text else None


def _int_value(values: dict[str, CardValue], key: str) -> int:
    raw = _string_value(values, key)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def parse_poll(card: TweetCard | None) -> Poll | None:
    """解析投票卡; 非投票卡或没有选项时返回 None"""
    if not is_poll_card(card) or card is None:
        return None
    values = _binding_values(card)

    choices: list[PollChoice] = []
    index = 1
    while True:
        label = _string_value(values, f"choice{index}_label")
        if label is None:
            break
        choices.append(
            PollChoice(label=label, count=_int_value(values, f"choice{index}_count"))
        )
        index += 1

    if not choices:
        return None

    total = sum(choice.count for choice in choices)
    for choice in choices:
        choice.percent = round(choice.count / total * 100, 1) if total else 0.0

    return Poll(
        choices=choices,
        total_votes=total,
        ends_at=_string_value(values, "end_datetime_utc") or "",
    )


def _decode_unified(card: TweetCard) -> UnifiedCard | None:
    if card.legacy is None:
        return None
    for binding in card.legacy.binding_values:
        if binding.key != "unified_card" or binding.value.string_value is None:
            continue
        raw = binding.value.string_value
        if not raw:
            continue
        try:
            return decode(raw, type=UnifiedCard)
        except (DecodeError, TypeError):
            continue
    return None


def _text_content(value: str | UnifiedText | None) -> str | None:
    if isinstance(value, UnifiedText):
        return value.content or None
    return value or None


def _unified_data(card: UnifiedCard) -> LinkCardData | None:
    url_data = next(
        (
            destination.data.url_data
            for destination in card.destination_objects.values()
            if destination.type == "browser" and destination.data.url_data
        ),
        None,
    )
    if not url_data or not url_data.url:
        return None

    title = site_name = description = preview_url = None
    for component_name in card.components:
        component = card.component_objects.get(component_name)
        if component is None:
            continue
        data = component.data
        if component.type == "details":
            title = _text_content(data.title) or title
            site_name = _text_content(data.subtitle) or site_name
            description = (
                _text_content(data.description)
                or _text_content(data.summary)
                or description
            )
        elif component.type == "media" and data.id:
            media = card.media_entities.get(data.id)
            if media and media.media_url_https:
                preview_url = media.media_url_https

    return LinkCardData(
        url=url_data.url,
        title=title or site_name or url_data.url,
        site_name=site_name or url_data.vanity,
        description=description,
        preview_url=preview_url,
    )


def _legacy_data(card: TweetCard) -> LinkCardData | None:
    if card.legacy is None:
        return None
    values = _binding_values(card)

    def string_value(key: str) -> str | None:
        value = values.get(key)
        text = value.string_value if value else None
        return text if isinstance(text, str) and text else None

    def image_url(*keys: str) -> str | None:
        for key in keys:
            value = values.get(key)
            image = value.image_value if value else None
            url = image.url if image else None
            if isinstance(url, str) and url:
                return url
        return None

    url = string_value("card_url") or card.legacy.url
    if not url:
        return None
    return LinkCardData(
        url=url,
        title=string_value("title") or url,
        site_name=string_value("vanity_url") or string_value("domain"),
        description=string_value("description"),
        preview_url=image_url(
            "thumbnail_image_large",
            "thumbnail_image",
            "player_image_large",
            "player_image",
            "photo_image_full_size_large",
            "photo_image_full_size",
        ),
    )


def parse_link_card(card: TweetCard | None) -> LinkCardData | None:
    """解析推文的链接卡片, 非卡片推文返回 None"""
    if card is None:
        return None
    # 投票卡不是链接卡, 否则会解析出一个假的 "https://twitter.com" 链接
    if is_poll_card(card):
        return None
    unified = _decode_unified(card)
    return _unified_data(unified) if unified is not None else _legacy_data(card)
