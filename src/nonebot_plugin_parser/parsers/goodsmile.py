import re
import json
from html import unescape
from typing import Any, ClassVar

from httpx import AsyncClient
from nonebot import logger

from .base import BaseParser, PlatformEnum, handle
from .data import Platform
from ..config import pconfig
from ..exception import ParseException

# 商品图片存储路径前缀，用于把图片限定在当前商品、排除推荐位
_STORAGE_PREFIX = "/gsc-webrevo-sdk-storage-prd/product/image/"
_SITE = "https://www.goodsmile.com"

# 上架状态映射
_AVAILABILITY_MAP = {
    "PreOrder": "预售中",
    "InStock": "有货",
    "OutOfStock": "售罄",
    "SoldOut": "售罄",
    "Discontinued": "已停产",
    "BackOrder": "补货中",
    "LimitedAvailability": "限量",
}


class GoodSmileParser(BaseParser):
    """Good Smile Company (goodsmile.com) 商品解析器"""

    platform: ClassVar[Platform] = Platform(name=PlatformEnum.GOODSMILE, display_name="GoodSmile")

    @handle(
        "goodsmile.com",
        r"(?:https?://)?(?:www\.)?goodsmile\.com/(?:[a-z]{2}/)?product/(\d+)",
    )
    async def _parse(self, searched: re.Match[str]):
        """解析 GoodSmile 商品页面"""
        url = searched.group(0)
        item_id = searched.group(1)

        if not url.startswith("http"):
            url = f"https://{url}"

        try:
            proxy = pconfig.proxy

            headers = self.headers.copy()
            headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en,ja;q=0.9",
            })

            async with AsyncClient(
                headers=headers,
                timeout=20.0,
                verify=False,
                proxy=proxy,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise ParseException(f"获取页面失败: HTTP {resp.status_code}")
                html = resp.text

            # 优先从 ld+json 的 Product 结构化数据提取
            product = self._extract_product_ldjson(html)

            # 标题
            title = ""
            if product:
                title = unescape(str(product.get("name", ""))).strip()
            if not title:
                og_title = re.search(r'<meta property="og:title" content="([^"]*)"', html)
                if og_title:
                    title = unescape(og_title.group(1)).strip()
            # 去掉标题里的站点后缀
            title = re.split(r"[｜|]\s*Good Smile", title)[0].strip()
            if not title:
                title = f"GoodSmile 商品 {item_id}"

            # 描述: 优先从正文容器提取以保留原始换行, ld+json / og 为兜底
            description = self._extract_description(html)
            if not description and product:
                description = unescape(str(product.get("description", ""))).strip()
            if not description:
                og_desc = re.search(r'<meta property="og:description" content="([^"]*)"', html)
                if og_desc:
                    description = unescape(og_desc.group(1)).strip()

            # 组装附加信息（品牌/分类/价格/上架状态）
            info_lines = self._build_info_lines(product)

            # 图片：用 ld+json image 字段做锚点，推导本商品图库前缀
            image_urls = self._extract_gallery(html, product, item_id)

            contents = []
            if image_urls:
                contents.extend(self.create_images(image_urls))

            text_parts = []
            if info_lines:
                text_parts.append("\n".join(info_lines))
            if description:
                text_parts.append(description)
            result_text = "\n\n".join(text_parts)

            return self.result(
                title=title,
                text=result_text,
                author=self.create_author("Good Smile Company"),
                contents=contents,
                url=url,
            )

        except ParseException:
            raise
        except Exception as e:
            logger.error(f"GoodSmileParser 解析失败: {e}")
            raise ParseException(f"GoodSmile 页面解析失败: {e}")

    @staticmethod
    def _extract_description(html: str) -> str:
        """从正文描述容器提取, 保留原始换行 (ld+json 那份会被压平)"""
        m = re.search(
            r'<div[^>]*\bname="description"[^>]*>(.*?)</div>',
            html,
            re.DOTALL,
        )
        if not m:
            return ""

        text = m.group(1)
        # <br> / </p> / </div> 转换行
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</div\s*>", "\n", text, flags=re.IGNORECASE)
        # 去掉剩余标签 (保留 <a> 等的文本内容)
        text = re.sub(r"<[^>]+>", "", text)
        # HTML 实体
        text = unescape(text)
        # 逐行清理首尾空白, 折叠多余空行
        lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
        cleaned: list[str] = []
        for ln in lines:
            if not ln and cleaned and not cleaned[-1]:
                continue  # 折叠连续空行
            cleaned.append(ln)
        return "\n".join(cleaned).strip()

    @staticmethod
    def _extract_product_ldjson(html: str) -> dict[str, Any] | None:
        """从页面所有 ld+json 块中找出 @type == Product 的对象"""
        for m in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        ):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # 可能是单对象或数组
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") == "Product":
                    return obj
        return None

    @staticmethod
    def _build_info_lines(product: dict[str, Any] | None) -> list[str]:
        if not product:
            return []
        lines: list[str] = []

        brand = product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        if brand:
            lines.append(f"品牌: {unescape(str(brand)).strip()}")

        category = product.get("category")
        if category:
            lines.append(f"分类: {unescape(str(category)).strip()}")

        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            price = offers.get("price")
            currency = offers.get("priceCurrency", "")
            if price:
                symbol = "￥" if currency == "JPY" else ""
                try:
                    price_str = f"{int(float(price)):,}"
                except (TypeError, ValueError):
                    price_str = str(price)
                if symbol:
                    lines.append(f"价格: {symbol}{price_str}")
                else:
                    lines.append(f"价格: {price_str} {currency}".rstrip())

            avail = offers.get("availability", "")
            if avail:
                key = str(avail).rsplit("/", 1)[-1]
                lines.append(f"状态: {_AVAILABILITY_MAP.get(key, key)}")

        return lines

    def _extract_gallery(
        self,
        html: str,
        product: dict[str, Any] | None,
        item_id: str,
    ) -> list[str]:
        """提取当前商品的图库，排除关联推荐位"""
        # 收集页面里所有商品存储图
        all_imgs = re.findall(rf'({re.escape(_STORAGE_PREFIX)}[^"\'\s]+\.(?:jpg|jpeg|png|webp))', html, re.IGNORECASE)

        # 用 ld+json image 字段推导本商品前缀锚点
        anchor = ""
        if product:
            img = product.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, str) and _STORAGE_PREFIX in img:
                # 取到倒数第二段作为归属前缀:
                #   旧格式 .../product/日期/组号/项号/large/hash.jpg -> 锚定到 组号
                #   新格式 .../product/image/{商品ID}/hash.jpg      -> 锚定到 商品ID
                path = img[img.index(_STORAGE_PREFIX):]
                parts = path.rstrip("/").split("/")
                if "large" in parts:
                    # 旧格式：large 前一段是项号，再前一段(组号)才是商品分组
                    li = parts.index("large")
                    anchor = "/".join(parts[: max(li - 1, 0)])
                else:
                    # 新格式：去掉文件名，剩下 .../image/{id}
                    anchor = "/".join(parts[:-1])

        seen: set[str] = set()
        gallery: list[str] = []
        for path in all_imgs:
            # 归属过滤
            if anchor and not path.startswith(anchor):
                continue
            # 兜底：至少 URL 里要含商品 ID（新格式）
            if not anchor and item_id not in path:
                continue
            full = path if path.startswith("http") else f"{_SITE}{path}"
            if full in seen:
                continue
            seen.add(full)
            gallery.append(full)

        # 实在没抓到就退回 og:image
        if not gallery:
            og_img = re.search(r'<meta property="og:image(?::\w+)?" content="([^"]*)"', html)
            if og_img:
                gallery.append(og_img.group(1).strip())

        return gallery
