"""腾讯频道 (pd.qq.com) 帖子解析器.

腾讯频道分享链接 (https://pd.qq.com/s/xxx 或 https://pd.qq.com/g/xxx/post/xxx)
在服务端由 Tencent EdgeOne 的 EO-Bot-Js-Token 挑战保护: 首次访问返回一段内联
混淆 JS, 需在 JS 环境里执行 `window.solveChallenge(challenge, session)` 算出 token,
带上 `EO-Bot-Js-Token` cookie 后才能拿到真实的 Nuxt SSR 页面 (内含 `__NUXT_DATA__`
完整帖子数据).

本解析器用本机 node 运行时执行挑战脚本 (无需真实浏览器), token 有效期内 (约 1 小时,
由服务端 max-age 决定) 缓存复用, 并配合服务端返回的 uuid/p_uin cookie 一起携带.

支持的链接/卡片格式:
- https://pd.qq.com/s/8s8shwqkd?b=2 (短链)
- https://pd.qq.com/g/<guild_number>/post/<feed_id> (帖子直链)
- [CQ:json,data={"app":"com.tencent.forum",...}] (QQ 频道分享卡片, 内嵌完整数据, 无需 HTTP)
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from ..config import pconfig
from ..constants import PlatformEnum
from .base import BaseParser, Platform, ParseException, handle
from .data import MediaContent
from .utils import fmt_stat

# 挑战页里的内联脚本由前缀 + 混淆 VM + 尾部 solveChallenge 调用组成;
# node 执行时只需 mock 浏览器全局, 即可让脚本自我调用 solveChallenge 并写出 token.
_NODE_MOCK_PREFIX = r"""
const fs = require('fs');
const captured = { cookies: [] };
let _cookieStore = "";
const document = {
  set cookie(v) { _cookieStore = v; captured.cookies.push(v); },
  get cookie() { return _cookieStore; },
};
const location = {
  href: __URL__,
  replace(re) {},
};
global.window = global;
global.document = document;
global.location = location;
"""

_NODE_TAIL = r"""
let __token = "";
for (const c of captured.cookies) {
  const m = /^EO-Bot-Js-Token=([^;]+)/.exec(c);
  if (m) { __token = m[1]; break; }
}
fs.writeFileSync(__OUT__, __token, "utf8");
"""

# 服务端返回的 EO-Bot-Js-Token 有效期 (秒), 缓存时留出安全余量
_TOKEN_TTL = 3600
_TOKEN_SAFE_MARGIN = 300


class PDQQParser(BaseParser):
    """腾讯频道帖子解析器."""

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.PDQQ, display_name="腾讯频道")

    # token / 会话 cookie 缓存 (跨请求复用, 挑战脚本每次随机无法复用)
    _token: ClassVar[str | None] = None
    _token_expires_at: ClassVar[float] = 0.0
    _session_cookies: ClassVar[dict[str, str]] = {}
    _token_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self):
        super().__init__()
        self.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self._node_path: str | None = None

    # ------------------------------------------------------------------ #
    # 匹配入口
    # ------------------------------------------------------------------ #

    @handle("pd.qq.com/s/", r"pd\.qq\.com/s/(?P<code>[\w-]+)")
    async def _parse_short(self, searched: re.Match[str]):
        """解析 /s/<code> 短链"""
        url = f"https://pd.qq.com/s/{searched.group('code')}"
        return await self.parse_url(url)

    @handle("pd.qq.com/g/", r"pd\.qq\.com/g/(?P<guild>[\w-]+)/post/(?P<feed>[\w-]+)")
    async def _parse_post(self, searched: re.Match[str]):
        """解析 /g/<guild>/post/<feed> 帖子直链"""
        url = f"https://pd.qq.com/g/{searched.group('guild')}/post/{searched.group('feed')}"
        return await self.parse_url(url)

    @handle("com.tencent.forum", r'"app"\s*:\s*"com\.tencent\.forum"')
    async def _parse_json_card(self, searched: re.Match[str]):
        """解析 QQ 频道 JSON 卡片 (app=com.tencent.forum).

        卡片内嵌 feed 数据, 但对多图帖子只带首图 (images 被裁剪为 1 张),
        故优先用 feed_id 走 HTTP 拿完整数据; 失败则回退到卡片数据兜底.
        """
        payload = self._extract_card_payload(searched.string)
        detail = payload.get("meta", {}).get("detail") if isinstance(payload, dict) else None
        if not isinstance(detail, dict):
            raise ParseException("频道卡片结构异常 (缺少 meta.detail)")
        feed = detail.get("feed")
        if not isinstance(feed, dict):
            raise ParseException("频道卡片中未找到帖子数据")

        # 优先: 用 feed_id + guild_id 拼标准直链走 HTTP 拿完整数据 (多图不被裁剪)
        feed_id = detail.get("feed_id")
        guild_id = (detail.get("channel_info") or {}).get("guild_id")
        if feed_id and guild_id:
            post_url = f"https://pd.qq.com/g/{guild_id}/post/{feed_id}"
            try:
                return await self.parse_url(post_url)
            except ParseException as e:
                logger.warning(f"频道卡片走 HTTP 补全失败, 回退卡片数据: {e}")

        # 回退: 直接用卡片内嵌数据 (至少能拿到标题 + 首图)
        url = detail.get("jump_url") or feed_id or None
        guild_name = (detail.get("channel_info") or {}).get("guild_name")
        channel_name = (detail.get("channel_info") or {}).get("channel_name")

        # 卡片字段为 snake_case (pic_url/comment_count/prefer_count),
        # 与 HTTP 页面的 camelCase 不同, 构建时补上字段别名;
        # 卡片的 poster 在 detail 层, 而 HTTP 页面在 feed 层
        return self._build_result(
            feed,
            url=url,
            guild_name=guild_name,
            channel_name=channel_name,
            poster=detail.get("poster"),
        )

    @staticmethod
    def _extract_card_payload(raw: str) -> dict[str, Any] | None:
        """从 JSON 卡片原始字符串提取 payload (容错各种前缀)."""
        raw = html.unescape(raw.strip())

        # 1. 直接是 JSON 对象
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # 2. 带 CQ/mirai 前缀: 找 data= 或 json= 后的 { ... }
        for prefix in ("[CQ:json,data=", "CQ:json,data=", "[json:data=", "json:data=", "data=", "json="):
            pos = raw.find(prefix)
            if pos == -1:
                continue
            start = raw.find("{", pos)
            if start == -1:
                continue
            try:
                end = PDQQParser._find_matching_brace(raw, start)
                return json.loads(raw[start:end])
            except (json.JSONDecodeError, ParseException):
                continue

        # 3. 兜底: 扫描每个 { 尝试解析
        idx = raw.find("{")
        while idx != -1:
            try:
                end = PDQQParser._find_matching_brace(raw, idx)
                try:
                    return json.loads(raw[idx:end])
                except json.JSONDecodeError:
                    pass
            except ParseException:
                pass
            idx = raw.find("{", idx + 1)
        return None

    @staticmethod
    def _find_matching_brace(text: str, start: int) -> int:
        """从 start 处 { 开始找配对的 } (跳过字符串内的括号)."""
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        raise ParseException("JSON 括号不匹配")

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def parse_url(self, url: str):
        """获取帖子信息并构建解析结果."""
        async with AsyncClient(
            headers=self.headers,
            timeout=self.timeout,
            verify=False,
            follow_redirects=True,
        ) as client:
            page_html = await self._fetch_page(client, url)

            feed = self._extract_feed(page_html)
            if feed is None:
                raise ParseException("未从页面解析到帖子数据 (帖子可能已删除或需要登录)")

            canonical = self._extract_canonical_url(page_html) or url
            return self._build_result(feed, url=canonical)

    def _build_result(
        self,
        feed: dict[str, Any],
        *,
        url: str | None,
        guild_name: str | None = None,
        channel_name: str | None = None,
        poster: dict[str, Any] | None = None,
    ):
        """由 feed 字典构建 ParseResult (兼容 HTTP 页面与 JSON 卡片两种字段命名)."""
        text = self._extract_text(feed)
        images = self._extract_images(feed)
        videos = self._extract_videos(feed)
        poster = poster if isinstance(poster, dict) else (feed.get("poster") or {})
        author_name = str(poster.get("nick") or "未知作者")
        avatar = self._extract_avatar(poster)

        stats = self._build_stats(feed)

        contents: list[MediaContent] = []
        for video_url, cover_url in videos:
            contents.append(self.create_video(video_url, cover_url=cover_url))
        for img_url in images:
            contents.append(self.create_image(img_url))

        if not text and not contents:
            raise ParseException("帖子无文本且无图片, 无法解析")

        author = self.create_author(author_name, avatar) if author_name else None

        # 内容类型: 有视频按视频, 否则按是否含图
        if videos:
            content_type = "视频"
        elif images:
            content_type = "图文"
        else:
            content_type = "动态"

        # 频道帖子的 title 与简介 text 高度重复 (title 常是 text 的首行/摘要),
        # 故不设置 title, 仅保留简介. 简介文字通过 extra 标志在渲染器的合并转发中加入.
        extra: dict[str, Any] = {
            "stats": stats,
            "content_type": content_type,
        }
        if text and contents:
            # 有媒体时, 简介文字进合并转发 (由渲染器处理)
            extra["text_in_forward"] = True
        if guild_name:
            extra["guild_name"] = guild_name
        if channel_name:
            extra["channel_name"] = channel_name

        return self.result(
            url=url,
            title=None,
            text=text,
            author=author,
            timestamp=self._extract_timestamp(feed),
            contents=contents,
            extra=extra,
        )

    # ------------------------------------------------------------------ #
    # 反爬: token 获取
    # ------------------------------------------------------------------ #

    async def _fetch_page(self, client: AsyncClient, url: str) -> str:
        """带挑战 token 请求页面, 拿到真实 HTML."""
        token = await self._ensure_token(client, url)

        cookies: dict[str, str] = {**self._session_cookies, "EO-Bot-Js-Token": token}
        resp = await client.get(url, cookies=cookies)
        resp.raise_for_status()
        html = resp.text

        # 若仍命中挑战页, token 可能已失效, 彻底重置后重解一次.
        # 注意: token 与首次下发的 uuid/p_uin 配套, 重解时不能沿用旧 cookie,
        # 否则新 token 与旧 uuid/p_uin 不匹配, 服务端仍会返回挑战页.
        if "solveChallenge" in html:
            logger.warning("pd.qq 挑战 token 失效, 重新求解")
            self._invalidate_token()
            client.cookies.clear()
            token = await self._ensure_token(client, url)
            resp = await client.get(url, cookies={"EO-Bot-Js-Token": token})
            resp.raise_for_status()
            html = resp.text
            if "solveChallenge" in html:
                raise ParseException("腾讯频道反爬挑战求解失败")

        # 记录服务端下发的会话 cookie (uuid/p_uin 等), 供后续复用
        for c in client.cookies.jar:
            if c.name in ("uuid", "p_uin"):
                self._session_cookies[c.name] = c.value
        return html

    async def _ensure_token(self, client: AsyncClient, url: str) -> str:
        """获取有效 token (命中缓存则直接返回).

        注意: token 状态是 ClassVar, 统一用 cls 读写, 与 _invalidate_token 保持一致;
        若误用 self 赋值会落为实例属性, 导致失效清理后仍读到旧 token.
        """
        cls = type(self)
        now = time.time()
        if cls._token and now < cls._token_expires_at:
            return cls._token

        async with cls._token_lock:
            # 双重检查, 防止并发重复求解
            if cls._token and time.time() < cls._token_expires_at:
                return cls._token

            token = await self._solve_token(client, url)
            cls._token = token
            cls._token_expires_at = time.time() + _TOKEN_TTL - _TOKEN_SAFE_MARGIN
            return token

    async def _solve_token(self, client: AsyncClient, url: str) -> str:
        """请求挑战页并用 node 执行挑战脚本解出 token."""
        node_path = await self._get_node_path()
        if not node_path:
            raise ParseException("未找到 node 运行时, 无法解析腾讯频道 (请安装 Node.js)")

        resp = await client.get(url)
        resp.raise_for_status()
        match = re.search(r"<script>(.*?)</script>", resp.text, re.S)
        if not match:
            # 可能没触发挑战 (少见), 但没 token 后续也会失败
            raise ParseException("未在挑战页找到挑战脚本")
        challenge_script = match.group(1)

        workdir = pconfig.cache_dir
        workdir.mkdir(parents=True, exist_ok=True)
        suffix = f"{int(time.time() * 1000)}_{id(self) % 100000}"
        js_file = workdir / f"pdqq_solve_{suffix}.js"
        out_file = workdir / f"pdqq_solve_{suffix}.token"

        js = (
            _NODE_MOCK_PREFIX.replace("__URL__", json.dumps(url))
            + "\n"
            + challenge_script
            + "\n"
            + _NODE_TAIL.replace("__OUT__", json.dumps(str(out_file)))
        )
        try:
            js_file.write_text(js, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                node_path,
                str(js_file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
            if not out_file.exists():
                raise ParseException("挑战脚本执行失败 (node 未产出 token)")
            token = out_file.read_text(encoding="utf-8").strip()
            if not token or token == "NO_TOKEN":
                raise ParseException("挑战脚本执行失败 (未得到 token)")
            return token
        finally:
            for f in (js_file, out_file):
                with contextlib.suppress(OSError):
                    f.unlink(missing_ok=True)

    async def _get_node_path(self) -> str | None:
        if self._node_path is not None:
            return self._node_path or None
        self._node_path = shutil.which("node") or ""
        if not self._node_path:
            logger.warning("未找到 node 运行时, 腾讯频道解析将不可用")
        return self._node_path or None

    @classmethod
    def _invalidate_token(cls) -> None:
        cls._token = None
        cls._token_expires_at = 0.0
        cls._session_cookies = {}

    # ------------------------------------------------------------------ #
    # 页面数据提取
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_feed(html: str) -> dict[str, Any] | None:
        """展开 __NUXT_DATA__ 并定位 feed 节点."""
        match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not match:
            return None
        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

        def resolve(idx, seen=None):
            if seen is None:
                seen = {}
            if isinstance(idx, int):
                if idx in seen:
                    return seen[idx]
                if idx >= len(raw):
                    return None
                val = raw[idx]
                if isinstance(val, list):
                    seen[idx] = None
                    r = [resolve(i, seen) for i in val]
                    seen[idx] = r
                    return r
                if isinstance(val, dict):
                    seen[idx] = None
                    r = {k: resolve(v, seen) for k, v in val.items()}
                    seen[idx] = r
                    return r
                seen[idx] = val
                return val
            if isinstance(idx, list):
                return [resolve(i, seen) for i in idx]
            if isinstance(idx, dict):
                return {k: resolve(v, seen) for k, v in idx.items()}
            return idx

        try:
            full = resolve(1)
        except Exception:
            return None

        # 深度优先找第一个含 title/images 的 feed 节点
        found: list[dict] = []

        def walk(obj, depth=0):
            if depth > 14 or len(found):
                return
            if isinstance(obj, dict):
                if "title" in obj and ("images" in obj or "contents" in obj):
                    if isinstance(obj.get("images"), list) or isinstance(obj.get("contents"), dict):
                        found.append(obj)
                        return
                for v in obj.values():
                    walk(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v, depth + 1)

        walk(full)
        return found[0] if found else None

    @staticmethod
    def _extract_text(feed: dict[str, Any]) -> str | None:
        """提取正文 (遍历多段 contents, 兼容文本/@/话题/链接段)."""
        contents = feed.get("contents") or {}
        if not isinstance(contents, dict):
            return None
        parts: list[str] = []
        for seg in contents.get("contents", []) or []:
            if not isinstance(seg, dict):
                continue
            if tc := seg.get("text_content"):
                t = tc.get("text") if isinstance(tc, dict) else None
                if t:
                    parts.append(str(t))
            elif ac := seg.get("at_content"):
                nick = (ac.get("nick") or ac.get("name")) if isinstance(ac, dict) else None
                if nick:
                    parts.append(f"@{nick}")
            elif ec := seg.get("url_content"):
                u = ec.get("url") if isinstance(ec, dict) else None
                if u:
                    parts.append(str(u))
            elif topic := seg.get("topic_content"):
                name = topic.get("topic_name") if isinstance(topic, dict) else None
                if name:
                    parts.append(str(name))
        text = "\n".join(p for p in parts if p).strip()
        return text or None

    @staticmethod
    def _extract_videos(feed: dict[str, Any]) -> list[tuple[str, str | None]]:
        """提取视频 (播放 URL, 封面 URL) 列表.

        视频节点 vecVideoUrl 含多档清晰度 (levelType), 取最高档的 playUrl.
        """
        result: list[tuple[str, str | None]] = []
        for v in feed.get("videos", []) or []:
            if not isinstance(v, dict):
                continue
            # 选最高清晰度档位 (levelType 最大)
            play_url = v.get("playUrl")
            vec = v.get("vecVideoUrl") or []
            if vec:
                best = max(vec, key=lambda x: x.get("levelType") or 0)
                play_url = (best.get("playUrl") or play_url) if isinstance(best, dict) else play_url
            if not play_url:
                continue
            # 封面: cover.picUrl 优先, 否则 vecImageUrl 最高档
            cover = v.get("cover") or {}
            cover_url = None
            if isinstance(cover, dict):
                cover_url = cover.get("picUrl") or cover.get("pic_url")
                if not cover_url:
                    cvec = cover.get("vecImageUrl") or []
                    if cvec and isinstance(cvec[-1], dict):
                        cover_url = cvec[-1].get("url")
            result.append((str(play_url), cover_url))
        return result

    @staticmethod
    def _extract_images(feed: dict[str, Any]) -> list[str]:
        """提取图片 URL (兼容 picUrl / pic_url 与 vecImageUrl 最高档)."""
        urls: list[str] = []
        for img in feed.get("images", []) or []:
            if not isinstance(img, dict):
                continue
            url = img.get("picUrl") or img.get("pic_url")
            if not url:
                vec = img.get("vecImageUrl") or []
                if vec and isinstance(vec[-1], dict):
                    url = vec[-1].get("url")
            if url:
                urls.append(str(url))
        return urls

    @staticmethod
    def _extract_avatar(poster: dict[str, Any]) -> str | None:
        # JSON 卡片: poster.avatar 直接是 URL
        avatar = poster.get("avatar")
        if avatar:
            return str(avatar)
        # HTTP 页面: poster.icon.iconUrl*
        icon = poster.get("icon") or {}
        if isinstance(icon, dict):
            return icon.get("iconUrl") or icon.get("iconUrl640") or icon.get("iconUrl100")
        return None

    @staticmethod
    def _extract_timestamp(feed: dict[str, Any]) -> int | None:
        share = feed.get("share") or {}
        if isinstance(share, dict):
            info = share.get("channelShareInfo") or {}
            if isinstance(info, dict):
                ts = info.get("feedPublishAt")
                if ts:
                    try:
                        return int(ts)
                    except (TypeError, ValueError):
                        pass
        # HTTP 页面: createTime / createTimeNs; JSON 卡片: create_time
        for key in ("createTime", "createTimeNs", "create_time"):
            val = feed.get(key)
            if val:
                try:
                    v = int(val)
                    if key == "createTimeNs" and v > 1e12:
                        return v // 1_000_000_000
                    return v
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    def _extract_canonical_url(html: str) -> str | None:
        m = re.search(r'<meta[^>]*property="og:url"[^>]*content="([^"]+)"', html)
        if m:
            return m.group(1)
        m = re.search(r'<meta[^>]*content="([^"]+)"[^>]*property="og:url"', html)
        return m.group(1) if m else None

    @staticmethod
    def _build_stats(feed: dict[str, Any]) -> list[dict[str, Any]]:
        stats: list[dict[str, Any]] = []

        def num(*keys: str) -> Any:
            for key in keys:
                if key not in feed:
                    continue
                val = feed[key]
                if val not in (None, ""):
                    return val
            return None

        # 点赞: HTTP 页面 total_like={"like_count": n}; JSON 卡片 prefer_count=n
        like = num("total_like", "prefer_count")
        if isinstance(like, dict):
            like = like.get("like_count")
        # 收藏: HTTP 页面 total_collect={"collect_count": n}
        collect = num("total_collect")
        if isinstance(collect, dict):
            collect = collect.get("collect_count")
        # 评论: commentCount (HTTP) / comment_count (卡片)
        comment = num("commentCount", "comment_count")
        # 分享: HTTP 页面 share.sharedCount
        share_info = feed.get("share") or {}
        shared = None
        if isinstance(share_info, dict):
            shared = share_info.get("sharedCount")

        mapping = (
            (like, "like", "点赞"),
            (collect, "star", "收藏"),
            (comment, "comment", "评论"),
            (shared, "share", "分享"),
        )
        for val, icon, label in mapping:
            if val not in (None, ""):
                try:
                    stats.append({"icon": icon, "value": fmt_stat(int(val)), "label": label})
                except (TypeError, ValueError):
                    continue
        return stats
