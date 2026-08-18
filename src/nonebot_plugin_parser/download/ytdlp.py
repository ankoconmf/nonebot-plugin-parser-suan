import asyncio
from typing import TYPE_CHECKING
from pathlib import Path
from collections import defaultdict

import yt_dlp
from msgspec import Struct, convert
from nonebot import logger

from .task import auto_task
from ..utils import LimitedSizeDict, generate_file_name
from ..config import pconfig
from ..exception import ParseException, IgnoreException


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""
    duration: int | None = None
    """时长 (直播/未开播时为 None)"""
    view_count: int | None = None
    """观看/播放数"""
    concurrent_view_count: int | None = None
    """直播实时观看人数"""
    like_count: int | None = None
    """点赞数"""
    comment_count: int | None = None
    """评论数"""
    repost_count: int | None = None
    """分享/转发数"""
    live_status: str | None = None
    """直播状态 (is_live/is_upcoming/was_live/not_live)"""

    @property
    def is_live(self) -> bool:
        """是否为正在直播或预约中的直播"""
        return self.live_status in ("is_live", "is_upcoming")

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class YtdlpDownloader:
    def __init__(self):
        if TYPE_CHECKING:
            from yt_dlp import _Params

        self._video_info_mapping = LimitedSizeDict[str, VideoInfo]()
        self._extract_base_opts: _Params = {
            "quiet": True,
            "skip_download": "1",
            "force_generic_extractor": True,
            "extractor_args": {"youtube": {"player_client": ["web_embedded"]}},
            "remote_components": ["ejs:github"],
        }
        self._download_base_opts: _Params = {
            "extractor_args": {"youtube": {"player_client": ["web_embedded"]}},
            "remote_components": ["ejs:github"],
        }
        self._url_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        if proxy := pconfig.proxy:
            self._download_base_opts["proxy"] = proxy
            self._extract_base_opts["proxy"] = proxy

    async def extract_video_info(self, url: str, cookiefile: Path | None = None) -> VideoInfo:
        """Get video info by yt-dlp"""

        video_info = self._video_info_mapping.get(url, None)
        if video_info:
            return video_info
        ydl_opts = self._extract_base_opts.copy()

        if cookiefile:
            ydl_opts["cookiefile"] = str(cookiefile)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await asyncio.to_thread(ydl.extract_info, url, download=False)
            if not info_dict:
                raise ParseException("获取视频信息失败")

        video_info = convert(info_dict, VideoInfo)
        self._video_info_mapping[url] = video_info
        return video_info

    @auto_task
    async def download_video(self, url: str, cookiefile: Path | None = None) -> Path:
        """Download video by yt-dlp"""

        video_info = await self.extract_video_info(url, cookiefile)
        duration = video_info.duration
        if duration is None:
            logger.warning(f"视频时长未知 (直播), 取消下载: {url}")
            raise IgnoreException
        if duration > pconfig.duration_maximum:
            logger.warning(f"视频时长 {duration} 秒, 超过 {pconfig.duration_maximum} 秒, 取消下载")
            raise IgnoreException

        video_path = pconfig.cache_dir / generate_file_name(url, ".mp4")
        if video_path.exists():
            return video_path

        async with self._url_locks[url]:
            if video_path.exists():
                return video_path

            pconfig.cache_dir.mkdir(parents=True, exist_ok=True)

            ydl_opts = self._download_base_opts.copy()
            ydl_opts["outtmpl"] = str(video_path)
            ydl_opts["merge_output_format"] = "mp4"
            ydl_opts["format"] = f"bv[filesize<={duration // 10 + 10}M]+ba/b[filesize<={duration // 8 + 10}M]"
            ydl_opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]

            if cookiefile:
                ydl_opts["cookiefile"] = str(cookiefile)

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    await asyncio.to_thread(ydl.download, [url])
            except Exception:
                if video_path.exists():
                    return video_path
                raise
        return video_path

    @auto_task
    async def download_audio(self, url: str, cookiefile: Path | None = None) -> Path:
        """Download audio by yt-dlp"""

        file_name = generate_file_name(url)
        audio_path = pconfig.cache_dir / f"{file_name}.flac"
        if audio_path.exists():
            return audio_path

        async with self._url_locks[url]:
            if audio_path.exists():
                return audio_path

            pconfig.cache_dir.mkdir(parents=True, exist_ok=True)

            ydl_opts = self._download_base_opts.copy()
            ydl_opts["outtmpl"] = f"{pconfig.cache_dir / file_name}.%(ext)s"
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                    "preferredquality": "0",
                }
            ]

            if cookiefile:
                ydl_opts["cookiefile"] = str(cookiefile)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    await asyncio.to_thread(ydl.download, [url])
            except Exception:
                if audio_path.exists():
                    return audio_path
                raise
        return audio_path
