import re
import asyncio
from pathlib import Path
from functools import partial
from contextlib import contextmanager
from collections.abc import AsyncIterator, Collection, Sequence
from urllib.parse import urljoin, urlparse

import httpx
import aiofiles
import curl_cffi
from curl_cffi.requests.exceptions import ContentDecodingError
from nonebot import logger, get_driver
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    DownloadColumn,
)

from .task import auto_task
from ..utils import merge_av, safe_unlink, generate_file_name, is_module_available
from ..config import pconfig
from ..constants import COMMON_HEADER, DOWNLOAD_TIMEOUT, RETRYABLE_HTTP_STATUSES
from ..exception import IgnoreException, DownloadException

# Content-Range: bytes 0-1023/2048
_RE_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")

# 允许的文件大小误差(部分 CDN 统计字节数会有一点点出入)
_SIZE_MISMATCH_TOLERANCE = 1024


class RetryableDownloadError(Exception):
    """当前下载失败, 但可以通过切换线路 / 断点续传恢复"""

    def __init__(self, message: str, *, keep_part: bool = True):
        super().__init__(message)
        self.message = message
        self.keep_part = keep_part


def _with_identity_encoding(headers: dict[str, str]) -> dict[str, str]:
    """统一使用 identity 编码, 保证 Content-Length / Content-Range 与实际字节数一致"""
    result = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
    result["Accept-Encoding"] = "identity"
    return result


def _short_url(url: str) -> str:
    """日志用的短链接(只保留主机与文件名), 避免带签名的长链接刷屏"""
    parsed = urlparse(url)
    file_name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{parsed.hostname}/{file_name}" if file_name else (parsed.hostname or url)


class UniResponse:
    """统一 httpx 与 curl_cffi 的响应接口"""

    __slots__ = ("_response",)

    def __init__(self, response: httpx.Response | curl_cffi.Response):
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        """全部小写的响应头"""
        return {key.lower(): value for key, value in self._response.headers.items()}

    @property
    def url(self) -> str:
        return str(self._response.url)

    async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        """按块迭代响应体, 把可恢复的网络错误包装成 RetryableDownloadError"""
        try:
            if isinstance(self._response, httpx.Response):
                async for chunk in self._response.aiter_bytes(chunk_size):
                    yield chunk
            else:
                async for chunk in self._response.aiter_content(chunk_size=chunk_size):
                    yield chunk
        except (httpx.DecodingError, ContentDecodingError) as e:
            # 内容解码失败后本地字节与远端 Range 不再对应, 不能保留断点
            raise RetryableDownloadError(f"响应解码失败: {e!r}", keep_part=False) from e
        except (httpx.HTTPError, curl_cffi.CurlError) as e:
            # 传输中断(连接被断开/读取超时等): 已写入的字节仍是完整前缀, 可以带 Range 续传
            raise RetryableDownloadError(f"网络中断: {e!r}") from e


class StreamDownloader:
    def __init__(self):
        self.headers: dict[str, str] = COMMON_HEADER.copy()
        self.cache_dir: Path = pconfig.cache_dir
        self.client: httpx.AsyncClient = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, verify=False)
        self._active_downloads: dict[str, asyncio.Task[Path]] = {}
        """同一文件的并发下载合并, 避免重复请求"""

    async def aclose(self):
        await self.client.aclose()

    @staticmethod
    @contextmanager
    def rich_progress(
        desc: str,
        total: int | None = None,
    ):
        with Progress(
            TextColumn("[bold blue]{task.description}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
        ) as progress:
            task_id = progress.add_task(description=desc, total=total)
            yield partial(progress.update, task_id)

    @staticmethod
    async def _part_size(part_path: Path) -> int:
        """已下载的字节数"""
        return part_path.stat().st_size if part_path.exists() else 0

    @staticmethod
    def _validate_total_size(url: str, total_size: int | None) -> None:
        """校验资源总大小"""
        if total_size is None:
            return

        if total_size == 0:
            logger.warning(f"媒体 url: {url}, 大小为0, 取消下载")
            raise IgnoreException

        if (file_size := total_size / 1024 / 1024) > pconfig.max_size:
            logger.warning(f"媒体 url: {url} 大小 {file_size:.2f} MB, 超过 {pconfig.max_size} MB, 取消下载")
            raise IgnoreException

    def _prepare_response(
        self,
        response: UniResponse,
        *,
        url: str,
        downloaded: int,
        retry_http_statuses: frozenset[int],
    ) -> int | None:
        """校验响应状态与断点续传信息, 返回资源总大小"""
        if downloaded > 0 and response.status_code == 416:
            # 断点位置无效, 需要重新完整下载
            raise RetryableDownloadError("断点位置无效, 重新完整下载", keep_part=False)

        if response.status_code in retry_http_statuses:
            raise RetryableDownloadError(
                f"HTTP {response.status_code}, 切换下载线路后重试", keep_part=False
            )

        if not 200 <= response.status_code < 300:
            raise DownloadException(f"HTTP {response.status_code} for url '{response.url}'")

        headers = response.headers
        if downloaded > 0:
            # 服务器必须支持断点续传, 否则追加写入会损坏文件
            if response.status_code != 206:
                raise RetryableDownloadError("服务器不支持断点续传", keep_part=False)

            match = _RE_CONTENT_RANGE.fullmatch(headers.get("content-range", "").strip())
            if match is None:
                raise RetryableDownloadError("服务器未返回有效的 Content-Range", keep_part=False)

            server_start = int(match[1])
            if server_start != downloaded:
                raise RetryableDownloadError(
                    f"Content-Range 错误: 请求 {downloaded}, 返回 {server_start}", keep_part=False
                )

            total_size = int(match[3]) if match[3] != "*" else None
        else:
            content_length = headers.get("content-length")
            try:
                total_size = int(content_length) if content_length else None
            except ValueError:
                total_size = None

        self._validate_total_size(url, total_size)
        return total_size

    async def _write_response(
        self,
        response: UniResponse,
        *,
        part_path: Path,
        desc: str,
        downloaded: int,
        total_size: int | None,
        chunk_size: int,
        url: str,
    ) -> None:
        """把响应体写入断点文件, 并校验最终大小"""
        written = downloaded

        with self.rich_progress(desc, total_size) as update_progress:
            if written:
                update_progress(advance=written)

            async with aiofiles.open(part_path, "ab" if downloaded else "wb") as file:
                async for chunk in response.aiter_bytes(chunk_size):
                    await file.write(chunk)
                    written += len(chunk)
                    update_progress(advance=len(chunk))

                    # 无 Content-Length 时按实际下载量兜底限制大小
                    if total_size is None and written / 1024 / 1024 > pconfig.max_size:
                        logger.warning(
                            f"媒体 url: {url} 实际下载大小 {written / 1024 / 1024:.2f} MB, "
                            f"超过 {pconfig.max_size} MB, 取消下载"
                        )
                        raise IgnoreException

        if total_size is not None and written != total_size:
            size_diff = abs(written - total_size)
            if size_diff > _SIZE_MISMATCH_TOLERANCE:
                raise RetryableDownloadError(
                    f"文件大小不匹配: {written}/{total_size} (差值: {size_diff} bytes)",
                    keep_part=written < total_size,
                )

    async def _download_stream(
        self,
        url: str,
        *,
        part_path: Path,
        headers: dict[str, str],
        chunk_size: int,
        downloaded: int,
        retry_http_statuses: frozenset[int],
        use_curl_cffi: bool,
        desc: str,
    ) -> None:
        """使用指定客户端下载单个 url 到断点文件"""
        request_headers = {**headers}
        if downloaded > 0:
            request_headers["Range"] = f"bytes={downloaded}-"

        try:
            await self._download_stream_inner(
                url,
                part_path=part_path,
                request_headers=request_headers,
                chunk_size=chunk_size,
                downloaded=downloaded,
                retry_http_statuses=retry_http_statuses,
                use_curl_cffi=use_curl_cffi,
                desc=desc,
            )
        except (RetryableDownloadError, DownloadException, IgnoreException):
            raise
        except (httpx.HTTPError, curl_cffi.CurlError) as e:
            # 连接失败 / 读取超时等, 可以换线路重试
            raise RetryableDownloadError(f"网络错误: {e!r}") from e

    async def _download_stream_inner(
        self,
        url: str,
        *,
        part_path: Path,
        request_headers: dict[str, str],
        chunk_size: int,
        downloaded: int,
        retry_http_statuses: frozenset[int],
        use_curl_cffi: bool,
        desc: str,
    ) -> None:
        if use_curl_cffi:
            timeout = float(max(DOWNLOAD_TIMEOUT.connect or 15, DOWNLOAD_TIMEOUT.read or 240))
            async with curl_cffi.AsyncSession(allow_redirects=True) as session:
                async with session.stream(
                    "GET",
                    url,
                    headers=request_headers,
                    timeout=timeout,
                ) as response:
                    await self._download_response(
                        UniResponse(response),
                        url=url,
                        part_path=part_path,
                        desc=desc,
                        downloaded=downloaded,
                        chunk_size=chunk_size,
                        retry_http_statuses=retry_http_statuses,
                    )
        else:
            async with self.client.stream(
                "GET",
                url,
                headers=request_headers,
                follow_redirects=True,
            ) as response:
                await self._download_response(
                    UniResponse(response),
                    url=url,
                    part_path=part_path,
                    desc=desc,
                    downloaded=downloaded,
                    chunk_size=chunk_size,
                    retry_http_statuses=retry_http_statuses,
                )

    async def _download_response(
        self,
        response: UniResponse,
        *,
        url: str,
        part_path: Path,
        desc: str,
        downloaded: int,
        chunk_size: int,
        retry_http_statuses: frozenset[int],
    ) -> None:
        total_size = self._prepare_response(
            response,
            url=url,
            downloaded=downloaded,
            retry_http_statuses=retry_http_statuses,
        )
        await self._write_response(
            response,
            part_path=part_path,
            desc=desc,
            downloaded=downloaded,
            total_size=total_size,
            chunk_size=chunk_size,
            url=url,
        )

    async def _download_from_url(
        self,
        url: str,
        *,
        part_path: Path,
        headers: dict[str, str],
        chunk_size: int,
        retry_http_statuses: frozenset[int],
        display_name: str,
    ) -> None:
        """依次尝试 httpx 与 curl_cffi 下载同一个 url"""
        last_error: Exception | None = None

        for use_curl_cffi in (False, True):
            backend = "curl_cffi" if use_curl_cffi else "httpx"
            # 上一次尝试可能已经写入部分数据, 重新读取断点位置
            downloaded = await self._part_size(part_path)
            try:
                await self._download_stream(
                    url,
                    part_path=part_path,
                    headers=headers,
                    chunk_size=chunk_size,
                    downloaded=downloaded,
                    retry_http_statuses=retry_http_statuses,
                    use_curl_cffi=use_curl_cffi,
                    desc=f"{backend} | {display_name}",
                )
                return
            except IgnoreException:
                raise
            except RetryableDownloadError as e:
                # 可恢复的失败: 已写入的字节会保留, 交给上层换线路/续传重试
                last_error = e
                logger.debug(f"下载中断({backend}) | {_short_url(url)}: {e}")
            except DownloadException as e:
                last_error = e
                logger.warning(f"下载失败({backend}) | {_short_url(url)}: {e}")

        if last_error is None:
            raise DownloadException(f"媒体下载失败: {_short_url(url)}")
        raise last_error

    async def _download_with_retry(
        self,
        *,
        download_urls: tuple[str, ...],
        file_path: Path,
        part_path: Path,
        headers: dict[str, str],
        chunk_size: int,
        retry_http_statuses: frozenset[int],
        display_name: str,
    ) -> Path:
        """轮换备用线路重试下载, 失败时保留断点用于续传"""
        max_retries = pconfig.max_retries
        last_error: Exception | None = None

        try:
            for attempt in range(max_retries + 1):
                current_url = download_urls[attempt % len(download_urls)]
                if attempt:
                    logger.debug(
                        f"下载第 {attempt + 1}/{max_retries + 1} 次尝试 | "
                        f"线路: {_short_url(current_url)}"
                    )

                try:
                    await self._download_from_url(
                        current_url,
                        part_path=part_path,
                        headers=headers,
                        chunk_size=chunk_size,
                        retry_http_statuses=retry_http_statuses,
                        display_name=display_name,
                    )
                except IgnoreException:
                    raise
                except DownloadException:
                    # 不可重试的错误(如 403/404), 直接放弃
                    raise
                except RetryableDownloadError as e:
                    last_error = e
                    if not e.keep_part:
                        await safe_unlink(part_path)

                    if attempt >= max_retries:
                        break

                    delay = min(2**attempt, 8)
                    logger.warning(
                        f"下载失败, {delay} 秒后重试 ({attempt + 1}/{max_retries}) | "
                        f"{_short_url(current_url)}: {e}"
                    )
                    await asyncio.sleep(delay)
                    continue

                # 下载完整后再原子重命名, 避免半成品被当成缓存命中
                part_path.replace(file_path)
                logger.debug(f"下载完成: {file_path.name}")
                return file_path

            raise DownloadException(f"媒体下载失败: {last_error}") from last_error
        except BaseException:
            # 放弃 / 取消时清理断点文件
            await safe_unlink(part_path)
            raise

    async def _download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        chunk_size: int = 64 * 1024,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = RETRYABLE_HTTP_STATUSES,
    ) -> Path:
        """download file by url with fallback

        :param url: 主下载链接
        :param fallback_urls: 同一资源的备用链接, 失败时按顺序轮换
        :param retry_http_statuses: 可切换线路重试的 HTTP 状态码
        """
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cache_dir / file_name
        if file_path.exists():
            return file_path

        # 去重并保持优先级顺序
        download_urls = tuple(
            dict.fromkeys(candidate for candidate in (url, *(fallback_urls or ())) if candidate)
        )
        headers = _with_identity_encoding({**self.headers, **(ext_headers or {})})
        part_path = file_path.with_name(f"{file_path.name}.part")

        # 合并同一文件的并发下载
        cache_key = str(file_path)
        download_task = self._active_downloads.get(cache_key)
        if download_task is None:
            download_task = asyncio.create_task(
                self._download_with_retry(
                    download_urls=download_urls,
                    file_path=file_path,
                    part_path=part_path,
                    headers=headers,
                    chunk_size=chunk_size,
                    retry_http_statuses=frozenset(retry_http_statuses),
                    display_name=file_name,
                ),
                name=f"download | {file_name}",
            )
            self._active_downloads[cache_key] = download_task

        try:
            return await download_task
        finally:
            if self._active_downloads.get(cache_key) is download_task:
                self._active_downloads.pop(cache_key, None)

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = RETRYABLE_HTTP_STATUSES,
    ) -> Path:
        """download video file by url with stream"""
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")

        return await self._download_file(
            url,
            file_name=video_name,
            ext_headers=ext_headers,
            chunk_size=1024 * 1024,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = RETRYABLE_HTTP_STATUSES,
    ) -> Path:
        """download audio file by url with stream"""
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")

        return await self._download_file(
            url,
            file_name=audio_name,
            ext_headers=ext_headers,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = RETRYABLE_HTTP_STATUSES,
    ) -> Path:
        """download image file by url with stream"""
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")

        return await self._download_file(
            url,
            file_name=img_name,
            ext_headers=ext_headers,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        ext_headers: dict[str, str] | None = None,
        video_fallback_urls: Sequence[str] | None = None,
        audio_fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = RETRYABLE_HTTP_STATUSES,
    ) -> Path:
        """download video and audio file by url with stream and merge"""
        v_path, a_path = await asyncio.gather(
            self._download_file(
                v_url,
                ext_headers=ext_headers,
                fallback_urls=video_fallback_urls,
                retry_http_statuses=retry_http_statuses,
            ),
            self._download_file(
                a_url,
                ext_headers=ext_headers,
                fallback_urls=audio_fallback_urls,
                retry_http_statuses=retry_http_statuses,
            ),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path

    @auto_task
    async def download_m3u8(
        self,
        m3u8_url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        """download m3u8 file by url with stream"""
        if video_name is None:
            video_name = generate_file_name(m3u8_url, ".mp4")

        video_path = pconfig.cache_dir / video_name

        try:
            async with aiofiles.open(video_path, "wb") as f:
                total_size = 0
                with self.rich_progress(desc=video_name) as update_progress:
                    for url in await self._get_m3u8_slices(m3u8_url):
                        async with self.client.stream("GET", url, headers=ext_headers) as response:
                            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                                await f.write(chunk)
                                total_size += len(chunk)
                                update_progress(advance=len(chunk), total=total_size)
        except httpx.HTTPError:
            await safe_unlink(video_path)
            logger.exception("m3u8 视频下载失败")
            raise DownloadException("m3u8 视频下载失败")

        return video_path

    async def _get_m3u8_slices(self, m3u8_url: str):
        """获取 m3u8 切片"""

        response = await self.client.get(m3u8_url)
        response.raise_for_status()

        slices_text = response.text
        slices: list[str] = []

        for line in slices_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            slices.append(urljoin(m3u8_url, line))

        return slices


downloader: StreamDownloader = StreamDownloader()
"""全局下载器实例，提供下载功能"""
yt_dlp_downloader = None
"""yt-dlp 下载器实例，提供下载视频功能，若 yt-dlp 未安装则为 None"""

if is_module_available("yt_dlp"):
    from .ytdlp import YtdlpDownloader

    yt_dlp_downloader = YtdlpDownloader()


@get_driver().on_shutdown
async def close_download_client():
    logger.debug("正在关闭下载器...")
    await downloader.aclose()
