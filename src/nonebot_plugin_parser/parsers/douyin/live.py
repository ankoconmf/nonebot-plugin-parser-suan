"""抖音直播/回放(webcast)房间信息提取.

抖音直播的分享短链会重定向到
``https://webcast.amemv.com/douyin/webcast/reflow/<room_id>``, 该 H5 页面由
Next.js 服务端渲染, 直播间信息以内联的 React Server Components (RSC) flight
数据形式出现在 ``self.__rsc_f.push([1, "..."])`` 脚本里, 无需浏览器即可解析.
直播中与已下播(回放)共用同一页面结构, 都能拿到封面/标题/主播信息.
"""

from __future__ import annotations

import json
import re
from typing import Any

# `self.__rsc_f.push([1, "..."] )` 中的 JS 字符串字面量
_RSC_PUSH_PATTERN = re.compile(r'self\.__rsc_f\.push\(\[1,("(?:[^"\\]|\\.)*")')


def _find_room(obj: Any) -> dict | None:
    """在 flight 数据中递归查找直播间信息 dict.

    直播间信息 dict 以 ``idStr`` + ``title`` + ``cover`` 三个字段为特征,
    与其余可能出现的 ``room``/``ownRoom`` 等字段区分开.
    """
    if isinstance(obj, dict):
        if isinstance(obj.get("idStr"), str) and "title" in obj and "cover" in obj:
            return obj
        for value in obj.values():
            if (room := _find_room(value)) is not None:
                return room
    elif isinstance(obj, list):
        for value in obj:
            if (room := _find_room(value)) is not None:
                return room
    return None


def extract_room_info(html: str) -> dict | None:
    """从 reflow 页面 HTML 中提取直播间信息, 失败返回 None."""
    for match in _RSC_PUSH_PATTERN.finditer(html):
        try:
            flight = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue

        for segment in flight.split("\n"):
            if '"room"' not in segment:
                continue
            # 去掉 flight 段前缀 `N:` 后, 剩余部分是标准 JSON 数组
            colon = segment.find(":")
            if colon <= 0:
                continue
            try:
                data = json.loads(segment[colon + 1:])
            except (json.JSONDecodeError, TypeError):
                continue
            if (room := _find_room(data)) is not None:
                return room

    return None
