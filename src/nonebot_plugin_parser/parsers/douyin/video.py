from random import choice

from msgspec import Struct, field

from ..utils import fmt_stat, fmt_duration


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int

    @property
    def video_url(self) -> str | None:
        return (
            choice(self.play_addr.url_list).replace("playwm", "play")
            if self.play_addr.url_list
            else None
        )

    @property
    def cover_url(self) -> str | None:
        return choice(self.cover.url_list) if self.cover.url_list else None


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)

    @property
    def image_url(self) -> str | None:
        return choice(self.url_list) if self.url_list else None

    @property
    def video_url(self) -> str | None:
        return self.video.video_url if self.video else None

    @property
    def cover_url(self) -> str | None:
        return self.video.cover_url if self.video else None

    @property
    def duration(self) -> int | None:
        """实况图视频时长(秒)"""
        return self.video.duration // 1000 if self.video else None


class PlayUrl(Struct):
    url_list: list[str] = field(default_factory=list)


class ShareInfo(Struct):
    """分享信息.

    share_desc_info 为完整分享文案(前缀固定是 '#{share_desc}#'),
    web API 的 desc 字段可能被截断成 '……版本过低，升级后可展示全部信息',
    用这里拿完整标题.
    """

    share_desc: str = ""
    share_desc_info: str = ""

    @property
    def text(self) -> str:
        """去掉开头固定的 '#在抖音，记录美好生活#' 前缀"""
        prefix = f"#{self.share_desc}#" if self.share_desc else ""
        if prefix and self.share_desc_info.startswith(prefix):
            return self.share_desc_info[len(prefix):]
        return self.share_desc_info


class Music(Struct):
    duration: int = 0
    play_url: PlayUrl = field(default_factory=PlayUrl)

    @property
    def audio_url(self) -> str | None:
        return choice(self.play_url.url_list) if self.play_url.url_list else None


class Statistics(Struct):
    digg_count: int = 0
    """点赞"""
    comment_count: int = 0
    """评论"""
    collect_count: int = 0
    """收藏"""
    share_count: int = 0
    """分享"""


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None
    statistics: Statistics | None = None
    music: Music | None = None
    """背景音乐(图集/图文/实况图)"""
    share_info: ShareInfo | None = None
    """分享信息, 含完整标题(desc 可能被截断成'版本过低')"""

    @property
    def title(self) -> str:
        """标题: 优先用 share_info 的完整文案, 兜底用 desc"""
        if self.share_info and (text := self.share_info.text):
            return text
        return self.desc

    @property
    def stats_panel(self) -> list[dict[str, str]]:
        """卡片底部互动数据面板 (点赞/评论/收藏/分享)"""
        if self.statistics is None:
            return []
        return [
            {"icon": "like", "value": fmt_stat(self.statistics.digg_count), "label": "点赞"},
            {"icon": "comment", "value": fmt_stat(self.statistics.comment_count), "label": "评论"},
            {"icon": "star", "value": fmt_stat(self.statistics.collect_count), "label": "收藏"},
            {"icon": "share", "value": fmt_stat(self.statistics.share_count), "label": "分享"},
        ]

    @property
    def meta_line(self) -> list[dict[str, str]]:
        """封面下 meta 行 (时长); 图文无时长返回空.

        图文/实况图的 detail 响应顶层 video 是占位符(duration=0),
        不能据此显示时长, 有 images 即视为图文.
        """
        if self.images or self.duration is None:
            return []
        return [{"icon": "clock", "text": fmt_duration(self.duration)}]

    @property
    def video_url(self) -> str | None:
        return choice(self.video.play_addr.url_list).replace("playwm", "play") if self.video else None

    @property
    def cover_url(self) -> str | None:
        return choice(self.video.cover.url_list) if self.video else None

    @property
    def duration(self) -> int | None:
        return self.video.duration // 1000 if self.video else None

    @property
    def avatar_url(self) -> str | None:
        if avatar := self.author.avatar_thumb:
            return choice(avatar.url_list)
        elif avatar := self.author.avatar_medium:
            return choice(avatar.url_list)
        return None
