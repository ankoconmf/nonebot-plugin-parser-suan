"""Apple Music 解析器

Apple 官方 API(amp-api.music.apple.com)需要开发者 Bearer token, 因此这里只用公开数据:
  - 网页内嵌 JSON-LD: 标题 / 艺人 / 专辑 / 时长 / 流派 / 发行日期 / 封面 / 官方试听直链
  - iTunes lookup API: 网页解析不到时的兜底(单曲), 顺便补齐发行日期

注意: 完整歌曲受 FairPlay DRM 保护, 只能取到官方试听片段(通常 30 秒),
因此卡片会标注"官方试听片段(非完整版)"。
"""

import json
import re
from datetime import datetime
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from .base import BaseParser, PlatformEnum, handle
from .data import Author, Platform
from .utils import fmt_duration
from ..config import pconfig
from ..exception import ParseException

_WEB_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_JSON_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_ARTWORK_SIZE_RE = re.compile(r"/\d+x\d+bb\.(?:jpg|png)$")
_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")
_SONG_ID_RE = re.compile(r"[?&]i=(\d+)")

_COVER_SIZE = "1600x1600"
"""封面请求尺寸(iTunes 图片服务支持任意尺寸, 直接要方形大图)"""

_GENERIC_GENRES = frozenset({"Music", "音乐"})


class AppleMusicParser(BaseParser):
    """Apple Music 单曲解析器"""

    platform: ClassVar[Platform] = Platform(
        name=PlatformEnum.APPLE_MUSIC,
        display_name="Apple Music",
    )

    def __init__(self):
        super().__init__()
        self.headers.update(_WEB_HEADERS)

    @handle(
        "music.apple.com",
        r"(?:https?://)?(?:[\w-]+\.)?music\.apple\.com/(?P<storefront>[a-z]{2})/"
        r"(?P<kind>album|song)/[^/?#\s]+/(?P<item_id>\d+)(?P<query>\?[^\s]*)?",
    )
    async def _parse(self, searched: re.Match[str]):
        storefront = searched.group("storefront")
        item_id = searched.group("item_id")
        kind = searched.group("kind")
        query = searched.group("query") or ""

        page_url = searched.group(0)
        if not page_url.startswith("http"):
            page_url = f"https://{page_url}"

        # /song/<slug>/<id> 本身就是单曲, /album/...?i=<id> 通过 i 参数指向单曲
        if kind == "song":
            song_id = item_id
        else:
            song_id = matched.group(1) if (matched := _SONG_ID_RE.search(query)) else None

        page = await self._fetch_json_ld(page_url)
        recording, album = self._extract_recording(page)
        audio_object = recording.get("audio") or {}
        if not isinstance(audio_object, dict):
            audio_object = {}

        # 单曲链接补一次 lookup(网页 JSON-LD 常缺发行日期); 网页没给出曲目时也用它兜底
        lookup = {}
        if song_id or not recording:
            lookup = await self._lookup(song_id or item_id, storefront) or {}

        title = self._first_str(
            recording.get("name"),
            lookup.get("trackName"),
            page.get("name") if page else None,
        )
        artist = self._first_artist(recording, album) or lookup.get("artistName")
        album_name = self._first_str(album.get("name"), lookup.get("collectionName"))
        cover_url = self._pick_cover(page, recording, audio_object, album, lookup)
        preview_url = self._first_str(audio_object.get("contentUrl"), lookup.get("previewUrl"))

        duration = self._parse_iso_duration(recording.get("duration"))
        if not duration and (millis := lookup.get("trackTimeMillis")):
            duration = float(millis) / 1000

        timestamp = self._parse_timestamp(
            self._first_str(album.get("datePublished"), lookup.get("releaseDate"))
        )

        if not title and not preview_url:
            raise ParseException("Apple Music 解析失败: 未获取到曲目信息")

        # 试听片段(30 秒左右), 完整歌曲受 FairPlay DRM 保护无法获取
        contents = []
        if cover_url:
            contents.append(self.create_image(self._square_artwork(cover_url)))
        if preview_url:
            contents.append(self.create_audio(preview_url, duration or 0.0))

        return self.result(
            title=title,
            text=self._build_text(album_name, duration, timestamp, page),
            author=Author(name=artist) if artist else None,
            contents=contents,
            timestamp=timestamp,
            url=self._first_str(recording.get("url"), lookup.get("trackViewUrl"), page_url),
            extra={
                "content_type": "音频",
                "info": self._build_info(recording, album, lookup),
                # 供 Apple Music 专属卡片(播放器样式)取用的结构化字段
                "album": album_name,
                "artist": artist,
                "genres": self._genres(recording, album, lookup),
                "duration": duration,
                "release_date": (
                    datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d") if timestamp else None
                ),
            },
        )

    @staticmethod
    def _first_str(*values: Any) -> str | None:
        """返回第一个非空字符串"""
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @classmethod
    def _first_artist(cls, recording: dict[str, Any], album: dict[str, Any]) -> str | None:
        for source in (recording, album):
            for artist in source.get("byArtist") or []:
                if isinstance(artist, dict) and (name := cls._first_str(artist.get("name"))):
                    return name
        return None

    @staticmethod
    def _extract_recording(page: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """从 JSON-LD 中取出目标单曲(MusicRecording)与所属专辑(MusicAlbum)"""
        if not page:
            return {}, {}

        # 专辑页: tracks[0] 为第一首(未指定单曲时的兜底)
        if page.get("@type") == "MusicAlbum":
            tracks = [t for t in (page.get("tracks") or []) if isinstance(t, dict)]
            return (tracks[0] if tracks else {}), page

        # 单曲页: audio 是 MusicRecording, 其 audio 字段才是 AudioObject
        recording = page.get("audio")
        if not isinstance(recording, dict):
            return {}, {}
        album = recording.get("inAlbum")
        return recording, album if isinstance(album, dict) else {}

    @classmethod
    def _pick_cover(
        cls,
        page: dict[str, Any] | None,
        recording: dict[str, Any],
        audio_object: dict[str, Any],
        album: dict[str, Any],
        lookup: dict[str, Any],
    ) -> str | None:
        is_album_page = bool(page) and page.get("@type") == "MusicAlbum"
        candidates = (
            (album.get("image"), audio_object.get("thumbnailUrl"), recording.get("image"))
            if is_album_page
            else (recording.get("image"), audio_object.get("thumbnailUrl"), album.get("image"))
        )
        return cls._first_str(*candidates, lookup.get("artworkUrl100"))

    @staticmethod
    def _square_artwork(url: str) -> str:
        """把封面链接换成方形大图(/1200x630bb.jpg -> /1600x1600bb.jpg)"""
        if _ARTWORK_SIZE_RE.search(url):
            return _ARTWORK_SIZE_RE.sub(f"/{_COVER_SIZE}bb.jpg", url)
        return url

    @staticmethod
    def _parse_iso_duration(value: Any) -> float:
        """解析 ISO 8601 时长(PT2M39S)"""
        if not isinstance(value, str):
            return 0.0
        if not (matched := _ISO_DURATION_RE.fullmatch(value.strip())):
            return 0.0
        hours, minutes, seconds = (float(part) if part else 0.0 for part in matched.groups())
        return hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def _parse_timestamp(value: str | None) -> int | None:
        """解析发行日期(ISO 8601)为时间戳"""
        if not value:
            return None
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None

    @staticmethod
    def _genres(recording: dict[str, Any], album: dict[str, Any], lookup: dict[str, Any]) -> list[str]:
        raw: list[Any] = []
        for source in (recording, album):
            genre = source.get("genre")
            raw.extend(genre if isinstance(genre, list) else [genre])
        raw.append(lookup.get("primaryGenreName"))

        genres: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip() and item not in _GENERIC_GENRES and item not in genres:
                genres.append(item.strip())
        return genres

    @classmethod
    def _build_text(
        cls,
        album_name: str | None,
        duration: float,
        timestamp: int | None,
        page: dict[str, Any] | None,
    ) -> str:
        lines = []
        if album_name:
            lines.append(f"专辑: {album_name}")
        if duration:
            lines.append(f"时长: {fmt_duration(duration)}")
        if timestamp:
            lines.append(f"发行: {datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')}")

        # 未带 ?i= 的专辑链接: 说明只取了第一首, 避免误以为是完整专辑解析
        track_count = len(page.get("tracks") or []) if page and page.get("@type") == "MusicAlbum" else 0
        if track_count > 1:
            lines.append(f"专辑共 {track_count} 首, 未指定单曲, 试听为第 1 首")

        return "\n".join(lines)

    @classmethod
    def _build_info(
        cls,
        recording: dict[str, Any],
        album: dict[str, Any],
        lookup: dict[str, Any],
    ) -> str:
        parts = cls._genres(recording, album, lookup)
        parts.append("官方试听片段(非完整版)")
        return " · ".join(parts)

    async def _request(self, url: str, **kwargs: Any):
        async with AsyncClient(
            headers=self.headers,
            timeout=20.0,
            verify=False,
            proxy=pconfig.proxy,
            follow_redirects=True,
        ) as client:
            return await client.get(url, **kwargs)

    async def _fetch_json_ld(self, url: str) -> dict[str, Any] | None:
        """获取页面并解析内嵌 JSON-LD"""
        try:
            response = await self._request(url)
            if response.status_code != 200:
                raise ParseException(f"HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"Apple Music 页面获取失败, 尝试回退到 iTunes API: {e!r}")
            return None

        if not (matched := _JSON_LD_RE.search(response.text)):
            logger.warning("Apple Music 页面未找到 JSON-LD, 尝试回退到 iTunes API")
            return None

        try:
            data = json.loads(matched.group(1))
        except ValueError:
            logger.warning("Apple Music JSON-LD 解析失败, 尝试回退到 iTunes API")
            return None

        return data if isinstance(data, dict) else None

    async def _lookup(self, item_id: str, storefront: str) -> dict[str, Any] | None:
        """iTunes lookup API(公开无需鉴权), 作为网页解析的兜底与补充

        注意: 部分 storefront(如 cn)的专辑查询只返回专辑本身, 不返回曲目,
        因此专辑链接的曲目以网页 JSON-LD 为准。
        """
        try:
            response = await self._request(
                "https://itunes.apple.com/lookup",
                params={
                    "id": item_id,
                    "country": storefront,
                    "entity": "song",
                    "limit": 200,
                },
            )
            response.raise_for_status()
            results = response.json().get("results") or []
        except Exception as e:
            logger.warning(f"iTunes lookup 失败: {e!r}")
            return None

        tracks = [item for item in results if item.get("wrapperType") == "track"]
        if tracks:
            return tracks[0]
        return results[0] if results else None
