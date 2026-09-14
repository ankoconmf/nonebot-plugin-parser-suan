"""X (Twitter) 解析

接口来源与 nonebot-plugin-parser-lite 的 x 解析保持一致:
    POST https://easycomment.ai/api/twitter/v1/free/get-tweet-detail  {"pid": 推文id}
返回的是 X 网页端的 threaded_conversation_with_injections_v2 结构,
从中取出目标推文及其引用/转发关系, 映射为本地 ParseResult。

支持的形态: 普通推文 / 图片 / 视频 / 动图 / 长文本(note_tweet) / X Article /
链接卡片 / 引用推文(带评论转发) / 直接转发 / 敏感内容(possibly_sensitive)。
"""

from re import Match
from typing import Any, ClassVar, Iterable

from httpx import AsyncClient
from msgspec import convert

from .model import Tweet, TweetEntry
from .util import LinkCardData, parse_link_card
from ..base import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, ParseResult
from ..utils import fmt_stat

API_URL: str = "https://easycomment.ai/api/twitter/v1/free/get-tweet-detail"
"""推文详情接口"""

SUCCESS_CODE: int = 100000
"""接口成功状态码"""

TWEET_TYPES: frozenset[str] = frozenset({"Tweet", "TweetWithVisibilityResults"})
"""正常的推文类型; 广告等其它类型直接跳过"""


def _get_tweet_result(item: dict[str, Any]) -> dict[str, Any] | None:
    """从 TimelineItem 中取出 tweet_results"""
    item_content = item.get("itemContent")
    if not isinstance(item_content, dict):
        return None
    if item_content.get("__typename") != "TimelineTweet":
        return None

    tweet_results = item_content.get("tweet_results") or {}
    result = tweet_results.get("result") or {}
    if result.get("__typename") not in TWEET_TYPES:
        return None
    return tweet_results


def _iter_timeline_tweet_results(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """提取 TimelineItem 与 TimelineModule.items 中的 Tweet"""
    if tweet_result := _get_tweet_result(node):
        yield tweet_result
        return

    content = node.get("content")
    if isinstance(content, dict):
        yield from _iter_timeline_tweet_results(content)

    for item in node.get("items", []):
        if isinstance(item, dict):
            yield from _iter_timeline_tweet_results(item)

    item = node.get("item")
    if isinstance(item, dict):
        yield from _iter_timeline_tweet_results(item)


def _get_legacy(result: dict[str, Any]) -> dict[str, Any]:
    """取真实推文的 legacy, 兼容 TweetWithVisibilityResults 包装"""
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    return result.get("legacy") or {}


def _get_rest_id(result: dict[str, Any]) -> str | None:
    """取真实推文 id, 兼容 TweetWithVisibilityResults 包装"""
    if result.get("__typename") == "TweetWithVisibilityResults":
        inner = result.get("tweet") or {}
        return inner.get("rest_id") or (inner.get("legacy") or {}).get("id_str")
    return result.get("rest_id") or (result.get("legacy") or {}).get("id_str")


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.TWITTER, display_name="X")

    def __init__(self):
        super().__init__()
        self.headers.update({"Content-Type": "application/json"})

    def _get_link_card(self, tweet: Tweet) -> LinkCardData | None:
        """推文链接卡片"""
        return parse_link_card(tweet.card)

    def _card_text(self, card: LinkCardData) -> str:
        """卡片的文字信息 (标题 / 站点 / 链接)"""
        lines = [card.title]
        if card.site_name:
            lines.append(card.site_name)
        lines.append(card.url)
        return "\n".join(lines)

    def _collect_result(
        self,
        raw: TweetEntry,
        is_repost: bool = False,
    ) -> ParseResult:
        tweet = raw.result.as_tweet
        legacy = tweet.legacy
        card = None if is_repost else self._get_link_card(tweet)

        user = tweet.core.user_results.result
        author = self.create_author(
            name=user.core.name,
            avatar_url=user.avatar_url,
            description=user.description,
        )

        # 本仓库的 graphics 是"图文"插槽 (文本 + 图片交错), 渲染时会替换正文区域,
        # 因此只在 Article 正文这类必须交错的场景使用; 普通正文走 text, 媒体走 contents。
        article_content = tweet.get_article_content(self.create_image, self.create_video)
        if article_content:
            graphics: list[str | Any] = []
            if article_text := "\n".join(
                item for item in article_content if isinstance(item, str)
            ):
                graphics.append(article_text)
            graphics.extend(
                item for item in article_content if not isinstance(item, str)
            )
            # Article 正文已包含 legacy 文本, 不再走 text 渲染
            text: str | None = None
        elif card is not None and card.preview_url:
            # 卡片推文正文很短, 用卡片预览图占据图文区域, 卡片文字并入正文
            graphics = [self.create_image(card.preview_url)]
            text = self._card_text(card)
        else:
            graphics = []
            text = tweet.get_text()

        contents = tweet.get_medias(self.create_image, self.create_video)

        # 被引用 / 被转发的推文
        repost: ParseResult | None = None
        repost_status = tweet.quoted_status_result or tweet.retweeted_status_result
        if not is_repost and repost_status is not None:
            repost = self._collect_result(repost_status, True)

        extra: dict[str, Any] = {}
        # 转发内容不显示统计面板
        if not is_repost:
            stats: list[dict[str, str]] = []
            if view_count := (tweet.views.view_count if tweet.views else 0):
                stats.append(
                    {"icon": "eye", "value": fmt_stat(view_count), "label": "浏览"}
                )
            stats.extend(
                [
                    {"icon": "like", "value": fmt_stat(legacy.favorite_count), "label": "点赞"},
                    {"icon": "share", "value": fmt_stat(legacy.retweet_count), "label": "转推"},
                    {"icon": "comment", "value": fmt_stat(legacy.reply_count), "label": "回复"},
                ]
            )
            if legacy.quote_count:
                stats.append(
                    {"icon": "share", "value": fmt_stat(legacy.quote_count), "label": "引用"}
                )
            if legacy.bookmark_count:
                stats.append(
                    {"icon": "star", "value": fmt_stat(legacy.bookmark_count), "label": "收藏"}
                )
            extra["stats"] = stats
            extra["source_id"] = f"@{user.core.screen_name}"

        return self.result(
            author=author,
            title=tweet.title,
            text=text,
            graphics=graphics,
            contents=contents,
            timestamp=legacy.time_local,
            url=f"https://x.com/{user.core.screen_name}/status/{tweet.rest_id}",
            extra=extra,
            repost=repost,
        )

    @handle("twitter.com", r"twitter\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tid>[0-9]+)")
    @handle("x.com", r"x\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tid>[0-9]+)")
    async def _parse(self, searched: Match[str]) -> ParseResult:
        return await self.parse_tweet(searched.group("tid"))

    async def parse_tweet(self, tweet_id: str) -> ParseResult:
        """按推文 id 解析"""
        async with AsyncClient(headers=self.headers, timeout=self.timeout) as client:
            response = await client.post(API_URL, json={"pid": tweet_id})

        try:
            response.raise_for_status()
        except Exception as e:
            raise ParseException(f"获取数据失败 {response.status_code}") from e

        res = response.json()
        if res.get("code") != SUCCESS_CODE:
            raise ParseException(res.get("message") or res)

        try:
            instructions = res["data"]["data"][
                "threaded_conversation_with_injections_v2"
            ]["instructions"]
        except (KeyError, TypeError) as e:
            raise ParseException("返回数据结构异常") from e

        entries = next(
            (
                instruction["entries"]
                for instruction in instructions
                if instruction.get("type") == "TimelineAddEntries"
            ),
            None,
        )
        if entries is None:
            raise ParseException("TimelineAddEntries not found")

        return self._collect_entries(tweet_id, entries)

    def _collect_entries(
        self,
        tweet_id: str,
        entries: list[dict[str, Any]],
    ) -> ParseResult:
        """从 entries 中取出目标推文并构建结果"""
        # 所有推文的索引: rest_id -> tweet_results
        tweet_map: dict[str, dict[str, Any]] = {}
        # 当前链接对应的那条推文
        root_entry: dict[str, Any] | None = None

        for entry in entries:
            for tweet_results in _iter_timeline_tweet_results(entry):
                result = tweet_results.get("result") or {}
                rest_id = _get_rest_id(result)
                if not rest_id:
                    continue

                tweet_map[rest_id] = tweet_results
                if rest_id == tweet_id:
                    root_entry = tweet_results

        if root_entry is None:
            raise ParseException(f"未找到推文 {tweet_id}")

        root_result = root_entry.get("result") or {}
        legacy = _get_legacy(root_result)

        # 链接指向的是回复时, 用被回复的推文补成 quoted_status_result, 交给 collect 统一处理
        if "quoted_status_result" not in root_result:
            in_reply_to_id = legacy.get("in_reply_to_status_id_str") or legacy.get(
                "conversation_id_str"
            )
            if in_reply_to_id and in_reply_to_id != tweet_id:
                parent_entry = tweet_map.get(in_reply_to_id)
                if parent_entry is not None:
                    root_result["quoted_status_result"] = parent_entry

        tweet = convert(root_entry, TweetEntry)
        return self._collect_result(tweet)
