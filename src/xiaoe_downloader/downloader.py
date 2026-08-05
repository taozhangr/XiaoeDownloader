"""直链与 HLS 下载实现。"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import TextIO
from urllib.parse import unquote, urlparse

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
ProgressCallback = Callable[[DownloadProgress], None]


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

                header_block = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
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
                ]
                # ffmpeg 不能把 HTTP 的 -headers 选项应用到本地 m3u8 输入，私有
                # 播放列表中的 CDN 分片已带签名，不需要额外请求头。
                if header_block and source.manifest_text is None:
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
