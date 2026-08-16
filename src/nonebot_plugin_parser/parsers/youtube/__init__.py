import json
import re
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from ..base import Platform, BaseParser, PlatformEnum, handle, pconfig
from ..cookie import save_cookies_with_netscape
from ..utils import fmt_stat
from ...download import yt_dlp_downloader
from ...exception import ParseException


def _join_runs(container: dict[str, Any] | None) -> str:
    """拼接 YouTube runs 结构中的文本"""
    if not container:
        return ""
    runs = container.get("runs") or []
    return "".join(run.get("text", "") for run in runs if isinstance(run, dict))


def _best_thumbnail_url(thumbnails: list | None) -> str | None:
    """从 thumbnails 里取最大尺寸的 url, 并补全协议"""
    if not thumbnails:
        return None

    best, best_width = None, -1
    for thumb in thumbnails:
        if not isinstance(thumb, dict) or not thumb.get("url"):
            continue
        try:
            width = int(thumb.get("width", 0))
        except (TypeError, ValueError):
            width = 0
        if width >= best_width:
            best_width = width
            best = thumb["url"]

    if best is None and isinstance(thumbnails[-1], dict):
        best = thumbnails[-1].get("url")

    if best and best.startswith("//"):
        best = "https:" + best
    return best


def _find_first(obj: Any, key: str) -> Any:
    """递归查找第一个包含指定 key 的 dict 的 value"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            if (found := _find_first(value, key)) is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            if (found := _find_first(item, key)) is not None:
                return found
    return None


def _extract_count(text: str) -> str | None:
    """从 '2 条评论' / '1,234 Comments' 中提取纯数字"""
    if match := re.search(r"[\d,]+", text):
        return match.group(0).replace(",", "")
    return None


def _browse_context(hl: str = "zh-HK") -> dict[str, Any]:
    """构造 YouTube InnerTube browse 请求的 context"""
    return {
        "context": {
            "client": {
                "hl": hl,
                "gl": "US",
                "deviceMake": "Apple",
                "deviceModel": "",
                "clientName": "WEB",
                "clientVersion": "2.20251002.00.00",
                "osName": "Macintosh",
                "osVersion": "10_15_7",
            },
            "user": {"lockedSafetyMode": False},
            "request": {
                "useSsl": True,
                "internalExperimentFlags": [],
                "consistencyTokenJars": [],
            },
        },
    }


class YouTubeParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.YOUTUBE, display_name="油管")

    def __init__(self):
        super().__init__()
        self.cookies_file = pconfig.config_dir / "ytb_cookies.txt"
        if pconfig.ytb_ck:
            save_cookies_with_netscape(
                pconfig.ytb_ck,
                self.cookies_file,
                "youtube.com",
            )

    @handle("youtu", r"youtu\.be/[A-Za-z\d\._\?%&\+\-=/#]+")
    @handle("youtube", r"youtube\.com/(?:watch|shorts|live|post)(?:/[A-Za-z\d_\-]+|\?v=[A-Za-z\d_\-]+)")
    async def _parse_video(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        if "/post/" in url:
            return await self.parse_post(url)
        return await self.parse_video(url)

    async def parse_video(self, url: str):
        video_info = await yt_dlp_downloader.extract_video_info(url, self.cookies_file)
        author = await self._fetch_author_info(video_info.channel_id)

        stats = []
        if video_info.is_live and video_info.concurrent_view_count is not None:
            stats.append(
                {
                    "icon": "eye",
                    "value": fmt_stat(video_info.concurrent_view_count),
                    "label": "正在观看",
                }
            )
        view_label = "累计观看" if video_info.is_live else "观看"
        for icon, value, label in (
            ("eye", video_info.view_count, view_label),
            ("like", video_info.like_count, "点赞"),
            ("comment", video_info.comment_count, "评论"),
        ):
            if value is not None:
                stats.append({"icon": icon, "value": fmt_stat(value), "label": label})

        extra: dict[str, Any] = {"stats": stats} if stats else {}
        if video_info.is_live:
            extra["content_type"] = "直播"

        result = self.result(
            author=author,
            title=video_info.title,
            text=video_info.description or None,
            timestamp=video_info.timestamp,
            extra=extra,
        )

        # 直播或时长未知时只渲染封面图, 不下载视频
        if video_info.is_live or video_info.duration is None:
            if video_info.duration is None and not video_info.is_live:
                # 时长未知但非直播(异常), 内容类型仍按视频处理
                result.extra["content_type"] = "视频"
            result.contents.extend(self.create_images([video_info.thumbnail]))
        elif video_info.duration <= pconfig.duration_maximum:
            video = yt_dlp_downloader.download_video(url, self.cookies_file)
            result.video = self.create_video(
                video,
                video_info.thumbnail,
                video_info.duration,
            )
        else:
            # 超时长只渲染封面, 内容类型仍为视频
            result.extra["content_type"] = "视频"
            result.contents.extend(self.create_images([video_info.thumbnail]))

        return result

    async def parse_post(self, url: str):
        """解析社区帖子 (/post/), 当前支持文字 + 单图/多图"""
        async with AsyncClient(
            headers=self.headers,
            timeout=self.timeout,
            verify=False,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        match = re.search(r"var ytInitialData = (\{.*?\});\s*</script>", html, re.DOTALL)
        if not match:
            raise ParseException("获取帖子信息失败")

        data = json.loads(match.group(1))
        post = _find_first(data, "backstagePostRenderer")
        if not isinstance(post, dict):
            raise ParseException("获取帖子信息失败")

        # 作者
        author_name = _join_runs(post.get("authorText")) or "YouTube"
        author_avatar = _best_thumbnail_url((post.get("authorThumbnail") or {}).get("thumbnails"))
        author = self.create_author(author_name, author_avatar)

        # 正文
        text = _join_runs(post.get("contentText")) or None

        # 图片
        images = self._extract_post_images(post)

        # 点赞 / 评论 / 发布时间
        like = (post.get("voteCount") or {}).get("simpleText")
        comment_text = await self._fetch_post_comment_count(data)
        published = _join_runs(post.get("publishedTimeText"))

        info_parts = []
        if like:
            info_parts.append(f"{like} 赞")
        if comment_text:
            info_parts.append(comment_text)
        if tip := self._post_attachment_tip(post.get("backstageAttachment")):
            info_parts.append(tip)

        extra: dict[str, Any] = {}
        stats = []
        if like:
            stats.append({"icon": "like", "value": like, "label": "赞"})
        if comment_text and (comment_num := _extract_count(comment_text)):
            stats.append({"icon": "comment", "value": comment_num, "label": "评论"})
        if stats:
            extra["stats"] = stats
        if info_parts:
            extra["info"] = " · ".join(info_parts)

        result = self.result(
            author=author,
            text=text,
            url=url,
            datetime_text=published or None,
            extra=extra,
        )

        if images:
            result.contents.extend(self.create_images(images))

        return result

    @staticmethod
    def _extract_post_images(post: dict[str, Any]) -> list[str]:
        """提取帖子中的图片 URL (支持单图/多图)"""
        attachment = post.get("backstageAttachment") or {}
        images: list[str] = []

        if multi := attachment.get("postMultiImageRenderer"):
            for item in multi.get("images") or []:
                if not isinstance(item, dict):
                    continue
                renderer = item.get("backstageImageRenderer") or {}
                if url := _best_thumbnail_url((renderer.get("image") or {}).get("thumbnails")):
                    images.append(url)
        elif renderer := attachment.get("backstageImageRenderer"):
            if url := _best_thumbnail_url((renderer.get("image") or {}).get("thumbnails")):
                images.append(url)

        return images

    @staticmethod
    def _post_attachment_tip(attachment: Any) -> str | None:
        """对暂不支持的帖子类型返回提示"""
        if not isinstance(attachment, dict):
            return None
        if "pollRenderer" in attachment:
            return "投票帖暂不支持解析"
        if "videoRenderer" in attachment or "playlistRenderer" in attachment:
            return "引用视频/播放列表暂不支持解析"
        return None

    async def _fetch_author_info(self, channel_id: str):
        from . import meta

        url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
        payload = {**_browse_context(), "browseId": channel_id}

        async with AsyncClient(headers=self.headers, timeout=self.timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

        browse = meta.decoder.decode(response.content)
        return self.create_author(browse.name, browse.avatar_url, browse.description)

    async def _fetch_post_comment_count(self, data: dict[str, Any]) -> str | None:
        """获取帖子评论数文本, 失败静默返回 None"""
        try:
            command = _find_first(data, "continuationCommand")
            token = command.get("token") if isinstance(command, dict) else None
            if not token:
                return None

            payload = {**_browse_context("zh-CN"), "continuation": token}
            url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
            async with AsyncClient(
                headers=self.headers,
                timeout=self.timeout,
                verify=False,
            ) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()

            header = _find_first(response.json(), "commentsHeaderRenderer")
            if isinstance(header, dict):
                return _join_runs(header.get("countText")) or None
        except Exception:
            logger.debug("获取帖子评论数失败", exc_info=True)
        return None
