"""QQ空间 (qzone.qq.com) 动态解析器.

支持的分享形态:
- JSON 分享卡: bizsrc 以 "qzone." 开头 (说说/图文/实况图等, app 多为 com.tencent.miniapp.lua),
  按 bizsrc 字段精确认领, 其它平台/小程序的 JSON 卡片不会进入本解析器
- H5 分享直链: https://h5.qzone.qq.com/ugc/share/?sharetag=...
- 短链: https://mobile.qzone.qq.com/l?... (按参数重组拼成 H5 分享链)
- schema: mqqapi://qzoneschema (经 URL 的 schema= 参数或 base64 解码出 oldlink;
  裸 mqzone:// 是 QQ 内部跳转协议, 群相册等系统卡也携带它, 不作为匹配入口)

解析分层 (没有公开接口, 内容全部从 H5 分享页硬挖):
1. 卡片入口: 容错提取 JSON payload, 过滤群相册系统卡, 归一 meta 字段, 提取跳转 URL
2. URL 归一: 各种短链/schema 统一拼成 h5.qzone.qq.com/ugc/share 分享链
3. H5 页面抓取: feed-bd 结构提取作者/正文/转发, 内嵌 JSON (cell_pic/cell_video) 提取媒体,
   全文正则与 HTML 标签兜底
4. 媒体处理: URL 指纹去重 / GIF 探测 / 画质升级 / 实况图图片视频成对 (merge_videos)
"""

import asyncio
import base64
import html
import json
import re
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlparse, urlencode

from bs4 import BeautifulSoup, NavigableString, Tag
from httpx import AsyncClient

from ..constants import PlatformEnum
from .base import BaseParser, Platform, ParseException, handle


class QQZoneParser(BaseParser):
    """QQ空间动态解析器."""

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.QZONE, display_name="QQ空间")

    def __init__(self):
        super().__init__()
        self._gif_cache: dict[str, bool] = {}
        """GIF 探测结果缓存, 按 URL 跨解析复用 (同一图片的 GIF 属性不会变)"""

    # ------------------------------------------------------------------ #
    # 匹配入口
    # ------------------------------------------------------------------ #

    @handle('"bizsrc":"qzone', r'"bizsrc"\s*:\s*"qzone\.')
    @handle(
        "com.tencent.tuwen.lua",
        # ark 卡字段顺序固定 app -> bizsrc, 用前瞻要求 tuwen 卡必须带 qzone. bizsrc
        # (群相册系统卡等借用 tuwen/feed.lua 外壳的非 qzone 卡不认领)
        r'(?s)"app"\s*:\s*"com\.tencent\.tuwen\.lua"(?=.*"bizsrc"\s*:\s*"qzone\.)',
    )
    async def _parse_card(self, searched: re.Match[str]):
        """解析 JSON 分享卡 (说说/图文等, bizsrc 以 qzone. 开头)."""
        raw = searched.string
        raw_stripped = raw.strip()
        if "data=" in raw_stripped or "{" in raw_stripped or "json:" in raw_stripped or "mirai:app:" in raw_stripped:
            payload = self._extract_json_payload(raw)

            if not payload:
                raise ParseException("无法解析 QQ 空间 JSON 卡片")

            data = payload
            if isinstance(payload.get("data"), str):
                try:
                    data = json.loads(payload["data"])
                except json.JSONDecodeError as e:
                    raise ParseException(f"解析 QQ 空间内部 data 失败: {e}")

            bizsrc = str(data.get("bizsrc", ""))
            prompt = str(data.get("prompt", ""))

            # 群相册互动、群动态评论等系统通知卡片, 静默取消解析
            if "groupalbum" in bizsrc or "群相册" in prompt or "群动态" in prompt:
                raise asyncio.CancelledError("静默取消群相册解析")

            meta = data.get("meta", {}) or {}
            video_meta = meta.get("video") or {}
            miniapp_meta = meta.get("miniapp") or {}
            # 纯文本说说卡 (bizsrc=qzone.shuoshuoshareonlytext) 的数据在 meta.news
            news_meta = meta.get("news") or {}

            title = data.get("title") or video_meta.get("title")
            desc = data.get("desc") or news_meta.get("desc")
            source = data.get("source")
            tag = data.get("tag") or video_meta.get("tag")
            preview = data.get("preview") or video_meta.get("preview")
            legacy_url = data.get("legacyUrl")
            source_url = data.get("sourceUrl")
            jump_url = (
                miniapp_meta.get("jumpUrl")
                or news_meta.get("jumpUrl")
                or video_meta.get("jumpURL")
                or video_meta.get("jumpUrl")
            )
            nickname = video_meta.get("nickname")
            avatar = video_meta.get("avatar")

            text_segments = []
            if title:
                text_segments.append(title)
            if desc:
                text_segments.append(desc)
            if source:
                text_segments.append(source)
            if tag:
                text_segments.append(tag)

            text = "\n".join(text_segments) if text_segments else None
            url = None
            for candidate in (jump_url, legacy_url, source_url):
                if not candidate:
                    continue
                extracted = self._extract_qzone_url(candidate)
                if extracted:
                    url = extracted
                    break

            result = self.result(
                title=title,
                text=text,
                url=url,
                extra={"bizsrc": data.get("bizsrc", ""), "app": data.get("app", ""), "type": data.get("type", "")},
            )

            if nickname:
                result.author = self.create_author(nickname, avatar)

            if preview:
                result.contents.append(self.create_image(preview, alt=title or tag))

            if url and ("h5.qzone.qq.com/ugc/share/" in url or "universal-share" in url):
                content = await self._fetch_qzone_h5_content(url)
                if content:
                    result = self._merge_h5_content(result, content)

            return result

        # 直接传入的 qzone 深度链接或 schema 链接, 走链接解析兜底
        return await self._parse_qzone_link(raw)

    @handle("h5.qzone", r"h5\.qzone\.qq\.com/ugc/share")
    @handle("mobile.qzone", r"mobile\.qzone\.qq\.com/l")
    @handle("qzoneschema", r"mqqapi://qzoneschema")
    async def _parse_link(self, searched: re.Match[str]):
        """解析纯链接 / schema 分享 (非 JSON 卡片形态)."""
        return await self._parse_qzone_link(searched.string)

    async def _parse_qzone_link(self, raw: str):
        url = self._extract_qzone_url(raw)
        result = self.result(
            title="QQ空间分享",
            text=None,
            url=url,
            extra={"fallback": True, "orig_raw": raw},
        )

        if url and "h5.qzone.qq.com/ugc/share/" in url:
            content = await self._fetch_qzone_h5_content(url)
            if content:
                result = self._merge_h5_content(result, content)

        return result

    # ------------------------------------------------------------------ #
    # JSON 卡片解析
    # ------------------------------------------------------------------ #

    def _extract_json_payload(self, raw: str) -> dict[str, Any]:
        raw = html.unescape(raw)

        start = raw.find("data=")
        if start != -1:
            start = raw.find("{", start)
            if start == -1:
                raise ParseException("未找到 QQ 空间 JSON 卡片起始标记")
            end = self._find_matching_brace(raw, start)
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError as e:
                raise ParseException(f"解析 QQ 空间 JSON 失败: {e}")

        item = self._find_json_object(raw)
        if item is not None:
            try:
                return json.loads(item)
            except json.JSONDecodeError as e:
                raise ParseException(f"解析 QQ 空间 JSON 失败: {e}")

        raise ParseException("未找到 QQ 空间 JSON 卡片数据")

    def _find_json_object(self, raw: str) -> str | None:
        raw = raw.strip()
        prefixes = ["[CQ:json,data=", "CQ:json,data=", "[json:data=", "json:data=", "json=data=", "json:", "[json:", "{"]
        for prefix in prefixes:
            pos = raw.find(prefix)
            if pos == -1:
                continue
            start = raw.find("{", pos)
            if start == -1:
                continue
            try:
                end = self._find_matching_brace(raw, start)
                return raw[start:end]
            except ParseException:
                continue

        idx = raw.find("{")
        while idx != -1:
            try:
                end = self._find_matching_brace(raw, idx)
                candidate = raw[idx:end]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass
            except ParseException:
                pass
            idx = raw.find("{", idx + 1)

        return None

    @staticmethod
    def _find_matching_brace(text: str, start: int) -> int:
        stack = 0
        in_string = False
        escape = False
        for index, char in enumerate(text[start:], start=start):
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                stack += 1
            elif char == "}":
                stack -= 1
                if stack == 0:
                    return index + 1
        raise ParseException("QQ 空间 JSON 卡片未找到匹配的结束括号")

    # ------------------------------------------------------------------ #
    # URL 归一: 各种短链/schema 统一成 h5.qzone.qq.com/ugc/share 分享链
    # ------------------------------------------------------------------ #

    def _extract_qzone_url(self, raw: str) -> str:
        raw = html.unescape(raw).strip()
        if "h5.qzone.qq.com/ugc/share/" in raw or "universal-share" in raw:
            return raw

        if "schema=" not in raw:
            if "mobile.qzone.qq.com/l" in raw:
                parsed = urlparse(raw)
                qs = parse_qs(parsed.query)
                if qs.get("sharetag") or qs.get("ciphertext"):
                    res_uin = qs.get("u", [""])[0] or qs.get("uw", [""])[0]
                    return self._build_h5_share_url(
                        sharetag=qs.get("sharetag", [""])[0] or qs.get("ciphertext", [""])[0],
                        loginfrom=qs.get("loginfrom", [""])[0],
                        jumptoqzone=qs.get("jumptoqzone", [""])[0],
                        feed_action_domain_type=qs.get("feed_action_domain_type", [""])[0],
                        banner_type=qs.get("banner_type", [""])[0],
                        ciphertext=qs.get("ciphertext", [""])[0],
                        g=qs.get("sg", [""])[0] or qs.get("g", [""])[0],
                        res_uin=res_uin,
                        cellid=qs.get("i", [""])[0] or qs.get("id", [""])[0] or qs.get("cellid", [""])[0],
                        bp2=qs.get("bp2", [""])[0],
                        appid=qs.get("a", [""])[0] or qs.get("appid", [""])[0],
                        subtype=qs.get("subtype", [""])[0],
                        blog_photo=qs.get("blog_photo", [""])[0],
                        g_f=qs.get("g_f", ["2000000103"])[0],
                        _wv=qs.get("_wv", [""])[0],
                    )
            return raw

        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        schema_values = qs.get("schema")
        if not schema_values:
            return raw

        schema = unquote(schema_values[0])
        if schema.startswith("http://") or schema.startswith("https://") or schema.startswith("mqqapi://"):
            return schema

        try:
            schema_bytes = base64.b64decode(schema + "=" * (-len(schema) % 4), validate=False)
            decoded = schema_bytes.decode("utf-8", errors="ignore")
            if decoded.startswith("mqzone://"):
                built = self._build_h5_share_url_from_mqzone(decoded)
                if built:
                    return built
            return decoded or raw
        except Exception:
            return raw

    def _build_h5_share_url(
        self,
        sharetag: str,
        loginfrom: str,
        jumptoqzone: str,
        feed_action_domain_type: str,
        banner_type: str,
        ciphertext: str,
        g: str,
        res_uin: str,
        cellid: str,
        bp2: str,
        appid: str,
        subtype: str = "",
        blog_photo: str = "",
        g_f: str = "2000000103",
        _wv: str = "",
    ) -> str:
        params = {
            "sharetag": sharetag,
            "loginfrom": loginfrom,
            "jumptoqzone": jumptoqzone,
            "feed_action_domain_type": feed_action_domain_type,
            "banner_type": banner_type.lstrip("0") or "0",
            "subtype": subtype,
            "ciphertext": ciphertext or sharetag,
            "blog_photo": blog_photo,
            "g": g,
            "res_uin": res_uin,
            "cellid": cellid,
            "subid": "",
            "bp1": "",
            "bp2": bp2,
            "bp7": "",
            "appid": appid,
            "g_f": g_f,
            "_wv": _wv,
        }
        return "https://h5.qzone.qq.com/ugc/share/?" + urlencode(params)

    def _build_h5_share_url_from_mqzone(self, mqzone_url: str) -> str | None:
        parsed = urlparse(mqzone_url)
        qs = parse_qs(parsed.query)
        oldlink = qs.get("oldlink", [""])[0]
        if not oldlink:
            return None

        old_q = parse_qs(oldlink)
        sharetag = old_q.get("sharetag", [""])[0] or qs.get("ciphertext", [""])[0]
        loginfrom = old_q.get("loginfrom", [""])[0]
        jumptoqzone = old_q.get("jumptoqzone", [""])[0]
        res_uin = qs.get("uin", [""])[0] or old_q.get("u", [""])[0]
        appid = qs.get("appid", [""])[0] or old_q.get("appid", [""])[0]
        if not (sharetag and loginfrom and jumptoqzone and res_uin and appid):
            return None

        return self._build_h5_share_url(
            sharetag=sharetag,
            loginfrom=loginfrom,
            jumptoqzone=jumptoqzone,
            feed_action_domain_type=qs.get("feed_action_domain_type", [""])[0] or old_q.get("feed_action_domain_type", [""])[0],
            banner_type=qs.get("banner_type", [""])[0] or old_q.get("banner_type", [""])[0],
            ciphertext=qs.get("ciphertext", [""])[0] or sharetag,
            g=old_q.get("g", [""])[0],
            res_uin=res_uin,
            cellid=qs.get("cellid", [""])[0] or old_q.get("id", [""])[0],
            bp2=old_q.get("bp2", [""])[0] or qs.get("bp2", [""])[0],
            appid=appid,
        )

    # ------------------------------------------------------------------ #
    # H5 页面抓取
    # ------------------------------------------------------------------ #

    async def _fetch_qzone_h5_content(self, url: str) -> dict[str, Any] | None:
        headers = {
            **self.headers,
            "referer": "https://h5.qzone.qq.com/",
        }
        async with AsyncClient(headers=headers, timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()

        # 页面内所有 GIF 探测复用同一客户端 (探测并发执行, 结果另有实例级缓存)
        gif_client = AsyncClient(headers=self.headers, verify=False, follow_redirects=True, timeout=3.0)
        try:
            text = html.unescape(response.text)
            # 帖子真实发布时间: 内嵌 feed JSON 的 "time" 字段 (秒级时间戳, 与页面展示时间一致);
            # 卡片里的 config.ctime 是分享卡生成时间, 语义错误, 不再使用
            m_time = re.search(r'"time"\s*:\s*(\d{9,13})', text)
            timestamp = int(m_time.group(1)) if m_time else None

            soup = BeautifulSoup(text, "html.parser")
            feed = soup.find("div", class_="feed-bd")
            if feed is None:
                # 无 feed-bd 结构时, 仅从内嵌 JSON / 全文正则提取媒体
                media = await self._extract_qzone_cell_pic_media(text, gif_client)
                main_images = []
                seen_hashes_local = set()
                for img_url, is_gif in media["images"]:
                    img_hash = self._get_qzone_hash(img_url)
                    if img_hash in seen_hashes_local:
                        continue
                    seen_hashes_local.add(img_hash)
                    main_images.append(self.create_gif(img_url) if is_gif else self.create_image(img_url))

                return {
                    "main_text": None,
                    "owner": None,
                    "main_images": main_images,
                    "videos": [self.create_video(v) for v in media["videos"]],
                    "repost_text": None,
                    "repost_images": [],
                    "repost_videos": [],
                    "repost_author": None,
                    "owner_avatar": None,
                    "timestamp": timestamp,
                }

            owner, owner_avatar = self._extract_h5_owner(soup, feed)
            main_texts, repost_texts = self._extract_h5_texts(feed)
            media = await self._extract_h5_media(soup, feed, text, gif_client)

            repost_author = None
            source_block = feed.find("blockquote", class_="source")
            if source_block:
                a = source_block.find("a", class_="username")
                if a and a.get_text(strip=True):
                    repost_author = a.get_text(strip=True)

            return {
                "main_text": "\n".join(main_texts).strip() or None,
                "owner": owner,
                "main_images": media["main_images"],
                "videos": media["videos"],
                "repost_text": "\n".join(repost_texts).strip() or None,
                "repost_images": media["repost_images"],
                "repost_videos": media["repost_videos"],
                "repost_author": repost_author,
                "owner_avatar": owner_avatar,
                "has_live_video": media["has_live_video"],
                "timestamp": timestamp,
            }
        finally:
            await gif_client.aclose()

    def _extract_h5_owner(self, soup: BeautifulSoup, feed: Tag) -> tuple[str | None, str | None]:
        """提取作者昵称与头像 URL, 返回 (owner, owner_avatar)."""
        owner = None
        owner_span = None
        title_p = feed.find("p", class_="title") or soup.find("p", class_="title")
        if title_p:
            owner_span = title_p.find("span", class_="username")
            if owner_span and owner_span.get_text(strip=True):
                owner = owner_span.get_text(strip=True)
        if not owner:
            owner_span = soup.find("span", class_="username") or soup.find(attrs={"data-hook": "global-profile"})
            if owner_span and owner_span.get_text(strip=True):
                owner = owner_span.get_text(strip=True)

        owner_avatar = None
        avatar_div = None
        if owner_span is not None:
            if owner_span.parent is not None:
                avatar_div = owner_span.parent.find("div", class_="avatar")
                if not avatar_div:
                    avatar_div = owner_span.parent.find_previous_sibling("div", class_="avatar")
        if not avatar_div:
            avatar_div = soup.find("div", class_="avatar")
        if not avatar_div:
            avatar_div = soup.find(attrs={"data-hook": "global-profile"})

        if avatar_div:
            pic = avatar_div.find("b", class_="pic") or avatar_div.find("img")
            if pic:
                if pic.name == "b":
                    s = pic.get("style", "")
                    m = re.search(r'background-image\s*:\s*url\((?:"|\')?(.*?)(?:"|\')?\)', s)
                    if m:
                        owner_avatar = self._normalize_qzone_image_url(html.unescape(m.group(1)))
                else:
                    src = pic.get("src") or pic.get("data-src") or pic.get("data-feedlazy")
                    if src:
                        owner_avatar = self._normalize_qzone_image_url(html.unescape(src))

        return owner, owner_avatar

    def _extract_h5_texts(self, feed: Tag) -> tuple[list[str], list[str]]:
        """提取正文与转发文本, 返回 (main_texts, repost_texts)."""
        main_texts: list[str] = []
        repost_texts: list[str] = []
        for p in feed.find_all("p", class_="txt"):
            parent_block = p.find_parent("blockquote")
            txt = self._extract_qzone_text(p).replace("\xa0", " ").strip()
            if not txt:
                continue
            if parent_block and "source" in parent_block.get("class", []):
                repost_texts.append(txt)
            else:
                main_texts.append(txt)
        return main_texts, repost_texts

    async def _extract_h5_media(self, soup: BeautifulSoup, feed: Tag, text: str, gif_client: AsyncClient | None = None) -> dict[str, Any]:
        """提取页面媒体 (图片/视频/实况图), 含转发区归属判定与全局去重."""
        seen_hashes: set[str] = set()
        seen_videos: set[str] = set()

        main_images = []
        repost_images = []
        videos = []
        repost_videos = []

        def extract_raw_src(elem: Tag) -> str | None:
            if getattr(elem, "name", None) == "img":
                s = elem.get("src") or elem.get("data-src") or elem.get("data-feedlazy") or elem.get("data-lazy")
                return html.unescape(s.strip()) if s else None
            s = elem.get("data-feedlazy") or elem.get("data-src") or elem.get("data-lazy") or elem.get("src")
            if s:
                return html.unescape(s.strip())
            style = html.unescape(elem.get("style", ""))
            m = re.search(r'background-image\s*:\s*url\((?:"|\')?(.*?)(?:"|\')?\)', style)
            return m.group(1).strip() if m else None

        # 先收集转发区内已有的媒体指纹, 用于判定图片/视频是否属于转发
        repost_hashes: set[str] = set()
        source_block = feed.find("blockquote", class_="source")

        # 只要存在 blockquote 或任意转发容器, 则强制为转发模式
        is_repost_post = source_block is not None
        if not is_repost_post and feed.find(class_=re.compile(r"feed-repost|repost-bd|qz-repost")):
            is_repost_post = True

        if source_block:
            for tag in source_block.find_all(True):
                src = extract_raw_src(tag)
                if src:
                    repost_hashes.add(self._get_qzone_hash(src))
                if tag.name == "button":
                    play = tag.get("data-playvideo") or tag.get("data-video") or tag.get("data-src")
                    if play:
                        repost_hashes.add(self._get_qzone_hash(html.unescape(play).strip()))
                elif tag.name == "video":
                    play = tag.get("src")
                    if play:
                        repost_hashes.add(self._get_qzone_hash(html.unescape(play).strip()))

        for key in ['"cell_summary"', '"cell_forward"', '"cell_repost"']:
            for match in re.finditer(rf"{key}\s*:\s*\{{", text):
                start = text.find("{", match.end() - 1)
                if start != -1:
                    try:
                        end = self._find_matching_brace(text, start)
                        summary_text = text[start:end].replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
                        summary_urls = re.findall(
                            r"(?:https?:)?//(?:[\w-]+\.)*(?:qpic\.cn|photo\.store\.qq\.com|photo\.qq\.com)/[^\s\"'<>\\,}]+",
                            summary_text,
                        )
                        for u in summary_urls:
                            u_clean = self._normalize_qzone_image_url(html.unescape(u).strip())
                            repost_hashes.add(self._get_qzone_hash(u_clean))
                    except Exception:
                        pass

        # 内嵌 JSON 媒体: 优先 cell_pic/cell_video, 空则用全文正则兜底
        media = await self._extract_qzone_cell_pic_media(text, gif_client)
        regex_media = await self._extract_qzone_cell_pic_media_from_text(text)
        live_photos = media.get("live_photos", [])
        if not media["images"] and not live_photos:
            media["images"].extend(regex_media["images"])
        if not media["videos"] and not live_photos:
            media["videos"].extend(regex_media["videos"])
            media["covers"].extend(regex_media.get("covers", []))

        # 双重保险: 极个别情况下通过 hash 补判转发
        is_repost_post = is_repost_post or len(repost_hashes) > 0

        # 封面绑定到视频: 封面不再作为独立图片, 避免视频动态被渲染成图片网格
        cover_urls = [c for c in media.get("covers", []) if c]
        cover_hashes = {self._get_qzone_hash(c) for c in cover_urls}
        cover_by_index = cover_urls if len(media["videos"]) == len(cover_urls) else []

        if media["images"]:
            for image_url, is_gif in media["images"]:
                if image_url:
                    img_hash = self._get_qzone_hash(image_url)
                    if img_hash in seen_hashes or img_hash in cover_hashes:
                        continue
                    seen_hashes.add(img_hash)

                    img_obj = self.create_gif(image_url) if is_gif else self.create_image(image_url)

                    if is_repost_post or img_hash in repost_hashes:
                        repost_images.append(img_obj)
                    else:
                        main_images.append(img_obj)

        for cover_url in cover_urls:
            seen_hashes.add(self._get_qzone_hash(cover_url))

        for i, video_url in enumerate(media["videos"]):
            if video_url:
                v_hash = self._get_qzone_hash(video_url)
                if v_hash in seen_videos:
                    continue
                seen_videos.add(v_hash)

                cover = cover_by_index[i] if i < len(cover_by_index) else None
                vid_obj = self.create_video(video_url, cover_url=cover)
                if is_repost_post or v_hash in repost_hashes:
                    repost_videos.append(vid_obj)
                else:
                    videos.append(vid_obj)

        # 实况图: 图片与短视频成对, 视频走合并转发 (与抖音一致)
        has_live_video = False
        live_llocs: set[str] = set()
        for lp in live_photos:
            image_url = lp.get("image")
            video_url = lp.get("video")
            if not (image_url and video_url):
                continue
            if lloc := (lp.get("lloc") or ""):
                live_llocs.add(lloc)
            has_live_video = True

            img_hash = self._get_qzone_hash(image_url)
            v_hash = self._get_qzone_hash(video_url)

            if is_repost_post:
                if img_hash not in seen_hashes:
                    seen_hashes.add(img_hash)
                    repost_images.append(self.create_image(image_url))
                if v_hash not in seen_videos:
                    seen_videos.add(v_hash)
                    repost_videos.append(self.create_video(video_url, cover_url=image_url))
            else:
                if img_hash not in seen_hashes:
                    seen_hashes.add(img_hash)
                    main_images.append(self.create_image(image_url))
                if v_hash not in seen_videos:
                    seen_videos.add(v_hash)
                    videos.append(self.create_video(video_url, cover_url=image_url))

        # HTML 视频兜底
        for btn in feed.select('button[data-hook="global-video"], button.btn.video'):
            play = btn.get("data-playvideo") or btn.get("data-video") or btn.get("data-src")
            if not play:
                sibling_video = btn.find_next("video")
                if sibling_video:
                    play = sibling_video.get("src")
            if not play:
                continue

            play = html.unescape(play).strip()
            v_hash = self._get_qzone_hash(play)

            cover = None
            style = btn.get("style", "")
            m = re.search(r'background-image\s*:\s*url\((?:"|\')?(.*?)(?:"|\')?\)', style)
            if m:
                cover = self._normalize_qzone_image_url(m.group(1))

            if not cover:
                poster_div = feed.find(id=re.compile(r"vpjs-videoPoster-|vpjs-playerContainer-"))
                if poster_div:
                    s = poster_div.get("style", "")
                    m2 = re.search(r'background:\s*url\((?:"|\')?(.*?)(?:"|\')?\)', s)
                    if m2:
                        cover = self._normalize_qzone_image_url(m2.group(1))

            if cover:
                seen_hashes.add(self._get_qzone_hash(cover))

            if v_hash in seen_videos:
                continue
            seen_videos.add(v_hash)

            # HTML 视频提取同样服从全局转发标记
            target_list = repost_videos if (is_repost_post or btn.find_parent("blockquote", class_="source")) else videos
            target_list.append(self.create_video(play, cover_url=cover))

        if not videos:
            for vid in feed.find_all("video"):
                src = vid.get("src") or vid.get("data-src") or vid.get("data-feedlazy")
                if not src:
                    continue

                src = html.unescape(src).strip()
                v_hash = self._get_qzone_hash(src)

                cover = vid.get("poster")
                if not cover:
                    parent = vid.find_parent()
                    if parent:
                        for node in parent.find_all(True):
                            st = node.get("style", "")
                            m = re.search(r'background(?:-image)?:\s*url\((?:"|\')?(.*?)(?:"|\')?\)', st)
                            if m:
                                cover = m.group(1)
                                break

                if cover:
                    seen_hashes.add(self._get_qzone_hash(cover))

                if v_hash in seen_videos:
                    continue
                seen_videos.add(v_hash)

                # 兜底 HTML 视频同样服从全局转发标记
                target_list = repost_videos if (is_repost_post or vid.find_parent("blockquote", class_="source")) else videos
                target_list.append(self.create_video(src, cover_url=cover))

        # HTML 图片兜底
        def _is_detail_view_pic(elem: Tag) -> bool:
            if getattr(elem, "name", None) is None:
                return False
            for node in [elem] + list(elem.parents):
                if not isinstance(node, Tag):
                    continue
                classes = node.get("class", [])
                if isinstance(classes, str):
                    classes = classes.split()
                if any(c in classes for c in ["detail-viewPic", "detail_viewPic", "slide-view", "comment-imgs", "J_imgCmt"]):
                    return True
                node_id = node.get("id", "")
                if node_id in ("detail-viewPic", "detail_viewPic", "slideView"):
                    return True
                # 评论区图片 figure 用 data-hook="detail-viewPic" 标记 (非 class/id)
                if node.get("data-hook", "") in ("detail-viewPic", "detail_viewPic"):
                    return True
            return False

        def _is_inline_emoji(elem: Tag) -> bool:
            if elem.name != "img":
                return False
            classes = elem.get("class", [])
            if isinstance(classes, str):
                classes = classes.split()
            src = elem.get("src", "") or elem.get("data-src", "") or elem.get("data-feedlazy", "")
            return "mini-em" in classes or "emoji" in classes or "qzone/em/" in src or elem.get("alt", "") == "表情"

        def _is_repost_element(elem: Tag) -> bool:
            for p in elem.parents:
                if not isinstance(p, Tag):
                    continue
                if p.name == "blockquote" and "source" in p.get("class", []):
                    return True
                classes = p.get("class", [])
                if isinstance(classes, list):
                    if any(c in ["feed-repost", "repost-bd", "qz-repost"] for c in classes):
                        return True
                elif isinstance(classes, str):
                    if any(c in classes for c in ["feed-repost", "repost-bd", "qz-repost"]):
                        return True
            return False

        # 第一遍: 静态筛选候选图片 (跳过评论区图/表情/重复), 收集地址待批量探测
        def collect_image_candidate(elem: Tag, target_list: list):
            if _is_detail_view_pic(elem):
                return
            if getattr(elem, "name", None) == "img" and _is_inline_emoji(elem):
                return

            src = extract_raw_src(elem)
            if not src:
                return

            # 实况图缩略图(wecam_pic)与 JSON 中的高清图是同一张, 跳过避免重复
            if live_llocs and any(lloc in src for lloc in live_llocs):
                return

            src = self._fix_qzone_domain(src)
            img_hash = self._get_qzone_hash(src)

            if img_hash in seen_hashes:
                return
            seen_hashes.add(img_hash)

            candidates.append((src, self._upgrade_qzone_quality(src), target_list))

        candidates: list[tuple[str, str, list]] = []
        for img in feed.find_all("img"):
            # 转发帖: 无视图片在 HTML 中是否散在转发框外, 强行纳入转发区
            collect_image_candidate(img, repost_images if (is_repost_post or _is_repost_element(img)) else main_images)

        for span in feed.find_all("span"):
            if not (span.has_attr("style") or span.has_attr("data-feedlazy") or "img" in span.get("class", [])):
                continue
            collect_image_candidate(span, repost_images if (is_repost_post or _is_repost_element(span)) else main_images)

        # 第二遍: 并发探测候选图的 GIF 属性 (原图与画质升级地址一次性批量探测)
        probe_urls: list[str] = []
        for src, upgraded_url, _ in candidates:
            for u in (src, upgraded_url):
                if u not in probe_urls and u not in self._gif_cache:
                    probe_urls.append(u)
        if probe_urls:
            results = await asyncio.gather(
                *(self._check_qzone_image_is_gif(u, client=gif_client) for u in probe_urls),
                return_exceptions=True,
            )
            for u, r in zip(probe_urls, results):
                self._gif_cache[u] = False if isinstance(r, Exception) else bool(r)

        # 第三遍: 按原选择逻辑归入目标列表
        for src, upgraded_url, target_list in candidates:
            if self._gif_cache[upgraded_url]:
                target_list.append(self.create_gif(upgraded_url))
            elif self._gif_cache[src]:
                target_list.append(self.create_gif(src))
            else:
                target_list.append(self.create_image(upgraded_url))

        return {
            "main_images": main_images,
            "videos": videos,
            "repost_images": repost_images,
            "repost_videos": repost_videos,
            "has_live_video": has_live_video,
        }

    def _merge_h5_content(self, result, content):
        main_text = content.get("main_text")
        main_images = content.get("main_images") or []
        videos = content.get("videos") or []
        repost_text = content.get("repost_text")
        repost_images = content.get("repost_images") or []
        repost_videos = content.get("repost_videos") or []

        if not any([main_text, main_images, videos, repost_text, repost_images, repost_videos]):
            return result

        result.title = None
        owner = content.get("owner")
        owner_avatar = content.get("owner_avatar")
        if owner:
            result.author = self.create_author(owner, owner_avatar)

        # 帖子真实发布时间优先于卡片携带的任何时间
        if content.get("timestamp"):
            result.timestamp = content["timestamp"]

        result.text = main_text or None
        result.contents = []

        if main_images:
            result.contents.extend(main_images)
        if videos:
            result.contents.extend(videos)

        # 实况图: 视频与图片成对, 视频走合并转发 (与抖音一致)
        if content.get("has_live_video"):
            result.extra["merge_videos"] = True

        if repost_text or repost_images or repost_videos:
            repost_author = content.get("repost_author")
            repost = self.result(text=repost_text)
            if repost_author:
                repost.author = self.create_author(repost_author)
            if repost_images:
                repost.contents.extend(repost_images)
            if repost_videos:
                repost.contents.extend(repost_videos)
            result.repost = repost

        return result

    # ------------------------------------------------------------------ #
    # 媒体提取: 页面内嵌 JSON (cell_pic/cell_video) 与全文正则兜底
    # ------------------------------------------------------------------ #

    async def _extract_qzone_cell_pic_media(self, text: str, gif_client: AsyncClient | None = None) -> dict[str, list]:
        # 优先解析 cell_video (视频动态)
        if video_media := self._extract_qzone_cell_video_media(text):
            return video_media

        payload = self._extract_qzone_cell_pic_payload(text)
        if not payload:
            return await self._extract_qzone_cell_pic_media_from_text(text)

        images: list[tuple[str, bool]] = []
        videos: list[str] = []
        covers: list[str] = []
        live_photos: list[dict[str, str]] = []
        gif_cache: dict[str, bool] = {}
        seen_hashes: set[str] = set()

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return await self._extract_qzone_cell_pic_media_from_text(text)

        # 第一遍: 同步收集各条目的地址与归属, 实况图直接入列
        entries: list[tuple[bool, str | None, str | None]] = []
        for item in data.get("picdata", []):
            if not isinstance(item, dict):
                continue

            videoflag = item.get("videoflag", 0)
            is_video = videoflag == 1 or bool(item.get("videodata")) or bool(item.get("videourl"))

            # 提取图片地址
            image_url = None
            photourl = item.get("photourl")
            if isinstance(photourl, dict):
                image_url = self._qzone_first_media_url(photourl)
            if not image_url:
                for key in ("sloc", "lloc", "url", "picurl"):
                    candidate = item.get(key)
                    if isinstance(candidate, str) and candidate.startswith("http"):
                        image_url = candidate
                        break

            # 提取视频地址
            video_url = None
            if is_video:
                videodata = item.get("videodata")
                if isinstance(videodata, dict):
                    video_url = videodata.get("videourl")
                    if not video_url and isinstance(videodata.get("videourls"), dict):
                        v_urls = videodata["videourls"]
                        video_url = (
                            v_urls.get("1", {}).get("url")
                            or v_urls.get("0", {}).get("url")
                            or v_urls.get("5", {}).get("url")
                        )

                if not video_url:
                    videourl = item.get("videourl")
                    if isinstance(videourl, str) and videourl:
                        video_url = videourl
                    elif isinstance(videourl, dict):
                        video_url = videourl.get("url") or videourl.get("videoUrl")

            if is_video and video_url and image_url:
                # 实况图: 图片与短视频成对存在
                live_photos.append(
                    {
                        "image": self._normalize_qzone_image_url(image_url),
                        "video": self._normalize_qzone_image_url(video_url),
                        "lloc": str(item.get("lloc") or ""),
                    }
                )
                continue

            entries.append((is_video, image_url, video_url))

        # 第二遍: 并发探测普通图片的 GIF 属性 (结果经实例级缓存跨解析复用)
        gif_targets: list[str] = []
        for is_video, image_url, _ in entries:
            if is_video or not image_url:
                continue
            u = self._fix_qzone_domain(image_url)
            if u in gif_cache or u in gif_targets:
                continue
            if ".gif" not in u.lower() and "fmt=gif" not in u.lower():
                gif_targets.append(u)
        if gif_targets:
            results = await asyncio.gather(
                *(self._check_qzone_image_is_gif(u, client=gif_client) for u in gif_targets),
                return_exceptions=True,
            )
            for u, r in zip(gif_targets, results):
                gif_cache[u] = False if isinstance(r, Exception) else bool(r)

        # 第三遍: 按原逻辑构建结果 (去重与归并顺序和原实现一致)
        for is_video, image_url, video_url in entries:
            if not is_video:
                if image_url:
                    image_url = self._fix_qzone_domain(image_url)
                    img_hash = self._get_qzone_hash(image_url)
                    if img_hash in seen_hashes:
                        continue
                    seen_hashes.add(img_hash)

                    if image_url not in gif_cache:
                        # 并发探测未覆盖的地址 (.gif 后缀等), 按 URL 特征判定
                        gif_cache[image_url] = ".gif" in image_url.lower() or "fmt=gif" in image_url.lower()

                    is_gif = gif_cache[image_url]

                    if not is_gif:
                        image_url = self._upgrade_qzone_quality(image_url)

                    images.append((image_url, is_gif))

            if is_video:
                if video_url and isinstance(video_url, str):
                    video_url = self._normalize_qzone_image_url(video_url)
                    v_hash = self._get_qzone_hash(video_url)
                    if v_hash not in seen_hashes:
                        seen_hashes.add(v_hash)
                        videos.append(video_url)

                if image_url:
                    covers.append(self._fix_qzone_domain(image_url))

        if not images and not videos and not live_photos:
            return await self._extract_qzone_cell_pic_media_from_text(text)

        return {"images": images, "videos": videos, "covers": covers, "live_photos": live_photos}

    @staticmethod
    def _find_matching_bracket(text: str, start: int) -> int:
        """从 start 处 [ 开始找配对的 ] (跳过字符串内的括号), 未找到返回 -1."""
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return i + 1
        return -1

    @staticmethod
    def _strip_qzone_comment_media(text: str) -> str:
        """剔除页面中评论区的图片载体, 防止全文正则兜底误捞评论区图片.

        评论区图片有两个载体: 内嵌 JSON 的 "commentpic":[...] 数组, 以及 DOM 中
        data-hook="detail-viewPic" 的 <figure> 节点; 与主内容图共用图床域名,
        只能按载体剔除.
        """
        text = re.sub(r'<figure[^>]*detail-viewPic[^>]*>.*?</figure>', "", text, flags=re.S)

        out: list[str] = []
        pos = 0
        for m in re.finditer(r'"commentpic"\s*:\s*\[', text):
            end = QQZoneParser._find_matching_bracket(text, m.end() - 1)
            if end == -1:
                continue
            out.append(text[pos:m.start()])
            pos = end
        out.append(text[pos:])
        return "".join(out)

    async def _extract_qzone_cell_pic_media_from_text(self, text: str) -> dict[str, list]:
        images: list[tuple[str, bool]] = []
        videos: list[str] = []
        seen_urls: set[str] = set()

        clean_text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
        # 剔除评论区图片载体: 评论区图片与主内容图片共用图床域名, 无法按 URL 区分,
        # 不剔除的话纯文字说说的全文兜底会把评论图片当正文图捞出来
        clean_text = self._strip_qzone_comment_media(clean_text)

        video_urls = re.findall(r"(?:https?:)?//(?:[\w-]+\.)*photo\.qq\.com/[^\s\"'<>\\,}]+", clean_text)
        for v in video_urls:
            if v.startswith("//"):
                v = "https:" + v
            v_clean = html.unescape(v).strip()
            v_hash = self._get_qzone_hash(v_clean)
            if v_hash not in seen_urls:
                seen_urls.add(v_hash)
                videos.append(v_clean)

        img_urls = re.findall(
            r"(?:https?:)?//(?:[\w-]+\.)*(?:qpic\.cn|photo\.store\.qq\.com|photo\.qq\.com)/[^\s\"'<>\\,}]+",
            clean_text,
        )
        for u in img_urls:
            if u.startswith("//"):
                u = "https:" + u
            u_clean = self._normalize_qzone_image_url(html.unescape(u).strip())
            img_hash = self._get_qzone_hash(u_clean)

            if img_hash not in seen_urls:
                seen_urls.add(img_hash)
                is_gif = "gif" in u_clean.lower()
                images.append((u_clean, is_gif))

        return {"images": images, "videos": videos, "covers": []}

    def _extract_qzone_cell_video_media(self, text: str) -> dict[str, list] | None:
        """解析 cell_video 数据 (视频动态): 提取视频地址和封面"""
        payload = self._extract_qzone_cell_video_payload(text)
        if not payload:
            return None

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        video_url = data.get("videourl")
        if not isinstance(video_url, str) or not video_url:
            return None

        video_url = self._normalize_qzone_image_url(html.unescape(video_url).strip())

        cover_url = None
        cover = data.get("coverurl")
        if isinstance(cover, dict):
            cover_url = self._qzone_first_media_url(cover)
        if cover_url:
            cover_url = self._normalize_qzone_image_url(html.unescape(cover_url).strip())

        return {"images": [], "videos": [video_url], "covers": [cover_url] if cover_url else []}

    def _extract_qzone_cell_pic_payload(self, text: str) -> str | None:
        patterns = [
            r'"cell_pic"\s*:\s*\{',
            r"'cell_pic'\s*:\s*\{",
            r"cell_pic\s*:\s*\{",
            r'"?cell_pic"?\s*:\s*\{',
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                start = text.find("{", match.end() - 1)
                if start != -1:
                    try:
                        end = self._find_matching_brace(text, start)
                        return text[start:end]
                    except ParseException:
                        continue

        return None

    def _extract_qzone_cell_video_payload(self, text: str) -> str | None:
        patterns = [
            r'"cell_video"\s*:\s*\{',
            r"'cell_video'\s*:\s*\{",
            r"cell_video\s*:\s*\{",
            r'"?cell_video"?\s*:\s*\{',
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                start = text.find("{", match.end() - 1)
                if start != -1:
                    try:
                        end = self._find_matching_brace(text, start)
                        return text[start:end]
                    except ParseException:
                        continue

        return None

    def _qzone_first_media_url(self, media_dict: Any) -> str | None:
        if not isinstance(media_dict, dict):
            return None

        for key in ["2", "3"]:
            if key in media_dict:
                value = media_dict[key]
                if isinstance(value, dict):
                    url = value.get("url") or value.get("src")
                    if isinstance(url, str) and url:
                        return url
                elif isinstance(value, str) and value:
                    return value

        best_url = None
        max_area = -1

        for key, value in media_dict.items():
            if not isinstance(value, dict):
                continue

            url = value.get("url") or value.get("src")
            if not isinstance(url, str) or not url:
                continue

            try:
                width = int(value.get("width", 0))
                height = int(value.get("height", 0))
                area = width * height
            except (ValueError, TypeError):
                area = 0

            if area >= max_area:
                max_area = area
                best_url = url

        if best_url:
            return best_url

        for value in media_dict.values():
            if isinstance(value, str) and value.startswith("http"):
                return value

        return None

    # ------------------------------------------------------------------ #
    # 工具函数
    # ------------------------------------------------------------------ #

    async def _check_qzone_image_is_gif(self, url: str, client: AsyncClient | None = None) -> bool:
        """探测图片是否为 GIF, 结果按 URL 缓存; 传入 client 时复用连接 (并发探测用)."""
        if not url:
            return False

        if ".gif" in url.lower() or "fmt=gif" in url.lower():
            return True

        if url in self._gif_cache:
            return self._gif_cache[url]

        result = False
        try:
            if client is None:
                async with AsyncClient(headers=self.headers, verify=False, follow_redirects=True, timeout=3.0) as own:
                    result = await self._probe_qzone_gif(own, url)
            else:
                result = await self._probe_qzone_gif(client, url)
        except Exception:
            result = False

        if len(self._gif_cache) > 2000:
            self._gif_cache.clear()
        self._gif_cache[url] = result
        return result

    @staticmethod
    async def _probe_qzone_gif(client: AsyncClient, url: str) -> bool:
        response = await client.head(url)
        if response.status_code == 405:
            response = await client.get(url)
        if response.status_code >= 400:
            return False
        content_type = response.headers.get("Content-Type", "").lower()
        return "image/gif" in content_type

    def _get_qzone_hash(self, url: str) -> str:
        if not url:
            return ""

        url = unquote(html.unescape(url).strip())

        m_ps = re.search(r"/ps[cb]\?/[^/]+/([^/!&?#]+)", url)
        if m_ps:
            return m_ps.group(1)

        m_su = re.search(r"(?:&|\?)su=([^&#]+)", url)
        if m_su:
            return f"su_{m_su.group(1)}"

        clean_url = re.sub(r"^https?:", "", url)
        clean_url = re.sub(r"^//[^/]+", "", clean_url)
        clean_url = re.sub(r"!/[a-zA-Z]+(?:&|$).*", "", clean_url)
        clean_url = clean_url.split("?")[0].split("#")[0]

        clean_url = re.sub(r"(?:/(?:0|1|b|m|s|large|bmiddle|small|big|orig)|\.mp4)$", "", clean_url, flags=re.IGNORECASE)

        m_id = re.search(r"([a-zA-Z0-9_-]{15,})", clean_url)
        if m_id:
            return m_id.group(1)

        return clean_url.strip("/")

    def _fix_qzone_domain(self, src: str) -> str:
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://h5.qzone.qq.com" + src

        src = src.replace("r.photo.store.qq.com", "m.qpic.cn")
        src = src.replace("photogz.photo.store.qq.com", "m.qpic.cn")
        return src

    def _upgrade_qzone_quality(self, src: str) -> str:
        src = re.sub(r"!/[rcsm](&|$)", r"!/b\1", src)
        src = src.replace("/bmiddle/", "/large/")
        src = src.replace("/m/", "/b/")
        return src

    def _normalize_qzone_image_url(self, src: str) -> str:
        return self._upgrade_qzone_quality(self._fix_qzone_domain(src))

    def _extract_qzone_text(self, node: Tag | NavigableString) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if node.name == "br":
            return "\n"
        if node.name == "img":
            return node.get("alt") or node.get("title") or node.get("data-text") or ""
        if node.name == "span" and node.has_attr("data-text"):
            return node["data-text"]

        text_parts: list[str] = []
        for child in node.children:
            text_parts.append(self._extract_qzone_text(child))
        return "".join(text_parts)
