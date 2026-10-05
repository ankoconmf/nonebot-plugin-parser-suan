import re
import asyncio
import hashlib
from pathlib import Path
from typing import TypeVar
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from nonebot import logger, get_driver, on_command
from nonebot.params import CommandArg
from nonebot.adapters import Message
from nonebot_plugin_alconna.uniseg import Video, Reference, Image
from nonebot_plugin_alconna.uniseg.segment import Media

try:  # OneBot V11 的发送异常 (其它适配器没有这个类型)
    from nonebot.adapters.onebot.v11.exception import ActionFailed
except Exception:  # pragma: no cover - 未安装 onebot v11 适配器时

    class ActionFailed(Exception):  # type: ignore[no-redef]
        """占位: 未安装 OneBot V11 时不会命中"""


from .rule import SUPER_PRIVATE, Searched, SearchResult, on_keyword_regex
from .filter import is_enabled
from ..utils import LimitedSizeDict
from ..config import pconfig
from ..helper import UniHelper, UniMessage
from ..parsers import BaseParser, ParseResult, BilibiliParser, VideoContent
from ..renders import get_renderer
from ..exception import DownloadException

SEND_RETRY: int = 1
"""发送失败的重试次数"""

SEND_RETRY_DELAY: float = 2.0
"""发送失败重试前的等待(秒)"""


def _get_enabled_parser_classes() -> list[type[BaseParser]]:
    disabled_platforms = set(pconfig.disabled_platforms)
    all_subclass = BaseParser.get_all_subclass()
    return [_cls for _cls in all_subclass if _cls.platform.name not in disabled_platforms]


# 关键词 -> Parser 映射
KEYWORD_PARSER_MAP: dict[str, BaseParser] = {}
T = TypeVar("T", bound=BaseParser)


def get_parser(keyword: str) -> BaseParser:
    return KEYWORD_PARSER_MAP[keyword]


def get_parser_by_type(parser_type: type[T]) -> T:
    for parser in KEYWORD_PARSER_MAP.values():
        if isinstance(parser, parser_type):
            return parser
    raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")


@get_driver().on_startup
def register_parser_matcher():
    enabled_classes = _get_enabled_parser_classes()

    enabled_platforms = []
    for _cls in enabled_classes:
        parser = _cls()
        enabled_platforms.append(parser.platform.display_name)
        for keyword, _ in _cls._key_patterns:
            KEYWORD_PARSER_MAP[keyword] = parser
    logger.info(f"启用平台: {', '.join(sorted(enabled_platforms))}")

    patterns = [p for _cls in enabled_classes for p in _cls._key_patterns]
    matcher = on_keyword_regex(*patterns)
    matcher.append_handler(parser_handler)


# 缓存结果
_RESULT_CACHE = LimitedSizeDict[str, ParseResult](max_size=50)
# 在途解析任务: 同一链接被多个群同时触发时, 共享同一次解析/下载/渲染
_PARSE_TASKS: dict[str, asyncio.Task[ParseResult]] = {}


def clear_result_cache():
    _RESULT_CACHE.clear()


async def _get_or_parse_result(sr: SearchResult) -> ParseResult:
    """获取解析结果, 同一 cache_key 的并发请求共享同一次解析"""
    cache_key = sr.text

    if (cached := _RESULT_CACHE.get(cache_key)) is not None:
        logger.debug(f"命中缓存: {cache_key}, 结果: {cached}")
        return cached

    task = _PARSE_TASKS.get(cache_key)
    if task is None:

        async def _parse_and_cache() -> ParseResult:
            parser = get_parser(sr.keyword)
            parsed = await parser.parse(sr.keyword, sr.searched)
            logger.debug(f"解析结果: {parsed}")
            # 解析完成即入缓存: 媒体是懒下载(PathTask),
            # 后到的群复用同一批下载任务, 不必重复下载/重复 ffmpeg 合并
            _RESULT_CACHE[cache_key] = parsed
            return parsed

        task = asyncio.create_task(_parse_and_cache(), name=f"parse | {cache_key[:48]}")
        _PARSE_TASKS[cache_key] = task

        def _discard(finished: asyncio.Task[ParseResult]) -> None:
            if _PARSE_TASKS.get(cache_key) is finished:
                _PARSE_TASKS.pop(cache_key, None)

        task.add_done_callback(_discard)

    # shield: 某个群的 matcher 被取消/超时时, 别让共用的解析任务跟着死掉
    return await asyncio.shield(task)


# --- 发送阶段按媒体内容串行 ---
# 协议端(NapCat/NTQQ)按文件内容 md5 落盘, 两个群同时发同一份内容时会撞出
# "EBUSY: resource busy or locked, copyfile", 因此同一份内容同一时刻只发一次。

_MEDIA_HASH_MAX_BYTES: int = 16 * 1024 * 1024
"""不超过该大小的媒体按内容哈希加锁, 更大的(如合并后的视频)按路径加锁"""
_SEND_LOCKS: dict[str, tuple[asyncio.Lock, int]] = {}
_SEND_LOCKS_GUARD = asyncio.Lock()


async def _iter_media_paths(message: UniMessage) -> AsyncIterator[Path]:
    """递归收集消息(含合并转发节点)里指向本地文件的媒体"""
    for seg in message:
        if isinstance(seg, Media):
            if seg.path:
                yield Path(seg.path)
            if isinstance(seg, Video) and seg.thumbnail is not None and seg.thumbnail.path:
                yield Path(seg.thumbnail.path)
        elif isinstance(seg, Reference):
            for node in seg.children:
                content = getattr(node, "content", None)
                if isinstance(content, UniMessage):
                    async for path in _iter_media_paths(content):
                        yield path
                elif isinstance(content, list):
                    async for path in _iter_media_paths(UniMessage(content)):
                        yield path


async def _media_lock_key(path: Path) -> str | None:
    """同一份内容(即使路径不同, 比如两次渲染出的卡片)必须映射到同一个 key"""
    try:
        if (size := (await asyncio.to_thread(path.stat)).st_size) <= 0:
            return None
        if size <= _MEDIA_HASH_MAX_BYTES:
            data = await asyncio.to_thread(path.read_bytes)
            return f"content:{hashlib.md5(data).hexdigest()}"
    except OSError:
        return None
    return f"path:{path.resolve()}"


@asynccontextmanager
async def _serialize_send(message: UniMessage) -> AsyncIterator[None]:
    """同一份媒体同一时刻只允许一次 send"""
    keys: set[str] = set()
    async for path in _iter_media_paths(message):
        if (key := await _media_lock_key(path)) is not None:
            keys.add(key)

    if not keys:
        yield
        return

    # 引用计数: 拿到 key 的同时登记占用, 无人引用后自动清理, 避免锁表无限增长
    async with _SEND_LOCKS_GUARD:
        held: list[tuple[str, asyncio.Lock]] = []
        for key in sorted(keys):
            lock, refs = _SEND_LOCKS.get(key, (asyncio.Lock(), 0))
            _SEND_LOCKS[key] = (lock, refs + 1)
            held.append((key, lock))

    acquired: list[asyncio.Lock] = []
    try:
        # 按排序后的 key 依次加锁, 多把锁也不会互相等待
        for _, lock in held:
            await lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()
        async with _SEND_LOCKS_GUARD:
            for key, lock in held:
                current = _SEND_LOCKS.get(key)
                if current is None:
                    continue
                if current[1] <= 1:
                    _SEND_LOCKS.pop(key, None)
                else:
                    _SEND_LOCKS[key] = (lock, current[1] - 1)


async def _send(message: UniMessage, gif_sources: dict[str, tuple[Path, Path | None]] | None = None) -> None:
    """发送消息, 同一份媒体内容串行发送

    发送失败(如 QQ 上传动图超时 retcode 1200)时重试一次; 仍失败则把其中的
    动图降级成视频再发 —— 动图是转换出来的, 体积往往比原视频大很多。
    """
    async with _serialize_send(message):
        try:
            await message.send()
            return
        except Exception as e:
            # 不吞掉媒体下载失败: 交由上层清理缓存结果
            if isinstance(e, DownloadException):
                raise
            logger.warning(f"发送失败({type(e).__name__}: {e}), 尝试重试")

        for remaining in range(SEND_RETRY, -1, -1):
            await asyncio.sleep(SEND_RETRY_DELAY)
            try:
                await message.send()
                return
            except Exception as e:
                if isinstance(e, DownloadException):
                    raise
                if remaining == 0:
                    logger.warning(f"重试仍失败: {type(e).__name__}: {e}")

        fallback = _downgrade_gif_to_video(message, gif_sources or {})
        if fallback is None:
            raise
        logger.info("动图发送失败, 降级为视频重发")
        await fallback.send()


def _downgrade_gif_to_video(
    message: UniMessage,
    gif_sources: dict[str, tuple[Path, Path | None]],
) -> UniMessage | None:
    """把消息里由动图转换来的图片段换回原始视频; 无可替换项时返回 None"""
    if not gif_sources:
        return None

    segments = list(message)
    changed = False
    for index, seg in enumerate(segments):
        if not isinstance(seg, Image):
            continue
        path = getattr(seg, "path", None)
        if path is None:
            continue
        source = gif_sources.get(str(path))
        if source is None:
            continue
        video_path, cover_path = source
        segments[index] = UniHelper.video_seg(video_path, cover_path)
        changed = True

    return UniMessage(segments) if changed else None


async def _collect_gif_sources(result: ParseResult) -> dict[str, tuple[Path, Path | None]]:
    """收集 动图路径 -> (原始视频路径, 封面路径)

    动图是本地转换出来的 (VideoContent.gif_path), 体积常比原视频大好几倍,
    发送失败时用它换回原视频重发。
    """
    sources: dict[str, tuple[Path, Path | None]] = {}

    async def walk(node: ParseResult | None) -> None:
        if node is None:
            return
        for cont in node.contents:
            if not isinstance(cont, VideoContent) or cont.gif_path is None:
                continue
            gif_file = await cont.gif_path.safe_get()
            if gif_file is None:
                continue
            video_file = await cont.path_task.safe_get()
            if video_file is None:
                continue
            cover_file = await cont.cover.safe_get() if cont.cover else None
            sources[str(gif_file)] = (video_file, cover_file)
        await walk(node.repost)

    await walk(result)
    return sources


@UniHelper.with_reaction
async def parser_handler(
    sr: SearchResult = Searched(),
):
    """统一的解析处理器"""
    cache_key = sr.text

    # 1. 获取(或等待其他人正在进行的)解析结果
    result = await _get_or_parse_result(sr)

    # 2. 渲染内容消息并发送
    renderer = get_renderer(result.platform.name)(result)
    gif_sources = await _collect_gif_sources(result)
    try:
        async for message in renderer.render_messages():
            await _send(message, gif_sources)
    except DownloadException:
        # 媒体下载失败时不留坏结果, 否则后续同链接会一直命中同一个失败的下载任务
        if _RESULT_CACHE.get(cache_key) is result:
            _RESULT_CACHE.pop(cache_key, None)
        raise


@on_command("bm", priority=3, block=True, rule=is_enabled).handle()
@UniHelper.with_reaction
async def _(message: Message = CommandArg()):
    text = message.extract_plain_text()
    matched = re.search(r"(BV[A-Za-z0-9]{10})(\s\d{1,3})?", text)
    if not matched:
        await UniMessage("请发送正确的 BV 号").finish()

    bvid, page_num = matched.group(1), matched.group(2)
    page_idx = int(page_num) - 1 if page_num else 0

    parser = get_parser_by_type(BilibiliParser)

    _, audio_urls = await parser.extract_download_urls(bvid=bvid, page_index=page_idx)
    if not audio_urls:
        await UniMessage("未找到可下载的音频").finish()

    audio_path = await parser.downloader.download_audio(
        audio_urls[0],
        audio_name=f"{bvid}-{page_idx}.mp3",
        ext_headers=parser.headers,
        fallback_urls=audio_urls[1:],
        retry_http_statuses=parser.BILI_RETRYABLE_HTTP_STATUSES,
    )
    await _send(UniMessage(UniHelper.record_seg(audio_path)))

    if pconfig.need_upload:
        await _send(UniMessage(UniHelper.file_seg(audio_path)))


from ..download import yt_dlp_downloader

if yt_dlp_downloader is not None:
    from ..parsers import YouTubeParser

    @on_command("ym", priority=3, block=True, rule=is_enabled).handle()
    @UniHelper.with_reaction
    async def _(message: Message = CommandArg()):
        text = message.extract_plain_text()
        parser = get_parser_by_type(YouTubeParser)
        _, matched = parser.search_url(text)
        if not matched:
            await UniMessage("请发送正确的油管链接").finish()

        url = matched.group(0)

        audio_path = await yt_dlp_downloader.download_audio(url)
        await _send(UniMessage(UniHelper.record_seg(audio_path)))

        if pconfig.need_upload:
            await _send(UniMessage(UniHelper.file_seg(audio_path)))


@on_command("blogin", block=True, permission=SUPER_PRIVATE).handle()
async def _():
    parser = get_parser_by_type(BilibiliParser)
    qrcode = await parser.login_with_qrcode()
    await UniMessage(UniHelper.img_seg(qrcode)).send()
    async for msg in parser.check_qr_state():
        await UniMessage(msg).send()
