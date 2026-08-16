"""使用 DrissionPage 启动浏览器, 用于解析需要执行前端 JS 的页面(抖音图文/实况图).

优先复用系统已安装的 Chrome/Edge, 其次 Playwright/Puppeteer 缓存目录的 Chromium.
DrissionPage 的接口是同步阻塞的, 这里统一用 asyncio.to_thread 包装, 避免阻塞事件循环.
"""

from __future__ import annotations

import os
import asyncio
import platform
import contextlib
from pathlib import Path
from typing import Any

from nonebot import logger, get_driver

from .config import pconfig

_system = platform.system()
_driver = get_driver()


class BrowserManager:
    BROWSER = None
    """Chromium 实例, 惰性启动"""
    _init_lock: asyncio.Lock = asyncio.Lock()
    _last_used: float | None = None
    _idle_timeout: float = 60 * 30
    """浏览器空闲超时时间(s)"""
    _idle_task: asyncio.Task[None] | None = None
    _shutdown_hooked: bool = False
    _pid: int | None = None
    """浏览器主进程 PID, 用于兜底清理进程树"""

    @staticmethod
    def _find_browser_from_system() -> str:
        """从系统默认安装位置寻找浏览器"""
        if _system == "Darwin":
            for path in (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ):
                if Path(path).is_file():
                    return path
        elif _system == "Windows":
            import winreg

            for path in (
                r"SOFTWARE\Clients\StartMenuInternet\Google Chrome\DefaultIcon",
                r"SOFTWARE\Clients\StartMenuInternet\Microsoft Edge\DefaultIcon",
            ):
                with contextlib.suppress(FileNotFoundError):
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                    value, _ = winreg.QueryValueEx(key, "")
                    # DefaultIcon 的值通常形如 "C:\\...\\chrome.exe,0"
                    return value.split(",")[0]
        return ""

    @staticmethod
    def _find_browser_from_playwright() -> str:
        """从 ms-playwright 默认目录寻找 Chromium 可执行文件"""
        if browser_path := os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            base = Path(browser_path)
        else:
            home = Path.home()
            if _system == "Darwin":
                base = home / "Library" / "Caches" / "ms-playwright"
            elif _system == "Windows":
                base = home / "AppData" / "Local" / "ms-playwright"
            else:
                base = home / ".cache" / "ms-playwright"
        if not base.is_dir():
            return ""

        for chromium_dir in sorted(base.glob("chromium-*"), reverse=True):
            if not chromium_dir.is_dir():
                continue
            if _system == "Windows":
                candidates = list(chromium_dir.glob("chrome-win*/chrome.exe"))
            elif _system == "Darwin":
                candidates = [chromium_dir / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"]
            else:
                candidates = list(chromium_dir.glob("chrome-linux*/chrome"))
            for exe in candidates:
                if exe.is_file():
                    return str(exe.resolve())
        return ""

    @classmethod
    def _resolve_browser_path(cls) -> str:
        """按优先级解析浏览器路径"""
        if pconfig.browser_path:
            return pconfig.browser_path
        if path := cls._find_browser_from_system():
            return path
        if path := cls._find_browser_from_playwright():
            return path
        raise RuntimeError("无法找到可启动的浏览器, 请在配置中设置 parser_browser_path")

    @classmethod
    def _touch(cls) -> None:
        """更新最近使用时间戳"""
        cls._last_used = asyncio.get_event_loop().time()

    @classmethod
    async def _idle_watcher(cls) -> None:
        """后台协程: 浏览器长时间空闲时自动关闭以节省资源"""
        try:
            while cls.BROWSER is not None:
                await asyncio.sleep(cls._idle_timeout / 2)
                if cls.BROWSER is None or cls._last_used is None:
                    continue
                now = asyncio.get_event_loop().time()
                if now - cls._last_used > cls._idle_timeout:
                    logger.info(f"Browser idle for {int(now - cls._last_used)}s, auto quitting.")
                    await cls.quit()
                    break
        finally:
            cls._idle_task = None

    @classmethod
    def _start_sync(cls) -> None:
        """同步启动浏览器(在线程中调用)"""
        from DrissionPage import Chromium, ChromiumOptions

        browser_path = cls._resolve_browser_path()
        if _system == "Linux" and not pconfig.headless:
            logger.warning("You are running on Linux. If there is no desktop environment, please enable headless mode.")

        # 启动前自愈: 清扫上次未正常关闭而残留的孤儿进程(此时无存活实例, 全清)
        with contextlib.suppress(Exception):
            cls._sweep_orphans()

        logger.info(f"Launching browser from {browser_path}")
        co = ChromiumOptions()
        co.mute(True)
        co.auto_port(True)
        co.headless(pconfig.headless)
        co.set_argument("--no-sandbox")
        co.set_argument("--guest")
        co.remove_extensions()
        co.set_browser_path(browser_path)
        cls.BROWSER = Chromium(co)
        with contextlib.suppress(Exception):
            cls._pid = cls.BROWSER.process_id

    @classmethod
    async def ensure_started(cls) -> None:
        """确保浏览器已启动(惰性初始化)"""
        if cls.BROWSER is not None:
            cls._touch()
            return
        async with cls._init_lock:
            if cls.BROWSER is None:
                await asyncio.to_thread(cls._start_sync)
                cls._touch()
                cls._idle_task = asyncio.create_task(cls._idle_watcher())
                if not cls._shutdown_hooked:
                    _driver.on_shutdown(cls.quit)
                    cls._shutdown_hooked = True

    @classmethod
    def _get_html_sync(cls, url: str, wait: float) -> str:
        """同步获取页面渲染后的 HTML(在线程中调用)"""
        import time

        assert cls.BROWSER is not None
        tab = cls.BROWSER.new_tab()
        try:
            tab.set.load_mode.eager()
            tab.get(url)
            if wait > 0:
                time.sleep(wait)
            return tab.html
        finally:
            with contextlib.suppress(Exception):
                tab.close()

    @classmethod
    async def get_html(cls, url: str, wait: float = 3.0) -> str:
        """打开 url, 等待前端渲染后返回 HTML"""
        await cls.ensure_started()
        cls._touch()
        return await asyncio.to_thread(cls._get_html_sync, url, wait)

    @classmethod
    def _get_json_response_sync(
        cls,
        url: str,
        target: str,
        timeout: float,
    ) -> dict[str, Any]:
        """打开页面并捕获指定 XHR/Fetch 请求的 JSON 响应。"""
        assert cls.BROWSER is not None
        tab = cls.BROWSER.new_tab()
        try:
            tab.set.load_mode.eager()
            tab.listen.start(
                targets=target,
                method="GET",
                res_type=("XHR", "FETCH"),
            )
            tab.get(url)
            packet = tab.listen.wait(timeout=timeout, raise_err=False)
            if not packet:
                raise TimeoutError(f"等待页面请求超时: {target}")
            if packet.is_failed:
                raise RuntimeError(f"页面请求失败: {packet.url}")

            body = packet.response.body
            if not isinstance(body, dict):
                raise RuntimeError(f"页面请求未返回 JSON 对象: {packet.url}")
            return body
        finally:
            with contextlib.suppress(Exception):
                tab.stop_loading()
            with contextlib.suppress(Exception):
                tab.listen.stop()
            with contextlib.suppress(Exception):
                tab.close()

    @classmethod
    async def get_json_response(
        cls,
        url: str,
        target: str,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """异步打开页面并返回指定 XHR/Fetch 请求的 JSON 响应。"""
        await cls.ensure_started()
        cls._touch()
        return await asyncio.to_thread(
            cls._get_json_response_sync,
            url,
            target,
            timeout,
        )

    @classmethod
    def _get_heybox_device_id_sync(cls) -> tuple[str, str]:
        """同步获取小黑盒设备 id 与浏览器 UA(在线程中调用).

        打开小黑盒首页, 等待指纹脚本 fp.min.js 加载完成后执行
        window.SMSdk.getDeviceId() 拿到设备 id, 用于 API 请求鉴权 cookie.
        同时返回浏览器真实 UA, 使后续 httpx 请求指纹与取 token 时一致, 降低被风控概率.
        """
        assert cls.BROWSER is not None
        tab = cls.BROWSER.new_tab()
        try:
            tab.set.load_mode.none()
            tab.listen.start(targets="fp.min.js", method="get", res_type="Script")
            tab.get("https://www.xiaoheihe.cn/")
            tab.listen.wait()
            tab.listen.stop()
            tab.stop_loading()
            device_id = tab.run_js("window.SMSdk.getDeviceId()", as_expr=True)
            user_agent = tab.run_js("navigator.userAgent", as_expr=True) or ""
            return device_id, user_agent
        finally:
            with contextlib.suppress(Exception):
                tab.close()

    @classmethod
    async def get_heybox_device_id(cls) -> tuple[str, str]:
        """获取小黑盒设备 id 与浏览器 UA"""
        await cls.ensure_started()
        cls._touch()
        return await asyncio.to_thread(cls._get_heybox_device_id_sync)

    @staticmethod
    def _sweep_orphans(keep_pid: int | None = None) -> int:
        """清扫所有本插件启动的 DrissionPage 孤儿浏览器进程.

        通过命令行里的 `DrissionPage\\autoPortData` 特征精确匹配, 只杀本插件起的进程,
        绝不误伤用户正常的 Chrome/Edge. keep_pid 及其子进程会被保留(当前存活实例).

        返回杀掉的进程数. 用于启动前自愈: 即使上次未正常关闭(进程异常退出、
        崩溃、或多进程并发未走 on_shutdown), 也不会累积残留实例.
        """
        try:
            import psutil
        except ImportError:
            return 0

        # 收集需要保留的 PID 集合(当前实例的主进程及其所有子进程)
        keep: set[int] = set()
        if keep_pid is not None:
            keep.add(keep_pid)
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                for child in psutil.Process(keep_pid).children(recursive=True):
                    keep.add(child.pid)

        marker = os.path.join("DrissionPage", "autoPortData")
        killed = 0
        for proc in psutil.process_iter(["name", "cmdline"]):
            if proc.pid in keep:
                continue
            name = (proc.info.get("name") or "").lower()
            if "chrome" not in name and "msedge" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not any(marker in str(arg) for arg in cmdline):
                continue
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
                killed += 1
        if killed:
            logger.info(f"Swept {killed} orphan DrissionPage browser process(es) before start")
        return killed

    @staticmethod
    def _kill_process_tree(pid: int | None) -> None:
        """兜底: 用 psutil 杀掉浏览器主进程及其所有子进程(在线程中调用)"""
        if pid is None:
            return
        try:
            import psutil
        except ImportError:
            return
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        procs = parent.children(recursive=True)
        procs.append(parent)
        for proc in procs:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
        # 等待回收, 避免僵尸进程
        with contextlib.suppress(Exception):
            psutil.wait_procs(procs, timeout=5)

    @classmethod
    async def quit(cls) -> None:
        """关闭浏览器"""
        if cls.BROWSER is None:
            # 即便实例已置空, 仍尝试清理可能残留的进程树
            if cls._pid is not None:
                await asyncio.to_thread(cls._kill_process_tree, cls._pid)
                cls._pid = None
            return
        logger.info("Closing browser launched by parser")
        browser = cls.BROWSER
        pid = cls._pid
        cls.BROWSER = None
        cls._last_used = None
        cls._pid = None
        if cls._idle_task is not None:
            cls._idle_task.cancel()
            cls._idle_task = None
        with contextlib.suppress(Exception):
            await asyncio.to_thread(browser.quit, force=True, del_data=True)
        # 兜底清理进程树, 防止 DrissionPage 未杀干净导致进程泄漏
        await asyncio.to_thread(cls._kill_process_tree, pid)
