"""命令行交互与应用编排。"""

from __future__ import annotations

import argparse
import math
import sys
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import imageio_ffmpeg
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import __version__
from .browser import XiaoetongBrowser
from .client import LoginExpiredError, XiaoetongClient, parse_course_reference
from .config import APP_NAME, PC_CLIENT_USER_AGENT, AppPaths
from .downloader import MediaDownloader, safe_filename
from .models import CourseItem, DownloadProgress

console = Console()


def format_size(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units[:-1]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


def format_eta(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--:--"
    seconds = max(0, math.ceil(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def update_download_task(progress: Progress, task_id: int, state: DownloadProgress) -> None:
    if state.total_bytes is None:
        downloaded = format_size(state.downloaded_bytes)
    else:
        downloaded = f"{format_size(state.downloaded_bytes)} / {format_size(state.total_bytes)}"
    speed = (
        f"{format_size(round(state.bytes_per_second))}/s"
        if state.bytes_per_second and state.bytes_per_second > 0
        else "--"
    )
    if state.fraction is None:
        progress.update(
            task_id,
            total=None,
            percent="--.--%",
            downloaded=downloaded,
            speed=speed,
            eta=format_eta(state.eta_seconds),
        )
        return
    percentage = min(max(state.fraction, 0.0), 1.0) * 100
    progress.update(
        task_id,
        total=100,
        completed=percentage,
        percent=f"{percentage:6.2f}%",
        downloaded=downloaded,
        speed=speed,
        eta=format_eta(state.eta_seconds),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--check", action="store_true", help="检查运行环境后退出")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def ask_text(prompt: str, *, default: str | None = None) -> str:
    """使用普通标准输入提问，不创建额外的 asyncio 事件循环。"""

    suffix = f" [dim]({default})[/dim]" if default else ""
    while True:
        value = console.input(f"{prompt}{suffix} ").strip()
        if value:
            return value
        if default is not None:
            return default
        console.print("[yellow]此项不能为空。[/yellow]")


def ask_course_url() -> str:
    while True:
        value = ask_text("请输入课程或专栏链接：")
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return value
        console.print("[yellow]请输入完整的 http/https 课程链接。[/yellow]")


def parse_selection(value: str, item_count: int) -> list[int]:
    """解析 `1,3-5` 形式的选择，空输入表示全部。"""

    if item_count < 1:
        return []
    text = value.strip().lower()
    if not text or text in {"a", "all", "全部"}:
        return list(range(item_count))

    selected: set[int] = set()
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))
    if not selected or min(selected) < 1 or max(selected) > item_count:
        raise ValueError(f"请输入 1 到 {item_count} 范围内的编号")
    return [index - 1 for index in sorted(selected)]


def choose_items(items: list[CourseItem]) -> list[CourseItem]:
    table = Table(title="课程内容", show_lines=False)
    table.add_column("编号", justify="right", style="cyan")
    table.add_column("标题")
    table.add_column("类型", style="dim")
    for index, item in enumerate(items, start=1):
        table.add_row(str(index), item.title, item.type_name)
    console.print(table)
    while True:
        value = console.input("请选择编号（如 1,3-5，直接回车表示全部）： ")
        try:
            indexes = parse_selection(value, len(items))
            return [items[index] for index in indexes]
        except (TypeError, ValueError) as error:
            console.print(f"[yellow]{error}[/yellow]")


def run_check(paths: AppPaths) -> int:
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    table = Table(title="环境检查")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("Python", sys.version.split()[0])
    table.add_row("项目目录", str(paths.project_root))
    table.add_row("浏览器数据", str(paths.browser_profile))
    table.add_row("下载目录", str(paths.downloads))
    table.add_row("ffmpeg", str(ffmpeg) if ffmpeg.is_file() else "未找到")
    table.add_row("系统浏览器", "运行时依次检测 Chrome、Edge")
    console.print(table)
    return 0 if ffmpeg.is_file() else 1


def _login_client(browser: XiaoetongBrowser) -> XiaoetongClient:
    token = browser.wait_for_login()
    client = XiaoetongClient(token)
    if client.verify_login():
        return client

    client.close()
    browser.clear_platform_login()
    console.print("[yellow]已有登录状态已过期，请在浏览器中重新登录。[/yellow]")
    token = browser.wait_for_login()
    client = XiaoetongClient(token)
    if not client.verify_login():
        client.close()
        raise LoginExpiredError("小鹅通登录状态校验失败")
    return client


def run() -> int:
    paths = AppPaths.create()
    console.print(
        Panel.fit(
            "登录小鹅通 → 输入课程链接 → 选择内容 → 下载\n"
            "仅处理当前账号已购且 PC 学员端仍授权访问的内容。",
            title=f"{APP_NAME} {__version__}",
        )
    )
    with XiaoetongBrowser(paths.browser_profile) as browser:
        console.print(f"[cyan]已打开系统 {browser.browser_name}，请完成小鹅通登录……[/cyan]")
        with _login_client(browser) as client:
            console.print("[green]登录成功。[/green]")

            while True:
                course_url = ask_course_url()
                try:
                    page = browser.resolve_course_page(course_url)
                    reference = parse_course_reference(course_url, page.url)
                    break
                except ValueError as error:
                    console.print(f"[yellow]{error}[/yellow]")

            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                transient=True,
                console=console,
            ) as progress:
                task = progress.add_task("正在读取 PC 已购课程和专栏目录……", total=None)
                identity = client.find_course(reference)
                ko_token = client.create_store_token(identity)
                catalog = client.load_catalog(identity, ko_token)
                progress.remove_task(task)

            browser.install_store_cookies(
                identity.store_origins,
                ko_token=ko_token,
                app_id=identity.app_id,
            )
            browser.show_course(course_url)

            console.print(
                f"已识别：[bold]{identity.title}[/bold]，"
                f"{len(catalog.lessons)} 节内容，{len(catalog.attachments)} 个附件。"
            )
            items = catalog.items
            if not items:
                raise RuntimeError("课程目录为空。")
            selected_items = choose_items(items)

            output_value = ask_text("下载目录：", default=str(paths.downloads))
            output_root = Path(output_value).expanduser().resolve()
            output_dir = output_root / safe_filename(identity.title)
            downloader = MediaDownloader(output_dir, PC_CLIENT_USER_AGENT)

            completed = 0
            skipped = 0
            failed = 0
            for position, item in enumerate(selected_items, start=1):
                console.rule(f"[{position}/{len(selected_items)}] {item.title}")
                try:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("{task.description}"),
                        transient=True,
                        console=console,
                    ) as progress:
                        task = progress.add_task("正在获取小鹅通播放地址……", total=None)
                        source = client.resolve_media(identity, item)
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("{task.description}"),
                        BarColumn(bar_width=24),
                        TextColumn("[progress.percentage]{task.fields[percent]}"),
                        TextColumn("已下载 [cyan]{task.fields[downloaded]}[/cyan]"),
                        TextColumn("速度 [green]{task.fields[speed]}[/green]"),
                        TextColumn("剩余 [magenta]{task.fields[eta]}[/magenta]"),
                        transient=True,
                        refresh_per_second=10,
                        console=console,
                    ) as progress:
                        task = progress.add_task(
                            "正在下载……",
                            total=None,
                            percent="--.--%",
                            downloaded="0 B",
                            speed="--",
                            eta="--:--",
                        )
                        result = downloader.download(
                            source,
                            item.order,
                            partial(update_download_task, progress, task),
                        )
                    if result.skipped:
                        console.print(f"[yellow]已存在，跳过：{result.path.name}[/yellow]")
                        skipped += 1
                    else:
                        console.print(f"[green]已保存：{result.path.name}[/green]")
                        completed += 1
                except Exception as error:
                    console.print(f"[red]下载失败：{error}[/red]")
                    failed += 1

            console.print(
                Panel.fit(
                    f"新下载 {completed} 项，已存在 {skipped} 项，失败 {failed} 项。\n"
                    f"下载目录：{output_dir}",
                    title="任务完成",
                )
            )
            return 0 if failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = AppPaths.create()
    if args.check:
        return run_check(paths)
    try:
        return run()
    except KeyboardInterrupt:
        console.print("\n[yellow]操作已取消。[/yellow]")
        return 130
    except Exception as error:
        console.print(f"[red]运行失败：{error}[/red]")
        return 1
