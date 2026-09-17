"""X (Twitter) 数据结构

对应 X GraphQL `TweetResultByRestId` 返回的 TweetResult 结构。
被转发的推文可能是 Tweet 或 TweetWithVisibilityResults 包装, 由 `TweetData` 统一兼容。

与 nonebot-plugin-parser-lite 的 x/model.py 对齐, 差异点:
- `views` / `rest_id` 在部分响应中缺失, 这里改为可选;
- 动图 (animated_gif) 仍按 GIF 处理 (lite 中无 mp4 变体时会被丢弃)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from msgspec import Struct, field

from ..data import ImageContent, MediaContent, VideoContent


class Views(Struct):
    count: str = "0"
    """浏览数"""

    @property
    def view_count(self) -> int:
        try:
            return int(self.count)
        except (TypeError, ValueError):
            return 0


class CardImage(Struct):
    url: str = ""


class CardValue(Struct):
    string_value: str | None = None
    image_value: CardImage | None = None


class CardBindingValue(Struct):
    key: str = ""
    value: CardValue = field(default_factory=CardValue)


class CardLegacy(Struct):
    name: str = ""
    url: str = ""
    binding_values: list[CardBindingValue] = field(default_factory=list)


class TweetCard(Struct):
    legacy: CardLegacy | None = None


class UnifiedText(Struct):
    content: str = ""


class UnifiedComponentData(Struct):
    id: str | None = None
    destination: str | None = None
    title: UnifiedText | None = None
    subtitle: UnifiedText | None = None
    description: str | UnifiedText | None = None
    summary: str | UnifiedText | None = None


class UnifiedComponent(Struct):
    type: str = ""
    data: UnifiedComponentData = field(default_factory=UnifiedComponentData)


class UnifiedUrlData(Struct):
    url: str = ""
    vanity: str | None = None


class UnifiedDestinationData(Struct):
    url_data: UnifiedUrlData | None = None


class UnifiedDestination(Struct):
    type: str = ""
    data: UnifiedDestinationData = field(default_factory=UnifiedDestinationData)


class UnifiedMediaEntity(Struct):
    media_url_https: str = ""


class UnifiedCard(Struct):
    # unified_card 的组件 id 是字符串 (例如 "media_1")
    components: list[str] = field(default_factory=list)
    component_objects: dict[str, UnifiedComponent] = field(default_factory=dict)
    destination_objects: dict[str, UnifiedDestination] = field(default_factory=dict)
    media_entities: dict[str, UnifiedMediaEntity] = field(default_factory=dict)


class VideoVariant(Struct):
    content_type: str
    """视频编码类型, 如 'video/mp4' 或 'application/x-mpegURL'"""
    url: str
    """视频地址"""
    bitrate: int | None = None
    """码率, 部分非 mp4 变体没有"""


class VideoInfo(Struct):
    variants: list[VideoVariant] = field(default_factory=list)
    duration_millis: int = 0
    """视频时长(ms)"""


class Media(Struct):
    type: str
    """媒体类型: 'photo' / 'video' / 'animated_gif'"""
    media_url_https: str
    """图片原图 / 视频封面"""
    video_info: VideoInfo | None = None
    """视频信息, 仅 video / animated_gif 存在"""

    @property
    def original_url(self) -> str:
        """图片原图链接"""
        if "?" in self.media_url_https:
            return f"{self.media_url_https}&name=4096x4096"
        return f"{self.media_url_https}?format=jpg&name=4096x4096"


class ExtendedEntities(Struct):
    media: list[Media] = field(default_factory=list)


class UserLegacy(Struct):
    description: str = ""
    """用户简介"""
    followers_count: int = 0
    """粉丝数"""
    profile_banner_url: str = ""
    """banner 图片"""


class UserCore(Struct):
    name: str
    """用户昵称"""
    screen_name: str
    """用户名"""
    created_at: str = ""
    """注册时间"""


class UserAvatar(Struct):
    image_url: str = (
        "https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png"
    )


class UserBio(Struct):
    description: str = ""


class UserData(Struct):
    core: UserCore
    legacy: UserLegacy = field(default_factory=UserLegacy)
    is_blue_verified: bool = False
    """蓝标认证"""
    id: str = ""
    """用户 id"""
    rest_id: str = ""
    """用户数字 id"""
    avatar: UserAvatar = field(default_factory=UserAvatar)
    profile_bio: UserBio | None = None

    @property
    def avatar_url(self) -> str:
        """头像链接"""
        return self.avatar.image_url.replace("_normal", "_bigger")

    @property
    def description(self) -> str:
        """用户简介, 优先取 profile_bio"""
        if self.profile_bio and self.profile_bio.description:
            return self.profile_bio.description
        return self.legacy.description


class UserResult(Struct):
    result: UserData


class TweetCore(Struct):
    user_results: UserResult


class NoteTweetResult(Struct):
    text: str = ""


class NoteTweetResults(Struct):
    result: NoteTweetResult | None = None


class NoteTweet(Struct):
    note_tweet_results: NoteTweetResults | None = None


class ArticleMediaPreview(Struct):
    original_img_url: str = ""


class ArticleMediaVariant(Struct):
    content_type: str = ""
    url: str = ""
    bit_rate: int | None = None


class ArticleMediaInfo(Struct):
    original_img_url: str = ""
    preview_image: ArticleMediaPreview | None = None
    variants: list[ArticleMediaVariant] = field(default_factory=list)
    duration_millis: int = 0


class ArticleCoverMedia(Struct):
    media_info: ArticleMediaInfo | None = None


class TextEntityRange(Struct):
    key: int | str = 0
    length: int = 0
    offset: int = 0


class TextEntityMediaItem(Struct):
    mediaId: str | int = ""


class TextEntityData(Struct):
    mediaItems: list[TextEntityMediaItem] = field(default_factory=list)


class TextEntityValue(Struct):
    type: str = ""
    data: TextEntityData = field(default_factory=TextEntityData)


class TextEntityMap(Struct):
    key: str | int = ""
    value: TextEntityValue = field(default_factory=TextEntityValue)


class TextBlock(Struct):
    text: str = ""
    entityRanges: list[TextEntityRange] = field(default_factory=list)


class TextContentState(Struct):
    blocks: list[TextBlock] = field(default_factory=list)
    entityMap: list[TextEntityMap] | dict[str, TextEntityValue] = field(
        default_factory=list
    )


class ArticleMediaEntity(Struct):
    media_id: str | int = ""
    media_info: ArticleMediaInfo | None = None


class ArticleResult(Struct):
    title: str = ""
    preview_text: str = ""
    rest_id: str = ""
    cover_media: ArticleCoverMedia | None = None
    content_state: TextContentState | None = None
    media_entities: list[ArticleMediaEntity] = field(default_factory=list)


def get_article_text(article: ArticleResult) -> str:
    """Article 正文纯文本"""
    content_state = article.content_state
    if content_state is None:
        return ""
    return "\n".join(block.text for block in content_state.blocks).strip()


def get_article_entities(
    content_state: TextContentState,
) -> dict[str, TextEntityValue]:
    if isinstance(content_state.entityMap, dict):
        return {str(key): value for key, value in content_state.entityMap.items()}
    return {str(entity.key): entity.value for entity in content_state.entityMap}


def get_article_media_entities_for_range(
    entity_range: TextEntityRange,
    entities: dict[str, TextEntityValue],
    media_by_id: dict[str, ArticleMediaEntity],
) -> list[ArticleMediaEntity]:
    """取出 entityRange 引用的 media"""
    entity = entities.get(str(entity_range.key))
    if entity is None or entity.type != "MEDIA":
        return []
    return [
        media_by_id[media_id]
        for item in entity.data.mediaItems
        if (media_id := str(item.mediaId)) in media_by_id
    ]


def get_article_preview_url(media_info: ArticleMediaInfo) -> str | None:
    if media_info.preview_image and media_info.preview_image.original_img_url:
        return media_info.preview_image.original_img_url
    return media_info.original_img_url or None


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_offset_to_index(text: str, offset: int) -> int:
    """将 Draft.js 的 UTF-16 偏移量映射为 Python 字符串索引"""
    offset = max(offset, 0)
    code_units = 0
    for index, char in enumerate(text):
        if offset <= code_units:
            return index
        code_units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def get_article_media_content(
    article: ArticleResult,
    media: ArticleMediaEntity,
    create_image: Any,
    create_video: Any,
) -> ImageContent | VideoContent | None:
    """单条 Article 媒体 -> 本地内容项 (create_image / create_video 由 parser 注入)"""
    media_info = media.media_info
    if media_info is None:
        return None

    variants = [
        variant
        for variant in media_info.variants
        if variant.content_type == "video/mp4" and variant.url
    ]
    if variants:
        video = max(variants, key=lambda variant: variant.bit_rate or 0)
        return create_video(
            video.url,
            get_article_preview_url(media_info),
            duration=media_info.duration_millis / 1000,
        )
    if image_url := media_info.original_img_url or get_article_preview_url(media_info):
        return create_image(image_url)
    return None


def get_article_content(
    article: ArticleResult,
    create_image: Any,
    create_video: Any,
) -> list[str | MediaContent]:
    """Article 正文 -> 文本 / 图片 / 视频交错的内容列表"""
    content_state = article.content_state
    if content_state is None:
        return [article.preview_text] if article.preview_text else []

    entities = get_article_entities(content_state)
    media_by_id = {
        str(media.media_id): media
        for media in article.media_entities
        if media.media_id != ""
    }
    content: list[str | MediaContent] = []
    text = ""

    def flush_text() -> None:
        nonlocal text
        if text:
            content.append(text)
        text = ""

    for index, block in enumerate(content_state.blocks):
        if index and text:
            text += "\n"

        cursor = 0
        block_length = utf16_length(block.text)
        for entity_range in sorted(
            block.entityRanges, key=lambda entity_range: entity_range.offset
        ):
            media_content: list[str | ImageContent | VideoContent] = [
                content_item
                for media in get_article_media_entities_for_range(
                    entity_range, entities, media_by_id
                )
                if (
                    content_item := get_article_media_content(
                        article, media, create_image, create_video
                    )
                )
                is not None
            ]
            if not media_content:
                continue

            start = min(max(entity_range.offset, cursor), block_length)
            end = min(max(entity_range.offset + entity_range.length, start), block_length)
            text += block.text[
                utf16_offset_to_index(block.text, cursor) : utf16_offset_to_index(
                    block.text, start
                )
            ]
            flush_text()
            content.extend(media_content)
            cursor = end

        text += block.text[utf16_offset_to_index(block.text, cursor) :]

    flush_text()
    return content or ([article.preview_text] if article.preview_text else [])


class ArticleResults(Struct):
    result: ArticleResult | None = None


class Article(Struct):
    article_results: ArticleResults | None = None


class TweetLegacy(Struct):
    bookmark_count: int = 0
    """收藏数"""
    favorite_count: int = 0
    """点赞数"""
    retweet_count: int = 0
    """转推数"""
    quote_count: int = 0
    """引用数"""
    reply_count: int = 0
    """评论数"""
    full_text: str = ""
    """推文原文, 含尾部 t.co 链接"""
    created_at: str = ""
    """utc 时间字符串, 例如 'Fri Feb 20 16:33:16 +0000 2026'"""
    display_text_range: tuple[int, int] = (0, 0)
    """推文文本范围, 用于裁掉尾部 t.co 链接"""
    possibly_sensitive: bool = False
    """是否敏感内容"""
    in_reply_to_status_id_str: str | None = None
    """被回复的推文 id"""
    conversation_id_str: str | None = None
    """会话 (根推文) id"""
    extended_entities: ExtendedEntities | None = None
    """媒体"""

    def medias(self, create_image: Any, create_video: Any) -> list[MediaContent]:
        """媒体 -> 本地内容项"""
        if not self.extended_entities or not self.extended_entities.media:
            return []

        medias: list[MediaContent] = []
        for media in self.extended_entities.media:
            if media.type == "photo":
                medias.append(create_image(media.original_url))
                continue

            video_info = media.video_info
            if video_info is None:
                continue

            # 视频 / 动图: 取最高码率的 mp4
            candidates = [
                (variant.bitrate or 0, variant.url)
                for variant in video_info.variants
                if variant.content_type == "video/mp4" and variant.url
            ]
            if not candidates:
                continue

            _, best_url = max(candidates, key=lambda item: item[0])
            medias.append(
                create_video(
                    best_url,
                    media.media_url_https,
                    duration=video_info.duration_millis / 1000,
                    is_gif=media.type == "animated_gif",
                )
            )

        return medias

    @property
    def text(self) -> str:
        """裁掉尾部 t.co 链接后的正文"""
        start, end = self.display_text_range
        if end <= start:
            return ""
        return self.full_text[start:end]

    @property
    def time_local(self) -> int | None:
        """创建时间的本地 Unix 时间戳(秒), 解析失败返回 None"""
        try:
            dt_utc = datetime.strptime(self.created_at, "%a %b %d %H:%M:%S %z %Y")
        except (TypeError, ValueError):
            return None
        return int(dt_utc.astimezone().timestamp())


class Tweet(Struct):
    core: TweetCore
    legacy: TweetLegacy
    """原始推文"""
    views: Views | None = None
    """浏览数, 部分响应缺失"""
    rest_id: str = ""
    """推文 id"""
    card: TweetCard | None = None
    """推文链接卡片"""
    note_tweet: NoteTweet | None = None
    """长文本推文结构"""
    article: Article | None = None
    """X Article 结构, 按普通正文处理"""
    quoted_status_result: TweetEntry | None = None
    """被引用推文 (转发时带评论)"""
    retweeted_status_result: TweetEntry | None = None
    """被转发推文 (直接转发)"""

    def get_article_result(self) -> ArticleResult | None:
        article_results = self.article.article_results if self.article else None
        return article_results.result if article_results else None

    def get_article_cover_url(self) -> str | None:
        """Article 封面"""
        result = self.get_article_result()
        cover_media = result.cover_media if result else None
        media_info = cover_media.media_info if cover_media else None
        if media_info and media_info.original_img_url:
            return media_info.original_img_url
        return None

    def get_text(self) -> str:
        """完整正文, 优先使用 note_tweet / Article 内容"""
        note_results = self.note_tweet.note_tweet_results if self.note_tweet else None
        note_result = note_results.result if note_results else None
        if note_result and note_result.text:
            return note_result.text

        if article_result := self.get_article_result():
            if (
                article_text := get_article_text(article_result)
                or article_result.preview_text
            ):
                return (
                    f"{self.legacy.text}\n\n{article_text}"
                    if self.legacy.text
                    else article_text
                )

        return self.legacy.text

    @property
    def title(self) -> str | None:
        """Article 标题"""
        article_result = self.get_article_result()
        return article_result.title if article_result and article_result.title else None

    def get_article_content(
        self, create_image: Any, create_video: Any
    ) -> list[str | MediaContent]:
        """Article 图文内容 (文本 / 图片 / 视频交错), 非 Article 推文返回空"""
        article_result = self.get_article_result()
        if article_result is None:
            return []

        content: list[str | MediaContent] = []
        if article_cover := self.get_article_cover_url():
            content.append(create_image(article_cover))
        content.extend(
            get_article_content(article_result, create_image, create_video)
        )
        return content

    def get_medias(self, create_image: Any, create_video: Any) -> list[MediaContent]:
        """普通媒体 (图片 / 视频 / 动图)"""
        return self.legacy.medias(create_image, create_video)


class TweetData(Struct):
    """兼容层: 既支持 Tweet, 也支持 TweetWithVisibilityResults"""

    tweet: Tweet | None = None
    """TweetWithVisibilityResults 包装内的真实推文"""

    @property
    def as_tweet(self) -> Tweet:
        if self.tweet:
            return self.tweet
        # 直接就是 Tweet 的情况 (msgspec 会把多余字段丢弃)
        core = self.core
        legacy = self.legacy
        rest_id = self.rest_id
        assert core is not None, "TweetData.core is missing"
        assert legacy is not None, "TweetData.legacy is missing"
        assert rest_id is not None, "TweetData.rest_id is missing"
        return Tweet(
            core=core,
            legacy=legacy,
            views=self.views,
            rest_id=rest_id,
            card=self.card,
            note_tweet=self.note_tweet,
            article=self.article,
            quoted_status_result=self.quoted_status_result,
            retweeted_status_result=self.retweeted_status_result,
        )

    core: TweetCore | None = None
    legacy: TweetLegacy | None = None
    views: Views | None = None
    rest_id: str | None = None
    card: TweetCard | None = None
    note_tweet: NoteTweet | None = None
    article: Article | None = None
    quoted_status_result: TweetEntry | None = None
    retweeted_status_result: TweetEntry | None = None


class TweetEntry(Struct):
    result: TweetData


# ---------------------------------------------------------------------------
# 备用接口 (api.fxtwitter.com) 的数据结构
# X 直连 (GraphQL) 失败时回退到 fxtwitter; 旧的 vxtwitter 目前已经 403 不可用。
# ---------------------------------------------------------------------------


class FxVideoVariant(Struct):
    url: str
    bitrate: int | None = None


class FxVideoInfo(Struct):
    duration: float = 0.0
    """时长(秒)"""
    variants: list[FxVideoVariant] = field(default_factory=list)


class FxMedia(Struct):
    type: str
    """'photo' / 'video' / 'gif'"""
    url: str
    thumbnail_url: str | None = None
    duration: float | None = None
    """时长(秒), 部分响应有"""
    video_info: FxVideoInfo | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.video_info and self.video_info.duration:
            return self.video_info.duration
        return self.duration or None

    @property
    def best_video_url(self) -> str | None:
        """最高码率的 mp4, 没有变体时退回 url"""
        variants = [
            variant
            for variant in (self.video_info.variants if self.video_info else [])
            if variant.url and "m3u8" not in variant.url
        ]
        if not variants:
            return self.url or None
        return max(variants, key=lambda variant: variant.bitrate or 0).url


class FxMediaGroup(Struct):
    all: list[FxMedia] = field(default_factory=list)


class FxAuthor(Struct):
    name: str = ""
    screen_name: str = ""
    avatar_url: str | None = None
    description: str | None = None


class FxTweet(Struct):
    id: str = ""
    url: str | None = None
    text: str = ""
    """已展开的长文本, 直接就是完整正文"""
    author: FxAuthor = field(default_factory=FxAuthor)
    created_timestamp: int | None = None
    """创建时间(Unix 秒)"""
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    bookmarks: int = 0
    views: int | None = None
    media: FxMediaGroup | None = None
    quote: "FxTweet | None" = None
    """引用的推文"""
    reposted_by: str | None = None
    """被转发的原推文作者"""
    is_note_tweet: bool = False


class FxResponse(Struct):
    code: int
    message: str = ""
    tweet: FxTweet | None = None
