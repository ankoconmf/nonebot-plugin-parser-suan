"""X (Twitter) 解析

主接口与 nonebot-plugin-parser-lite 的 x 解析保持一致: 直接请求 X 的 GraphQL
    GET https://x.com/i/api/graphql/Xl0tsHf4AzflMRjbw9e70A/TweetResultByRestId
鉴权用内置的 web Bearer, 未配置 cookie 时走游客 token (guest token) 激活流程。
翻译(Grok)需要登录 cookie (parser_x_ck), 未配置时自动跳过。

X 直连失败时回退到 api.fxtwitter.com (旧的 vxtwitter 目前已 403 不可用),
回退源只保证正文/媒体/统计, 没有 Article 富文本、链接卡片与翻译。

支持的形态: 普通推文 / 图片 / 视频 / 动图 / 长文本(note_tweet) / X Article /
链接卡片 / 引用推文(带评论转发) / 直接转发 / 敏感内容(possibly_sensitive)。
"""

from re import Match
from uuid import uuid4
from time import monotonic
from json import loads as json_loads
from collections.abc import Callable
from typing import Any, ClassVar

from httpx import AsyncClient
from msgspec import convert
from msgspec.json import Decoder, encode

from nonebot import logger

from .model import FxResponse, FxTweet, Poll, PollChoice, Tweet, TweetEntry
from .util import LinkCardData, parse_link_card, parse_poll
from ..base import BaseParser, PlatformEnum, ParseException, handle
from ..cookie import ck2dict
from ..data import Platform, ParseResult
from ..utils import fmt_stat
from ...config import pconfig

TWEET_RESULT_API: str = (
    "https://x.com/i/api/graphql/Xl0tsHf4AzflMRjbw9e70A/TweetResultByRestId"
)
"""主接口: 按推文 id 取推文详情 (GraphQL)"""

GUEST_ACTIVATE_API: str = "https://api.x.com/1.1/guest/activate.json"
"""游客 token 激活接口"""

TRANSLATION_API: str = "https://api.x.com/2/grok/translation.json"
"""翻译接口(Grok), 需要登录 cookie"""

GUEST_TOKEN_TTL: float = 2 * 60 * 60
"""游客 token 有效期(秒), 超过后重新激活"""

LANGUAGE_NAMES: dict[str, str] = {
    "ja": "日语",
    "en": "英语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "ar": "阿拉伯语",
    "tr": "土耳其语",
    "hi": "印地语",
    "zh-cn": "简体中文",
    "zh-tw": "繁体中文",
    "zh": "中文",
}
"""推文语言代码 -> 中文名"""


def language_name(code: str | None) -> str | None:
    """语言代码转中文名, 未知代码原样返回"""
    if not code:
        return None
    code = code.strip()
    return LANGUAGE_NAMES.get(code.lower(), code)

V2_BEARER: str = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8x"
    "nZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
"""X web 端内置 Bearer"""

FEATURES: bytes = encode(
    {
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "premium_content_api_read_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
        "responsive_web_grok_analyze_post_followups_enabled": True,
        "rweb_cashtags_composer_attachment_enabled": True,
        "responsive_web_jetfuel_frame": True,
        "rweb_sports_post_context_enabled": True,
        "responsive_web_grok_share_attachment_enabled": True,
        "responsive_web_grok_annotations_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "rweb_conversational_replies_downvote_enabled": False,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "content_disclosure_indicator_enabled": True,
        "content_disclosure_ai_generated_indicator_enabled": True,
        "responsive_web_grok_show_grok_translated_post": True,
        "responsive_web_grok_analysis_button_from_backend": True,
        "post_ctas_fetch_enabled": False,
        "rweb_cashtags_enabled": True,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": False,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "responsive_web_profile_redirect_enabled": True,
        "rweb_tipjar_consumption_enabled": False,
        "verified_phone_label_enabled": False,
        "responsive_web_nested_quote_preview_enabled": False,
        "responsive_web_grok_image_annotation_enabled": True,
        "responsive_web_grok_imagine_annotation_enabled": True,
        "responsive_web_grok_community_note_auto_translation_is_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    }
)
"""GraphQL features 参数"""

FIELD_TOGGLES: bytes = encode(
    {
        "withArticleRichContentState": True,
        "withArticlePlainText": False,
        "withArticleSummaryText": True,
        "withArticleVoiceOver": True,
    }
)
"""GraphQL fieldToggles 参数"""

FALLBACK_API_URL: str = "https://api.fxtwitter.com/Twitter/status/{tid}"
"""备用接口: 只需要推文 id, 直接拼在路径里"""

fx_decoder = Decoder(FxResponse)
"""备用接口响应解码器"""


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.TWITTER, display_name="X")

    def __init__(self):
        super().__init__()
        self.headers.update({"Authorization": V2_BEARER})
        self.cookies: dict[str, str] | None = None
        if ck := pconfig.x_ck:
            try:
                self.cookies = ck2dict(ck)
            except ValueError:
                logger.warning("X cookies 格式异常, 已忽略")
        # 游客 token 用实例状态, 避免多个 parser 实例互相干扰
        self.guest_token: str | None = None
        self.guest_token_created_at: float = 0.0

    @property
    def _has_cookies(self) -> bool:
        """是否配置了登录 cookie (翻译需要)"""
        return bool(self.cookies and self.cookies.get("auth_token"))

    def _get_csrf_token(self) -> str:
        """CSRF token: 优先复用 cookie 里的 ct0 (与真实浏览器一致), 否则随机生成"""
        ct0 = (self.cookies or {}).get("ct0", "").strip()
        # 含分隔符/换行的值会破坏请求头, 退回随机值
        if ct0 and not any(ch in ct0 for ch in ";\r\n"):
            return ct0
        return uuid4().hex

    # ------------------------------------------------------------------
    # 鉴权: 优先 cookie, 否则走游客 token
    # ------------------------------------------------------------------

    async def _ensure_guest_token(self, client: AsyncClient) -> str:
        """激活游客 token (带 TTL 缓存)"""
        response = await client.post(GUEST_ACTIVATE_API)
        try:
            response.raise_for_status()
            token = response.json().get("guest_token")
            if not isinstance(token, str) or not token:
                raise ValueError("guest_token missing")
        except Exception as e:
            raise ParseException(f"获取游客 token 失败: {response.text[:120]}") from e

        self.guest_token = token
        self.guest_token_created_at = monotonic()
        return token

    async def _get_auth_headers(self, client: AsyncClient) -> dict[str, str]:
        """构造 GraphQL 请求头 (游客 token 或登录 cookie)"""
        csrf_token = self._get_csrf_token()
        headers = {
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "zh-cn",
            "x-csrf-token": csrf_token,
        }
        if self._has_cookies:
            cookies = self.cookies or {}
            headers["Cookie"] = (
                f"auth_token={cookies.get('auth_token', '')}; ct0={csrf_token};"
            )
            headers["x-twitter-auth-type"] = "OAuth2Session"
        else:
            headers["x-guest-token"] = await self._guest_token(client)
        return headers

    async def _guest_token(self, client: AsyncClient) -> str:
        """游客 token 超过 TTL 后重新激活"""
        if (
            self.guest_token is None
            or monotonic() - self.guest_token_created_at >= GUEST_TOKEN_TTL
        ):
            return await self._ensure_guest_token(client)
        return self.guest_token

    def _client(self) -> AsyncClient:
        return AsyncClient(
            headers=self.headers,
            proxy=pconfig.proxy,
            timeout=self.timeout,
            verify=False,
        )

    # ------------------------------------------------------------------
    # 翻译 (Grok): 需要登录 cookie
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_translation_body(text: str) -> tuple[str | None, dict[int, str]]:
        """解析翻译响应

        投票推文返回的是 NDJSON (每行一个 JSON):
            {"result": {"content_type": "POST", "text": "..."}}
            {"result": {"content_type": "POLL", "index": 1, "text": "..."}}
        普通推文只返回第一行。
        """
        tweet_text: str | None = None
        poll_choices: dict[int, str] = {}

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json_loads(line)
            except ValueError:
                continue
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                continue

            translated = result.get("text")
            if not isinstance(translated, str) or not translated:
                continue

            if result.get("content_type") == "POLL":
                index = result.get("index")
                if isinstance(index, int):
                    poll_choices[index] = translated
            elif tweet_text is None:
                tweet_text = translated

        return tweet_text, poll_choices

    async def _get_translation(
        self,
        client: AsyncClient,
        tweet: Tweet | FxTweet,
    ) -> tuple[str, str | None, dict[int, str]] | None:
        """获取推文的翻译, 返回 (译文, 原文语言, 选项译文); 不需要或失败时返回 None

        主备两个源的推文对象字段不同, 这里统一取 id / 语言 / 是否需要翻译。
        """
        if not self._has_cookies:
            return None

        if isinstance(tweet, Tweet):
            tweet_id, lang, needed = tweet.rest_id, tweet.legacy.lang, tweet.needs_translation()
        else:
            tweet_id, lang, needed = tweet.id, tweet.lang, tweet.needs_translation
        if not needed:
            return None

        try:
            response = await client.post(
                TRANSLATION_API,
                headers={**self.headers, **await self._get_auth_headers(client)},
                json={"content_type": "POST", "id": tweet_id, "dst_lang": "zh"},
            )
            response.raise_for_status()
            translated, poll_choices = self._parse_translation_body(response.text)
            if not translated:
                raise ValueError("translation text missing")
            return translated, language_name(lang), poll_choices
        except Exception:
            logger.opt(exception=True).debug("获取 X 翻译失败")
            return None

    # ------------------------------------------------------------------
    # 结果构建
    # ------------------------------------------------------------------

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

    @staticmethod
    def _stats_panel(
        view_count: int | None,
        favorite_count: int | None,
        retweet_count: int | None,
        reply_count: int | None,
        quote_count: int | None = None,
        bookmark_count: int | None = None,
    ) -> list[dict[str, str]]:
        """卡片底部互动数据面板"""
        stats: list[dict[str, str]] = []
        if view_count:
            stats.append({"icon": "eye", "value": fmt_stat(view_count), "label": "浏览"})
        stats.extend(
            [
                {"icon": "like", "value": fmt_stat(favorite_count), "label": "点赞"},
                {"icon": "share", "value": fmt_stat(retweet_count), "label": "转推"},
                {"icon": "comment", "value": fmt_stat(reply_count), "label": "回复"},
            ]
        )
        if quote_count:
            stats.append({"icon": "share", "value": fmt_stat(quote_count), "label": "引用"})
        if bookmark_count:
            stats.append({"icon": "star", "value": fmt_stat(bookmark_count), "label": "收藏"})
        return stats

    async def _collect_result(
        self,
        raw: TweetEntry,
        client: AsyncClient,
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
            repost = await self._collect_result(repost_status, client, True)

        extra: dict[str, Any] = {}
        # 转发内容不显示统计面板 (翻译仍然提供)
        if not is_repost:
            extra["stats"] = self._stats_panel(
                view_count=tweet.views.view_count if tweet.views else 0,
                favorite_count=legacy.favorite_count,
                retweet_count=legacy.retweet_count,
                reply_count=legacy.reply_count,
                quote_count=legacy.quote_count,
                bookmark_count=legacy.bookmark_count,
            )
            extra["source_id"] = f"@{user.core.screen_name}"

        # 投票选项的译文由翻译接口一并返回 (content_type=POLL)
        poll_choices: dict[int, str] = {}
        if translation := await self._get_translation(client, tweet):
            translated, translation_from, poll_choices = translation
            extra["translation"] = translated
            extra["translation_from"] = translation_from

        if poll := parse_poll(tweet.card):
            for index, choice in enumerate(poll.choices, start=1):
                if label := poll_choices.get(index):
                    choice.label = label
            extra["poll"] = poll

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

    # ------------------------------------------------------------------
    # 备用接口: api.fxtwitter.com
    # ------------------------------------------------------------------

    async def _collect_fallback(
        self,
        tweet: FxTweet,
        client: AsyncClient,
        is_repost: bool = False,
    ) -> ParseResult:
        author = self.create_author(
            name=tweet.author.name,
            avatar_url=tweet.author.avatar_url,
            description=tweet.author.description,
        )

        # 与主接口一致: 图片 / 视频都走 contents (模板按图片网格渲染);
        # graphics 是"图文插槽", 每项会铺满纵向堆叠 (文章式排版), 普通推文不能用。
        contents: list[Any] = []
        for media in tweet.media.all if tweet.media else []:
            if media.type == "photo":
                contents.append(self.create_image(media.url))
                continue
            if video_url := media.best_video_url:
                contents.append(
                    self.create_video(
                        video_url,
                        media.thumbnail_url,
                        duration=media.duration_seconds,
                        is_gif=media.type == "gif",
                    )
                )

        repost: ParseResult | None = None
        if not is_repost and tweet.quote is not None:
            repost = await self._collect_fallback(tweet.quote, client, True)

        extra: dict[str, Any] = {}
        if not is_repost:
            extra["stats"] = self._stats_panel(
                view_count=tweet.views,
                favorite_count=tweet.likes,
                retweet_count=tweet.retweets,
                reply_count=tweet.replies,
                quote_count=tweet.quotes,
                bookmark_count=tweet.bookmarks,
            )
            extra["source_id"] = f"@{tweet.author.screen_name}"

        # 回退源没有翻译数据, 仍然用 X 的翻译接口补齐;
        # 回退响应没有 is_translatable, 只按语言粗略判断 (语言缺失时不跳过)
        tweet.needs_translation = not (tweet.lang or "").lower().startswith("zh")
        poll_choices: dict[int, str] = {}
        if translation := await self._get_translation(client, tweet):
            translated, translation_from, poll_choices = translation
            extra["translation"] = translated
            extra["translation_from"] = translation_from

        if tweet.poll is not None and tweet.poll.choices:
            poll = tweet.poll.to_poll()
            for index, choice in enumerate(poll.choices, start=1):
                if label := poll_choices.get(index):
                    choice.label = label
            extra["poll"] = poll

        return self.result(
            author=author,
            text=tweet.text,
            contents=contents,
            timestamp=tweet.created_timestamp,
            url=tweet.url or f"https://x.com/{tweet.author.screen_name}/status/{tweet.id}",
            extra=extra,
            repost=repost,
        )

    async def _parse_by_fallback(self, tweet_id: str) -> ParseResult:
        """按推文 id 走备用接口解析"""
        async with self._client() as client:
            response = await client.get(FALLBACK_API_URL.format(tid=tweet_id))

            if response.status_code >= 400:
                # 接口用 404 表示推文不存在/不可见
                raise ParseException(f"备用接口获取数据失败 {response.status_code}")

            data = fx_decoder.decode(response.content)
            if data.code != 200 or data.tweet is None:
                raise ParseException(f"解析失败: {data.message or data.code}")
            return await self._collect_fallback(data.tweet, client)

    @handle("twitter.com", r"twitter\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tid>[0-9]+)")
    @handle("x.com", r"x\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tid>[0-9]+)")
    async def _parse(self, searched: Match[str]) -> ParseResult:
        return await self.parse_tweet(searched.group("tid"))

    def _get_sources(self) -> list[Callable[[str], Any]]:
        """解析源, 按顺序尝试; X 直连失败时回退到 fxtwitter"""
        return [self._parse_by_primary, self._parse_by_fallback]

    async def parse_tweet(self, tweet_id: str) -> ParseResult:
        """按推文 id 解析: 主接口失败时自动回退到备用接口"""
        errors: list[str] = []
        for source in self._get_sources():
            try:
                return await source(tweet_id)
            except ParseException as e:
                errors.append(str(e) or type(e).__name__)

        detail = "; ".join(errors) if errors else "无可用解析源"
        raise ParseException(f"解析失败: {detail}")

    async def _parse_by_primary(self, tweet_id: str) -> ParseResult:
        """走 X 官方 GraphQL 接口 (与 parser-lite 一致)"""
        async with self._client() as client:
            response = await client.get(
                TWEET_RESULT_API,
                params={
                    "variables": encode(
                        {
                            "tweetId": tweet_id,
                            "includePromotedContent": True,
                            "withBirdwatchNotes": True,
                            "withVoice": True,
                            "withCommunity": True,
                            "withV2Timeline": True,
                            "withQuickPromoteEligibilityTweetFields": True,
                        }
                    ).decode(),
                    "features": FEATURES.decode(),
                    "fieldToggles": FIELD_TOGGLES.decode(),
                },
                headers=await self._get_auth_headers(client),
            )

            if response.status_code >= 400:
                raise ParseException(
                    f"主接口获取数据失败 {response.status_code}: {response.text[:200]}"
                )

            try:
                payload = response.json()
            except Exception as e:
                raise ParseException("主接口返回了无效 JSON") from e
            if not isinstance(payload, dict):
                raise ParseException("主接口返回了无效 JSON 对象")

            tweet_result = (payload.get("data") or {}).get("tweetResult") or {}
            if not tweet_result:
                raise ParseException("主接口未返回 tweetResult")

            try:
                tweet = convert(tweet_result, TweetEntry)
            except Exception as e:
                logger.opt(exception=True).debug(f"解析 TweetResult 失败: {tweet_result}")
                raise ParseException("解析推文结构失败") from e

            return await self._collect_result(tweet, client)
