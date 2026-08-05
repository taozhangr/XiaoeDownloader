"""调用系统 Chrome/Edge 完成可见登录与课程链接跳转。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .config import LOGIN_URL, PLATFORM_ORIGIN


@dataclass(frozen=True, slots=True)
class ResolvedPage:
    title: str
    url: str


class XiaoetongBrowser:
    """使用系统浏览器内核和本程序专用的持久化用户目录。"""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.browser_name = ""

    def __enter__(self) -> XiaoetongBrowser:
        self._playwright = sync_playwright().start()
        self.context = self._launch_context()
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.context is not None:
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _launch_context(self) -> BrowserContext:
        assert self._playwright is not None
        options = {
            "user_data_dir": str(self.profile_dir),
            "headless": False,
            "accept_downloads": False,
            "viewport": {"width": 1280, "height": 860},
            "locale": "zh-CN",
        }
        errors: list[str] = []
        for channel, label in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")):
            try:
                context = self._playwright.chromium.launch_persistent_context(
                    channel=channel,
                    **options,
                )
                self.browser_name = label
                return context
            except Exception as error:  # Playwright 对浏览器缺失没有稳定异常类型
                errors.append(f"{label}: {error}")
        details = "\n".join(errors)
        raise RuntimeError(
            "没有找到可供自动化的系统 Chrome 或 Edge，请先安装其中一个。\n" + details
        )

    def wait_for_login(self, timeout_seconds: int = 600) -> str:
        """打开原程序使用的 PC 登录页，等待有效格式的 p_token。"""

        page = self._require_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError("登录浏览器已关闭。")
            if token := self.platform_token():
                return token
            page.wait_for_timeout(1_000)
        raise TimeoutError("等待小鹅通登录超时，请重新运行后再试。")

    def platform_token(self) -> str | None:
        for cookie in self._require_context().cookies([PLATFORM_ORIGIN]):
            if cookie["name"] == "p_token" and cookie["value"]:
                return str(cookie["value"])
        return None

    def clear_platform_login(self) -> None:
        """清除失效的平台 Cookie，但保留此程序的其他浏览器设置。"""

        self._require_context().clear_cookies(name="p_token")

    def resolve_course_page(self, url: str) -> ResolvedPage:
        """让系统浏览器执行店铺重定向，返回最终链接与页面标题。"""

        page = self._require_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            page.wait_for_timeout(2_000)
        return ResolvedPage(page.title().strip() or "当前课程", page.url)

    def install_store_cookies(
        self,
        origins: tuple[str, ...],
        *,
        ko_token: str,
        app_id: str,
    ) -> None:
        """把 PC 客户端换取的店铺身份同步给当前浏览器窗口。"""

        cookies: list[dict[str, object]] = []
        values = {
            "ko_token": ko_token,
            "pc_user_key": ko_token,
            "app_id": app_id,
        }
        for origin in origins:
            parsed = urlsplit(origin)
            if not parsed.hostname:
                continue
            for name, value in values.items():
                cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": parsed.hostname,
                        "path": "/",
                        "secure": parsed.scheme == "https",
                    }
                )
        if cookies:
            self._require_context().add_cookies(cookies)

    def show_course(self, url: str) -> None:
        page = self._require_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("浏览器尚未启动。")
        return self.page

    def _require_context(self) -> BrowserContext:
        if self.context is None:
            raise RuntimeError("浏览器尚未启动。")
        return self.context
