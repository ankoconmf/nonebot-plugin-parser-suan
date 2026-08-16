import re
from typing import ClassVar

from .base import BaseParser, PlatformEnum, handle
from .data import Author, Platform
from .utils import fmt_stat
from ..download import yt_dlp_downloader


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

        return self.result(
            title=video_info.title,
            author=Author(name=video_info.channel),
            contents=[video_content],
            timestamp=video_info.timestamp,
            extra={"stats": stats} if stats else {},
        )
