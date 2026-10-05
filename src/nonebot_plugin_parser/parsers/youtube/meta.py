import re

from msgspec import Struct
from msgspec.json import Decoder


class Thumbnail(Struct):
    url: str


class AvatarInfo(Struct):
    thumbnails: list[Thumbnail]


class ChannelMetadataRenderer(Struct):
    title: str
    description: str
    avatar: AvatarInfo


class Metadata(Struct):
    channelMetadataRenderer: ChannelMetadataRenderer


class Text(Struct):
    content: str | None = None
    simpleText: str | None = None


class MetadataPart(Struct):
    text: Text | None = None


class MetadataRow(Struct):
    metadataParts: list[MetadataPart] | None = None


class ContentMetadataViewModel(Struct):
    metadataRows: list[MetadataRow] | None = None


class MetadataContainer(Struct):
    contentMetadataViewModel: ContentMetadataViewModel | None = None


class PageHeaderViewModel(Struct):
    metadata: MetadataContainer | None = None


class PageHeaderContent(Struct):
    pageHeaderViewModel: PageHeaderViewModel | None = None


class PageHeaderRenderer(Struct):
    content: PageHeaderContent | None = None


class Header(Struct):
    pageHeaderRenderer: PageHeaderRenderer | None = None


class Avatar(Struct):
    thumbnails: list[Thumbnail]


# 头部元数据里的订阅数文案, 例如 "8.19K 位訂閱者" / "12.3萬 订阅者" / "8.19K subscribers"
# 文案随客户端语言变化, 这里只认标签, 数字部分原样保留
_SUBSCRIBER_LABEL = re.compile(r"\s*(?:位)?\s*(?:訂閱者|订阅者|subscribers?)\s*", re.IGNORECASE)


class BrowseResponse(Struct):
    metadata: Metadata
    header: Header | None = None

    @property
    def name(self) -> str:
        return self.metadata.channelMetadataRenderer.title

    @property
    def avatar_url(self) -> str | None:
        thumbnails = self.metadata.channelMetadataRenderer.avatar.thumbnails
        return thumbnails[0].url if thumbnails else None

    @property
    def description(self) -> str:
        return self.metadata.channelMetadataRenderer.description

    @property
    def subscriber_count(self) -> str | None:
        """频道订阅数文案 (已去掉"位訂閱者"这类标签, 如 "8.19K"), 取不到返回 None"""
        if (header := self.header) is None or (renderer := header.pageHeaderRenderer) is None:
            return None
        if (content := renderer.content) is None or (header_vm := content.pageHeaderViewModel) is None:
            return None
        if (metadata := header_vm.metadata) is None:
            return None
        if (meta_vm := metadata.contentMetadataViewModel) is None:
            return None

        for row in meta_vm.metadataRows or []:
            for part in row.metadataParts or []:
                if part.text is None:
                    continue
                text = part.text.content or part.text.simpleText
                if not text or not _SUBSCRIBER_LABEL.search(text):
                    continue
                if count := _SUBSCRIBER_LABEL.sub("", text).strip():
                    return count
        return None


decoder = Decoder(BrowseResponse)
