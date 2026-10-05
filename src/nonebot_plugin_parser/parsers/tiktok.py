import re
import json
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from .base import BaseParser, PlatformEnum, handle
from .data import Author, Platform
from .utils import fmt_stat, followers_extra
from ..config import pconfig
from ..download import yt_dlp_downloader

_UNIVERSAL_DATA_PATTERN = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(?P<data>.*?)</script>',
    re.DOTALL,
)
"""视频/用户页 SSR 数据所在 script 标签"""

_TIKTOK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""TikTok 网页 UA (COMMON_HEADER 的旧 UA 会被判定为爬虫)"""


def extract_follower_count(html: str) -> int | str | None:
    """从 TikTok 网页 SSR 数据里取博主粉丝数, 取不到返回 None.

    视频页的数据在 `webapp.video-detail.itemInfo.itemStruct`,
    用户页在 `webapp.user-detail.userInfo`, 粉丝数在
    `authorStats` / `stats` / `author` 之一的 `followerCount` 字段。
    """
    matched = _UNIVERSAL_DATA_PATTERN.search(html)
    if matched is None:
        return None

    try:
        data = json.loads(matched.group("data"))
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None
    scope = data.get("__DEFAULT_SCOPE__") or {}
    if not isinstance(scope, dict):
        return None

    for detail_key, node_keys in (
        ("webapp.video-detail", ("itemInfo", "itemStruct")),
        ("webapp.user-detail", ("userInfo",)),
    ):
        node: Any = scope.get(detail_key)
        for node_key in node_keys:
            node = node.get(node_key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue

        for key in ("authorStats", "stats", "author"):
            holder = node.get(key)
            if isinstance(holder, dict) and holder.get("followerCount") is not None:
                return holder["followerCount"]

    return None


class TikTokParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.TIKTOK, display_name="TikTok")

    @handle("tiktok", r"(www|vt|vm)\.tiktok\.com/[A-Za-z0-9._?%&+\-=/#@]*")
    async def _parse(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url, prefix = f"https://{searched.group(0)}", searched.group(1)

        if prefix in ("vt", "vm"):
            url = await self.get_redirect_url(url)

        # 获取视频信息
        video_info = await yt_dlp_downloader.extract_video_info(url)

        # 下载封面和视频
        video = yt_dlp_downloader.download_video(url)
        video_content = self.create_video(
            video,
            video_info.thumbnail,
            duration=video_info.duration,
        )

        stats = []
        for icon, value, label in (
            ("eye", video_info.view_count, "播放"),
            ("like", video_info.like_count, "点赞"),
            ("comment", video_info.comment_count, "评论"),
            ("share", video_info.repost_count, "分享"),
        ):
            if value is not None:
                stats.append({"icon": icon, "value": fmt_stat(value), "label": label})

        extra: dict[str, Any] = {"stats": stats} if stats else {}
        # yt-dlp 已提供粉丝数时直接用, 否则从网页 SSR 数据里取 (失败则跳过)
        follower_count = video_info.channel_follower_count
        if follower_count is None:
            follower_count = await self._fetch_follower_count(url)
        extra.update(followers_extra(follower_count))

        return self.result(
            title=video_info.title,
            author=Author(name=video_info.channel),
            contents=[video_content],
            timestamp=video_info.timestamp,
            extra=extra,
        )

    async def _fetch_follower_count(self, url: str) -> int | str | None:
        """获取博主粉丝数.

        TikTok 的 user/detail 接口需要签名(msToken/X-Bogus), 这里退而解析视频页
        HTML 里的 SSR 数据; 被风控或页面结构变化时静默返回 None, 不影响解析。
        """
        try:
            async with AsyncClient(
                headers={"User-Agent": _TIKTOK_UA},
                proxy=pconfig.proxy,
                timeout=5,
                verify=False,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                if response.status_code != 200:
                    return None
                return extract_follower_count(response.text)
        except Exception:
            logger.debug(f"获取 TikTok 粉丝数失败: {url}", exc_info=True)
            return None
