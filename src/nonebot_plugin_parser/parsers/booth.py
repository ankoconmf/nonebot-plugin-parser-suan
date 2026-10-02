import json
import re
from html import unescape
from typing import Any, ClassVar

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
            # 新版页面(2024+)：正文是 div.description 内 whitespace-pre-line 的 span，
            # 换行是文本里真实的 \n，必须原样保留，否则简介会被压成一行
            description = self._extract_preline_description(html)

            # 旧版页面：主描述容器
            desc_match = None
            if not description:
                desc_match = re.search(
                    r'<div[^>]*js-market-item-detail-description[^>]*>(.*?)</div>',
                    html,
                    re.DOTALL
                )
            
            if not description and not desc_match:
                # 尝试从 section.deco-text 中提取
                desc_match = re.search(
                    r'<section[^>]*deco-text[^>]*>(.*?)</section>',
                    html,
                    re.DOTALL
                )
            
            if not description and not desc_match:
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
            elif not description:
                # 退回结构化数据(description 字段本身带换行)，最后才是被压平的 og:description
                product = self._extract_product_ldjson(html)
                ld_desc = product.get("description") if product else None
                if ld_desc:
                    description = unescape(str(ld_desc)).strip()
                if not description:
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
            

            # 价格：优先取 ld+json 结构化数据，其次退回页面可见的价格标签
            price_line = self._extract_price_line(html)

            # 构建解析结果
            contents = []
            
            # 添加图片
            if image_urls:
                contents.extend(self.create_images(image_urls))
            
            # 创建文本内容（价格 + 描述），渲染器会把它放进简介框
            text_parts = []
            if price_line:
                text_parts.append(price_line)
            if description:
                text_parts.append(description)
            result_text = "\n\n".join(text_parts)
            
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

    # 新版页面: 简介正文是 div.description 里 whitespace-pre-line 的 span(移动端多套一层 <p>)
    _PRELINE_INTRO_RE = re.compile(
        r'<div[^>]*class="[^"]*\bdescription\b[^"]*"[^>]*>\s*(?:<p[^>]*>\s*)?'
        r'<span[^>]*whitespace-pre-line[^>]*>(.*?)</span>',
        re.DOTALL | re.IGNORECASE,
    )

    @classmethod
    def _extract_preline_description(cls, html: str) -> str:
        """新版 BOOTH 页面的简介正文，保留原始换行；取不到返回空串"""
        match = cls._PRELINE_INTRO_RE.search(html)
        if not match:
            return ""
        return cls._html_to_text(match.group(1))

    @staticmethod
    def _html_to_text(fragment: str) -> str:
        """把一小段 HTML 转成纯文本：保留换行、去掉首尾空白、折叠连续空行"""
        text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|li|h[1-6]|section)\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)

        lines = [
            line.strip(" \t\u3000")
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        ]
        cleaned: list[str] = []
        for line in lines:
            if not line:
                # 丢掉开头的空行和连续空行, 只保留段落之间的一个空行
                if not cleaned or cleaned[-1] == "":
                    continue
                cleaned.append("")
            else:
                cleaned.append(line)
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        return "\n".join(cleaned)

    @classmethod
    def _extract_price_line(cls, html: str) -> str:
        """提取商品价格并格式化为简介首行；取不到时返回空串"""
        product = cls._extract_product_ldjson(html)
        offers = product.get("offers") if product else None
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            currency = str(offers.get("priceCurrency") or "")
            price = offers.get("price")
            if price is None:
                # AggregateOffer(多规格商品) 只有区间价格
                low, high = offers.get("lowPrice"), offers.get("highPrice")
                if low is not None and high is not None:
                    return f"价格: {cls._format_price(low, currency)} ～ {cls._format_price(high, currency)}"
                price = low if low is not None else high
            if price is not None:
                formatted = cls._format_price(price, currency)
                if formatted:
                    return f"价格: {formatted}"

        # 兜底：页面可见的价格(如 <div class="variation-price">¥ 5,500</div>)
        visible_match = re.search(
            r'class="[^"]*(?:variation-price|\bprice\b)[^"]*"[^>]*>\s*([^<]{1,40})<',
            html,
        )
        if visible_match:
            visible = unescape(visible_match.group(1)).strip()
            amount_match = re.search(r"([¥￥$€])?\s*([\d][\d.,]*)", visible)
            if amount_match:
                symbol = amount_match.group(1) or ""
                digits = amount_match.group(2).replace(",", "")
                currency = {"¥": "JPY", "￥": "JPY", "$": "USD", "€": "EUR"}.get(symbol, "")
                formatted = cls._format_price(digits, currency)
                if formatted:
                    return f"价格: {formatted}"
        return ""

    @staticmethod
    def _format_price(value: Any, currency: str) -> str:
        """把价格数值格式化成 5,500 JPY 这类可读文本"""
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return f"{str(value).strip()} {currency}".strip()
        if amount == 0:
            return "免费"
        amount_str = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
        return f"{amount_str} {currency}".strip()

    @staticmethod
    def _extract_product_ldjson(html: str) -> dict[str, Any] | None:
        """从页面 ld+json 块中找出 @type == Product 的对象"""
        for m in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        ):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            # 可能是单对象或数组
            for obj in data if isinstance(data, list) else [data]:
                if isinstance(obj, dict) and obj.get("@type") == "Product":
                    return obj
        return None
