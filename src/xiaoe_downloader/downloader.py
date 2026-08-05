"""直链与 HLS 下载实现。"""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import TextIO
from urllib.parse import unquote, urljoin, urlparse

import httpx
import imageio_ffmpeg

from .client import PRIVATE_KEY_PLACEHOLDER
from .models import DownloadProgress, DownloadResult, MediaSource

INVALID_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
DRM_KEY_FORMATS = ("com.widevine", "com.apple.streamingkeydelivery", "playready")
EXTINF_PATTERN = re.compile(r"^#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
FFMPEG_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):([0-9.]+)")
HLS_URI_PATTERN = re.compile(r'URI="([^"]+)"')
HLS_CONCURRENCY = 8
HLS_MAX_RETRIES = 3
HLS_RETRY_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
ProgressCallback = Callable[[DownloadProgress], None]


@dataclass(frozen=True, slots=True)
class _HlsResource:
    url: str
    path: Path
    duration: float = 0.0


def safe_filename(value: str, fallback: str = "课程") -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:120].rstrip(" .")


def _manifest_duration(manifest: str) -> float | None:
    duration = sum(float(value) for value in EXTINF_PATTERN.findall(manifest))
    return duration if duration > 0 else None


def _timestamp_seconds(value: str) -> float | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _ffmpeg_out_time(values: dict[str, str]) -> float | None:
    microseconds = values.get("out_time_us")
    if microseconds:
        try:
            return max(0.0, int(microseconds) / 1_000_000)
        except ValueError:
            pass
    timestamp = values.get("out_time")
    if timestamp:
        return _timestamp_seconds(timestamp)
    # 较旧的 ffmpeg 虽然字段名是 out_time_ms，实际单位仍为微秒。
    microseconds = values.get("out_time_ms")
    if microseconds:
        try:
            return max(0.0, int(microseconds) / 1_000_000)
        except ValueError:
            pass
    return None


class _ProgressTracker:
    """把字节进度或媒体时长进度归一化为同一种回调。"""

    def __init__(
        self,
        callback: ProgressCallback | None,
        *,
        total_bytes: int | None = None,
        total_duration: float | None = None,
    ) -> None:
        self.callback = callback
        self.total_bytes = total_bytes if total_bytes and total_bytes > 0 else None
        self.total_duration = total_duration if total_duration and total_duration > 0 else None
        self.started_at = time.monotonic()
        self.last_emit_at = self.started_at
        self.last_speed_at = self.started_at
        self.last_speed_bytes = 0
        self.smoothed_speed: float | None = None

    def report(
        self,
        downloaded_bytes: int,
        *,
        completed_duration: float | None = None,
        finished: bool = False,
        force: bool = False,
    ) -> None:
        if self.callback is None:
            return

        now = time.monotonic()
        if not force and not finished and now - self.last_emit_at < 0.1:
            return

        downloaded_bytes = max(0, downloaded_bytes)
        speed_elapsed = now - self.last_speed_at
        byte_delta = downloaded_bytes - self.last_speed_bytes
        if speed_elapsed >= 0.2 and byte_delta >= 0:
            current_speed = byte_delta / speed_elapsed
            if current_speed > 0:
                if self.smoothed_speed is None:
                    self.smoothed_speed = current_speed
                else:
                    self.smoothed_speed = self.smoothed_speed * 0.65 + current_speed * 0.35
            self.last_speed_at = now
            self.last_speed_bytes = downloaded_bytes

        elapsed = max(now - self.started_at, 0.001)
        speed = self.smoothed_speed
        if speed is None and downloaded_bytes > 0:
            speed = downloaded_bytes / elapsed

        fraction: float | None = None
        eta_seconds: float | None = None
        if self.total_bytes is not None:
            fraction = min(downloaded_bytes / self.total_bytes, 1.0)
            if speed and speed > 0:
                eta_seconds = max(self.total_bytes - downloaded_bytes, 0) / speed
        elif self.total_duration is not None and completed_duration is not None:
            completed_duration = max(0.0, completed_duration)
            fraction = min(completed_duration / self.total_duration, 1.0)
            media_rate = completed_duration / elapsed
            if media_rate > 0:
                eta_seconds = max(self.total_duration - completed_duration, 0) / media_rate

        if finished:
            fraction = 1.0
            eta_seconds = 0.0

        self.callback(
            DownloadProgress(
                fraction=fraction,
                downloaded_bytes=downloaded_bytes,
                total_bytes=self.total_bytes,
                bytes_per_second=speed,
                eta_seconds=eta_seconds,
            )
        )
        self.last_emit_at = now


def _drain_ffmpeg_stderr(
    stream: TextIO,
    lines: deque[str],
    detected_duration: list[float | None],
) -> None:
    for raw_line in stream:
        line = raw_line.strip()
        if not line:
            continue
        lines.append(line)
        if detected_duration[0] is None:
            match = FFMPEG_DURATION_PATTERN.search(line)
            if match:
                detected_duration[0] = (
                    int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                )


class MediaDownloader:
    def __init__(self, output_dir: Path, user_agent: str) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent

    def download(
        self,
        source: MediaSource,
        index: int,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        if source.extension == ".m3u8":
            return self._download_hls(source, index, progress_callback)
        return self._download_file(source, index, progress_callback)

    def _base_name(self, source: MediaSource, index: int) -> str:
        return f"{index:03d}_{safe_filename(source.title)}"

    def _headers(self, source: MediaSource) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Referer": source.referer,
        }
        for name, value in source.headers.items():
            if name.lower() in {"origin", "authorization", "app-token", "cookie"}:
                headers[name] = value
        return headers

    @staticmethod
    def _can_parallelize_hls(manifest: str) -> bool:
        """判断播放列表是否是可一次性并发下载的 VOD media playlist。"""

        if "#EXT-X-ENDLIST" not in manifest:
            return False
        for raw_line in manifest.splitlines():
            line = raw_line.strip()
            if line.startswith(
                (
                    "#EXT-X-STREAM-INF",
                    "#EXT-X-I-FRAME-STREAM-INF",
                    "#EXT-X-BYTERANGE",
                )
            ):
                return False
            if line.startswith("#EXT-X-MEDIA:") and "URI=" in line:
                return False
        return True

    @staticmethod
    def _replace_hls_uri(line: str, value: str) -> str:
        return HLS_URI_PATTERN.sub(f'URI="{value}"', line, count=1)

    def _prepare_parallel_hls_manifest(
        self,
        source: MediaSource,
        manifest: str,
        temp_dir: Path,
    ) -> tuple[str, list[_HlsResource]]:
        """把网络 URI 改成本地文件，并返回需要并发下载的资源清单。"""

        resources: list[_HlsResource] = []
        segments: list[_HlsResource] = []
        auxiliary_names: dict[str, str] = {}
        local_lines: list[str] = []
        pending_duration = 0.0

        private_key_name: str | None = None
        if PRIVATE_KEY_PLACEHOLDER in manifest:
            if source.private_key is None:
                raise RuntimeError("私有 HLS 播放列表缺少解密密钥。")
            private_key_name = "key.bin"
            (temp_dir / private_key_name).write_bytes(source.private_key)

        def localize_auxiliary(uri: str, prefix: str) -> str:
            if uri.startswith(("data:", "file:")):
                return uri
            resolved = urljoin(source.url, uri)
            local_name = auxiliary_names.get(resolved)
            if local_name is None:
                local_name = f"{prefix}_{len(auxiliary_names):06d}.bin"
                auxiliary_names[resolved] = local_name
                resource = _HlsResource(resolved, temp_dir / local_name)
                resources.append(resource)
            return local_name

        for raw_line in manifest.splitlines():
            line = raw_line.strip()
            if not line:
                local_lines.append("")
                continue

            if line.startswith("#EXTINF:"):
                match = EXTINF_PATTERN.match(line)
                if match:
                    pending_duration = float(match.group(1))
                local_lines.append(line)
                continue

            if line.startswith("#EXT-X-KEY:"):
                match = HLS_URI_PATTERN.search(line)
                if match:
                    uri = match.group(1)
                    if uri == PRIVATE_KEY_PLACEHOLDER and private_key_name is not None:
                        local_uri = private_key_name
                    else:
                        local_uri = localize_auxiliary(uri, "key")
                    line = self._replace_hls_uri(line, local_uri)
                local_lines.append(line)
                continue

            if line.startswith("#EXT-X-MAP:"):
                match = HLS_URI_PATTERN.search(line)
                if match:
                    local_uri = localize_auxiliary(match.group(1), "init")
                    line = self._replace_hls_uri(line, local_uri)
                local_lines.append(line)
                continue

            if line.startswith("#"):
                local_lines.append(line)
                continue

            segment_name = f"segment_{len(segments):06d}.bin"
            segment = _HlsResource(
                urljoin(source.url, line),
                temp_dir / segment_name,
                pending_duration,
            )
            segments.append(segment)
            resources.append(segment)
            local_lines.append(segment_name)
            pending_duration = 0.0

        if not segments:
            raise RuntimeError("HLS 播放列表中没有找到媒体分片。")
        return "\n".join(local_lines) + "\n", resources

    @staticmethod
    def _is_retryable_hls_error(error: Exception) -> bool:
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in HLS_RETRY_STATUS_CODES
        return isinstance(error, (httpx.HTTPError, OSError, RuntimeError))

    @staticmethod
    def _hls_error_detail(error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            return f"HTTP {error.response.status_code}"
        if isinstance(error, httpx.TimeoutException):
            return "请求超时"
        if isinstance(error, OSError):
            return str(error)
        return type(error).__name__

    async def _download_hls_resources_async(
        self,
        resources: list[_HlsResource],
        headers: dict[str, str],
        tracker: _ProgressTracker,
    ) -> None:
        limits = httpx.Limits(
            max_connections=HLS_CONCURRENCY,
            max_keepalive_connections=HLS_CONCURRENCY,
        )
        timeout = httpx.Timeout(30, read=None)
        completed_bytes = 0
        completed_duration = 0.0

        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=headers,
            http2=False,
            limits=limits,
            timeout=timeout,
            verify=False,
        ) as client:

            async def download_one(resource: _HlsResource) -> None:
                nonlocal completed_bytes, completed_duration
                partial = resource.path.with_name(resource.path.name + ".part")
                for attempt in range(HLS_MAX_RETRIES + 1):
                    received_bytes = 0
                    try:
                        async with client.stream("GET", resource.url) as response:
                            response.raise_for_status()
                            with partial.open("wb") as stream:
                                async for chunk in response.aiter_bytes(1024 * 1024):
                                    if not chunk:
                                        continue
                                    stream.write(chunk)
                                    received_bytes += len(chunk)
                                    tracker.report(
                                        completed_bytes + received_bytes,
                                        completed_duration=completed_duration,
                                    )
                        if received_bytes <= 0:
                            raise RuntimeError("服务器返回了空分片")
                        partial.replace(resource.path)
                        completed_bytes += received_bytes
                        completed_duration += resource.duration
                        tracker.report(
                            completed_bytes,
                            completed_duration=completed_duration,
                        )
                        return
                    except asyncio.CancelledError:
                        partial.unlink(missing_ok=True)
                        raise
                    except Exception as error:
                        partial.unlink(missing_ok=True)
                        tracker.report(
                            completed_bytes,
                            completed_duration=completed_duration,
                        )
                        if attempt >= HLS_MAX_RETRIES or not self._is_retryable_hls_error(error):
                            raise RuntimeError(
                                f"HLS 分片下载失败：{resource.path.name}，"
                                f"{self._hls_error_detail(error)}"
                            ) from error
                        await asyncio.sleep(min(2**attempt, 8))

            tasks = [asyncio.create_task(download_one(resource)) for resource in resources]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    def _download_hls_resources(
        self,
        resources: list[_HlsResource],
        headers: dict[str, str],
        tracker: _ProgressTracker,
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._download_hls_resources_async(resources, headers, tracker))
            return

        # Playwright 的同步 API 会在当前线程维持一个 asyncio 事件循环，
        # 因此不能在这里再次调用 asyncio.run()。下载接口仍需同步阻塞，
        # 将并发 HLS 请求放到独立线程的事件循环中执行。
        errors: list[BaseException] = []

        def run_in_thread() -> None:
            try:
                asyncio.run(self._download_hls_resources_async(resources, headers, tracker))
            except BaseException as error:
                errors.append(error)

        worker = Thread(target=run_in_thread, name="xiaoe-hls-downloader")
        worker.start()
        worker.join()
        if errors:
            raise errors[0]

    def _ffmpeg_command(
        self,
        input_value: str,
        partial: Path,
        headers: dict[str, str],
        *,
        local_manifest: bool,
    ) -> list[str]:
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "info",
            "-progress",
            "pipe:1",
            "-nostats",
            "-y",
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto,data",
            "-http_multiple",
            "1",
        ]
        if not local_manifest:
            header_block = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            if header_block:
                command.extend(("-headers", header_block))
        command.extend(
            (
                "-allowed_extensions",
                "ALL",
                "-i",
                input_value,
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(partial),
            )
        )
        return command

    def _download_file(
        self,
        source: MediaSource,
        index: int,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        suffix = source.extension
        if not suffix:
            suffix = Path(unquote(urlparse(source.url).path)).suffix or ".bin"
        target = self.output_dir / f"{self._base_name(source, index)}{suffix}"
        if target.is_file() and target.stat().st_size > 0:
            return DownloadResult(target, skipped=True)

        partial = target.with_suffix(target.suffix + ".part")
        with httpx.stream(
            "GET",
            source.url,
            headers=self._headers(source),
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=None),
            verify=False,
        ) as response:
            response.raise_for_status()
            try:
                total_bytes = int(response.headers.get("Content-Length", ""))
            except ValueError:
                total_bytes = None
            tracker = _ProgressTracker(progress_callback, total_bytes=total_bytes)
            downloaded_bytes = 0
            tracker.report(0, force=True)
            with partial.open("wb") as stream:
                for chunk in response.iter_bytes(1024 * 1024):
                    stream.write(chunk)
                    downloaded_bytes += len(chunk)
                    tracker.report(downloaded_bytes)
            tracker.report(downloaded_bytes, finished=True)
        partial.replace(target)
        return DownloadResult(target)

    def _download_hls(
        self,
        source: MediaSource,
        index: int,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        headers = self._headers(source)
        if source.manifest_text is None:
            with httpx.Client(
                follow_redirects=True,
                timeout=30,
                headers=headers,
                verify=False,
            ) as client:
                response = client.get(source.url)
                response.raise_for_status()
                manifest = response.text
        else:
            manifest = source.manifest_text

        lowered = manifest.lower()
        if any(key_format in lowered for key_format in DRM_KEY_FORMATS):
            raise RuntimeError("检测到 Widevine/FairPlay/PlayReady，已跳过。")

        target = self.output_dir / f"{self._base_name(source, index)}.mp4"
        if target.is_file() and target.stat().st_size > 0:
            return DownloadResult(target, skipped=True)
        partial = target.with_suffix(".part.mp4")
        detected_duration = [_manifest_duration(manifest)]
        tracker = _ProgressTracker(
            progress_callback,
            total_duration=detected_duration[0],
        )
        tracker.report(0, completed_duration=0, force=True)

        try:
            with tempfile.TemporaryDirectory(prefix=".xiaoe-", dir=self.output_dir) as temp_value:
                temp_dir = Path(temp_value)
                if self._can_parallelize_hls(manifest):
                    local_manifest, resources = self._prepare_parallel_hls_manifest(
                        source,
                        manifest,
                        temp_dir,
                    )
                    manifest_path = temp_dir / "index.m3u8"
                    manifest_path.write_text(local_manifest, encoding="utf-8", newline="\n")
                    self._download_hls_resources(resources, headers, tracker)
                    command = self._ffmpeg_command(
                        str(manifest_path),
                        partial,
                        headers,
                        local_manifest=True,
                    )
                else:
                    input_value = source.url
                    if source.manifest_text is not None:
                        if PRIVATE_KEY_PLACEHOLDER in manifest:
                            if source.private_key is None:
                                raise RuntimeError("私有 HLS 播放列表缺少解密密钥。")
                            key_path = temp_dir / "key.bin"
                            key_path.write_bytes(source.private_key)
                            manifest = manifest.replace(PRIVATE_KEY_PLACEHOLDER, key_path.name)
                        manifest_path = temp_dir / "index.m3u8"
                        manifest_path.write_text(manifest, encoding="utf-8", newline="\n")
                        input_value = str(manifest_path)
                    command = self._ffmpeg_command(
                        input_value,
                        partial,
                        headers,
                        local_manifest=source.manifest_text is not None,
                    )
                return_code, error_lines = self._run_ffmpeg(
                    command,
                    partial,
                    tracker,
                    detected_duration,
                )
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        if return_code != 0:
            partial.unlink(missing_ok=True)
            detail = error_lines[-1] if error_lines else "未知错误"
            raise RuntimeError(f"ffmpeg 下载失败：{detail}")
        tracker.report(partial.stat().st_size, finished=True)
        partial.replace(target)
        return DownloadResult(target)

    @staticmethod
    def _run_ffmpeg(
        command: list[str],
        partial: Path,
        tracker: _ProgressTracker,
        detected_duration: list[float | None],
    ) -> tuple[int, list[str]]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        error_lines: deque[str] = deque(maxlen=30)
        stderr_thread = Thread(
            target=_drain_ffmpeg_stderr,
            args=(process.stderr, error_lines, detected_duration),
            daemon=True,
        )
        stderr_thread.start()
        values: dict[str, str] = {}
        try:
            for raw_line in process.stdout:
                key, separator, value = raw_line.strip().partition("=")
                if not separator:
                    continue
                values[key] = value
                if key != "progress":
                    continue
                if tracker.total_duration is None and detected_duration[0] is not None:
                    tracker.total_duration = detected_duration[0]
                completed_duration = _ffmpeg_out_time(values)
                try:
                    downloaded_bytes = int(values.get("total_size", ""))
                except ValueError:
                    downloaded_bytes = partial.stat().st_size if partial.exists() else 0
                tracker.report(
                    downloaded_bytes,
                    completed_duration=completed_duration,
                    force=value == "end",
                )
                values.clear()
            return_code = process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            process.stdout.close()
            stderr_thread.join(timeout=2)
            process.stderr.close()

        return return_code, list(error_lines)
