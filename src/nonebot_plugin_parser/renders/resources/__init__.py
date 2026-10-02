import random
from pathlib import Path

RESOURCES_DIR = Path(__file__).parent
"""默认资源目录"""
DEFAULT_FONT_PATH = RESOURCES_DIR / "HYSongYunLangHeiW.ttf"
"""默认字体文件路径"""
DEFAULT_AVATAR_PATH = RESOURCES_DIR / "avatar.png"
"""默认头像文件路径"""
DEFAULT_VIDEO_BUTTON_PATH = RESOURCES_DIR / "play.png"
"""默认视频播放按钮文件路径"""
GROK_ICON_PATH = RESOURCES_DIR / "grok.png"
"""Grok 翻译标签图标 (黑色透明底, 夜间主题需反色)"""
FALLBACK_PIC_DIR = RESOURCES_DIR / "fallback_pic"
"""下载失败显示的图片文件路径"""


def _normalize_logo_name(name: str) -> str:
    """文件名归一化: 忽略大小写与 _ - 空格 差异"""
    return name.replace("_", "").replace("-", "").replace(" ", "").casefold()


def find_platform_logo(platform_name: str) -> Path | None:
    """查找平台 logo

    平台名以 PlatformEnum 的值为准(如 apple_music), 但资源文件名常按品牌写法命名
    (如 AppleMusic.png), 因此按"忽略大小写与分隔符"匹配, 避免素材放对目录却加载不到。
    """
    target = _normalize_logo_name(platform_name)
    for path in sorted(RESOURCES_DIR.glob("*.png")):
        if _normalize_logo_name(path.stem) == target:
            return path

    # 兜底: 精确同名文件(含非 png 扩展名的历史素材)
    for path in sorted(RESOURCES_DIR.glob(f"{platform_name}.*")):
        if path.is_file():
            return path
    return None


def random_fallback_pic() -> Path:
    return FALLBACK_PIC_DIR / f"{random.randint(1, 9)}.jpg"
