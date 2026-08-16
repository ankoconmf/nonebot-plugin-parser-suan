from msgspec import Struct, field
from msgspec.json import Decoder

from .common import Video
from ..utils import fmt_stat


class StreamItem(Struct):
    masterUrl: str = ""


class Stream(Struct):
    h264: list[StreamItem] = field(default_factory=list)
    h265: list[StreamItem] = field(default_factory=list)


class Image(Struct):
    urlDefault: str
    livePhoto: bool = False
    stream: Stream | None = None

    @property
    def live_video_url(self) -> str | None:
        """实况图视频地址"""
        if not self.livePhoto or self.stream is None:
            return None
        for items in (self.stream.h264, self.stream.h265):
            if items and items[0].masterUrl:
                return items[0].masterUrl
        return None


class User(Struct):
    nickname: str
    avatar: str


class InteractInfo(Struct):
    """互动数据 (小红书返回的是字符串)"""

    likedCount: str = "0"
    """点赞"""
    collectedCount: str = "0"
    """收藏"""
    commentCount: str = "0"
    """评论"""
    shareCount: str = "0"
    """分享"""


class NoteDetail(Struct):
    type: str
    title: str
    desc: str
    user: User
    imageList: list[Image] = field(default_factory=list)
    video: Video | None = None
    interactInfo: InteractInfo | None = None
    time: int | None = None
    """发布时间戳, 毫秒"""
    lastUpdateTime: int | None = None
    """最后更新时间戳, 毫秒"""
    ipLocation: str | None = None
    """发布地区 (IP 属地)"""

    @property
    def timestamp(self) -> int | None:
        """发布时间戳, 秒 (优先发布时间, 缺失时回退到最后更新时间)"""
        ts = self.time if self.time is not None else self.lastUpdateTime
        return ts // 1000 if ts is not None else None

    @property
    def ip_location(self) -> str | None:
        """发布地区 (IP 属地), 去除前后空白"""
        return self.ipLocation.strip() if self.ipLocation else None

    @property
    def stats_panel(self) -> list[dict[str, str]]:
        """卡片底部互动数据面板 (点赞/收藏/评论/分享)"""
        if self.interactInfo is None:
            return []
        return [
            {"icon": "like", "value": fmt_stat(self.interactInfo.likedCount), "label": "点赞"},
            {"icon": "star", "value": fmt_stat(self.interactInfo.collectedCount), "label": "收藏"},
            {"icon": "comment", "value": fmt_stat(self.interactInfo.commentCount), "label": "评论"},
            {"icon": "share", "value": fmt_stat(self.interactInfo.shareCount), "label": "分享"},
        ]

    @property
    def nickname(self) -> str:
        return self.user.nickname

    @property
    def avatar_url(self) -> str:
        return self.user.avatar

    @property
    def image_urls(self) -> list[str]:
        return [item.urlDefault for item in self.imageList]

    @property
    def _cover_url(self) -> str | None:
        # 取第一张图片作为封面，如果没有图片则返回 None
        return self.imageList[0].urlDefault if self.imageList else None

    @property
    def is_video(self) -> bool:
        return self.type == "video" and self.video is not None

    @property
    def video_cover_duration(self):
        assert self.video is not None
        video_url, duration = self.video.url_and_duration
        assert video_url is not None
        return video_url, self._cover_url, duration


class NoteDetailWrapper(Struct):
    """Wrapper for note detail, represents the value in noteDetailMap[xhs_id]"""

    note: NoteDetail


class Note(Struct):
    """Top-level note container with noteDetailMap"""

    noteDetailMap: dict[str, NoteDetailWrapper]


class InitialState(Struct):
    """Root structure of window.__INITIAL_STATE__"""

    note: Note


decoder = Decoder(InitialState)
