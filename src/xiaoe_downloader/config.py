"""本地路径与小鹅通 PC 客户端协议常量。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

APP_NAME = "小鹅通下载器"
PLATFORM_ORIGIN = "https://study.xiaoe-tech.com"
LOGIN_URL = f"{PLATFORM_ORIGIN}/t_l/pcClientLogin"
PC_CLIENT_APP_ID = "apposolbh821040"
PC_CLIENT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) xiaoe-tong-client/1.2.11 "
    "Chrome/112.0.5615.204 Electron/24.8.8 Safari/537.36"
)
STORE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 5.1.1; SM-N976N Build/QP1A.190711.020; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/74.0.3729.136 Mobile Safari/537.36 MMWEBID/6627 "
    "MicroMessenger/8.0.27.2220(0x00000000) WeChat/arm32 Weixin "
    "NetType/WIFI Language/zh_CN ABI/arm32 MiniProgramEnv/android 5QcPp2doIU6Z4SNi"
)


@dataclass(frozen=True, slots=True)
class AppPaths:
    project_root: Path
    browser_profile: Path
    downloads: Path

    @classmethod
    def create(cls) -> AppPaths:
        project_root = Path(__file__).resolve().parents[2]
        app_data = user_data_path("XiaoeDownloader", appauthor=False, ensure_exists=True)
        browser_profile = app_data / "browser-profile"
        browser_profile.mkdir(parents=True, exist_ok=True)
        downloads = project_root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        return cls(project_root, browser_profile, downloads)
