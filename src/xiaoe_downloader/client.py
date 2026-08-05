"""根据原程序恢复的小鹅通 PC 客户端与店铺接口。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .config import (
    PC_CLIENT_APP_ID,
    PC_CLIENT_USER_AGENT,
    PLATFORM_ORIGIN,
    STORE_USER_AGENT,
)
from .models import (
    CourseCatalog,
    CourseIdentity,
    CourseItem,
    CourseReference,
    MediaSource,
)

TOKEN_CHECK_URL = f"{PLATFORM_ORIGIN}/xe.pc_client.course/platform.token.check/1.0.0"
COURSE_LIST_URLS = (
    f"{PLATFORM_ORIGIN}/xe.pc_client.course/my.all.course.lists.get/3.0.1",
    f"{PLATFORM_ORIGIN}/xe.pc_client.course/my.course.pay.get/2.0.0",
)
STORE_TOKEN_URL = f"{PLATFORM_ORIGIN}/xe.pc_client.course/kotoken.create/1.0.0"
VIDEO_DETAIL_URL = f"{PLATFORM_ORIGIN}/xe.pc_client.course.business.video.detail_info.get/2.0.0"
PRIVATE_KEY_URL = f"{PLATFORM_ORIGIN}/xe.pc_client.xe.course-bff.video.play.private.key"
PRIVATE_KEY_PLACEHOLDER = "__XIAOE_PRIVATE_KEY__"

RESOURCE_ID_PATTERN = re.compile(r"^[a-zA-Z]_[a-zA-Z0-9_-]+$")
DECLARED_TYPES = {
    "text": 1,
    "audio": 2,
    "video": 3,
    "member": 5,
    "column": 6,
    "live": 12,
    "ebook": 20,
    "train": 25,
}
QUOTED_URI_PATTERN = re.compile(r'URI="([^"]+)"')


class XiaoetongError(RuntimeError):
    """小鹅通协议调用失败。"""


class LoginExpiredError(XiaoetongError):
    """PC 学员端登录态已失效。"""


class CourseNotFoundError(XiaoetongError):
    """当前登录账号的 PC 课程列表中没有目标资源。"""


def parse_course_reference(original_url: str, resolved_url: str | None = None) -> CourseReference:
    """从小鹅通公开域名或商家自定义域名链接中提取资源 ID。"""

    final_url = (resolved_url or original_url).strip()
    parsed = urlsplit(final_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请输入完整的 http/https 课程链接")

    segments = [segment for segment in parsed.path.split("/") if segment]
    resource_id = next(
        (segment for segment in reversed(segments) if RESOURCE_ID_PATTERN.fullmatch(segment)),
        "",
    )
    if not resource_id:
        original = urlsplit(original_url.strip())
        original_segments = [segment for segment in original.path.split("/") if segment]
        resource_id = next(
            (
                segment
                for segment in reversed(original_segments)
                if RESOURCE_ID_PATTERN.fullmatch(segment)
            ),
            "",
        )
    if not resource_id:
        raise ValueError("链接中没有找到小鹅通资源 ID")

    lowered = {segment.lower() for segment in segments}
    declared_type = next(
        (resource_type for name, resource_type in DECLARED_TYPES.items() if name in lowered),
        None,
    )
    return CourseReference(original_url.strip(), final_url, resource_id, declared_type)


class XiaoetongClient:
    """复现原程序的小鹅通 PC 客户端协议，不包含原程序的付费服务。"""

    def __init__(self, p_token: str) -> None:
        if not p_token:
            raise LoginExpiredError("没有读取到小鹅通 p_token")
        self._p_token = p_token
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=90),
        )
        # 原程序对商家店铺/H5/CDN 请求使用 verify=False，部分自定义域名证书链
        # 在 Python CA 包中不完整，但系统浏览器可以正常访问。
        self._store_client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=90),
            verify=False,
        )

    def __enter__(self) -> XiaoetongClient:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._store_client.close()

    def verify_login(self) -> bool:
        try:
            payload = self._post_platform(TOKEN_CHECK_URL, {"platform": "pc_client"})
        except XiaoetongError:
            return False
        return payload.get("code") == 0

    def find_course(self, reference: CourseReference) -> CourseIdentity:
        """按原程序顺序遍历 PC 学员端课程列表，补齐 app_id 与 user_id。"""

        for endpoint in COURSE_LIST_URLS:
            for tab_type in range(3):
                page = 1
                while page <= 100:
                    payload = self._post_platform(
                        endpoint,
                        {
                            "tab_type": tab_type,
                            "page": page,
                            "page_size": 30,
                            "platform": "pc_client",
                        },
                    )
                    data = payload.get("data")
                    if not isinstance(data, Mapping):
                        break
                    entries = data.get("list")
                    if not isinstance(entries, list):
                        break
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            continue
                        if str(entry.get("resource_id") or "") != reference.resource_id:
                            continue
                        return self._identity_from_entry(reference, entry)
                    if data.get("is_last") in {1, "1", "true"} or not entries:
                        break
                    page += 1
        raise CourseNotFoundError(
            "当前登录账号的小鹅通 PC 课程列表中没有找到该资源。"
            "请确认浏览器登录的是购买课程时使用的账号。"
        )

    def create_store_token(self, identity: CourseIdentity) -> str:
        payload = self._post_platform(
            STORE_TOKEN_URL,
            {
                "user_id": identity.user_id,
                "app_id": identity.app_id,
                "platform": "pc_client",
            },
        )
        data = payload.get("data")
        token = data.get("token") if isinstance(data, Mapping) else None
        if isinstance(token, Mapping):
            token = token.get("value")
        if not isinstance(token, str) or not token:
            raise XiaoetongError("小鹅通没有返回店铺 ko_token")
        return token

    def load_catalog(self, identity: CourseIdentity, ko_token: str) -> CourseCatalog:
        """从店铺业务接口读取目录，专栏不再依赖页面扫描。"""

        errors: list[str] = []
        for origin in identity.store_origins:
            try:
                lessons = self._load_lessons(origin, identity, ko_token)
                attachments = self._load_attachments(origin, identity, ko_token, len(lessons))
                if lessons or attachments:
                    return CourseCatalog(identity, tuple(lessons), tuple(attachments))
            except XiaoetongError as error:
                errors.append(f"{origin}: {error}")
        details = "; ".join(errors) if errors else "没有可用店铺域名"
        raise XiaoetongError(f"未能读取课程目录：{details}")

    def resolve_media(self, identity: CourseIdentity, item: CourseItem) -> MediaSource:
        if item.direct_url:
            return MediaSource(
                item.direct_url,
                item.title,
                identity.original_url,
                extension_hint=self._extension_from_url(item.direct_url),
            )
        if item.resource_type != 3:
            raise XiaoetongError(f"暂不支持下载此类内容：{item.type_name}")
        return self._resolve_video(identity, item)

    def store_cookie_header(self, identity: CourseIdentity, ko_token: str) -> str:
        return f"ko_token={ko_token}; pc_user_key={ko_token}; app_id={identity.app_id}"

    def _identity_from_entry(
        self,
        reference: CourseReference,
        entry: Mapping[str, Any],
    ) -> CourseIdentity:
        app_id = str(entry.get("app_id") or "").strip()
        user_id = str(entry.get("user_id") or "").strip()
        if not app_id or not user_id:
            raise XiaoetongError("课程记录缺少 app_id 或 user_id")
        resource_type = int(entry.get("resource_type") or reference.declared_type or 0)
        title = str(
            entry.get("resource_title") or entry.get("title") or reference.resource_id
        ).strip()

        origins: list[str] = []
        for url in (
            reference.resolved_url,
            reference.original_url,
            str(entry.get("jump_url") or ""),
        ):
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                origin = f"{parsed.scheme}://{parsed.netloc}"
                if parsed.hostname != "study.xiaoe-tech.com" and origin not in origins:
                    origins.append(origin)
        official_origin = f"https://{app_id}.h5.xiaoeknow.com"
        if official_origin not in origins:
            origins.append(official_origin)
        return CourseIdentity(
            reference.resource_id,
            resource_type,
            title,
            app_id,
            user_id,
            reference.original_url,
            tuple(origins),
        )

    def _load_lessons(
        self,
        origin: str,
        identity: CourseIdentity,
        ko_token: str,
    ) -> list[CourseItem]:
        if identity.resource_type not in {5, 6, 8, 18}:
            return [
                CourseItem(
                    1,
                    identity.title,
                    identity.resource_id,
                    identity.resource_type,
                    identity.resource_id,
                )
            ]

        endpoint = f"{origin}/xe.course.business.column.items.get/2.0.0"
        lessons: list[CourseItem] = []
        page = 1
        total: int | None = None
        while page <= 100:
            payload = self._post_store(
                endpoint,
                {
                    "column_id": identity.resource_id,
                    "page_index": str(page),
                    "page_size": "100",
                    "sort": "desc",
                },
                identity,
                ko_token,
            )
            data = payload.get("data")
            if not isinstance(data, Mapping):
                break
            if total is None:
                try:
                    total = int(data.get("total") or 0)
                except (TypeError, ValueError):
                    total = 0
            entries = data.get("list")
            if not isinstance(entries, list) or not entries:
                break
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                resource_id = str(entry.get("resource_id") or "").strip()
                title = str(entry.get("resource_title") or "").strip()
                if not resource_id or not title:
                    continue
                lessons.append(
                    CourseItem(
                        len(lessons) + 1,
                        title,
                        resource_id,
                        int(entry.get("resource_type") or 0),
                        identity.resource_id,
                    )
                )
            if (total and len(lessons) >= total) or len(entries) < 100:
                break
            page += 1
        return lessons

    def _load_attachments(
        self,
        origin: str,
        identity: CourseIdentity,
        ko_token: str,
        lesson_count: int,
    ) -> list[CourseItem]:
        endpoint = f"{origin}/xe.course.business.courseware_list.get/2.0.0"
        payload = self._post_store(
            endpoint,
            {
                "resource_id": identity.resource_id,
                "resource_type": str(identity.resource_type),
            },
            identity,
            ko_token,
        )
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        attachments: list[CourseItem] = []
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            url = str(entry.get("url") or "").replace("\\/", "/").strip()
            title = str(entry.get("title") or "附件").strip()
            if not url:
                continue
            resource_id = str(entry.get("material_id") or f"attachment-{len(attachments) + 1}")
            attachments.append(
                CourseItem(
                    lesson_count + len(attachments) + 1,
                    title,
                    resource_id,
                    51,
                    identity.resource_id,
                    url,
                )
            )
        return attachments

    def _resolve_video(self, identity: CourseIdentity, item: CourseItem) -> MediaSource:
        payload = self._post_platform(
            VIDEO_DETAIL_URL,
            {
                "app_id": identity.app_id,
                "user_id": identity.user_id,
                "buz_data": {
                    "resource_id": item.resource_id,
                    "course_id": item.product_id,
                },
            },
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise XiaoetongError(f"视频“{item.title}”没有返回详情")
        variants = self._decode_video_urls(str(data.get("video_urls") or ""))
        chosen = max(variants, key=self._quality_score, default=None)
        if not chosen:
            raise XiaoetongError(f"视频“{item.title}”没有可用播放地址")
        url = str(chosen.get("url") or "").replace("\\/", "/")
        if not url:
            raise XiaoetongError(f"视频“{item.title}”的播放地址为空")

        extension = self._extension_from_url(url)
        if extension != ".m3u8":
            return MediaSource(url, item.title, identity.original_url, extension_hint=extension)

        raw_manifest = self._get_text(url, identity.original_url)
        lowered = raw_manifest.lower()
        if any(name in lowered for name in ("com.widevine", "fairplay", "playready")):
            raise XiaoetongError("检测到 Widevine/FairPlay/PlayReady，已跳过")

        video_info = data.get("video_info")
        video_info = video_info if isinstance(video_info, Mapping) else {}
        ext_info = chosen.get("ext")
        ext_info = ext_info if isinstance(ext_info, Mapping) else {}
        private_key: bytes | None = None
        if "#EXT-X-KEY" in raw_manifest and bool(data.get("video_info")):
            material_id = str(video_info.get("material_id") or ext_info.get("material_id") or "")
            if material_id:
                private_key = self._get_private_key(identity, material_id)
        manifest = self._rewrite_manifest(
            raw_manifest,
            url,
            ext_info,
            replace_private_key=private_key is not None,
        )
        return MediaSource(
            url,
            item.title,
            identity.original_url,
            extension_hint=".m3u8",
            manifest_text=manifest,
            private_key=private_key,
        )

    def _get_private_key(self, identity: CourseIdentity, material_id: str) -> bytes:
        payload = self._post_platform(
            PRIVATE_KEY_URL,
            {
                "app_id": identity.app_id,
                "user_id": identity.user_id,
                "material_id": material_id,
            },
        )
        data = payload.get("data")
        encrypted = data.get("key") if isinstance(data, Mapping) else None
        if not isinstance(encrypted, str) or not encrypted:
            raise XiaoetongError("小鹅通没有返回私有视频密钥")
        try:
            ciphertext = base64.b64decode(encrypted)
            aes_key = hashlib.md5(b"xiaoePcClient2025").hexdigest()[:16].encode("ascii")
            plaintext = AES.new(aes_key, AES.MODE_ECB).decrypt(ciphertext)
            result = unpad(plaintext, AES.block_size)
        except (ValueError, TypeError) as error:
            raise XiaoetongError("无法解码小鹅通私有视频密钥") from error
        if len(result) not in {16, 24, 32}:
            raise XiaoetongError("小鹅通私有视频密钥长度异常")
        return result

    def _post_platform(self, url: str, data: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(url, json=data, headers=self._platform_headers())
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
            raise XiaoetongError(f"PC 学员端请求失败：{url}") from error
        if not isinstance(payload, dict):
            raise XiaoetongError("PC 学员端返回了未知数据")
        if payload.get("code") != 0:
            message = str(payload.get("msg") or "未知错误")
            if any(word in message for word in ("登录", "token", "Token", "未授权")):
                raise LoginExpiredError(message)
            raise XiaoetongError(message)
        return payload

    def _post_store(
        self,
        url: str,
        data: Mapping[str, Any],
        identity: CourseIdentity,
        ko_token: str,
    ) -> dict[str, Any]:
        headers = {
            "Cookie": self.store_cookie_header(identity, ko_token),
            "Referer": identity.original_url,
            "User-Agent": STORE_USER_AGENT,
        }
        try:
            response = self._store_client.post(url, data=data, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
            raise XiaoetongError(f"店铺请求失败：{url}") from error
        if not isinstance(payload, dict):
            raise XiaoetongError("店铺返回了未知数据")
        if payload.get("code") != 0:
            raise XiaoetongError(str(payload.get("msg") or "店铺接口返回错误"))
        return payload

    def _platform_headers(self) -> dict[str, str]:
        return {
            "User-Agent": PC_CLIENT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "xiaoetongclient://entrance",
            "App-Token": self._p_token,
            "login_app": "eapppc",
            "login_client": "pc",
            "app_id": PC_CLIENT_APP_ID,
            "Cookie": f"p_token={self._p_token}",
        }

    def _get_text(self, url: str, referer: str) -> str:
        try:
            response = self._store_client.get(
                url,
                headers={"Referer": referer, "User-Agent": PC_CLIENT_USER_AGENT},
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise XiaoetongError("无法读取视频播放列表") from error
        if "#EXTM3U" not in response.text:
            raise XiaoetongError("小鹅通返回的播放列表格式异常")
        return response.text

    @staticmethod
    def _decode_video_urls(encoded: str) -> list[dict[str, Any]]:
        if not encoded:
            return []
        value = encoded.removesuffix("__ba").translate(str.maketrans("@#$%", "1234"))
        value += "=" * (-len(value) % 4)
        try:
            decoded = base64.b64decode(value).decode("utf-8")
            payload = json.loads(decoded)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return []
        return (
            [dict(item) for item in payload if isinstance(item, Mapping)]
            if isinstance(payload, list)
            else []
        )

    @staticmethod
    def _quality_score(value: Mapping[str, Any]) -> tuple[int, int]:
        definition = str(value.get("definition_p") or value.get("definition_name") or "")
        match = re.search(r"(\d{3,4})", definition)
        height = int(match.group(1)) if match else 0
        labels = {"原画": 5000, "蓝光": 4000, "超清": 1080, "高清": 720, "标清": 480}
        label_score = max(
            (score for label, score in labels.items() if label in definition), default=0
        )
        return max(height, label_score), int(bool(value.get("is_support", True)))

    def _rewrite_manifest(
        self,
        text: str,
        manifest_url: str,
        ext_info: Mapping[str, Any],
        *,
        replace_private_key: bool,
    ) -> str:
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                lines.append("")
                continue
            if line.startswith("#EXT-X-KEY") and replace_private_key:
                lines.append(
                    QUOTED_URI_PATTERN.sub(
                        f'URI="{PRIVATE_KEY_PLACEHOLDER}"',
                        line,
                        count=1,
                    )
                )
                continue
            if line.startswith("#"):

                def replace_uri(match: re.Match[str]) -> str:
                    resolved = self._absolute_media_url(match.group(1), manifest_url, ext_info)
                    return f'URI="{resolved}"'

                lines.append(
                    QUOTED_URI_PATTERN.sub(
                        replace_uri,
                        line,
                    )
                )
                continue
            lines.append(self._absolute_media_url(line, manifest_url, ext_info))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _absolute_media_url(
        value: str,
        manifest_url: str,
        ext_info: Mapping[str, Any],
    ) -> str:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https", "data", "file"}:
            return value
        host = str(ext_info.get("host") or "").rstrip("/")
        path = str(ext_info.get("path") or "").strip("/")
        if host and path:
            absolute = urljoin(f"{host}/{path}/", parsed.path)
            absolute_parts = urlsplit(absolute)
            extra_query = str(ext_info.get("param") or "").lstrip("?&")
            query = "&".join(part for part in (parsed.query, extra_query) if part)
            return urlunsplit(
                (
                    absolute_parts.scheme,
                    absolute_parts.netloc,
                    absolute_parts.path,
                    query,
                    parsed.fragment,
                )
            )
        return urljoin(manifest_url, value)

    @staticmethod
    def _extension_from_url(url: str) -> str:
        path = urlsplit(url).path.lower()
        for extension in (".m3u8", ".mp4", ".mp3", ".m4a", ".aac", ".pdf", ".zip", ".txt"):
            if path.endswith(extension):
                return extension
        return ""


def unique_items(items: Iterable[CourseItem]) -> list[CourseItem]:
    """按接口首次出现顺序去重，供后续扩展的组合课程使用。"""

    result: dict[str, CourseItem] = {}
    for item in items:
        result.setdefault(item.key, item)
    return list(result.values())
