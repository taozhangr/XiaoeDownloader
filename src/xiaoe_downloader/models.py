"""小鹅通课程、目录项与媒体模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

RESOURCE_TYPE_NAMES = {
    1: "图文",
    2: "音频",
    3: "视频",
    5: "会员",
    6: "专栏",
    8: "大专栏",
    12: "直播",
    16: "打卡",
    20: "电子书",
    25: "训练营",
    34: "练习",
    50: "课程",
    51: "附件",
}


@dataclass(frozen=True, slots=True)
class CourseReference:
    """从用户链接中提取出的资源标识。"""

    original_url: str
    resolved_url: str
    resource_id: str
    declared_type: int | None = None


@dataclass(frozen=True, slots=True)
class CourseIdentity:
    """PC 学员端返回的已购课程身份。"""

    resource_id: str
    resource_type: int
    title: str
    app_id: str
    user_id: str
    original_url: str
    store_origins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CourseItem:
    """供用户选择的课程内容或附件。"""

    order: int
    title: str
    resource_id: str
    resource_type: int
    product_id: str
    direct_url: str | None = None

    @property
    def type_name(self) -> str:
        return RESOURCE_TYPE_NAMES.get(self.resource_type, str(self.resource_type))

    @property
    def key(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"


@dataclass(frozen=True, slots=True)
class CourseCatalog:
    """一门课程的身份、目录与相关附件。"""

    identity: CourseIdentity
    lessons: tuple[CourseItem, ...]
    attachments: tuple[CourseItem, ...] = ()

    @property
    def items(self) -> list[CourseItem]:
        return [*self.lessons, *self.attachments]


@dataclass(frozen=True, slots=True)
class MediaSource:
    """已经由小鹅通接口解析出的可下载媒体。"""

    url: str
    title: str
    referer: str
    headers: dict[str, str] = field(default_factory=dict)
    extension_hint: str = ""
    manifest_text: str | None = None
    private_key: bytes | None = field(default=None, repr=False)

    @property
    def extension(self) -> str:
        if self.extension_hint:
            return self.extension_hint
        path = self.url.lower().split("?", 1)[0]
        for suffix in (
            ".m3u8",
            ".mp4",
            ".m4a",
            ".mp3",
            ".aac",
            ".pdf",
            ".zip",
            ".txt",
        ):
            if path.endswith(suffix):
                return suffix
        return ""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """下载器向终端界面报告的统一进度快照。"""

    fraction: float | None
    downloaded_bytes: int
    total_bytes: int | None
    bytes_per_second: float | None
    eta_seconds: float | None
