import re
import json
from asyncio import sleep
from time import monotonic
from typing import Any, ClassVar

from httpx import Cookies, AsyncClient
from msgspec import DecodeError, ValidationError
from nonebot import logger

from ..base import Platform, BaseParser, PlatformEnum, ParseException, handle, pconfig
from ..utils import followers_extra


PROFILE_RETRY_COOLDOWN: float = 5 * 60
"""用户主页被风控/请求失败后的冷却时间(秒), 避免连续解析反复请求触发限流"""

SHARE_RETRY_TIMES: int = 2
"""分享页最多请求次数: 风控会间歇性返回验证码页, 隔一会儿重试通常就能拿到数据"""

SHARE_RETRY_DELAY: float = 1.0
"""分享页重试间隔(秒), 第 n 次重试等待 n 倍"""

_TOPIC_MARKER = re.compile(r"[ \t]*(?:【话题】|\[话题\])")
_INITIAL_STATE_TOKEN = re.compile(
    r'(?P<string>"(?:[^"\\]|\\.)*")'
    r'|(?P<undefined>\bundefined\b)'
    r'|\bnew\s+Map\s*\(\s*\[\s*\]\s*\)'
)
_XSEC_TOKEN = re.compile(r"[?&]xsec_token=(?P<token>[^&\s]+)")
"""笔记链接里的分享令牌: 分享页必须携带, 缺失时页面里没有笔记数据"""


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
    """小红书解析

    唯一可用路径是「QQ 内置浏览器 UA + H5 分享页」(见 ``parse_share``):
    PC 的 ``explore`` 页面和 iPhone UA 请求的 H5 页面实测都取不到笔记数据
    (页面里没有该笔记, 或被 302 到登录/验证码页), 因此不再保留这些兜底路径,
    改为在唯一路径上重试, 应对间歇性的验证码页风控。
    """

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.XIAOHONGSHU, display_name="小红书")

    def __init__(self):
        super().__init__()
        # 只有作者主页(补粉丝数/属地)和媒体下载会带登录态; 分享页一律不带 cookie,
        # 实测带失效 cookie 请求会被直接打成验证码页
        if pconfig.xhs_ck:
            self.headers["cookie"] = pconfig.xhs_ck
            self.ios_headers["cookie"] = pconfig.xhs_ck

        self._profile_retry_after: float = 0.0
        """用户主页请求的冷却截止时间(monotonic 秒)"""

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+")
    @handle("xhslink.cn", r"xhslink\.cn/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        # 短链跳转同样用 QQ UA, 落地页才会是分享页而不是登录页
        return await self.parse_with_redirect(url, self.qq_headers)

    # https://www.xiaohongshu.com/explore/68feefe40000000007030c4a?xsec_token=ABjAKjfMHJ7ck4UjPlugzVqMb35utHMRe_vrgGJ2AwJnc=&xsec_source=pc_feed
    # https://www.xiaohongshu.com/discovery/item/68e8e3fa00000000030342ec?app_platform=android&ignoreEngage=true&app_version=9.6.0&share_from_user_hidden=true&xsec_source=app_share&type=normal&xsec_token=CBW9rwIV2qhcCD-JsQAOSHd2tTW9jXAtzqlgVXp6c52Sw%3D&author_share=1&xhsshare=QQ&shareRedId=ODs3RUk5ND42NzUyOTgwNjY3OTo8S0tK&apptime=1761372823&share_id=3b61945239ac403db86bea84a4f15124&share_channel=qq
    @handle("xiaohongshu.com", r"(explore|discovery/item)/(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)\?[A-Za-z0-9._%&+=/#@-]+)")
    async def _parse_common(self, searched: re.Match[str]):
        query, xhs_id = searched.group("query", "xhs_id")

        # 分享页必须带 xsec_token(实测不带 token 的页面里没有笔记数据), 没有就直接报错
        matched = _XSEC_TOKEN.search(query)
        if not matched:
            raise ParseException("小红书链接缺少 xsec_token, 请重新分享一次")

        return await self.parse_share(xhs_id, matched.group("token"))

    async def parse_share(self, xhs_id: str, xsec_token: str):
        """QQ 内置浏览器 UA 请求 H5 分享页 (无需登录态 / cookie)

        小红书对"在 QQ 里点开分享链接"的落地页放行, 返回带完整 __INITIAL_STATE__
        的 HTML; 实测 token 与 ``xsec_source`` 是否同源无关, 统一用 pc_share 即可。
        """
        from . import discovery

        url = (
            f"https://www.xiaohongshu.com/discovery/item/{xhs_id}"
            # token 可能已经是百分号编码的, 原样拼接避免二次编码
            f"?xsec_token={xsec_token}&xsec_source=pc_share"
        )
        raw = await self._fetch_initial_state(url)
        try:
            init_state = discovery.decoder.decode(raw)
            note_data = init_state.noteData.data.noteData
        except (DecodeError, ValidationError) as e:
            # 失效/被删的笔记会被 302 到 404 提示页, 结构里没有 noteData
            raise ParseException("小红书分享链接失效或内容已删除") from e
        preload_data = init_state.noteData.normalNotePreloadData
        video_info = note_data.url_and_duration if note_data.is_video else None

        author = self.create_author(note_data.user.nickName, note_data.user.avatar)

        extra = {}
        if stats := note_data.stats_panel:
            extra["stats"] = stats
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

    async def _fetch_initial_state(self, url: str) -> str:
        """请求分享页并取出 __INITIAL_STATE__

        风控会间歇性把匿名请求打成验证码页(没有 __INITIAL_STATE__, 约 10KB),
        这种是瞬时的, 隔一会儿重试通常就能拿到真正的分享页 —— 实测 PC explore
        和 iPhone UA 的 H5 兜底路径此时也取不到数据, 所以只重试这一条路径。
        """
        error: Exception = ParseException("小红书分享页请求失败")

        for attempt in range(SHARE_RETRY_TIMES):
            if attempt:
                await sleep(SHARE_RETRY_DELAY * attempt)

            html = await self._fetch_html(url, self.qq_headers)
            try:
                return self._extract_initial_state_raw(html)
            except ParseException as e:
                error = e
                logger.warning(
                    f"小红书分享页第 {attempt + 1}/{SHARE_RETRY_TIMES} 次未取到数据, 稍后重试"
                )

        raise ParseException("小红书返回验证码页(可能触发风控), 请稍后重试") from error

    async def _fetch_html(self, url: str, headers: dict[str, str]) -> str:
        """请求笔记页面 HTML (独立 cookie jar, 不读环境代理)"""
        async with AsyncClient(
            headers=headers,
            timeout=self.timeout,
            follow_redirects=True,
            cookies=Cookies(),
            trust_env=False,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _author_extra(self, user: Any, region: str | None = None) -> dict[str, str]:
        """作者相关 extra: 粉丝数 + IP 属地.

        H5 分享页不给 IP 属地, 所以基本都靠作者主页 H5 页面的 `profile.userInfo`
        (同时带 `fans` 和 `ipLocation`, 一次请求就够); 取不到的项目不写, 模板不渲染。
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
