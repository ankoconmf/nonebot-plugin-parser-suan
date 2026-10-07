"""抖音评论区 (web 评论列表接口)

获取方式参考 sokoko-org/nonebot-plugin-parser-lite:

1. `POST https://ttwid.bytedance.com/ttwid/union/register/` 注册 ttwid cookie
   (网页端"游客登录态"凭证, 有效期按小时缓存)
2. `GET https://www.douyin.com/aweme/v1/web/comment/list/` 带上 ttwid 即可,
   `msToken` / `X-Bogus` 留空也能过 (评论接口不做签名校验)

`cursor=0` 的第一页就是网页端展示的热门评论: 返回的条目都带
`is_hot=true` / `sort_tags.top_list=1`(还混了 `interest_comment` 兴趣推荐),
**但那个顺序不按点赞**(实测 2512 赞能排在第 3、8 赞排第 5), 所以默认改成
按点赞降序重排, 用 `parser_comment_sort=hot` 可以退回抖音自己的顺序。
"""

from typing import Callable
from pathlib import PurePath
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

from msgspec import Struct, field

from ..data import CommentItem
from ..task import PathTask
from ...constants import CommentSort

CHINA_TIMEZONE = timezone(timedelta(hours=8))

SECONDS_PER_DAY = 86400
"""相对时间最多显示到 30 天前, 超过则显示日期"""

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
"""能直接下发的图片后缀 (.heic/.image 这类变体不作为首选)"""


class Avatar(Struct):
    url_list: list[str] = field(default_factory=list)


class CommentUser(Struct):
    nickname: str = ""
    uid: str = ""
    avatar_thumb: Avatar | None = None


class CommentImage(Struct):
    """评论配图 (同一张图有多种尺寸/格式变体, 取原图)"""

    origin_url: Avatar | None = None
    download_url: Avatar | None = None
    medium_url: Avatar | None = None
    thumb_url: Avatar | None = None

    @property
    def url(self) -> str | None:
        """按 原图 → 下载图 → 中图 → 缩略图 取链接

        抖音同一张图会给出 `.image` / `.heic` / `.jpeg` 等多种变体, 只有后缀明确的
        才是能直接下发的格式(否则文件名会退化成 .jpg 而内容是 webp/heic)。
        """
        urls = [
            url
            for variant in (self.origin_url, self.download_url, self.medium_url, self.thumb_url)
            if variant
            for url in variant.url_list
        ]
        for url in urls:
            if PurePath(urlparse(url).path).suffix.lower() in IMAGE_SUFFIXES:
                return url
        return urls[-1] if urls else None


class Sticker(Struct):
    """评论大表情

    链接没有文件后缀, 且可能是动图(实测 CDN 会返回动图 GIF/webp),
    下发容易变成"后缀不符 + 体积很大", 因此只在卡片里标注 [表情], 不下发。
    """

    static_url: Avatar | None = None


class Comment(Struct):
    cid: str = ""
    text: str | None = None
    create_time: int = 0
    digg_count: int = 0
    """点赞数"""
    reply_comment_total: int = 0
    """回复数"""
    ip_label: str | None = None
    """IP 归属地"""
    label_text: str | None = None
    """作者本人的评论会带上 '作者' 标签"""
    user: CommentUser | None = None
    image_list: list[CommentImage] | None = None
    sticker: Sticker | None = None

    @property
    def nickname(self) -> str:
        return (self.user.nickname if self.user else "") or "抖音用户"

    @property
    def avatar_url(self) -> str | None:
        if self.user and (avatar := self.user.avatar_thumb) and avatar.url_list:
            return avatar.url_list[-1]
        return None

    @property
    def content_text(self) -> str:
        """评论正文: `[图片表情]` 是抖音给表情评论塞的占位符, 由 media_label 单独标注"""
        return (self.text or "").replace("[图片表情]", "").strip()

    @property
    def media_label(self) -> str | None:
        """评论附带的媒体类型 (下载失败/未展示时在卡片里用文字标注)"""
        if self.image_list:
            return "图片"
        if self.sticker:
            return "表情"
        return None

    @property
    def image_urls(self) -> list[str]:
        """评论配图 (渲染在卡片评论区里)"""
        return [url for image in (self.image_list or []) if (url := image.url)]

    @property
    def sticker_url(self) -> str | None:
        """评论大表情 (可能是静态 webp, 也可能是动图 GIF)"""
        if (sticker := self.sticker) and (static := sticker.static_url) and static.url_list:
            return static.url_list[-1]
        return None

    @property
    def is_author(self) -> bool:
        return self.label_text == "作者"


class CommentList(Struct):
    comments: list[Comment] = field(default_factory=list)
    total: int = 0
    """评论总数"""


def fmt_comment_time(timestamp: int) -> str | None:
    """评论时间: 1 小时内显示 n分钟前, 24 小时内 n小时前, 30 天内 n天前, 更早显示日期"""
    if not timestamp:
        return None

    now = datetime.now(CHINA_TIMEZONE)
    created = datetime.fromtimestamp(timestamp, CHINA_TIMEZONE)
    delta = (now - created).total_seconds()

    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < SECONDS_PER_DAY:
        return f"{int(delta // 3600)}小时前"
    if delta < 30 * SECONDS_PER_DAY:
        return f"{int(delta // SECONDS_PER_DAY)}天前"
    return created.strftime("%Y-%m-%d" if created.year != now.year else "%m-%d")


def sort_comments(comments: list[Comment], mode: CommentSort) -> list[Comment]:
    """按配置排序评论 (点赞降序时点赞相同保持接口原序)"""
    if mode == CommentSort.hot:
        return comments
    return sorted(comments, key=lambda comment: comment.digg_count, reverse=True)


def build_comments(
    comments: list[Comment],
    download: Callable[[str], PathTask],
    count: int,
    max_images: int = 0,
) -> list[CommentItem]:
    """把接口返回的评论转成卡片评论区用的 CommentItem (取前 count 条)

    `download` 同时用于头像与评论配图 (都是图片, 共用下载器即可);
    `max_images` 限制评论配图(含大表情)的下载总数, 0 表示不下载, 卡片里只留文字标注。
    """
    items: list[CommentItem] = []
    remaining = max(max_images, 0)

    for comment in comments[:count]:
        image_urls = comment.image_urls
        images: list[PathTask] = []
        for url in image_urls:
            if remaining <= 0:
                break
            images.append(download(url))
            remaining -= 1

        # 大表情跟在评论配图后面, 同样计入上限
        sticker: PathTask | None = None
        if remaining > 0 and (sticker_url := comment.sticker_url):
            sticker = download(sticker_url)
            remaining -= 1

        avatar_url = comment.avatar_url
        items.append(
            CommentItem(
                name=comment.nickname,
                text=comment.content_text,
                avatar=download(avatar_url) if avatar_url else None,
                images=images,
                sticker=sticker,
                image_total=len(image_urls),
                likes=comment.digg_count,
                replies=comment.reply_comment_total,
                location=comment.ip_label,
                datetime_text=fmt_comment_time(comment.create_time),
                is_author=comment.is_author,
            )
        )
    return items
