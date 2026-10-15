from msgspec import Struct, field

from ..utils import fmt_stat


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

    @property
    def stats_panel(self) -> list[dict[str, str]]:
        """卡片底部互动数据面板 (点赞/收藏/评论/分享)"""
        return [
            {"icon": "like", "value": fmt_stat(self.likedCount), "label": "点赞"},
            {"icon": "star", "value": fmt_stat(self.collectedCount), "label": "收藏"},
            {"icon": "comment", "value": fmt_stat(self.commentCount), "label": "评论"},
            {"icon": "share", "value": fmt_stat(self.shareCount), "label": "分享"},
        ]


class StreamItem(Struct):
    masterUrl: str
    duration: int  # milliseconds
    backupUrl: list[str] | None = None


class Stream(Struct):
    h264: list[StreamItem] | None = None
    h265: list[StreamItem] | None = None
    av1: list[StreamItem] | None = None
    h266: list[StreamItem] | None = None
    # 登录态网页使用的新字段名，与 h264 / h265 指向相同的编码流。
    ef4: list[StreamItem] | None = field(default=None, name="EF4")
    ef5: list[StreamItem] | None = field(default=None, name="EF5")


class Media(Struct):
    stream: Stream


class Video(Struct):
    media: Media

    @property
    def url_and_duration(self) -> tuple[str | None, float]:
        stream = self.media.stream

        # h264 有水印，h265 无水印
        for items in (stream.h265, stream.ef5, stream.h264, stream.ef4, stream.av1, stream.h266):
            for item in items or ():
                if item.masterUrl:
                    return item.masterUrl, item.duration / 1000

        return None, 0.0
