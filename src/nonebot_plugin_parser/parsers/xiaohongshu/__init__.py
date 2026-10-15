import re
from asyncio import sleep
from typing import ClassVar

from httpx import Cookies, AsyncClient
from msgspec import DecodeError, ValidationError
from nonebot import logger

from ..base import Platform, BaseParser, PlatformEnum, ParseException, handle, pconfig


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


class XiaoHongShuParser(BaseParser):
    """小红书解析

    两条可用路径, 按登录态选择:

    - PC ``explore`` 页面(``parse_explore``): 需要**有效的登录 cookie**, 且令牌要与
      来源同源(pc_feed); 配了 ``parser_xhs_ck`` 时优先走它
    - QQ 内置浏览器 UA + H5 分享页(``parse_share``): 免登录态, 实测 pc_share /
      pc_feed / app_share 各种来源的令牌都能出数据, 作为主力兜底

    风控会间歇性返回验证码页(瞬时), 分享页在同一路径上重试一次即可。
    """

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.XIAOHONGSHU, display_name="小红书")

    def __init__(self):
        super().__init__()
        # PC 的 explore 页面按浏览器页面请求
        self.headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                    "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                )
            }
        )

        # 只有 PC 页面和媒体下载会带登录态;
        # H5 分享页一律不带 cookie —— 实测带失效 cookie 会被直接打成验证码页
        if pconfig.xhs_ck:
            self.headers["cookie"] = pconfig.xhs_ck

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
        xhs_domain = "https://www.xiaohongshu.com"
        query, xhs_id = searched.group("query", "xhs_id")
        explore_url = f"{xhs_domain}/explore/{query}"

        matched = _XSEC_TOKEN.search(query)
        xsec_token = matched.group("token") if matched else None

        # 1) 配了 cookie: 优先 PC 页面 —— 生效时能带出笔记自带的 IP 属地
        if pconfig.xhs_ck:
            try:
                return await self.parse_explore(explore_url, xhs_id)
            except Exception as e:
                logger.warning(f"parse_explore with cookie failed, error: {e}, fallback to parse_share")

        # 2) QQ 分享页: 免登录态也不消耗 cookie, 各来源的令牌都能用; 没有 token 时走不通
        if xsec_token:
            try:
                return await self.parse_share(xhs_id, xsec_token)
            except Exception as e:
                logger.warning(f"parse_share (小红书分享页) failed, error: {e}, fallback to parse_explore")

        # 3) 最后再试一次匿名 PC 页面(没配 cookie 时的随机兜底)
        try:
            return await self.parse_explore(explore_url, xhs_id)
        except Exception as e:
            if not xsec_token:
                raise ParseException("小红书链接缺少 xsec_token, 请重新分享一次") from e
            raise

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
