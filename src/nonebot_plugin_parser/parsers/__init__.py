# 导出所有 Parser 类
from .nga import NGAParser as NGAParser
from .base import BaseParser as BaseParser
from .acfun import AcfunParser as AcfunParser
from .weibo import WeiBoParser as WeiBoParser
from .douyin import DouyinParser as DouyinParser
from .twitter import TwitterParser as TwitterParser
from .bilibili import BilibiliParser as BilibiliParser
from .kuaishou import KuaiShouParser as KuaiShouParser
from ..download import yt_dlp_downloader as yt_dlp_downloader
from .xiaohongshu import XiaoHongShuParser as XiaoHongShuParser
from .instagram import InstagramParser as InstagramParser
from .pixiv import PixivParser as PixivParser
from .booth import BoothParser as BoothParser
from .heybox import HeyBoxParser as HeyBoxParser
from .goodsmile import GoodSmileParser as GoodSmileParser
from .qzone import QQZoneParser as QQZoneParser
from .pdqq import PDQQParser as PDQQParser

if yt_dlp_downloader is not None:
    from .tiktok import TikTokParser as TikTokParser
    from .youtube import YouTubeParser as YouTubeParser

from .base import handle
from .data import (
    Author,
    Platform,
    ParseResult,
    AudioContent,
    ImageContent,
    VideoContent,
)

__all__ = [
    "AudioContent",
    "Author",
    "BaseParser",
    "ImageContent",
    "ParseResult",
    "Platform",
    "VideoContent",
    "handle",
]
