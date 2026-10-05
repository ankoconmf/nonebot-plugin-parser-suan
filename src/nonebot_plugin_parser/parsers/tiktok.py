import re
import json
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from .base import BaseParser, PlatformEnum, handle
from .data import Platform
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


def _universal_data(html: str) -> dict[str, Any] | None:
    """解析 `__UNIVERSAL_DATA_FOR_REHYDRATION__` 里的 JSON"""
    matched = _UNIVERSAL_DATA_PATTERN.search(html)
    if matched is None:
        return None

    try:
        data = json.loads(matched.group("data"))
    except (ValueError, TypeError):
        return None

    return data if isinstance(data, dict) else None


def extract_author_info(html: str) -> dict[str, Any]:
    """从 TikTok 网页 SSR 数据里取作者信息, 取不到返回空字典.

    视频页的数据在 `webapp.video-detail.itemInfo.itemStruct`,
    用户页在 `webapp.user-detail.userInfo`:

    - 粉丝数: `authorStats` / `stats` 的 `followerCount`
    - 头像: 作者节点的 `avatarLarger` / `avatarMedium` / `avatarThumb`
    """
    data = _universal_data(html)
    if data is None:
        return {}

    scope = data.get("__DEFAULT_SCOPE__") or {}
    if not isinstance(scope, dict):
        return {}

    video_detail = scope.get("webapp.video-detail") or {}
    item = ((video_detail.get("itemInfo") or {}).get("itemStruct")) or {}
    user_info = (scope.get("webapp.user-detail") or {}).get("userInfo") or {}
    if not isinstance(item, dict):
        item = {}
    if not isinstance(user_info, dict):
        user_info = {}

    info: dict[str, Any] = {}
    for holder in (
        item.get("authorStats"),
        item.get("stats"),
        user_info.get("stats"),
    ):
        if isinstance(holder, dict) and holder.get("followerCount") is not None:
            info["follower_count"] = holder["followerCount"]
            break

    for holder in (item.get("author"), user_info.get("user"), user_info):
        if not isinstance(holder, dict):
            continue
        for key in ("avatarLarger", "avatarMedium", "avatarThumb"):
            if isinstance(url := holder.get(key), str) and url:
                info["avatar"] = url
                break
        if "avatar" in info:
            break

    return info


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
        # 作者信息 (粉丝数 / 头像): yt-dlp 都不提供, 从网页 SSR 数据里取 (失败则跳过)
        author_info = await self._fetch_author_info(url, video_info.uploader)
        extra.update(
            followers_extra(author_info.get("follower_count") or video_info.channel_follower_count)
        )

        # TikTok CDN 的头像下载需要 Referer
        self.headers["Referer"] = "https://www.tiktok.com/"
        author = self.create_author(video_info.channel, author_info.get("avatar"))

        return self.result(
            title=video_info.title,
            author=author,
            contents=[video_content],
            timestamp=video_info.timestamp,
            extra=extra,
        )

    async def _fetch_author_info(self, url: str, uploader: str | None = None) -> dict[str, Any]:
        """从网页 SSR 数据里取作者信息 (粉丝数 / 头像).

        视频页经常被 WAF 拦(只返回 JS 挑战页), 作者主页通常能正常返回 SSR 数据,
        所以优先用 `@用户名` 主页, 拿不到再退回视频页; 都拿不到时返回空字典,
        不影响视频解析。TikTok 的 user/detail 接口需要签名(msToken/X-Bogus), 不用。
        """
        handle = self._extract_handle(url) or (uploader or "").lstrip("@") or None

        candidates: list[str] = []
        if handle:
            candidates.append(f"https://www.tiktok.com/@{handle}")
        candidates.append(url)

        for candidate in dict.fromkeys(candidates):
            html = await self._fetch_html(candidate)
            if html and (info := extract_author_info(html)):
                return info

        return {}

    @staticmethod
    def _extract_handle(url: str) -> str | None:
        """从视频/主页 URL 里取 @用户名"""
        matched = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", url)
        return matched.group(1) if matched else None

    async def _fetch_html(self, url: str) -> str | None:
        """请求 TikTok 网页 (被风控/结构变化时返回 None)"""
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
                return response.text
        except Exception:
            logger.debug(f"请求 TikTok 页面失败: {url}", exc_info=True)
            return None
