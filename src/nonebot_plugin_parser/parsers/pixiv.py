"""pixiv 解析器.

数据源两种, 按是否配置 refresh token 自动选择:
- 配置了 `parser_pixiv_refresh_token`: 走 app API(`app-api.pixiv.net`), 支持 R18,
  且能直接拿到每页原图地址(`meta_pages`).
- 未配置: 走网页版 ajax 接口(`ajax/illust/{id}`), 仅公开作品.

原图(`urls.original` / `image_urls.original`)位于 `img-original` 节点, 国内可能
连不上; 下载失败时自动回退到 regular/large(`img-master` 节点).

ugoira 动图本质是 zip(多帧) + 每帧延迟, 需要下载 zip 合成 GIF.

支持的链接格式:
- https://www.pixiv.net/artworks/12345678
- https://www.pixiv.net/en/artworks/12345678 (任意语言前缀)
- https://www.pixiv.net/i/12345678 (短链)
- https://www.pixiv.net/member_illust.php?mode=medium&illust_id=12345678
"""

import asyncio
import html
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from ..config import pconfig
from .base import BaseParser, PlatformEnum, ParseException, handle
from .data import ImageContent, Platform
from .utils import fmt_stat

# pixiv 官方移动端公开的 OAuth 凭据(用于 refresh_token 换取 access_token)
_PIXIV_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
_PIXIV_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
_PIXIV_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
_PIXIV_APP_API = "https://app-api.pixiv.net"
_PIXIV_APP_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"


def _strip_html(text: str | None) -> str | None:
    """把 pixiv caption 的 HTML 转成纯文本."""
    if not text:
        return None
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").strip()
    return text or None


def _to_timestamp(value: str | None) -> int | None:
    """ISO 时间字符串转 unix 秒."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        logger.warning(f"无法解析时间戳: {value}")
        return None


def _first(data: Any, *keys: str) -> Any:
    """按顺序取第一个非空字段, 兼容 pixiv 不同响应里的字段命名差异."""
    if not isinstance(data, dict):
        return None
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None


class PixivParser(BaseParser):
    """pixiv 插画/漫画/动图解析器."""

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.PIXIV, display_name="pixiv")

    def __init__(self):
        super().__init__()
        self.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.pixiv.net/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            }
        )
        if pconfig.pixiv_ck:
            # 网页版接口与图片 CDN 请求带上 cookie(可作为 refresh_token 的补充)
            self.headers["Cookie"] = pconfig.pixiv_ck

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @handle("pixiv.net/i/", r"pixiv\.net/i/(?P<illust_id>\d+)")
    async def _parse_short(self, searched: re.Match[str]):
        """解析短链"""
        illust_id = int(searched.group("illust_id"))
        return await self.parse_illust(illust_id)

    @handle(
        "artworks",
        r"pixiv\.net/(?:[a-z]{2}/)?artworks/(?P<illust_id>\d+)",
    )
    @handle(
        "member_illust",
        r"pixiv\.net/member_illust\.php\?(?:[^#\s]*&)?illust_id=(?P<illust_id>\d+)",
    )
    async def _parse(self, searched: re.Match[str]):
        """解析 artworks / member_illust 链接"""
        illust_id = int(searched.group("illust_id"))
        return await self.parse_illust(illust_id)

    async def parse_illust(self, illust_id: int):
        """获取作品信息并构建解析结果"""
        url = f"https://www.pixiv.net/artworks/{illust_id}"

        async with AsyncClient(
            headers=self.headers,
            timeout=self.timeout,
            verify=False,
            follow_redirects=True,
        ) as client:
            info = await self._fetch_illust(client, illust_id)

            # 屏蔽 R18
            if pconfig.pixiv_skip_r18 and int(info.get("x_restrict") or 0) > 0:
                raise ParseException("已屏蔽 R18 作品")

            author = self.create_author(info["user_name"], info["avatar_url"])

            if info["illust_type"] == 2:
                # ugoira 动图: 下载 zip 合成 GIF
                contents = await self._build_ugoira_gif_content(client, illust_id, info)
            else:
                contents = self._build_image_contents(info["images"])
                if not contents:
                    raise ParseException("未找到可下载的图片")

            # 统计信息
            stats = []
            if info["like"] is not None:
                stats.append({"icon": "like", "value": fmt_stat(info["like"]), "label": "点赞"})
            if info["bookmark"] is not None:
                stats.append({"icon": "star", "value": fmt_stat(info["bookmark"]), "label": "收藏"})
            if info["view"] is not None:
                stats.append({"icon": "eye", "value": fmt_stat(info["view"]), "label": "浏览"})
            if info["comment"] is not None:
                stats.append({"icon": "comment", "value": fmt_stat(info["comment"]), "label": "评论"})

            # 简介: 正文 + 标签
            tags_line = " ".join(f"#{t}" for t in info["tags"][:8])
            text = "\n\n".join(part for part in (info["caption"], tags_line) if part) or None

            return self.result(
                url=url,
                title=info["title"],
                text=text,
                author=author,
                timestamp=_to_timestamp(info["create_date"]),
                contents=contents,
                extra={
                    "stats": stats,
                    "source_id": str(illust_id),
                    "content_type": "图文",
                },
            )

    # ------------------------------------------------------------------ #
    # 数据获取
    # ------------------------------------------------------------------ #

    async def _get_access_token(self) -> str | None:
        """用 refresh_token 换取 access_token(内存缓存, 快过期时自动重换)."""
        refresh_token = pconfig.pixiv_refresh_token
        if not refresh_token:
            return None

        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        try:
            async with AsyncClient(verify=False, timeout=self.timeout) as client:
                resp = await client.post(
                    _PIXIV_TOKEN_URL,
                    data={
                        "client_id": _PIXIV_CLIENT_ID,
                        "client_secret": _PIXIV_CLIENT_SECRET,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "include_policy_response": "true",
                    },
                    headers={
                        "User-Agent": _PIXIV_APP_UA,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            token = data.get("access_token")
            if not token:
                logger.warning("pixiv refresh_token 换取 access_token 失败: 响应中无 access_token")
                return None

            self._access_token = token
            self._token_expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
            return token
        except Exception as e:
            logger.warning(f"pixiv refresh_token 换取 access_token 失败: {e}")
            return None

    async def _fetch_illust(self, client: AsyncClient, illust_id: int) -> dict[str, Any]:
        """按是否有 refresh token 选择 app API 或网页 ajax 接口."""
        if access_token := await self._get_access_token():
            return await self._fetch_illust_app(client, illust_id, access_token)
        return await self._fetch_illust_ajax(client, illust_id)

    async def _fetch_illust_ajax(self, client: AsyncClient, illust_id: int) -> dict[str, Any]:
        """网页版 ajax 接口(公开作品)."""
        resp = await client.get(f"https://www.pixiv.net/ajax/illust/{illust_id}")
        if resp.status_code == 403:
            raise ParseException(
                "pixiv 返回 403 (可能是 R18 作品, 请配置 `parser_pixiv_refresh_token`)"
            )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise ParseException(f"pixiv 解析失败: {payload.get('message') or '作品不存在或已删除'}")

        illust = payload.get("body")
        if not isinstance(illust, dict) or not illust:
            raise ParseException("pixiv 返回数据为空")

        illust_type = int(_first(illust, "illustType", "type") or 0)
        urls = illust.get("urls") or {}
        original = _first(urls, "original") if isinstance(urls, dict) else None
        regular = _first(urls, "regular") if isinstance(urls, dict) else None
        page_count = int(_first(illust, "pageCount") or 1)

        if illust_type == 2:
            # 静态帧回退: regular 优先, original 兜底
            images = [(regular, original)] if regular else ([(original, None)] if original else [])
        else:
            images = []
            for i in range(page_count):
                orig = self._page_url(original, i) if original else None
                reg = self._page_url(regular, i) if regular else None
                images.append((orig, reg))

        user_id = _first(illust, "userId")
        user_name = _first(illust, "userName", "userAccount") or "未知作者"
        avatar_url = None
        if user_id:
            if profile := await self._fetch_user_profile(client, str(user_id)):
                user_name = _first(profile, "name") or user_name
                avatar_url = _first(profile, "imageBig", "image")

        tags = [
            t.get("tag")
            for t in ((illust.get("tags") or {}).get("tags") or [])
            if isinstance(t, dict) and t.get("tag")
        ]

        return {
            "id": illust_id,
            "title": _first(illust, "illustTitle", "title") or f"illust_{illust_id}",
            "caption": _strip_html(_first(illust, "illustComment", "description", "comment")),
            "create_date": _first(illust, "createDate", "uploadDate"),
            "illust_type": illust_type,
            "x_restrict": int(_first(illust, "xRestrict", "x_restrict") or 0),
            "page_count": page_count,
            "user_name": user_name,
            "user_id": str(user_id) if user_id else None,
            "avatar_url": avatar_url,
            "images": images,
            "tags": tags,
            "view": _first(illust, "viewCount"),
            "bookmark": _first(illust, "bookmarkCount"),
            "like": _first(illust, "likeCount"),
            "comment": _first(illust, "commentCount"),
            "use_app": False,
        }

    async def _fetch_illust_app(
        self,
        client: AsyncClient,
        illust_id: int,
        access_token: str,
    ) -> dict[str, Any]:
        """app API(需 refresh token, 支持 R18)."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": _PIXIV_APP_UA,
            "Accept-Language": "zh-cn",
        }
        resp = await client.get(
            f"{_PIXIV_APP_API}/v1/illust/detail",
            params={"illust_id": illust_id},
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
        illust = payload.get("illust")
        if not isinstance(illust, dict):
            raise ParseException("pixiv app 接口返回数据为空")

        illust_type = {"illust": 0, "manga": 1, "ugoira": 2}.get(str(_first(illust, "type") or "illust"), 0)
        user = illust.get("user") or {}
        image_urls = illust.get("image_urls") or {}
        large = _first(image_urls, "large") if isinstance(image_urls, dict) else None

        if illust_type == 2:
            # 静态帧回退(动图本体走 ugoira metadata 接口)
            images = [(large, None)] if large else []
        elif meta_pages := illust.get("meta_pages"):
            images = []
            for page in meta_pages:
                if not isinstance(page, dict):
                    continue
                iu = page.get("image_urls") or {}
                if isinstance(iu, dict):
                    images.append((_first(iu, "original"), _first(iu, "large")))
        elif isinstance(illust.get("meta_single_page"), dict):
            images = [(_first(illust["meta_single_page"], "original_image_url"), large)]
        elif large:
            images = [(large, None)]
        else:
            images = []

        avatar_url = None
        if isinstance(user.get("profile_image_urls"), dict):
            avatar_url = _first(user["profile_image_urls"], "medium")

        tags = [
            t.get("name")
            for t in (illust.get("tags") or [])
            if isinstance(t, dict) and t.get("name")
        ]

        return {
            "id": illust_id,
            "title": _first(illust, "title") or f"illust_{illust_id}",
            "caption": _strip_html(_first(illust, "caption")),
            "create_date": _first(illust, "create_date"),
            "illust_type": illust_type,
            "x_restrict": int(_first(illust, "x_restrict", "xRestrict") or 0),
            "page_count": int(_first(illust, "page_count") or 1),
            "user_name": _first(user, "name") or "未知作者",
            "user_id": str(_first(user, "id") or illust_id),
            "avatar_url": avatar_url,
            "images": images,
            "tags": tags,
            "view": _first(illust, "total_view"),
            "bookmark": _first(illust, "total_bookmarks"),
            "like": None,
            "comment": _first(illust, "total_comments"),
            "use_app": True,
        }

    async def _fetch_user_profile(
        self,
        client: AsyncClient,
        user_id: str,
    ) -> dict[str, Any] | None:
        """网页版 ajax/user 接口获取用户资料(主要拿头像), 失败返回 None."""
        try:
            resp = await client.get(f"https://www.pixiv.net/ajax/user/{user_id}?full=1")
            if resp.status_code != 200:
                return None
            body = resp.json().get("body")
            return body if isinstance(body, dict) else None
        except Exception:
            logger.warning(f"获取 pixiv 用户 {user_id} 资料失败")
            return None

    # ------------------------------------------------------------------ #
    # 图片下载 / ugoira 合成
    # ------------------------------------------------------------------ #

    def _build_image_contents(
        self,
        images: list[tuple[str | None, str | None]],
    ) -> list[ImageContent]:
        """构建图片内容: 优先原图, 失败时回退到 fallback_url."""
        contents: list[ImageContent] = []
        for original, fallback in images:
            if original is None and fallback is None:
                continue
            if original and fallback:
                task = asyncio.create_task(self._download_img_fallback(original, fallback))
            else:
                task = asyncio.create_task(self._download_img_fallback(original or fallback, None))
            contents.append(self.create_image(task))
        return contents

    async def _download_img_fallback(
        self,
        url: str,
        fallback_url: str | None,
    ) -> Path:
        """下载图片; 原图下载失败时回退到 fallback_url."""
        try:
            return await self.downloader._download_file(url, ext_headers=self.headers)
        except ParseException:
            if fallback_url:
                logger.warning(f"pixiv 原图下载失败, 回退到备用源: {url}")
                return await self.downloader._download_file(fallback_url, ext_headers=self.headers)
            raise

    async def _build_ugoira_gif_content(
        self,
        client: AsyncClient,
        illust_id: int,
        info: dict[str, Any],
    ) -> list[ImageContent]:
        """下载 ugoira zip 并合成 GIF; 失败回退到静态预览帧."""
        if info.get("use_app"):
            if token := await self._get_access_token():
                zip_url, frames = await self._get_ugoira_source_app(client, illust_id, token)
            else:
                zip_url, frames = None, []
        else:
            zip_url, frames = await self._get_ugoira_source_ajax(client, illust_id)

        if zip_url and frames:
            task = asyncio.create_task(self._download_ugoira_gif(zip_url, frames, f"{illust_id}.gif"))
            return [self.create_image(task)]

        # 回退: 静态预览帧
        if info.get("images"):
            return self._build_image_contents(info["images"])
        raise ParseException("未找到可下载的图片")

    async def _get_ugoira_source_ajax(
        self,
        client: AsyncClient,
        illust_id: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        try:
            resp = await client.get(f"https://www.pixiv.net/ajax/illust/{illust_id}/ugoira_meta")
            if resp.status_code != 200:
                return None, []
            body = resp.json().get("body") or {}
            if not isinstance(body, dict):
                return None, []
            return body.get("src") or body.get("originalSrc"), body.get("frames") or []
        except Exception:
            return None, []

    async def _get_ugoira_source_app(
        self,
        client: AsyncClient,
        illust_id: int,
        access_token: str,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        try:
            resp = await client.get(
                f"{_PIXIV_APP_API}/v1/ugoira/metadata",
                params={"illust_id": illust_id},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": _PIXIV_APP_UA,
                    "Accept-Language": "zh-cn",
                },
            )
            if resp.status_code != 200:
                return None, []
            body = resp.json().get("ugoira_metadata") or {}
            if not isinstance(body, dict):
                return None, []
            zip_urls = body.get("zip_urls") or {}
            zip_url = zip_urls.get("medium") or zip_urls.get("large")
            return zip_url, body.get("frames") or []
        except Exception:
            return None, []

    async def _download_ugoira_gif(
        self,
        zip_url: str,
        frames: list[dict[str, Any]],
        gif_name: str,
    ) -> Path:
        """下载 ugoira zip, 解压帧并按延迟合成 GIF."""
        import zipfile
        from io import BytesIO

        from PIL import Image

        gif_path = pconfig.cache_dir / gif_name
        if gif_path.exists():
            return gif_path

        zip_path = await self.downloader._download_file(zip_url, ext_headers=self.headers)

        sorted_frames = sorted(frames, key=lambda f: str(f.get("file") or ""))
        rgb_frames: list[Image.Image] = []
        durations: list[int] = []
        with zipfile.ZipFile(zip_path) as zf:
            for fr in sorted_frames:
                name = fr.get("file")
                if not name:
                    continue
                try:
                    with Image.open(BytesIO(zf.read(name))) as im:
                        rgb_frames.append(im.convert("RGB"))
                    durations.append(max(int(fr.get("delay") or 40), 20))
                except Exception:
                    continue

        if not rgb_frames:
            raise ParseException("动图帧解析失败")

        # 统一量化到同一调色板, 避免帧间颜色闪烁
        palette = rgb_frames[0].quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        gif_frames = [palette] + [f.quantize(colors=256, palette=palette) for f in rgb_frames[1:]]

        gif_frames[0].save(
            gif_path,
            format="GIF",
            save_all=True,
            append_images=gif_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
        return gif_path

    @staticmethod
    def _page_url(base: str, index: int) -> str:
        """构造多页作品第 index 页的图片地址 (pixiv 按 _p{index} 编号)."""
        if index == 0:
            return base
        return re.sub(r"_p\d+", f"_p{index}", base, count=1)
