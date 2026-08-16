import re
from typing import Any, ClassVar

from httpx import AsyncClient
from msgspec import convert
from nonebot import logger

from ..base import Platform, BaseParser, PlatformEnum, ParseException, handle, pconfig
from ..utils import fmt_stat
from .model import BaseResult, is_image_url
from .encrypt import build_url


class HeyBoxParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.HEYBOX, display_name="小黑盒")

    def __init__(self):
        super().__init__()
        # self.headers 会被 create_image/create_author 下载媒体时复用,
        # 只放下载图片 CDN(imgheybox.max-c.com)安全的 Referer, 否则触发防盗链 418
        self.headers["Referer"] = "https://www.xiaoheihe.cn/"
        # API 专用头(含 Host/Origin), 仅请求 api.xiaoheihe.cn 时使用, 不能污染下载头
        self.api_headers: dict[str, str] = {
            **self.headers,
            "Origin": "https://www.xiaoheihe.cn",
            "Accept": "application/json, text/plain, */*",
        }
        self.device_path = pconfig.config_dir / "heybox_device.txt"
        self.device_id: str = ""

    def _apply_user_agent(self, ua: str) -> None:
        """让 API 请求的 UA 与取 token 时浏览器的 UA 一致, 降低指纹不匹配被风控概率。

        device_id 是浏览器指纹脚本基于其 UA 生成的, httpx 请求若用不同 UA,
        小黑盒服务端更容易判定为异常并返回 show_captcha。
        """
        if ua:
            self.api_headers["User-Agent"] = ua

    async def ensure_token(self) -> None:
        """确保拿到设备 id(鉴权 cookie)与配套 UA, 优先读缓存, 否则用浏览器获取并落盘。

        缓存文件两行: 第一行 device_id, 第二行取 token 时浏览器的 UA。
        """
        if self.device_id:
            return
        if self.device_path.exists():
            lines = self.device_path.read_text(encoding="utf-8").splitlines()
            self.device_id = lines[0].strip() if lines else ""
            if self.device_id:
                if len(lines) > 1:
                    self._apply_user_agent(lines[1].strip())
                return

        from ...browser import BrowserManager

        device_id, ua = await BrowserManager.get_heybox_device_id()
        self.device_id = device_id.strip()
        if not self.device_id:
            raise ParseException("获取小黑盒设备 id 失败")
        self._apply_user_agent(ua.strip())
        logger.info(f"成功获取到小黑盒 tokenid: {self.device_id[:5]}...")
        self.device_path.write_text(f"{self.device_id}\n{ua.strip()}", encoding="utf-8")

    # https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=xxxx
    @handle("api.xiaoheihe.cn", r"api\.xiaoheihe\.cn/[^\s]*?link_id=(?P<link_id>[A-Za-z0-9]+)")
    # https://xiaoheihe.cn/bbs/post_share?link_id=xxxx
    @handle("xiaoheihe.cn/bbs/post_share", r"post_share\?[^\s]*?link_id=(?P<link_id>[A-Za-z0-9]+)")
    # https://xiaoheihe.cn/app/bbs/link/xxxx
    @handle("xiaoheihe.cn/app/bbs", r"link/(?P<link_id>[A-Za-z0-9]+)")
    async def _parse(self, searched: re.Match[str]):
        link_id = searched.group("link_id")
        await self.ensure_token()

        async with AsyncClient(headers=self.api_headers, timeout=self.timeout, verify=False) as client:
            response = await client.get(
                build_url(link_id),
                cookies={"x_xhh_tokenid": self.device_id},
            )
            response.raise_for_status()
            res = response.json()

        if res.get("status") != "ok":
            raise ParseException(f"小黑盒解析失败: {res}")

        data = convert(res["result"], BaseResult)
        link = data.link

        if link.user is None:
            raise ParseException("小黑盒解析失败: 缺少作者信息")

        author = self.create_author(
            name=link.user.username,
            avatar_url=link.user.avatar_url,
        )

        extra: dict[str, Any] = {
            "stats": [
                {"icon": "like", "value": fmt_stat(link.link_award_num), "label": "点赞"},
                {"icon": "star", "value": fmt_stat(link.favour_count), "label": "收藏"},
                {"icon": "comment", "value": fmt_stat(link.comment_num), "label": "评论"},
                {"icon": "share", "value": fmt_stat(link.forward_num), "label": "转发"},
                {"icon": "eye", "value": fmt_stat(link.click), "label": "浏览"},
            ]
        }

        result = self.result(
            title=link.title,
            timestamp=link.create_at,
            url=f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}",
            author=author,
            extra=extra,
        )

        # 有视频: 走 contents(视频卡片); 正文作为 text
        if link.has_video and link.video_url:
            result.text = link.description or link.text
            result.video = self.create_video(link.video_url, link.video_thumb)
            return result

        # 无视频: 图文, 文本段与图片交错放进 graphics
        for part in link.graphics:
            if is_image_url(part):
                result.graphics.append(self.create_image(part))
            else:
                result.graphics.append(part)

        # graphics 为空时用纯文本兜底
        if not result.graphics:
            result.text = link.description or link.text

        return result
