from msgspec import Struct, field
from msgspec.json import Decoder

from .common import Video


class StreamItem(Struct):
    masterUrl: str = ""


class Stream(Struct):
    h264: list[StreamItem] = field(default_factory=list)
    h265: list[StreamItem] = field(default_factory=list)


class Image(Struct):
    url: str
    urlSizeLarge: str | None = None
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
    nickName: str
    avatar: str


class NoteData(Struct):
    type: str
    title: str
    desc: str
    user: User
    lastUpdateTime: int
    """最后更新时间戳, 毫秒"""
    time: int | None = None
    """发布时间戳, 毫秒"""
    imageList: list[Image] = []  # 有水印
    video: Video | None = None
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
    def image_urls(self) -> list[str]:
        return [item.url for item in self.imageList]

    @property
    def is_video(self) -> bool:
        return self.type == "video" and self.video is not None

    @property
    def url_and_duration(self):
        assert self.video is not None
        video_url, duration = self.video.url_and_duration
        assert video_url is not None
        return video_url, duration


class NormalNotePreloadData(Struct):
    title: str
    desc: str
    imagesList: list[Image] = []  # 无水印, 但只有一只，用于视频封面

    @property
    def image_urls(self) -> list[str]:
        return [item.urlSizeLarge or item.url for item in self.imagesList]


class NoteDataWrapper(Struct):
    noteData: NoteData


class NoteDataContainer(Struct):
    data: NoteDataWrapper
    normalNotePreloadData: NormalNotePreloadData | None = None


class InitialState(Struct):
    noteData: NoteDataContainer


decoder = Decoder(InitialState)
