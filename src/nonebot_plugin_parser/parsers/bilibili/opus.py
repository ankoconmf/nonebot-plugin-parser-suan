from typing import Any
from dataclasses import dataclass

from msgspec import Struct


class Author(Struct):
    """图文动态作者信息"""

    name: str
    face: str
    mid: int
    pub_time: str
    pub_ts: str


class Image(Struct):
    """图文动态图片信息"""

    url: str
    """图片链接"""
    live_url: str | None = None
    """实况图视频链接(有值则为实况图)"""
    # width: int
    # height: int
    # size: float


class Album(Struct):
    """图文动态相册(动态型图文的图片放在 module_top.display.album 里)"""

    pics: list[Image] = []


class Display(Struct):
    """图文动态展示信息"""

    album: Album | None = None


class Top(Struct):
    """图文动态顶部模块"""

    display: Display | None = None


class Pic(Struct):
    """图文动态图片组"""

    pics: list[Image]
    style: int


class Text(Struct):
    """图文动态文本"""

    nodes: list[dict[str, Any]]


class Paragraph(Struct):
    """图文动态段落"""

    para_type: int
    text: Text | None = None
    pic: Pic | None = None
    # align: int = 0
    # format: dict[str, Any] | None = None


class Content(Struct):
    """图文动态内容"""

    paragraphs: list[Paragraph]


class Stat(Struct):
    """图文动态统计"""

    like: dict[str, Any] | None = None
    comment: dict[str, Any] | None = None
    forward: dict[str, Any] | None = None
    favorite: dict[str, Any] | None = None
    coin: dict[str, Any] | None = None


class Module(Struct):
    """图文动态模块"""

    module_type: str
    module_author: Author | None = None
    module_content: Content | None = None
    module_top: Top | None = None
    # module_stat: OpusStat | None = None


class Basic(Struct):
    """图文动态基本信息"""

    title: str


class Info(Struct):
    """图文动态信息"""

    id_str: str
    type: int
    modules: list[Module]
    basic: Basic | None = None


@dataclass(slots=True)
class ImageNode:
    """图文动态图片节点"""

    url: str
    """图片链接"""
    alt: str | None = None
    """图片描述"""
    live_url: str | None = None
    """实况图视频链接(有值则为实况图)"""


class OpusItem(Struct):
    """图文动态项目"""

    item: Info

    @property
    def title(self) -> str | None:
        return self.item.basic.title if self.item.basic else None

    @property
    def name_avatar(self) -> tuple[str, str]:
        author_module = next(module.module_author for module in self.item.modules if module.module_author)
        return author_module.name, author_module.face

    @property
    def mid(self) -> int | None:
        """获取作者 mid"""
        for module in self.item.modules:
            if module.module_author:
                return module.module_author.mid
        return None

    @property
    def timestamp(self) -> int | None:
        """获取发布时间戳"""
        for module in self.item.modules:
            if module.module_type == "MODULE_TYPE_AUTHOR" and module.module_author:
                return int(module.module_author.pub_ts)
        return None

    @property
    def top_images(self) -> list[Image]:
        """获取顶部展示的图片

        动态型图文(动态被转成图文)的图片不在段落里, 而是放在
        ``module_top.display.album.pics``, 实况图同样带 ``live_url``。
        """
        images: list[Image] = []
        for module in self.item.modules:
            if (
                module.module_top
                and (display := module.module_top.display)
                and (album := display.album)
            ):
                images.extend(album.pics)
        return images

    def extract_nodes(self):
        """提取图文节点（保持顺序）"""
        seen_images: set[str] = set()
        for module in self.item.modules:
            if module.module_type == "MODULE_TYPE_CONTENT" and module.module_content:
                iterator = iter(module.module_content.paragraphs)
                for paragraph in iterator:
                    # 处理文本段落
                    if paragraph.text and paragraph.text.nodes:
                        cur_text = "".join(
                            text for text, _ in self._extract_texts_from_nodes(paragraph.text.nodes)
                        ).strip()
                        if cur_text:
                            yield cur_text
                    # 处理图片段落
                    if paragraph.pic and paragraph.pic.pics:
                        for pic in paragraph.pic.pics:
                            seen_images.add(pic.url)
                            image_node = ImageNode(url=pic.url, live_url=pic.live_url)
                            next_text = ""
                            if (next_par := next(iterator, None)) and next_par.text and next_par.text.nodes:
                                for text, color in self._extract_texts_from_nodes(next_par.text.nodes):
                                    if color == "#999999":
                                        image_node.alt = text
                                    else:
                                        next_text += text
                            yield image_node
                            next_text = next_text.strip()
                            if next_text:
                                yield next_text

        # 动态型图文: 图片在 module_top.display.album 里, 段落中没有图片节点
        for image in self.top_images:
            if image.url in seen_images:
                continue
            seen_images.add(image.url)
            yield ImageNode(url=image.url, live_url=image.live_url)

    def _extract_texts_from_nodes(self, nodes: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
        """从节点列表中提取文本内容"""
        texts: list[tuple[str, str | None]] = []
        for node in nodes:
            if node.get("type") in (
                "TEXT_NODE_TYPE_WORD",
                "TEXT_NODE_TYPE_RICH",
            ) and node.get("word"):
                text = node["word"]["words"]
                color = node["word"]["color"]
                texts.append((text, color))

        return texts
