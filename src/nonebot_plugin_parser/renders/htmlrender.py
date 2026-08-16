from typing_extensions import override
from datetime import datetime, timedelta, timezone

from nonebot import get_driver, require

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import template_to_html
from nonebot_plugin_htmlrender.browser import get_new_page

from . import resources
from .base import ImageRenderer, pconfig

CHINA_TIMEZONE = timezone(timedelta(hours=8))


def get_auto_theme(hour: int | None = None) -> str:
    """北京时间 19:00-06:59 使用夜间主题。"""
    current_hour = datetime.now(CHINA_TIMEZONE).hour if hour is None else hour
    return "dark" if current_hour >= 19 or current_hour < 7 else "light"


def get_footer_brand() -> str:
    """页脚署名: 取 NoneBot 配置的 NICKNAME 第一个昵称拼接 '<昵称>解析'; 无昵称则仅 '解析'."""
    try:
        nicknames = get_driver().config.nickname
    except Exception:
        return "解析"
    nickname = next((n.strip() for n in nicknames if n and n.strip()), "")
    return f"{nickname}解析" if nickname else "解析"


class HtmlRenderer(ImageRenderer):
    """HTML 渲染器"""

    @override
    async def render_image(self) -> bytes:
        # await self.result.ensure_downloads_complete(img_only=True)

        logo = resources.RESOURCES_DIR / f"{self.result.platform.name}.png"
        logo = logo.as_uri() if logo.exists() else None

        font = pconfig.custom_font or resources.DEFAULT_FONT_PATH
        font = font.as_uri() if font.exists() else None

        html = await template_to_html(
            template_path=str(self.templates_dir),
            template_name="card.html.jinja2",
            logo=logo,
            font=font,
            result=self.result,
            font_weight=pconfig.custom_font_weight,
            fallback_pic=resources.random_fallback_pic().as_uri(),
            play_button=resources.DEFAULT_VIDEO_BUTTON_PATH.as_uri(),
            default_avatar=resources.DEFAULT_AVATAR_PATH.as_uri(),
            theme=get_auto_theme(),
            footer_brand=get_footer_brand(),
        )

        async with get_new_page(2, viewport={"width": 800, "height": 100}) as page:
            await page.goto(f"file://{self.templates_dir}")
            await page.set_content(html, wait_until="networkidle")
            return await page.screenshot(
                full_page=True,
                type="png",
                omit_background=True,
            )
