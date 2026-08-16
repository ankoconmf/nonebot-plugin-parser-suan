import re
from html import unescape
from typing import ClassVar

from httpx import AsyncClient
from nonebot import logger

from .base import BaseParser, PlatformEnum, handle
from .data import Platform
from ..config import pconfig
from ..exception import ParseException


class BoothParser(BaseParser):
    """BOOTH (booth.pm) 商品解析器"""

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.BOOTH, display_name="BOOTH")

    @handle(
        "booth.pm",
        r"(?:https?://)?(?:[\w-]+\.)?booth\.pm/(?:[a-z]{2}(?:-[a-z]{2})?/)?items/(\d+)",
    )
    async def _parse(self, searched: re.Match[str]):
        """解析 BOOTH 商品页面"""
        url = searched.group(0)
        item_id = searched.group(1)
        
        # 添加 https:// 前缀如果没有的话
        if not url.startswith("http"):
            url = f"https://{url}"

        try:
            # 获取代理配置
            proxy = pconfig.proxy
            
            # 设置自定义 headers，BOOTH 可能需要正确的 User-Agent
            headers = self.headers.copy()
            headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.9",
            })
            
            # 获取页面内容
            async with AsyncClient(
                headers=headers, 
                timeout=20.0,  # 增加到 20 秒
                verify=False,
                proxy=proxy,
                follow_redirects=True
            ) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise ParseException(f"获取页面失败: HTTP {resp.status_code}")
                
                html = resp.text

            # 解析页面信息
            # 商品标题 - 优先使用 og:title
            title = None
            og_title_match = re.search(r'<meta property="og:title" content="([^"]*)"', html)
            if og_title_match:
                title = og_title_match.group(1).strip()
            
            if not title:
                title_match = re.search(r'<title>([^<]+)</title>', html)
                title = title_match.group(1).strip() if title_match else f"BOOTH 商品 {item_id}"
            
            # 商品描述 - 从多个可能的位置提取
            description = ""
            
            # 首先尝试从 js-market-item-detail-description 中提取（主要描述容器）
            desc_match = re.search(
                r'<div[^>]*js-market-item-detail-description[^>]*>(.*?)</div>',
                html,
                re.DOTALL
            )
            
            if not desc_match:
                # 尝试从 section.deco-text 中提取
                desc_match = re.search(
                    r'<section[^>]*deco-text[^>]*>(.*?)</section>',
                    html,
                    re.DOTALL
                )
            
            if not desc_match:
                # 尝试从 div 的 deco-text 中提取
                desc_match = re.search(
                    r'<div[^>]*deco-text[^>]*>(.*?)</div>',
                    html,
                    re.DOTALL
                )
            
            if desc_match:
                desc_html = desc_match.group(1)
                
                # 先保留一些关键的 block 元素之间的内容
                # 将 <br> 和 <br/> 转换为换行符
                desc_html = re.sub(r'<br\s*/?>', '\n', desc_html, flags=re.IGNORECASE)
                # 处理段落 - </p> 之后通常是新内容
                desc_html = re.sub(r'</p>\s*', '\n\n', desc_html, flags=re.IGNORECASE)
                desc_html = re.sub(r'\s*<p[^>]*>', '', desc_html, flags=re.IGNORECASE)
                # 处理 div
                desc_html = re.sub(r'</div>\s*<div[^>]*>', '\n', desc_html, flags=re.IGNORECASE)
                # 处理列表
                desc_html = re.sub(r'</li>\s*<li[^>]*>', '\n', desc_html, flags=re.IGNORECASE)
                desc_html = re.sub(r'<li[^>]*>', '', desc_html, flags=re.IGNORECASE)
                desc_html = re.sub(r'</li>', '\n', desc_html, flags=re.IGNORECASE)
                
                # 移除 <a> 标签但保留文本内容
                desc_html = re.sub(r'<a[^>]*>([^<]*)</a>', r'\1', desc_html, flags=re.IGNORECASE)
                # 移除 <span> 标签但保留文本内容
                desc_html = re.sub(r'<span[^>]*>([^<]*)</span>', r'\1', desc_html, flags=re.IGNORECASE)
                # 移除其他 HTML 标签
                desc_html = re.sub(r'<[^>]+>', '', desc_html)
                
                # 处理 CSS 的 before/after 标记
                desc_html = re.sub(r':\s*before', '', desc_html, flags=re.IGNORECASE)
                desc_html = re.sub(r':\s*after', '', desc_html, flags=re.IGNORECASE)
                
                # 处理 HTML 实体
                desc_html = desc_html.replace('&nbsp;', ' ').replace('&amp;', '&')
                desc_html = re.sub(r'&#\d+;', '', desc_html)
                
                # 清理多余空白但保留换行符
                parts = desc_html.split('\n')
                cleaned_parts = [part.strip() for part in parts if part.strip()]
                description = '\n'.join(cleaned_parts)
            else:
                # 最后尝试使用 og:description
                og_desc_match = re.search(r'<meta property="og:description" content="([^"]*)"', html)
                if og_desc_match:
                    description = og_desc_match.group(1).strip()

            # 店铺信息。渲染器只有在存在 author/header 时才会绘制平台 Logo。
            author_name = "BOOTH"
            author_avatar = None
            author_img_match = re.search(
                r'<img[^>]+alt="([^"]+)"[^>]+src="([^"]*?/users/\d+/icon_image/[^"]+)"',
                html,
                re.DOTALL,
            )
            if author_img_match:
                author_name = unescape(author_img_match.group(1)).strip() or author_name
                author_avatar = author_img_match.group(2).strip()

            shop_match = re.search(
                r'<a[^>]+href="https?://[\w-]+\.booth\.pm/?[^"]*"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            if shop_match and author_name == "BOOTH":
                shop_name = re.sub(r"<[^>]+>", "", shop_match.group(1))
                shop_name = unescape(shop_name).strip()
                if shop_name:
                    author_name = shop_name

            
            # 商品图片 URLs
            image_urls = []
            
            # 只从 data-origin 属性提取原图（这是 BOOTH 页面的原始高清图片）
            origin_images = re.findall(r'data-origin="([^"]*)"', html)
            if origin_images:
                image_urls.extend(origin_images)
            else:
                # 如果没有 data-origin，才尝试从 og:image 获取
                og_images = re.findall(r'<meta property="og:image(?::\w+)?" content="([^"]*)"', html)
                image_urls.extend(og_images)
            

            # 构建解析结果
            contents = []
            
            # 添加图片
            if image_urls:
                contents.extend(self.create_images(image_urls))
            
            # 创建文本内容（标题和描述）
            result_text = description if description else ""
            
            return self.result(
                title=title,
                text=result_text,
                author=self.create_author(author_name, author_avatar),
                contents=contents,
                url=url,
            )

        except ParseException:
            raise
        except Exception as e:
            logger.error(f"BoothParser 解析失败: {e}")
            raise ParseException(f"BOOTH 页面解析失败: {e}")
