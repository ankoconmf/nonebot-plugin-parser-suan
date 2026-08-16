"""小黑盒(xiaoheihe)帖子的数据模型.

数据来源: api.xiaoheihe.cn/bbs/app/link/tree 返回的 JSON.
帖子正文有两种形态:
- 无视频时 link.text 为富文本 JSON(html/text/img 分段);
- 有视频时 link.text 为纯文本, 视频直链在 link.video_url。
本仓库渲染模型只支持 graphics(文本 + 图片)与 contents(视频), 不支持贴纸,
因此这里把富文本降级为纯文本 + 图片 URL 列表。
"""

from __future__ import annotations

import re
import json

from bs4 import Tag, BeautifulSoup, NavigableString
from msgspec import Struct, field

# 小黑盒表情占位符形如 [微笑], 本仓库无贴纸渲染, 统一按原文保留
HEYBOX_PATTERN = re.compile(r"\[(?P<name>[^\[\]]+)\]")


class User(Struct):
    avatar: str
    username: str
    userid: str | int = ""

    @property
    def avatar_url(self) -> str:
        return self.avatar


class Img(Struct):
    url: str


class CommentItem(Struct):
    create_at: int = 0
    text: str = ""
    ip_location: str = ""
    child_num: int = 0
    """评论数"""
    up: int = 0
    """点赞数"""
    is_cy: int = 0
    """是否插眼"""
    user: User | None = None
    imgs: list[Img] = field(default_factory=list)


class CommentData(Struct):
    comment: list[CommentItem] = field(default_factory=list)
    """第一个是主评论, 后面都是回复"""


class Link(Struct):
    title: str = ""
    description: str = ""
    """纯文本内容"""
    text: str = ""
    """可能的富文本内容"""
    has_video: int = 0
    """是否有视频, 无视频则 text 为 json, 否则为纯文本"""
    ip_location: str = ""
    click: int = 0
    """浏览数"""
    comment_num: int = 0
    """评论数"""
    create_at: int = 0
    """创建时间"""
    favour_count: int = 0
    """收藏数"""
    link_award_num: int = 0
    """点赞数"""
    forward_num: int = 0
    """转发数"""
    user: User | None = None
    video_url: str | None = None
    video_thumb: str | None = None

    @property
    def graphics(self) -> list[str]:
        """图文内容: 文本段与图片 URL 交错的列表(纯文本占位)。

        返回项为 str: 普通文本段, 或图片 URL(交由解析器转换为 ImageContent)。
        这里只区分是不是 URL 由调用方处理, 统一返回字符串列表。
        """
        parts: list[str] = []
        try:
            segments = json.loads(self.text)
        except (json.JSONDecodeError, TypeError):
            # 非富文本(通常是有视频的帖子), 直接用纯文本
            if self.description:
                parts.append(self.description)
            elif self.text:
                parts.append(self.text)
            return parts

        for seg in segments:
            seg_type = seg.get("type")
            if seg_type == "html":
                parts.extend(_extract_from_html(seg.get("text", "")))
                break
            if seg_type == "text":
                if text := seg.get("text", "").strip():
                    parts.append(text)
            elif seg_type == "img":
                if url := seg.get("url"):
                    parts.append(url)
        return parts


class BaseResult(Struct):
    link: Link
    comments: list[CommentData] = field(default_factory=list)


def _extract_from_html(html: str) -> list[str]:
    """从 HTML 内容中按顺序提取纯文本和图片 URL。"""
    soup = BeautifulSoup(html.replace(r"\"", '"'), "html.parser")

    # 忽略 <noscript> 中的内容, 避免重复或无效的占位文本干扰顺序
    for noscript in soup.find_all("noscript"):
        noscript.decompose()

    result: list[str] = []
    for element in soup.descendants:
        if isinstance(element, Tag) and element.name == "img":
            attrs = {
                str(k): str(v[0] if isinstance(v, list) and v else v)
                for k, v in (element.attrs or {}).items()
                if v is not None
            }
            if src := (
                attrs.get("data-original")
                or attrs.get("data-actualsrc")
                or attrs.get("src")
            ):
                result.append(src)
        elif isinstance(element, NavigableString):
            if text := str(element).strip():
                result.append(text)
    return result


def is_image_url(part: str) -> bool:
    """粗略判断 graphics 项是不是图片 URL。"""
    return part.startswith(("http://", "https://")) and bool(
        re.search(r"\.(?:jpe?g|png|gif|webp|bmp)(?:[?#]|$)", part, re.IGNORECASE)
    )
