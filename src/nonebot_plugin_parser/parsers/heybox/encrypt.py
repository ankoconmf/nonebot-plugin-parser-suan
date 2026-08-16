# 小黑盒 API 请求签名逻辑, 逆向自前端 hkey&nonce.js
# 逆向工程来源: 焦化葱葱(negichan)
# 仅用于学习交流, 严禁非法滥用

import time as _time
import hashlib
import itertools
from typing import Final

BASE_URL: Final[str] = "api.xiaoheihe.cn"
PATH: Final[str] = "/bbs/app/link/tree"


def get_nonce(time: int) -> str:
    t = str(time).encode("utf-8")
    return hashlib.md5(t).hexdigest().upper()


def _vm(e: int) -> int:
    """等价 JS Vm。"""
    return ((e << 1) & 0xFF) ^ 27 if (e & 0x80) else ((e << 1) & 0xFF)


def _qm(e: int) -> int:
    """等价 JS qm。"""
    return _vm(e) ^ e


def _mm(e: int) -> int:
    """等价 JS $m。"""
    return _qm(_vm(e))


def _ym(e: int) -> int:
    """等价 JS Ym。"""
    return _mm(_qm(_vm(e)))


def _gm(e: int) -> int:
    """等价 JS Gm。"""
    return _ym(e) ^ _mm(e) ^ _qm(e)


def _km(e: list[int]) -> list[int]:
    """等价 JS Km。"""
    t0 = _gm(e[0]) ^ _ym(e[1]) ^ _mm(e[2]) ^ _qm(e[3])
    t1 = _qm(e[0]) ^ _gm(e[1]) ^ _ym(e[2]) ^ _mm(e[3])
    t2 = _mm(e[0]) ^ _qm(e[1]) ^ _gm(e[2]) ^ _ym(e[3])
    t3 = _ym(e[0]) ^ _mm(e[1]) ^ _qm(e[2]) ^ _gm(e[3])
    e[0], e[1], e[2], e[3] = t0, t1, t2, t3
    return e


def _av(e: str, t: str, n: int) -> str:
    """等价 JS av(e, t, n)。"""
    # JS: var i = t.slice(0, n);
    i = t[:n]
    if not i:
        return ""
    res_chars: list[str] = []
    for ch in e:
        idx = ord(ch) % len(i)
        res_chars.append(i[idx])
    return "".join(res_chars)


def _sv(e: str, t: str) -> str:
    """等价 JS sv(e, t)。"""
    if not t:
        return ""
    res_chars: list[str] = [t[ord(ch) % len(t)] for ch in e]
    return "".join(res_chars)


def _interleave_js(arr: list[str]) -> str:
    """等价 JS 中 iv + 匿名函数的交错拼接逻辑。"""
    if not arr:
        return ""
    max_len = max(len(s) for s in arr)
    out: list[str] = [s[i] for i, s in itertools.product(range(max_len), arr) if i < len(s)]
    return "".join(out)


def get_hkey(time: int) -> str:
    """还原前端 getHkey 逻辑。"""
    e = PATH
    t = time + 1
    n = get_nonce(time)

    parts = [seg for seg in e.split("/") if seg]
    e_norm = "/" + "/".join(parts) + "/"

    r = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

    i_str = _interleave_js(
        [
            _av(str(t), r, -2),
            _sv(e_norm, r),
            _sv(n, r),
        ]
    )[:20]

    o = hashlib.md5(i_str.encode("utf-8")).hexdigest()

    last6 = o[-6:]
    arr = [ord(ch) for ch in last6]

    mixed = _km(arr)
    total = sum(mixed)
    a_val = total % 100
    a = f"{a_val:02d}"

    s = _av(o[:5], r, -4)

    return f"{s}{a}"


def build_url(link_id: str) -> str:
    """构造带签名的请求 URL。"""
    time = int(_time.time())
    return (
        f"https://{BASE_URL}{PATH}"
        "?os_type=web&app=heybox&client_type=web&version=999.0.4"
        f"&_time={time}&nonce={get_nonce(time)}&hkey={get_hkey(time)}&link_id={link_id}"
        "&page=1&index=1&limit=5&x_client_type=weboutapp&x_app=heybox_website&x_os_type=Windows"
        "&web_version=2.5"
    )
