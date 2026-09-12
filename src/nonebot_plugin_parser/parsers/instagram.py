import json
import re
import asyncio
from html import unescape
from typing import Any, ClassVar
from xml.etree import ElementTree

from httpx import AsyncClient
from nonebot import logger

from ..config import pconfig
from .base import BaseParser, PlatformEnum, handle
from .data import Platform
from .utils import fmt_stat
from ..utils import generate_file_name
from ..download import downloader
from ..exception import ParseException

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _shortcode_to_pk(shortcode: str) -> str:
    """shortcode 是媒体 pk 的 base64url 整数编码 (与 yt-dlp _id_to_pk 一致).

    超过 28 位的是带签名的私有分享链接, 末尾 28 位为签名需去掉.
    """
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    pk = 0
    for ch in shortcode:
        pk = pk * 64 + _IG_ALPHABET.index(ch)
    return str(pk)


class InstagramParser(BaseParser):
    """Instagram 解析器: 直连官方接口, 免登录为主, 配置 cookies 可解析登录内容.

    - 匿名: www.instagram.com/graphql/query, doc_id 27128499623469141
      (xdt_api__v1__media__shortcode__web_info, instaloader 同款方案),
      需先访问 instagram.com 首页获取 csrftoken 匿名 cookie
    - 登录态 (pconfig.instagram_ck 配置 cookies): i.instagram.com/api/v1
      私有 API, 可解析需要登录的内容(快拍等), 失效时自动降级匿名接口

    两条路径返回的都是 API-v1 媒体结构 (caption/user/video_versions/
    image_versions2/carousel_media/like_count/...), 共用 _build_result.
    """

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.INSTAGRAM, display_name="Instagram")

    GRAPHQL_URL: ClassVar[str] = "https://www.instagram.com/graphql/query"
    DOC_ID_WEB_INFO: ClassVar[str] = "27128499623469141"
    IG_APP_ID: ClassVar[str] = "936619743392459"
    _anon_cookies: ClassVar[dict[str, str] | None] = None
    """匿名 cookies (csrftoken/mid), 缓存复用避免每次解析都先请求首页"""

    @handle("instagr.am", r"instagr\.am/\w+[\S]*")
    async def _parse_short(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        logger.debug(f"InstagramParser._parse_short matched: {url}")
        return await self.parse_with_redirect(url)

    # https://www.instagram.com/share/p/xxxx / share/xxxx → 302 到真实帖子页
    @handle("instagram.com/share", r"(?:https?://)?(?:www\.)?instagram\.com/share/[\w/.-]+")
    async def _parse_share(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://www.instagram.com/p/CuE2WNQs6vH/
    # https://www.instagram.com/reel/DV5T344iDAn/
    @handle(
        "instagram.com",
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.-]+/)?(?:p|reel|tv)/(?P<code>[A-Za-z0-9_-]+)',
    )
    async def _parse(self, searched: re.Match[str]):
        url = searched.group(0)
        shortcode = searched.group("code")
        logger.debug(f"InstagramParser._parse called with: {url}")

        # 配置了 cookies: 优先登录态私有 API (公开帖也可用, 数据更全)
        ck = pconfig.instagram_ck
        if ck:
            try:
                media = await self._fetch_via_private_api(_shortcode_to_pk(shortcode))
                return self._build_result(media, url)
            except Exception as e:
                logger.warning(f"failed to parse instagram {shortcode} via private api, fallback to graphql, error: {e}")

        try:
            media = await self._fetch_via_graphql(shortcode)
        except ParseException as e:
            # 两条路都失败且配置了 cookies: 大概率是 cookies 过期或内容需要登录
            if ck:
                raise ParseException(f"{e}, cookies 可能已过期或该内容需要登录") from e
            raise
        return self._build_result(media, url)

    # https://www.instagram.com/stories/{username}/{id}/ — 时效内容, 匿名接口拿不到
    @handle("instagram.com/stories", r"(?:https?://)?(?:www\.)?instagram\.com/stories/[\w/.-]+")
    async def _parse_stories(self, searched: re.Match[str]):
        url = searched.group(0)
        if "/highlights/" in url:
            raise ParseException("Instagram 精选集 (highlights) 暂不支持解析")

        # URL 尾段就是快拍媒体的数字 pk
        m = re.search(r"/stories/[\w.]+/(\d+)", url)
        if not m:
            raise ParseException("Instagram 快拍仅支持单条快拍分享链接")

        ck = pconfig.instagram_ck
        if not ck:
            raise ParseException("Instagram 快拍 (stories) 为登录内容, 请配置 parser_instagram_ck")

        media = await self._fetch_via_private_api(m.group(1))
        return self._build_result(media, url)

    async def _fetch_via_graphql(self, shortcode: str) -> dict[str, Any]:
        """直连官方 web GraphQL, 免登录获取帖子数据 (API-v1 媒体结构)."""
        proxy = pconfig.instagram_proxy or pconfig.proxy
        headers = {
            "User-Agent": _UA,
            "Accept-Language": "en-US,en;q=0.9",
            "x-ig-app-id": self.IG_APP_ID,
        }
        if pconfig.instagram_ck:
            headers["Cookie"] = pconfig.instagram_ck
        async with AsyncClient(headers=headers, proxy=proxy, timeout=self.timeout, verify=False) as client:
            csrf = await self._ensure_cookies(client)
            variables = json.dumps(
                {
                    "shortcode": shortcode,
                    "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                },
                separators=(",", ":"),
            )
            resp = await client.post(
                self.GRAPHQL_URL,
                data={
                    "variables": variables,
                    "doc_id": self.DOC_ID_WEB_INFO,
                    "server_timestamps": "true",
                },
                headers={
                    "x-csrftoken": csrf,
                    "authority": "www.instagram.com",
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": f"https://www.instagram.com/p/{shortcode}/",
                },
            )
            if resp.status_code != 200:
                # 大概率被限流, 丢弃缓存的匿名 cookies, 下次解析重新引导
                InstagramParser._anon_cookies = None
                raise ParseException(f"Instagram 接口返回 {resp.status_code}, 可能触发限流, 请稍后再试")
            data = resp.json()

        items = ((data.get("data") or {}).get("xdt_api__v1__media__shortcode__web_info") or {}).get("items") or []
        if not items:
            raise ParseException("未找到帖子数据, 可能为私密内容或链接失效")
        return items[0]

    async def _fetch_via_private_api(self, pk: str) -> dict[str, Any]:
        """登录态私有 API (需配置 cookies), 可解析需要登录的内容.

        与匿名接口同为 API-v1 媒体结构, cookies 失效时抛异常由调用方降级.
        """
        proxy = pconfig.instagram_proxy or pconfig.proxy
        headers = {
            "User-Agent": _UA,
            "x-ig-app-id": self.IG_APP_ID,
            "X-ASBD-ID": "359341",
            "X-IG-WWW-Claim": "0",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
            "Cookie": pconfig.instagram_ck or "",
        }
        async with AsyncClient(headers=headers, proxy=proxy, timeout=self.timeout, verify=False) as client:
            resp = await client.get(f"https://i.instagram.com/api/v1/media/{pk}/info/")
            if resp.status_code != 200:
                raise ParseException(f"private api status: {resp.status_code} (cookies 可能已失效)")
            data = resp.json()

        items = data.get("items") or []
        if not items:
            raise ParseException("private api 返回空, 可能为私密内容或 cookies 无权限")
        media = items[0]
        # 登录接口的高分辨率视频在 DASH 清单中，video_versions 通常只有 720p。
        dash_videos, dash_audio = self._parse_dash_streams(media.get("video_dash_manifest"))
        if dash_videos:
            media = dict(media)
            media["video_versions"] = [*media.get("video_versions", []), *dash_videos]
            if dash_audio:
                media["_dash_audio_url"] = dash_audio
        return media

    @staticmethod
    def _parse_dash_streams(manifest: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """提取 Instagram DASH 清单中的视频轨和音频轨。"""
        if not manifest:
            return [], None
        try:
            root = ElementTree.fromstring(manifest)
        except ElementTree.ParseError:
            return [], None
        videos = []
        audio_url = None
        for representation in root.iter():
            if not representation.tag.endswith("Representation"):
                continue
            mime = representation.attrib.get("mimeType", "")
            width = int(representation.attrib.get("width") or 0)
            height = int(representation.attrib.get("height") or 0)
            base_url = next((child.text for child in representation if child.tag.endswith("BaseURL") and child.text), None)
            if mime.startswith("audio/"):
                if base_url:
                    audio_url = unescape(base_url)
                continue
            if not mime.startswith("video/") or not width or not height:
                continue
            if base_url:
                videos.append(
                    {
                        "url": unescape(base_url),
                        "width": width,
                        "height": height,
                        "bandwidth": int(representation.attrib.get("bandwidth") or 0),
                        "filesize": int(representation.attrib.get("FBContentLength") or 0),
                    }
                )
        return videos, audio_url

    async def _ensure_cookies(self, client: AsyncClient) -> str:
        """确保拿到匿名 cookies (csrftoken 等), 返回 csrftoken."""
        if self._anon_cookies:
            client.cookies.update(self._anon_cookies)
            return self._anon_cookies.get("csrftoken", "")

        await client.get("https://www.instagram.com/")
        cookies = dict(client.cookies)
        if cookies:
            InstagramParser._anon_cookies = cookies
        return cookies.get("csrftoken", "")

    def _build_result(self, media: dict[str, Any], url: str):
        """把 API-v1 媒体结构转换为统一解析结果."""
        # Instagram CDN 资源下载需要 Referer
        self.headers["Referer"] = "https://www.instagram.com/"

        user = media.get("user") or {}
        author = None
        if user.get("username"):
            author = self.create_author(
                user.get("full_name") or user["username"],
                user.get("profile_pic_url"),
            )

        contents = []
        if children := media.get("carousel_media"):
            # 图集: 子项可能混排视频与图片
            for child in children:
                if videos := child.get("video_versions"):
                    video = self._best_video(videos)
                    video_source = self._video_source(video, child.get("_dash_audio_url"))
                    contents.append(
                        self.create_video(
                            video_source,
                            self._image_url(child),
                            child.get("video_duration"),
                        )
                    )
                elif image := self._image_url(child):
                    contents.extend(self.create_images([image]))
        elif videos := media.get("video_versions"):
            video = self._best_video(videos)
            video_source = self._video_source(video, media.get("_dash_audio_url"))
            contents.append(
                self.create_video(
                    video_source,
                    self._image_url(media),
                    media.get("video_duration"),
                )
            )
        elif image := self._image_url(media):
            contents.extend(self.create_images([image]))
        else:
            raise ParseException("未找到媒体链接")

        stats = []
        for icon, value, label in (
            ("eye", media.get("view_count"), "播放"),
            ("like", media.get("like_count"), "点赞"),
            ("comment", media.get("comment_count"), "评论"),
            ("share", media.get("media_repost_count"), "分享"),
        ):
            if isinstance(value, int) and value >= 0:
                stats.append({"icon": icon, "value": fmt_stat(value), "label": label})

        caption = (media.get("caption") or {}).get("text") or None
        return self.result(
            title=None,
            text=caption,
            author=author,
            contents=contents,
            timestamp=media.get("taken_at"),
            url=url,
            extra={"stats": stats} if stats else {},
        )

    def _video_source(self, video: dict[str, Any], audio_url: str | None):
        if not audio_url:
            return video["url"]
        output_path = pconfig.cache_dir / generate_file_name(video["url"], ".mp4")
        merged_path = output_path.with_name(f"{output_path.stem}.merged{output_path.suffix}")

        async def merge():
            if merged_path.exists():
                return output_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            await downloader.download_av_and_merge(
                video["url"],
                audio_url,
                output_path=merged_path,
                ext_headers=self.headers,
            )
            if output_path.exists():
                output_path.unlink()
            merged_path.replace(output_path)
            return output_path

        return asyncio.create_task(merge())

    @staticmethod
    def _best_video(videos: list[dict[str, Any]]) -> dict[str, Any]:
        """选择最高画质视频，超过 100 MB 时自动选择下一档。"""
        candidates = [video for video in videos if video.get("url")]
        if not candidates:
            raise ParseException("未找到可用的视频链接")

        def quality(video: dict[str, Any]) -> tuple[int, int, int]:
            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
            bitrate = int(video.get("bitrate") or video.get("bandwidth") or 0)
            return width * height, bitrate, width + height

        eligible = [
            video for video in candidates
            if not video.get("filesize")
            or int(video["filesize"]) <= 100 * 1024 * 1024
        ]
        return max(eligible or candidates, key=quality)

    @staticmethod
    def _image_url(media: dict[str, Any]) -> str | None:
        """取分辨率最高的一档图片 (candidates 按分辨率降序排列, 首档最大)."""
        candidates = (media.get("image_versions2") or {}).get("candidates") or []
        return candidates[0].get("url") if candidates else None
