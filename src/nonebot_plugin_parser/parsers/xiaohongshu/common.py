from msgspec import Struct, field


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
