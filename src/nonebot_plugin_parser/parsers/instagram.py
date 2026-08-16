import re
from nonebot import logger
from typing import Any, ClassVar

from httpx import AsyncClient

from ..config import pconfig
from .base import BaseParser, PlatformEnum, handle
from .data import Platform
from .utils import fmt_stat
from ..exception import ParseException


def _as_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        for key in ("count", "total", "total_count", "value"):
            if key in value:
                return _as_count(value[key])
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return max(count, 0)


def _extract_stats(data: dict[str, Any]) -> list[dict[str, str]]:
    """兼容 RapidAPI 扁平响应与 Instagram GraphQL 响应中的统计字段。"""
    payloads = [data]
    for path in (
        ("data",),
        ("result",),
        ("post",),
        ("media",),
        ("shortcode_media",),
        ("xdt_shortcode_media",),
        ("graphql", "shortcode_media"),
        ("data", "shortcode_media"),
        ("data", "xdt_shortcode_media"),
    ):
        value: Any = data
        for key in path:
            if not isinstance(value, dict):
                break
            value = value.get(key)
        if isinstance(value, dict):
            payloads.append(value)

    items = data.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        payloads.append(items[0])

    def find_count(*keys: str) -> int | None:
        for payload in payloads:
            for key in keys:
                if key in payload and (count := _as_count(payload[key])) is not None:
                    return count
        return None

    stats = []
    for icon, value, label in (
        (
            "eye",
            find_count(
                "video_view_count",
                "video_play_count",
                "play_count",
                "view_count",
                "playCount",
                "viewCount",
                "plays",
                "views",
            ),
            "播放",
        ),
        (
            "like",
            find_count(
                "like_count",
                "likes_count",
                "likeCount",
                "likes",
                "edge_media_preview_like",
                "edge_liked_by",
            ),
            "点赞",
        ),
        (
            "comment",
            find_count(
                "comment_count",
                "comments_count",
                "commentCount",
                "comments",
                "edge_media_to_parent_comment",
                "edge_media_to_comment",
                "edge_media_preview_comment",
            ),
            "评论",
        ),
        (
            "star",
            find_count(
                "save_count",
                "saved_count",
                "saveCount",
                "saves",
                "bookmark_count",
            ),
            "收藏",
        ),
        (
            "share",
            find_count(
                "share_count",
                "shares_count",
                "shareCount",
                "shares",
            ),
            "分享",
        ),
    ):
        if value is not None:
            stats.append({"icon": icon, "value": fmt_stat(value), "label": label})
    return stats


class InstagramParser(BaseParser):
    """Instagram (RapidAPI) 解析器，使用插件配置 `pconfig` 中的字段：

    - `pconfig.instagram_rapidapi_key`
    - `pconfig.instagram_rapidapi_host`
    - `pconfig.instagram_proxy`
    """

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.INSTAGRAM, display_name="Instagram")

    @handle("instagr.am", r"instagr\.am/\w+[\S]*")
    async def _parse_short(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        logger.debug(f"InstagramParser._parse_short matched: {url}")
        return await self.parse_with_redirect(url)

    @handle(
        "instagram.com",
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|stories)/[^\s]+',
    )
    async def _parse(self, searched: re.Match[str]):
        url = searched.group(0)
        logger.debug(f"InstagramParser._parse called with: {url}")

        rapidapi_key = pconfig.instagram_rapidapi_key
        rapidapi_host = pconfig.instagram_rapidapi_host or "instagram-looter2.p.rapidapi.com"
        proxy = pconfig.instagram_proxy or pconfig.proxy

        if not rapidapi_key:
            raise ParseException("Instagram RapidAPI key 未配置，请在插件配置中设置 `instagram_rapidapi_key`。")

        api_url = f"https://{rapidapi_host}/post"
        headers = {
            "x-rapidapi-key": rapidapi_key,
            "x-rapidapi-host": rapidapi_host,
        }

        async with AsyncClient(headers=headers, proxy=proxy, timeout=self.timeout, verify=False) as client:
            resp = await client.get(api_url, params={"url": url})
            if resp.status_code != 200:
                raise ParseException(f"RapidAPI 返回错误: {resp.status_code}")
            data = resp.json()

        # 解析响应并构建 ParseResult，复用 BaseParser 的 create_* 方法
        caption = ""
        try:
            if data.get("caption"):
                caption = data.get("caption")
            elif data.get("edge_media_to_caption"):
                edges = data["edge_media_to_caption"].get("edges", [])
                if edges:
                    caption = edges[0].get("node", {}).get("text", "")
        except Exception:
            caption = ""

        contents = []
        try:
            if data.get("edge_sidecar_to_children"):
                edges = data["edge_sidecar_to_children"].get("edges", [])
                for edge in edges:
                    node = edge.get("node", {})
                    if node.get("is_video") and node.get("video_url"):
                        contents.append(self.create_video(node.get("video_url"), node.get("display_url"), node.get("duration", 0)))
                    elif node.get("display_url"):
                        contents.extend(self.create_images([node.get("display_url")]))
            elif data.get("video_url"):
                contents.append(self.create_video(data.get("video_url"), data.get("display_url"), data.get("duration", 0)))
            elif data.get("display_url"):
                contents.extend(self.create_images([data.get("display_url")] ))
            else:
                raise ParseException("未找到媒体链接")
        except Exception as e:
            raise ParseException(f"解析媒体失败: {e}")

        author = None
        try:
            author_name = None
            avatar = None
            if data.get("author"):
                author_name = data["author"].get("name")
                avatar = data["author"].get("avatar")
            elif data.get("owner"):
                owner = data["owner"]
                author_name = owner.get("username") or owner.get("id")
                avatar = owner.get("profile_pic_url")
            if author_name:
                author = self.create_author(author_name, avatar)
        except Exception:
            author = None

        stats = _extract_stats(data)

        return self.result(
            title=None,
            text=caption or None,
            author=author,
            contents=contents,
            timestamp=data.get("timestamp") or data.get("taken_at_timestamp"),
            url=url,
            extra={"stats": stats} if stats else {},
        )
