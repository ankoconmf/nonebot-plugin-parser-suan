import re
import json
import asyncio
from re import Match
from typing import Any, ClassVar
from urllib.parse import urlparse
from collections.abc import AsyncGenerator

import httpx
from msgspec import convert
from nonebot import logger
from bilibili_api import HEADERS, Credential, select_client, request_settings
from bilibili_api.opus import Opus
from bilibili_api.video import Video
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

from ..base import (
    BaseParser,
    PlatformEnum,
    ParseException,
    IgnoreException,
    DownloadException,
    handle,
    pconfig,
)
from ..data import Platform, ImageContent, MediaContent
from ..cookie import ck2dict
from ..utils import followers_extra
from .dynamic import DynamicInfo

# 选择客户端
select_client("curl_cffi")
# 模拟浏览器，第二参数数值参考 curl_cffi 文档
# https://curl-cffi.readthedocs.io/en/latest/impersonate.html
request_settings.set("impersonate", "chrome131")


# PCDN / P2P 节点主机名特征 (这类节点对非官方客户端常返回 403 或无法连接)
_PCDN_HOST_PATTERN = re.compile(r"(?:^|\.)(?:mcdn\.bilivideo\.cn|szbdyd\.com)$", re.IGNORECASE)
# PCDN 路径特征
_PCDN_PATH_PATTERN = re.compile(r"/(?:upgcxcode|v1/resource).*?(?:mcdn|pcdn)", re.IGNORECASE)
# 私有 IP 段 (10.x / 172.16-31.x / 192.168.x / 127.x)
_PRIVATE_IP_PATTERN = re.compile(
    r"^(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d"
)


def is_pcdn_url(url: str) -> bool:
    """判断链接是否指向 PCDN / P2P 节点或内网地址"""
    if not url:
        return False
    host = urlparse(url).hostname or ""
    if _PRIVATE_IP_PATTERN.match(host):
        return True
    if _PCDN_HOST_PATTERN.search(host):
        return True
    if _PCDN_PATH_PATTERN.search(url):
        return True
    return False


def pick_clean_url(url: str, backup_url: list[str] | None) -> str:
    """主链接为 PCDN 时, 从备用链接中挑第一个干净的顶上; 否则原样返回"""
    return order_download_urls(url, backup_url)[0]


def order_download_urls(url: str, backup_url: list[str] | None) -> tuple[str, ...]:
    """合并主链接与备用链接, 干净的 CDN 线路优先, PCDN 线路垫底

    B 站的播放地址接口会返回一组等价线路, 单条线路 503 / 超时属于常态,
    因此这里保留全部线路, 交给下载器失败轮换重试, 而不是只挑一条。
    """
    candidates = [item for item in dict.fromkeys([url, *(backup_url or [])]) if item]
    clean = [item for item in candidates if not is_pcdn_url(item)]
    pcdn = [item for item in candidates if is_pcdn_url(item)]

    if not clean and pcdn:
        # 一个干净的备用链接都没有, 只能用原链接
        logger.warning("检测到 PCDN 链接, 但无可用备用源, 下载可能失败")
    elif clean and clean[0] != url:
        logger.debug(f"检测到 PCDN 链接, 已切换到备用源: {urlparse(clean[0]).hostname}")

    return tuple([*clean, *pcdn])


# 直播间页面里服务端渲染(getInfoByRoom 同构数据)的注入位置
_LIVE_SSR_PATTERN = re.compile(r"window\.__NEPTUNE_IS_MY_WAIFU__\s*=\s*")
_LIVE_PAGE_URL = "https://live.bilibili.com/{room_id}"
# 老版直播接口: 不带 buvid 也能访问, 风控比 getInfoByRoom 宽松
_LIVE_ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
_LIVE_ANCHOR_INFO_URL = "https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room"
# 老接口返回的性别是数字, 转成 room_info 里的文案
_LIVE_GENDER_MAP = {1: "男", 2: "女"}


def extract_live_ssr_info(html: str) -> dict[str, Any] | None:
    """从直播间页面的服务端渲染数据中提取房间信息

    页面里有一段 ``window.__NEPTUNE_IS_MY_WAIFU__={...}`` 的 JSON, 其中
    ``roomInfoRes.data`` 与直播信息接口 ``getInfoByRoom`` 的返回结构一致
    (都含 room_info / anchor_info), 可以作为接口被风控时的兜底数据源。
    提取失败时返回 None。
    """
    match = _LIVE_SSR_PATTERN.search(html)
    if match is None:
        return None
    index = match.end()
    while index < len(html) and html[index].isspace():
        index += 1
    try:
        payload, _ = json.JSONDecoder().raw_decode(html, index)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    room_info_res = payload.get("roomInfoRes")
    if not isinstance(room_info_res, dict) or room_info_res.get("code") != 0:
        return None
    data = room_info_res.get("data")
    if not isinstance(data, dict):
        return None
    return data


class BilibiliParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.BILIBILI, display_name="哔哩哔哩")

    BILI_RETRYABLE_HTTP_STATUSES: ClassVar[frozenset[int]] = frozenset(
        {403, 404, 408, 425, 429, *range(500, 600)}
    )
    """B站 CDN 节点常见的可换线路重试状态码(限流/节点异常等)"""

    LIVE_INFO_TIMEOUT: ClassVar[float] = 10.0
    """直播信息兜底请求超时时间(s)"""

    def __init__(self):
        self.headers = HEADERS.copy()
        self._credential: Credential | None = None
        self._cookies_file = pconfig.config_dir / "bilibili_cookies.json"

    @handle("b23.tv", r"b23\.tv/[0-9a-zA-Z._?%&+-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[0-9a-zA-Z._?%&+-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        """解析短链"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/BV", r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9A-Za-z]{10})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_bv(self, searched: Match[str]):
        """解析视频信息"""
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/av", r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_av(self, searched: Match[str]):
        """解析视频信息"""
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("/opus/", r"bilibili\.com/opus/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: Match[str]):
        """解析动态信息"""
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic_or_opus(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: Match[str]):
        """解析直播信息"""
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: Match[str]):
        """解析收藏夹信息"""
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: Match[str]):
        """解析专栏信息"""
        from bilibili_api.article import Article

        read_id = int(searched.group("read_id"))
        article = Article(read_id)
        opus = await article.turn_to_opus()
        return await self._parse_bilibli_api_opus(opus)

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        """解析视频信息"""

        from .video import VideoInfo

        credential = await self.credential
        video = Video(bvid=bvid, aid=avid, credential=credential)
        video_info = convert(await video.get_info(), VideoInfo)
        # UP
        author = self.create_author(video_info.owner.name, video_info.owner.face)
        # 处理分 p
        page_info = video_info.extract_info_with_page(page_num)

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        # 视频下载 task
        async def download_video():
            output_path = pconfig.cache_dir / f"{video_info.bvid}-{page_num}.mp4"
            if output_path.exists():
                return output_path
            v_urls, a_urls = await self.extract_download_urls(video=video, page_index=page_info.index)
            if page_info.duration > pconfig.duration_maximum:
                logger.warning(f"视频时长 {page_info.duration} 秒, 超过 {pconfig.duration_maximum} 秒, 取消下载")
                raise IgnoreException
            if a_urls:
                path = await self.downloader.download_av_and_merge(
                    v_urls[0],
                    a_urls[0],
                    output_path=output_path,
                    ext_headers=self.headers,
                    video_fallback_urls=v_urls[1:],
                    audio_fallback_urls=a_urls[1:],
                    retry_http_statuses=self.BILI_RETRYABLE_HTTP_STATUSES,
                )
            else:
                path = await self.downloader.download_video(
                    v_urls[0],
                    video_name=output_path.name,
                    ext_headers=self.headers,
                    fallback_urls=v_urls[1:],
                    retry_http_statuses=self.BILI_RETRYABLE_HTTP_STATUSES,
                )
            return path

        video_content = self.create_video(
            asyncio.create_task(download_video()),
            page_info.cover,
            page_info.duration,
        )

        extra: dict[str, Any] = {
            "stats": video_info.stats_panel,
            "meta": video_info.meta_line(page_info.duration),
            "source_id": video_info.bvid,
        }
        # 粉丝数, 模板渲染在作者名下方、时间前面 (与视频下载并发)
        extra.update(
            followers_extra(await self._fetch_follower_count(video_info.owner.mid, credential))
        )

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=video_info.desc,
            author=author,
            contents=[video_content],
            extra=extra,
        )

    async def parse_dynamic_or_opus(self, dynamic_id: int):
        """解析动态或图文"""
        from bilibili_api.dynamic import Dynamic

        from .dynamic import DynamicWrapper

        dynamic = Dynamic(dynamic_id, await self.credential)
        if await dynamic.is_article():
            return await self._parse_bilibli_api_opus(dynamic.turn_to_opus())

        dynamic_info = convert(await dynamic.get_info(), DynamicWrapper).item
        return await self._parse_dynamic_info(dynamic_info)

    async def _parse_dynamic_info(self, dynamic_info: DynamicInfo):
        if dynamic_info.is_video():
            if (major := dynamic_info.modules.major) and (archive := major.archive):
                result = await self.parse_video(bvid=archive.bvid)
                result.text = dynamic_info.text
                result.extra["content_type"] = "动态"
                if stats := dynamic_info.stats_panel:
                    result.extra["stats"] = stats
                return result

        # 下载图片
        author = self.create_author(dynamic_info.name, dynamic_info.avatar)
        contents: list[MediaContent] = []
        contents.extend(self.create_images(dynamic_info.image_urls))

        repost = None
        if dynamic_info.type == "DYNAMIC_TYPE_FORWARD" and dynamic_info.orig is not None:
            repost = await self._parse_dynamic_info(dynamic_info.orig)

        extra: dict[str, Any] = {"content_type": "动态"}
        if stats := dynamic_info.stats_panel:
            extra["stats"] = stats
        # 粉丝数, 模板渲染在作者名下方、时间前面
        extra.update(followers_extra(await self._fetch_follower_count(dynamic_info.mid)))

        return self.result(
            title=dynamic_info.title,
            text=dynamic_info.text,
            timestamp=dynamic_info.timestamp,
            author=author,
            contents=contents,
            repost=repost,
            extra=extra,
        )

    async def parse_opus_by_id(self, opus_id: int):
        """解析图文动态(opus id)"""
        opus = Opus(opus_id, await self.credential)
        return await self._parse_bilibli_api_opus(opus)

    async def _parse_bilibli_api_opus(self, bili_opus: Opus):
        """解析图文动态(Opus)"""

        from .opus import OpusItem

        opus_info = await bili_opus.get_info()
        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")

        # 转换为结构体
        opus_data = convert(opus_info, OpusItem)
        logger.debug(f"opus_data: {opus_data}")
        author = self.create_author(*opus_data.name_avatar)

        # 按顺序处理图文内容
        result = self.result(
            author=author,
            title=opus_data.title,
            timestamp=opus_data.timestamp,
            # 粉丝数, 模板渲染在作者名下方、时间前面
            extra=followers_extra(await self._fetch_follower_count(opus_data.mid)),
        )

        for node in opus_data.extract_nodes():
            if isinstance(node, str):
                result.graphics.append(node)
            else:
                result.graphics.append(self.create_image(node.url, alt=node.alt))

        return result

    async def parse_live(self, room_id: int):
        """解析直播"""
        from .live import RoomData

        credential = await self.credential
        info_dict = await self._get_live_room_info(room_id, credential)

        room_data = convert(info_dict, RoomData)
        contents: list[MediaContent] = []
        # 下载封面
        if cover := room_data.cover:
            cover_task = self.downloader.download_img(cover, ext_headers=self.headers)
            contents.append(self.create_image(cover_task))

        # 下载关键帧
        if keyframe := room_data.keyframe:
            keyframe_task = self.downloader.download_img(keyframe, ext_headers=self.headers)
            contents.append(self.create_image(keyframe_task))

        author = self.create_author(room_data.name, room_data.avatar)

        url = f"https://www.bilibili.com/blackboard/live/live-activity-player.html?enterTheRoom=0&cid={room_id}"
        # 直播类型: 顶栏胶囊显示"直播"
        # (不设置时 ParseResult.content_type 会回退成"动态", 模板再显示成"图文")
        extra = {"content_type": "直播"}
        # 粉丝数, 模板渲染在作者名下方、时间前面
        extra.update(followers_extra(await self._fetch_follower_count(room_data.uid, credential)))

        return self.result(
            url=url,
            title=room_data.title,
            text=room_data.detail,
            contents=contents,
            author=author,
            extra=extra,
        )

    async def _get_live_room_info(
        self,
        room_id: int,
        credential: Credential | None = None,
    ) -> dict[str, Any]:
        """获取直播间信息, 接口被风控(-352 等)时用兜底数据源

        直播信息接口 ``getInfoByRoom`` 对请求的风控(尤其是 buvid 与 IP)比较严格,
        会返回 ``-352``(风控校验失败), 导致直播链接解析直接报错。
        这里在主接口失败后依次尝试:
        1. 直播间页面的服务端渲染数据 (与接口返回结构一致)
        2. 老版 ``Room/get_info`` + ``UserInfo/get_anchor_in_room`` 接口
        """
        from bilibili_api.live import LiveRoom

        from .live import RoomData

        room = LiveRoom(room_display_id=room_id, credential=credential)
        primary_error: Exception | None = None
        try:
            return await room.get_room_info()
        except Exception as exc:
            primary_error = exc
            logger.warning(f"获取直播间 {room_id} 信息失败({exc!r}), 尝试兜底数据源")

        # 兜底 1: 页面 SSR 数据, 结构最完整
        if info := await self._fetch_live_info_from_page(room_id):
            # 页面结构可能变化, 先确认能转成 RoomData, 不完整则继续走老接口
            try:
                convert(info, RoomData)
            except Exception:
                logger.debug(f"直播间 {room_id} 页面 SSR 数据结构不完整", exc_info=True)
            else:
                logger.info(f"直播间 {room_id} 信息已从页面 SSR 数据兜底获取")
                return info

        # 兜底 2: 老接口, 不依赖 buvid 也不经过直播信息接口的风控
        try:
            info = await self._fetch_live_info_from_legacy_api(room_id)
        except Exception as fallback_error:
            raise ParseException(
                f"获取直播间 {room_id} 信息失败"
                f"(主接口: {primary_error!r}; 兜底接口: {fallback_error!r})"
            ) from primary_error
        logger.info(f"直播间 {room_id} 信息已从老版接口兜底获取")
        return info

    def _live_fallback_client(self, room_id: int) -> httpx.AsyncClient:
        """兜底请求使用的 HTTP 客户端

        直连且不读取环境代理: B 站是境内服务, 走本机代理(如 Clash 的海外节点)
        反而容易被风控判成异常请求, 再次拿到 -352。
        """
        headers = self.headers.copy()
        headers["Referer"] = f"https://live.bilibili.com/{room_id}"
        return httpx.AsyncClient(
            headers=headers,
            timeout=self.LIVE_INFO_TIMEOUT,
            follow_redirects=True,
            trust_env=False,
        )

    async def _fetch_live_info_from_page(self, room_id: int) -> dict[str, Any] | None:
        """从直播间页面的服务端渲染数据中获取房间信息, 失败返回 None"""
        try:
            async with self._live_fallback_client(room_id) as client:
                resp = await client.get(_LIVE_PAGE_URL.format(room_id=room_id))
                resp.raise_for_status()
            info = extract_live_ssr_info(resp.text)
        except Exception:
            logger.debug(f"直播间 {room_id} 页面 SSR 数据兜底失败", exc_info=True)
            return None
        if info is None:
            logger.debug(f"直播间 {room_id} 页面未找到 SSR 房间信息")
        return info

    async def _fetch_live_info_from_legacy_api(self, room_id: int) -> dict[str, Any]:
        """老接口兜底: Room/get_info 取房间信息, UserInfo/get_anchor_in_room 取主播信息

        返回结构与 ``getInfoByRoom`` 一致, 便于上层统一用 ``RoomData`` 转换。
        """
        async with self._live_fallback_client(room_id) as client:
            room_resp = await client.get(_LIVE_ROOM_INFO_URL, params={"room_id": room_id})
            room_resp.raise_for_status()
            anchor_resp = await client.get(_LIVE_ANCHOR_INFO_URL, params={"roomid": room_id})
            anchor_resp.raise_for_status()

        room_payload: dict[str, Any] = room_resp.json()
        room_data = room_payload.get("data")
        if room_payload.get("code") != 0 or not isinstance(room_data, dict):
            raise ParseException(
                f"获取直播间 {room_id} 信息失败: {room_payload.get('message') or room_payload.get('code')}"
            )

        anchor_payload: dict[str, Any] = anchor_resp.json()
        anchor_data = anchor_payload.get("data")
        anchor = anchor_data.get("info") if isinstance(anchor_data, dict) else None
        if not isinstance(anchor, dict):
            # 主播信息拿不到不影响解析, 只记录日志
            logger.debug(
                f"获取直播间 {room_id} 主播信息失败: "
                f"{anchor_payload.get('message') or anchor_payload.get('code')}"
            )
            anchor = {}

        level = anchor.get("level")
        if not isinstance(level, int):
            level = 0

        return {
            "room_info": {
                "title": room_data.get("title") or "",
                "cover": room_data.get("user_cover") or "",
                "keyframe": room_data.get("keyframe") or "",
                "tags": room_data.get("tags") or "",
                "area_name": room_data.get("area_name") or "",
                "parent_area_name": room_data.get("parent_area_name") or "",
                "uid": room_data.get("uid"),
            },
            "anchor_info": {
                "base_info": {
                    "uname": anchor.get("uname") or "",
                    "face": anchor.get("face") or "",
                    "gender": _LIVE_GENDER_MAP.get(anchor.get("gender"), "保密"),
                },
                "live_info": {
                    "level": level,
                    "level_color": 0,
                    "score": 0,
                },
            },
        }

    async def parse_favlist(self, fav_id: int):
        """解析收藏夹"""
        from bilibili_api.favorite_list import get_video_favorite_list_content

        from .favlist import FavData

        # 只会取一页，20 个
        fav_dict = await get_video_favorite_list_content(fav_id)

        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")

        favdata = convert(fav_dict, FavData)

        author = self.create_author(favdata.info.upper.name, favdata.info.upper.face)

        graphics: list[str | ImageContent] = []
        for fav in favdata.medias:
            graphics.append(self.create_image(fav.cover, alt=fav.desc))
            graphics.append(fav.desc)

        return self.result(
            title=favdata.title,
            timestamp=favdata.timestamp,
            author=author,
            graphics=graphics,
            # 粉丝数, 模板渲染在作者名下方、时间前面
            extra=followers_extra(await self._fetch_follower_count(favdata.info.upper.mid)),
        )

    async def _fetch_follower_count(
        self,
        mid: int | None,
        credential: Credential | None = None,
    ) -> int | str | None:
        """获取 UP 主粉丝数 (取不到返回 None, 不影响解析)

        :param mid: UP 主 mid
        :param credential: 调用方已取到的凭证, 传进来可以省掉一次凭证校验请求
        """
        if not mid:
            return None

        from bilibili_api.user import User

        try:
            stat = await User(mid, credential or await self.credential).get_relation_info()
        except Exception:
            logger.debug(f"获取 B站用户 {mid} 粉丝数失败", exc_info=True)
            return None

        return stat.get("follower")

    async def extract_download_urls(
        self,
        video: Video | None = None,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_index: int = 0,
    ) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
        """解析视频下载链接

        :return: (视频线路候选, 音频线路候选), 线路按优先级排序, 失败时可依次轮换
        """

        from bilibili_api.video import (
            AudioStreamDownloadURL,
            VideoStreamDownloadURL,
            VideoDownloadURLDataDetecter,
        )

        if video is None:
            video = Video(bvid=bvid, aid=avid, credential=await self.credential)

        # 获取下载数据
        download_url_data = await video.get_download_url(page_index=page_index)
        detecter = VideoDownloadURLDataDetecter(download_url_data)
        streams = detecter.detect_best_streams(
            video_max_quality=pconfig.bili_video_quality,
            codecs=pconfig.bili_video_codes,
            no_dolby_video=True,
            no_hdr=True,
        )
        video_stream = streams[0]
        if not isinstance(video_stream, VideoStreamDownloadURL):
            raise DownloadException("未找到可下载的视频流")
        logger.debug(f"视频流质量: {video_stream.video_quality.name}, 编码: {video_stream.video_codecs}")
        video_urls = order_download_urls(video_stream.url, video_stream.backup_url)

        audio_stream = streams[1]
        if not isinstance(audio_stream, AudioStreamDownloadURL):
            return video_urls, None
        logger.debug(f"音频流质量: {audio_stream.audio_quality.name}")
        audio_urls = order_download_urls(audio_stream.url, audio_stream.backup_url)
        return video_urls, audio_urls

    def _save_credential(self):
        """存储哔哩哔哩登录凭证"""
        if self._credential is None:
            return

        self._cookies_file.write_text(json.dumps(self._credential.get_cookies()))

    def _load_credential(self):
        """从文件加载哔哩哔哩登录凭证"""
        if not self._cookies_file.exists():
            return

        self._credential = Credential.from_cookies(json.loads(self._cookies_file.read_text()))

    async def login_with_qrcode(self) -> bytes:
        """通过二维码登录获取哔哩哔哩登录凭证"""
        self._qr_login = QrCodeLogin()
        await self._qr_login.generate_qrcode()

        qr_pic = self._qr_login.get_qrcode_picture()
        return qr_pic.content

    async def check_qr_state(self) -> AsyncGenerator[str]:
        """检查二维码登录状态"""
        scan_tip_pending = True

        for _ in range(30):
            state = await self._qr_login.check_state()
            match state:
                case QrCodeLoginEvents.DONE:
                    yield "登录成功"
                    self._credential = self._qr_login.get_credential()
                    self._save_credential()
                    break
                case QrCodeLoginEvents.CONF:
                    if scan_tip_pending:
                        yield "二维码已扫描, 请确认登录"
                        scan_tip_pending = False
                case QrCodeLoginEvents.TIMEOUT:
                    yield "二维码过期, 请重新生成"
                    break
            await asyncio.sleep(2)
        else:
            yield "二维码登录超时, 请重新生成"

    async def _init_credential(self):
        """初始化哔哩哔哩登录凭证"""
        if pconfig.bili_ck is None:
            self._load_credential()
            return

        credential = Credential.from_cookies(ck2dict(pconfig.bili_ck))
        if await credential.check_valid():
            logger.info(f"`parser_bili_ck` 有效, 保存到 {self._cookies_file}")
            self._credential = credential
            self._save_credential()
        else:
            logger.info(f"`parser_bili_ck` 已过期, 尝试从 {self._cookies_file} 加载")
            self._load_credential()

    @property
    async def credential(self) -> Credential | None:
        """哔哩哔哩登录凭证"""

        if self._credential is None:
            await self._init_credential()
            return self._credential

        if not await self._credential.check_valid():
            logger.warning("哔哩哔哩凭证已过期, 请重新配置")
            return None

        if await self._credential.check_refresh():
            logger.info("哔哩哔哩凭证需要刷新")
            if self._credential.has_ac_time_value() and self._credential.has_bili_jct():
                await self._credential.refresh()
                logger.info(f"哔哩哔哩凭证刷新成功, 保存到 {self._cookies_file}")
                self._save_credential()
            else:
                logger.warning("哔哩哔哩凭证刷新需要包含 `SESSDATA`, `ac_time_value` 项")

        return self._credential
