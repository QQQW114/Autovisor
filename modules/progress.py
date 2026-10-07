# encoding=utf-8
import os
import queue
import random
import sys
import threading

from playwright.async_api import Page, TimeoutError
from modules.lesson_navigation import (
    CatalogSelectors,
    lesson_progress,
    parse_progress_value,
)
from modules.logger import Logger

logger = Logger()


# ---------------------------------------------------------------------------
# [本地改动] 控制台进度输出改为"非阻塞"
#
# 踩过的坑：主循环每 0.5 秒调一次 show_course_progress，它直接 print。
# 在 Windows 上只要用鼠标点一下控制台窗口，就会进入"快速编辑模式"的选择态，
# 此时所有控制台写入被系统冻结 —— print 永久阻塞，整个学习循环随之卡死。
# 实测现场：py-spy 抓到的栈停在 progress.py 的 print 上，
# 日志停止输出、视频已播完但程序不动；在控制台按一下回车就恢复。
#
# 两道防线：
#   1. Autovisor.relax_console() 在启动时关闭控制台的快速编辑模式（治本）
#   2. 这里把输出丢给独立守护线程，队列满就丢帧，主循环永不阻塞（兜底）
#
# 注意：写线程必须用 os.write 直接写文件描述符，不能用 print/sys.stdout ——
# 守护线程一旦在退出时仍持有 sys.stdout 的缓冲锁，解释器关闭时会触发
# "Fatal Python error: _enter_buffered_busy" 直接崩掉进程（实测踩过）。
# ---------------------------------------------------------------------------
_print_queue: "queue.Queue[str]" = queue.Queue(maxsize=2)
_print_thread: threading.Thread | None = None
_print_lock = threading.Lock()


def _console_writer() -> None:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    while True:
        try:
            line = _print_queue.get()
        except Exception:
            return
        if line is None:
            return
        try:
            os.write(1, line.encode(enc, "replace"))
        except Exception:
            pass          # 控制台不可用就安静丢弃


def emit_console(line: str) -> None:
    """把一行控制台输出交给后台线程；绝不阻塞调用方。"""
    global _print_thread
    if _print_thread is None or not _print_thread.is_alive():
        with _print_lock:
            if _print_thread is None or not _print_thread.is_alive():
                _print_thread = threading.Thread(
                    target=_console_writer, daemon=True, name="console-writer"
                )
                _print_thread.start()
    try:
        _print_queue.put_nowait(line)
    except queue.Full:
        pass          # 控制台卡住时丢帧，不影响主流程


# 视频区域内移动鼠标
async def move_mouse(page: Page):
    try:
        await page.wait_for_selector(".videoArea", state="attached", timeout=5000)
        elem = page.locator(".videoArea")
        await elem.hover(timeout=4000)
        pos = await elem.bounding_box()
        if not pos:
            return
        # Calculate the target position to move the mouse
        target_x = pos['x'] + random.uniform(-10, 10)
        target_y = pos['y'] + random.uniform(-10, 10)
        await page.mouse.move(target_x, target_y)
    except TimeoutError:
        return


# 获取课程进度
async def get_course_progress(page: Page, catalog: CatalogSelectors) -> str:
    await move_mouse(page)
    current_lesson = page.locator(catalog.active).first
    if await current_lesson.count() == 0:
        return "0%"
    return f"{await lesson_progress(current_lesson, catalog)}%"


# 打印课程播放进度
def show_course_progress(desc, cur_time=None, limit_time=0):
    assert limit_time >= 0, "limit_time 必须为非负数!"
    if limit_time == 0:
        cur_time = "0%" if cur_time == '' or cur_time is None else cur_time
        percent = parse_progress_value(str(cur_time).rstrip("%"))
        length = int(percent * 30 // 100)
        progress = ("█" * length).ljust(30, " ")
        emit_console(f"\r{desc} |{progress}| {percent}%\t".ljust(50))
    else:
        cur_time = 0 if cur_time == '' or cur_time is None else cur_time
        if isinstance(cur_time, str):
            cur_time = 0
        left_time = round(limit_time - cur_time, 1)
        percent = int(cur_time / limit_time * 100)
        if left_time <= 0:
            percent = 100
        percent = max(0, min(percent, 100))
        length = int(percent * 20 // 100)
        progress = ("█" * length).ljust(20, " ")
        emit_console(
            f"\r{desc} |{progress}| {percent}%\t剩余 {left_time} min\t".ljust(50)
        )


# 打印通用版进度条
def show_progress(desc, current, total, suffix="", width=30):
    if total <= 0:
        emit_console(f"\r{desc} 已下载 {current} bytes\t{suffix}".ljust(50))
        return
    percent = int(current / total * 100)
    length = int(percent * width // 100)
    progress = ("█" * length).ljust(width, " ")
    emit_console(f"\r{desc} |{progress}| {percent}%\t{suffix}".ljust(50))
