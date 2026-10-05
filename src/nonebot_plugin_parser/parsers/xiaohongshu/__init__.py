import re
import json
from time import monotonic
from typing import Any, ClassVar

from httpx import Cookies, AsyncClient
from nonebot import logger

from ..base import Platform, BaseParser, PlatformEnum, ParseException, handle, pconfig
from ..utils import followers_extra


PROFILE_RETRY_COOLDOWN: float = 5 * 60
"""用户主页被风控/请求失败后的冷却时间(秒), 避免连续解析反复请求触发限流"""

_TOPIC_MARKER = re.compile(r"[ \t]*(?:【话题】|\[话题\])")
_INITIAL_STATE_TOKEN = re.compile(
    r'(?P<string>"(?:[^"\\]|\\.)*")'
    r'|(?P<undefined>\bundefined\b)'
    r'|\bnew\s+Map\s*\(\s*\[\s*\]\s*\)'
)


def _clean_description(text: str) -> str:
    """移除小红书简介中的话题标记, 保留话题名称本身。"""
    return _TOPIC_MARKER.sub("", text).rstrip()


def _extract_profile(raw_state: str) -> dict[str, str]:
    """从 H5 用户主页的 INITIAL_STATE 里取作者信息, 取不到返回空字典.

    H5 用户主页把作者信息放在 ``profile.userInfo``:
    ``{"follows": "31", "fans": "240", "ipLocation": "上海", ...}``;
    未登录时粉丝数是占位符 ``"-"``, 这种情况按取不到处理。
    """
    try:
        data = json.loads(raw_state)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}

    user_info = (data.get("profile") or {}).get("userInfo") or {}
    profile: dict[str, str] = {}

    fans = user_info.get("fans")
    if isinstance(fans, int):
        profile["fans"] = str(fans)
    elif isinstance(fans, str) and (text := fans.strip()) not in ("", "-"):
        profile["fans"] = text

    if isinstance(region := user_info.get("ipLocation"), str) and (text := region.strip()):
        profile["region"] = text

    return profile


class XiaoHongShuParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.XIAOHONGSHU, display_name="小红书")

    def __init__(self):
        super().__init__()
        explore_headers = {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            )
        }
        self.headers.update(explore_headers)

        discovery_headers = {
            "origin": "https://www.xiaohongshu.com",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        self.ios_headers.update(discovery_headers)

        if pconfig.xhs_ck:
            self.headers["cookie"] = pconfig.xhs_ck
            self.ios_headers["cookie"] = pconfig.xhs_ck

        self._profile_retry_after: float = 0.0
        """用户主页请求的冷却截止时间(monotonic 秒)"""

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+")
    @handle("xhslink.cn", r"xhslink\.cn/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url, self.ios_headers)

    # https://www.xiaohongshu.com/explore/68feefe40000000007030c4a?xsec_token=ABjAKjfMHJ7ck4UjPlugzVqMb35utHMRe_vrgGJ2AwJnc=&xsec_source=pc_feed
    # https://www.xiaohongshu.com/discovery/item/68e8e3fa00000000030342ec?app_platform=android&ignoreEngage=true&app_version=9.6.0&share_from_user_hidden=true&xsec_source=app_share&type=normal&xsec_token=CBW9rwIV2qhcCD-JsQAOSHd2tTW9jXAtzqlgVXp6c52Sw%3D&author_share=1&xhsshare=QQ&shareRedId=ODs3RUk5ND42NzUyOTgwNjY3OTo8S0tK&apptime=1761372823&share_id=3b61945239ac403db86bea84a4f15124&share_channel=qq
    @handle("xiaohongshu.com", r"(explore|discovery/item)/(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)\?[A-Za-z0-9._%&+=/#@-]+)")
    async def _parse_common(self, searched: re.Match[str]):
        xhs_domain = "https://www.xiaohongshu.com"
        query, xhs_id = searched.group("query", "xhs_id")
        explore_url = f"{xhs_domain}/explore/{query}"

        try:
            return await self.parse_explore(explore_url, xhs_id)
        except Exception as e:
            if "cookie" in self.headers:
                logger.warning(f"parse_explore with cookie failed, error: {e}, retry without cookie")
                anonymous_headers = {key: value for key, value in self.headers.items() if key.lower() != "cookie"}
                try:
                    return await self.parse_explore(explore_url, xhs_id, headers=anonymous_headers)
                except Exception as anonymous_error:
                    logger.warning(
                        f"parse_explore without cookie failed, error: {anonymous_error}, fallback to parse_discovery"
                    )
            else:
                logger.warning(f"parse_explore failed, error: {e}, fallback to parse_discovery")

        return await self.parse_discovery(f"{xhs_domain}/discovery/item/{query}")

    async def parse_explore(self, url: str, xhs_id: str, headers: dict[str, str] | None = None):
        from . import explore

        request_headers = self.headers if headers is None else headers
        async with AsyncClient(headers=request_headers, timeout=self.timeout) as client:
            response = await client.get(url)
            # may be 302
            if response.status_code > 400:
                response.raise_for_status()

        html = response.text
        raw = self._extract_initial_state_raw(html)

        # Decode the JSON into InitialState struct
        init_state = explore.decoder.decode(raw)

        # Access: ["note"]["noteDetailMap"][xhs_id]["note"]
        note_detail_wrapper = init_state.note.noteDetailMap.get(xhs_id)
        if not note_detail_wrapper:
            raise ParseException(f"can't find note detail for xhs_id: {xhs_id}")

        note_detail = note_detail_wrapper.note
        video_info = note_detail.video_cover_duration if note_detail.is_video else None

        author = self.create_author(note_detail.nickname, note_detail.avatar_url)

        extra = {}
        if stats := note_detail.stats_panel:
            extra["stats"] = stats
        # 粉丝数 + IP 属地 (笔记里缺的从作者主页补), 模板渲染在作者名下方、时间前面
        extra.update(await self._author_extra(note_detail.user, note_detail.ip_location))

        result = self.result(
            author=author,
            title=note_detail.title,
            text=_clean_description(note_detail.desc),
            timestamp=note_detail.timestamp,
            extra=extra,
        )

        # 添加视频内容
        if video_info is not None:
            result.video = self.create_video(*video_info)

        # 添加图片内容(实况图同时发视频, 走合并转发)
        elif note_detail.imageList:
            has_live = False
            for image in note_detail.imageList:
                if live_url := image.live_video_url:
                    result.contents.append(self.create_video(live_url, image.urlDefault))
                    has_live = True
                result.contents.append(self.create_image(image.urlDefault))
            if has_live:
                # 视频进合并转发, 且网格图跳过与图片重复的视频封面
                result.extra["merge_videos"] = True
                result.extra["live_photos"] = True

        return result

    async def parse_discovery(self, url: str):
        from . import discovery

        async with AsyncClient(
            headers=self.ios_headers,
            timeout=self.timeout,
            follow_redirects=True,
            cookies=Cookies(),
            trust_env=False,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        raw = self._extract_initial_state_raw(html)
        init_state = discovery.decoder.decode(raw)
        note_data = init_state.noteData.data.noteData
        preload_data = init_state.noteData.normalNotePreloadData
        video_info = note_data.url_and_duration if note_data.is_video else None

        author = self.create_author(note_data.user.nickName, note_data.user.avatar)

        extra = {}
        # 粉丝数 + IP 属地 (笔记里缺的从作者主页补), 模板渲染在作者名下方、时间前面
        extra.update(await self._author_extra(note_data.user, note_data.ip_location))

        result = self.result(
            author=author,
            title=note_data.title,
            text=_clean_description(note_data.desc),
            timestamp=note_data.timestamp,
            extra=extra,
        )

        if video_info is not None:
            video_url, duration = video_info

            if preload_data:
                cover_url = preload_data.image_urls[0]
            else:
                cover_url = note_data.image_urls[0]

            result.video = self.create_video(
                video_url,
                cover_url,
                duration,
            )
        elif note_data.imageList:
            has_live = False
            for image in note_data.imageList:
                image_url = image.download_url
                if live_url := image.live_video_url:
                    result.contents.append(self.create_video(live_url, image_url))
                    has_live = True
                result.contents.append(self.create_image(image_url))
            if has_live:
                # 视频进合并转发, 且网格图跳过与图片重复的视频封面
                result.extra["merge_videos"] = True
                result.extra["live_photos"] = True

        return result

    async def _author_extra(self, user: Any, region: str | None = None) -> dict[str, str]:
        """作者相关 extra: 粉丝数 + IP 属地.

        `region` 是笔记自带的 IP 属地(只有 PC 笔记页面会给, H5 页面没有);
        笔记里缺的项从作者主页补齐 —— 主页 H5 页面的 `profile.userInfo` 同时带
        `fans` 和 `ipLocation`, 一次请求就够; 取不到的项目不写, 模板不渲染。
        """
        extra: dict[str, str] = {}
        if region:
            extra["region"] = region
        extra.update(followers_extra(user.fans))

        # 粉丝数和属地都有, 或者拿不到 userId: 不需要再请求主页
        if ("subscribers" in extra and region) or not user.userId:
            return extra

        profile = await self._fetch_profile(user.userId)
        if "subscribers" not in extra:
            extra.update(followers_extra(profile.get("fans")))
        if not region and (profile_region := profile.get("region")):
            extra["region"] = profile_region
        return extra

    async def _fetch_profile(self, user_id: str) -> dict[str, str]:
        """从用户主页 H5 页面取作者信息 (粉丝数 / IP 属地), 取不到返回空字典

        PC 主页要求登录, H5 主页不需要登录态, 但必须带上 `xsec_source` 参数,
        否则会被重定向到登录页; 撞上风控(跳验证码页)时冷却一段时间再试,
        避免连续解析时反复请求。
        """
        if monotonic() < self._profile_retry_after:
            return {}

        url = f"https://www.xiaohongshu.com/user/profile/{user_id}?xsec_source=app_share"
        try:
            async with AsyncClient(
                headers=self.ios_headers,
                timeout=5,
                follow_redirects=True,
                cookies=Cookies(),
                trust_env=False,
            ) as client:
                response = await client.get(url)
                # 登录/验证码跳转页没有 INITIAL_STATE, 解析会抛 ParseException
                raw_state = (
                    self._extract_initial_state_raw(response.text)
                    if response.status_code == 200
                    else ""
                )
        except Exception:
            logger.debug(f"获取小红书用户 {user_id} 主页信息失败", exc_info=True)
            self._profile_retry_after = monotonic() + PROFILE_RETRY_COOLDOWN
            return {}

        if not raw_state:
            self._profile_retry_after = monotonic() + PROFILE_RETRY_COOLDOWN
            return {}

        return _extract_profile(raw_state)

    def _extract_initial_state_raw(self, html: str) -> str:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html)
        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")

        # 登录态页面包含空 Map；只转换字符串外的 JS 值，保留正文和 URL。
        return _INITIAL_STATE_TOKEN.sub(
            lambda token: token.group("string")
            if token.group("string") is not None
            else "null" if token.group("undefined") is not None else "{}",
            matched.group(1),
        )
