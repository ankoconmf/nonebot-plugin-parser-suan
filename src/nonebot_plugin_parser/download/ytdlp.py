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
    release_timestamp: int | None = None
    """预约开播时间戳"""

    @property
    def is_live(self) -> bool:
        """是否为正在直播或预约中的直播"""
        return self.live_status in ("is_live", "is_upcoming")

    @property
    def is_upcoming(self) -> bool:
        """是否为预约中(未开播)的直播"""
        return self.live_status == "is_upcoming"

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class YtdlpDownloader:
    def __init__(self):
        if TYPE_CHECKING:
            from yt_dlp import _Params

        self._video_info_mapping = LimitedSizeDict[str, VideoInfo]()
        # 带 cookies 取不到流、必须改用匿名的 URL (下载时也要保持一致)
        self._no_cookie_urls = LimitedSizeDict[str, bool]()
        self._extract_base_opts: _Params = {
            "quiet": True,
            "skip_download": "1",
            "force_generic_extractor": True,
            "extractor_args": {"youtube": {"player_client": ["web_embedded", "default", "-android_vr"]}},
            "remote_components": ["ejs:github"],
        }
        self._download_base_opts: _Params = {
            "extractor_args": {"youtube": {"player_client": ["web_embedded", "default", "-android_vr"]}},
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

        base_opts = self._extract_base_opts.copy()
        use_cookies = bool(cookiefile and cookiefile.exists())
        if use_cookies:
            base_opts["cookiefile"] = str(cookiefile)

        def _extract(opts: dict) -> dict | None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        # 依次尝试 (选项, 是否带 cookies, 是否只接受"未开播"):
        # 1) 正常
        # 2) 去掉 cookies —— 有些视频在登录态下反而拿不到流(账号受限), 匿名却可以
        # 3) 放开 no formats —— 未开播的直播没有任何格式, 只用这一次取元数据
        candidates: list[tuple[dict, bool, bool]] = [(base_opts, use_cookies, False)]
        if use_cookies:
            candidates.append((self._extract_base_opts.copy(), False, False))
        candidates.append(({**base_opts, "ignore_no_formats_error": True}, use_cookies, True))

        first_error: Exception | None = None
        info_dict: dict | None = None
        for opts, with_cookies, upcoming_only in candidates:
            try:
                result = await asyncio.to_thread(_extract, opts)
            except Exception as error:
                if first_error is None:
                    first_error = error
                continue
            # 第 3 种只接受"未开播", 其它情况(登录失效/地区限制)不能吞掉错误
            if not isinstance(result, dict) or (upcoming_only and result.get("live_status") != "is_upcoming"):
                continue
            info_dict = result
            if use_cookies and not with_cookies:
                # 记住这个 URL 要匿名, 下载时保持一致(流的 URL 与请求会话绑定)
                self._no_cookie_urls[url] = True
            break

        if info_dict is None:
            if first_error is not None:
                raise first_error from None
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

            # 提取时若发现该视频在登录态下拿不到流, 下载也必须匿名
            if cookiefile and cookiefile.exists() and url not in self._no_cookie_urls:
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

            # 提取时若发现该视频在登录态下拿不到流, 下载也必须匿名
            if cookiefile and cookiefile.exists() and url not in self._no_cookie_urls:
                ydl_opts["cookiefile"] = str(cookiefile)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    await asyncio.to_thread(ydl.download, [url])
            except Exception:
                if audio_path.exists():
                    return audio_path
                raise
        return audio_path
