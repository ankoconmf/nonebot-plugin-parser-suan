import re
import time
import asyncio
from random import choice
from typing import Any, ClassVar

from httpx import AsyncClient
from msgspec import convert
from nonebot import logger

from ..base import (
    COMMON_TIMEOUT,
    Platform,
    BaseParser,
    PlatformEnum,
    ParseException,
    handle,
)
from ..task import PathTask
from ..data import CommentItem
from ...config import pconfig
from ..utils import followers_extra
from ...download import downloader
from ...constants import CommentSort


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.DOUYIN, display_name="抖音")

    # web detail API: 带 open.douyin.com Origin/Referer 伪装即可免 web 端签名校验
    DETAIL_API_URL: ClassVar[str] = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    DETAIL_API_AID: ClassVar[int] = 6383

    # web 评论列表 API: 注册 ttwid 后同样免签名(msToken/X-Bogus 留空),
    # cursor=0 的第一页即网页端展示的热门评论
    COMMENT_API_URL: ClassVar[str] = "https://www.douyin.com/aweme/v1/web/comment/list/"
    TTWID_REGISTER_URL: ClassVar[str] = "https://ttwid.bytedance.com/ttwid/union/register/"
    TTWID_TTL: ClassVar[float] = 3600.0
    """ttwid cookie 缓存时长(秒)"""

    COMMENT_SORT_PAGE_SIZE: ClassVar[int] = 50
    """按点赞排序时取的评论页大小(接口单页上限就是 50)"""

    # ttwid 是网页端"游客登录态"凭证, 评论接口没有它会被判为未登录
    ttwid: str = ""
    ttwid_update_at: float = 0.0

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://webcast.amemv.com/douyin/webcast/reflow/7674613819385613097
    # 直播/回放分享短链的重定向目标
    @handle("webcast.amemv", r"webcast\.amemv\.com/douyin/webcast/reflow/(?P<room_id>\d+)")
    # https://live.douyin.com/7674613819385613097
    @handle("live.douyin", r"live\.douyin\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: re.Match[str]):
        """解析直播/回放房间"""
        room_id = searched.group("room_id")
        return await self.parse_live(room_id)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    # 类型段通配 [a-z]+, 无论 video/note/slides/图集 都统一取 vid 调同一个 detail API
    @handle("douyin", r"douyin\.com/[a-z]+/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/[a-z]+/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/[a-z]+/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle("jingxuan.douyin", r"jingxuan\.douyin.com/m/[a-z]+/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        vid = searched.group("vid")
        # 首选: 直连 web detail API(免浏览器, 图文/实况图也能拿到视频与背景音乐)
        try:
            return await self.parse_aweme(vid)
        except Exception as e:
            logger.warning(f"failed to parse douyin {vid} via API, fallback to browser, error: {e}")
        # 兜底: 浏览器渲染页面, 拦截真实发出的 detail 请求(带真实 cookie/签名, 可过风控)
        try:
            return await self.parse_video_by_browser(vid)
        except Exception as e:
            logger.warning(f"failed to parse douyin {vid} via browser, error: {e}")
        raise ParseException("分享已删除或资源直链提取失败, 请稍后再试")

    async def parse_aweme(self, vid: str):
        """直接调用抖音 web detail API 获取作品数据.

        与 parse_video_by_browser 拦截的浏览器请求同源同参数, 返回结构一致:
        普通视频在 aweme_detail.video, 图集/图文/实况图在 aweme_detail.images
        (实况图图片带 video 字段), 背景音乐在 aweme_detail.music.
        评论区走另一个接口, 与详情并发请求, 拿不到就只是不展示评论区.
        """
        from . import video

        detail, comments = await asyncio.gather(
            self.fetch_aweme_detail(vid),
            self.fetch_comments_safely(vid),
        )

        # 抖音资源下载需要 Referer
        self.headers["Referer"] = "https://www.douyin.com/"

        video_data = convert(detail, video.VideoData)
        return self._build_video_result(video_data, *comments)

    async def fetch_aweme_detail(self, vid: str) -> dict[str, Any]:
        """请求 web detail API, 返回 aweme_detail 原始字典"""
        headers = {
            **self.android_headers,
            "Origin": "https://open.douyin.com",
            "Referer": "https://open.douyin.com/",
        }
        params = {"aweme_id": vid, "aid": self.DETAIL_API_AID}
        async with AsyncClient(
            headers=headers,
            timeout=COMMON_TIMEOUT,
            verify=False,
        ) as client:
            response = await client.get(self.DETAIL_API_URL, params=params)
            if response.status_code != 200:
                raise ParseException(f"status: {response.status_code}")
            payload = response.json()

        detail = payload.get("aweme_detail")
        if not isinstance(detail, dict):
            raise ParseException("can't find aweme_detail in API response")
        return detail

    async def ensure_ttwid(self) -> str:
        """获取(并缓存) ttwid cookie: 评论接口需要它才认这是网页端请求"""
        if self.ttwid and time.time() - self.ttwid_update_at < self.TTWID_TTL:
            return self.ttwid

        async with AsyncClient(
            headers=self.headers,
            timeout=COMMON_TIMEOUT,
            verify=False,
        ) as client:
            response = await client.post(
                self.TTWID_REGISTER_URL,
                json={
                    "region": "cn",
                    "aid": 1768,
                    "needFid": False,
                    "service": "www.douyin.com",
                    "migrate_info": {"ticket": "", "source": "node"},
                    "cbUrlProtocol": "https",
                    "union": True,
                },
            )

        ttwid = response.cookies.get("ttwid")
        if not ttwid:
            raise ParseException(f"ttwid 注册失败: status={response.status_code}")

        self.ttwid = ttwid
        self.ttwid_update_at = time.time()
        return ttwid

    async def fetch_comments(self, vid: str, count: int) -> tuple[list[CommentItem], int]:
        """取评论区第一页 (热门页), 排序后返回前 count 条与评论总数"""
        from .comment import CommentList, build_comments, sort_comments

        sort_mode = pconfig.comment_sort
        # 按点赞排序时多取一些再挑: 一页最多 50 条, 且高赞评论基本都在热门页
        fetch_count = self.COMMENT_SORT_PAGE_SIZE if sort_mode is CommentSort.like else count

        ttwid = await self.ensure_ttwid()
        params = {
            "device_platform": "webapp",
            "aid": self.DETAIL_API_AID,
            "channel": "channel_pc_web",
            "aweme_id": vid,
            "cursor": 0,
            "count": fetch_count,
            "msToken": "",
            "X-Bogus": "",
        }
        headers = {**self.headers, "Referer": "https://www.douyin.com/"}
        async with AsyncClient(
            headers=headers,
            cookies={"ttwid": ttwid},
            timeout=COMMON_TIMEOUT,
            verify=False,
        ) as client:
            response = await client.get(self.COMMENT_API_URL, params=params)
            if response.status_code != 200:
                raise ParseException(f"status: {response.status_code}")
            payload = response.json()

        # status_code 非 0 一般是风控/评论已关闭, 让上层记一条 warning 便于排查
        if payload.get("status_code"):
            raise ParseException(f"comment api status_code: {payload['status_code']}")

        data = convert(payload, CommentList)
        comments = sort_comments(data.comments, sort_mode)

        def download_media(url: str) -> PathTask:
            return PathTask(downloader.download_img(url, ext_headers=headers))

        return build_comments(
            comments,
            download_media,
            count,
            pconfig.max_comment_images,
        ), data.total

    async def fetch_comments_safely(self, vid: str) -> tuple[list[CommentItem], int]:
        """评论区兜底: 关闭 / 失败都只返回空列表, 不影响作品解析"""
        if pconfig.max_comments <= 0:
            return [], 0

        try:
            return await self.fetch_comments(vid, pconfig.max_comments)
        except Exception as e:
            logger.warning(f"failed to fetch douyin comments for {vid}, error: {e}")
            return [], 0

    async def parse_live(self, room_id: str):
        """解析抖音直播/回放房间(封面、标题、主播、观看数).

        直播间信息内联在 reflow H5 页面的 RSC flight 数据里, 直接请求页面并解析,
        无需浏览器. 直播中与已下播(回放)共用同一页面结构, 二者都能拿到封面.
        """
        from .live import extract_room_info

        url = f"https://webcast.amemv.com/douyin/webcast/reflow/{room_id}"
        async with AsyncClient(
            headers=self.ios_headers,
            timeout=COMMON_TIMEOUT,
            verify=False,
        ) as client:
            response = await client.get(url, follow_redirects=True)
            if response.status_code != 200:
                raise ParseException(f"status: {response.status_code}")
            html = response.text

        room = extract_room_info(html)
        if not room:
            raise ParseException("直播间信息提取失败, 可能已下播或链接失效")

        # 抖音资源下载需要 Referer
        self.headers["Referer"] = "https://live.douyin.com/"

        # 封面
        cover = room.get("cover") or {}
        cover_url = choice(cover["urlList"]) if cover.get("urlList") else None

        # 主播
        owner = room.get("owner") or {}
        nickname = owner.get("nickname") or ""
        avatar = owner.get("avatarThumb") or owner.get("avatarMedium") or {}
        avatar_url = choice(avatar["urlList"]) if avatar.get("urlList") else None
        author = self.create_author(nickname, avatar_url)

        # 观看数(直播中显示"在看", 回放显示"看过")
        stats = room.get("roomViewStats") or {}
        view_text = stats.get("displayLong") or stats.get("displayMiddle") or ""

        title = room.get("title") or ""
        contents = []
        if cover_url:
            contents.append(self.create_image(cover_url))

        extra: dict[str, Any] = {"content_type": "直播"}
        # 粉丝数, 模板渲染在作者名下方、时间前面
        follow_info = owner.get("followInfo") or {}
        extra.update(
            followers_extra(follow_info.get("followerCount") or follow_info.get("followerCountStr"))
        )

        return self.result(
            url=f"https://live.douyin.com/{room_id}",
            title=f"直播 - {title}" if title else "直播",
            text=view_text or None,
            timestamp=room.get("createTime") or None,
            author=author,
            contents=contents,
            extra=extra,
        )

    async def parse_video_by_browser(self, vid: str):
        """浏览器兜底: 打开作品页, 拦截页面真实发出的 detail API 请求.

        评论区不受此影响, 仍走接口并发的 fetch_comments_safely.
        """
        from . import video

        detail, comments = await asyncio.gather(
            self.fetch_detail_by_browser(vid),
            self.fetch_comments_safely(vid),
        )

        try:
            video_data = convert(detail, video.VideoData)
        except Exception as e:
            raise ParseException(f"invalid aweme_detail: {e}") from e

        self.headers["Referer"] = "https://www.douyin.com/"
        return self._build_video_result(video_data, *comments)

    async def fetch_detail_by_browser(self, vid: str) -> dict[str, Any]:
        """浏览器打开作品页, 拦截页面真实发出的 detail 请求.

        视频页路由对视频/图文/实况图都会触发同一个 detail 接口(按 aweme_id 返回),
        note 页路由反而不会及时触发, 故只走 video 页. 冷启动首次加载偶发超时, 重试一次.
        """
        from ...browser import BrowserManager

        url = f"https://www.douyin.com/video/{vid}"
        for attempt in range(2):
            try:
                payload = await BrowserManager.get_json_response(
                    url,
                    target="/aweme/v1/web/aweme/detail/",
                    timeout=30,
                )
                candidate = payload.get("aweme_detail")
                if isinstance(candidate, dict):
                    return candidate
            except Exception as e:
                logger.debug(f"browser detail parse failed for {url} (attempt {attempt + 1}), error: {e}")

        raise ParseException("can't find aweme_detail in browser response")

    def _build_video_result(
        self,
        video_data,
        comments: list[CommentItem] | None = None,
        comments_total: int = 0,
    ):
        """把抖音作品数据转换为统一解析结果."""

        author = self.create_author(
            video_data.author.nickname,
            video_data.avatar_url,
        )

        extra: dict[str, Any] = {}
        if stats := video_data.stats_panel:
            extra["stats"] = stats
        if meta := video_data.meta_line:
            extra["meta"] = meta
        if comments:
            # 评论区 (HtmlRenderer 渲染, 其它渲染器忽略)
            extra["comments"] = comments
            extra["comments_total"] = comments_total
        # 粉丝数, 模板渲染在作者名下方、时间前面
        extra.update(followers_extra(video_data.author.follower_count))

        result = self.result(
            title=video_data.title,
            author=author,
            timestamp=video_data.create_time,
            extra=extra,
        )

        images = video_data.images or []
        if images:
            # 图集/图文/实况图: 图片列表; 实况图图片带视频, 视频与图片成对发(走合并转发)
            has_live_video = False
            for image in images:
                if video_url := image.video_url:
                    result.contents.append(
                        self.create_video(video_url, image.cover_url, image.duration)
                    )
                    if image.image_url:
                        result.contents.append(self.create_image(image.image_url))
                    has_live_video = True
                elif image_url := image.image_url:
                    result.contents.append(self.create_image(image_url))
            if has_live_video:
                # 视频进合并转发, 且网格图跳过与图片重复的视频封面
                result.extra["merge_videos"] = True
                result.extra["live_photos"] = True
            # 背景音乐转为语音消息
            if (music := video_data.music) and (audio_url := music.audio_url):
                result.contents.append(self.create_audio(audio_url, music.duration))
        elif video_url := video_data.video_url:
            # 普通视频
            result.video = self.create_video(
                video_url,
                video_data.cover_url,
                video_data.duration,
            )

        return result
