import asyncio
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError
from playwright.async_api import Page
from modules.configs import Config
from modules.utils import get_video_attr, display_window, hide_window, run_on
from playwright._impl._errors import TargetClosedError
from modules.logger import Logger
from modules.video_state import video_at_end

logger = Logger()

VERIFICATION_SELECTORS = (
    ".yidun_popup .yidun_modal",
    ".yidun_modal__title",
    "[id^='tcaptcha_transform']",
)

# [本地改动] 验证码下方的"辅助/切换验证方式"入口。
# 实测：点它会关掉验证码并弹出帮助界面，此时验证元素消失，
# 若只看"验证元素是否消失"就会误判成"验证已完成"从而错误地继续播放。
VERIFY_HELP_SELECTORS = (
    "text=切换其他验证方式",
    "text=其他验证方式",
    "text=无法完成",
    "text=帮助",
    "text=无障碍",
    "[class*='help']",
    "[class*='switch-way']",
)

# [本地改动] 自动作答尝试上限；超过就停下等人工，避免连续失败触发风控
MAX_CAPTCHA_ATTEMPTS = 3

# [本地改动] 判定"真的显示着验证码界面"用到的选择器。
# 注意 [id^='tcaptcha_transform'] 是常驻容器，但 has_visible_element 会因
# 它 opacity=0/屏幕外而正确排除，所以可以放进列表。
HUMAN_VERIFY_SELECTORS = (
    ".yidun_slider",
    ".yidun_bgimg",
    ".yidun_jigsaw",
    ".yidun_popup .yidun_modal",
    ".yidun_modal__title",
    ".yidun_tips",
    "[id^='tcaptcha_transform']",
)

# 课中安全验证的取证目录。检测到验证码时把画面存下来，
# 便于事后分析验证码类型（点选文字/点选字母/语序点选等），
# 以及为自动识别方案准备真实样本。
VERIFY_SHOT_DIR = Path(__file__).resolve().parent.parent / "verify_shots"


async def capture_verification(page: Page) -> None:
    """检测到安全验证时截图存档。失败不影响主流程。"""
    try:
        VERIFY_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 整页
        full = VERIFY_SHOT_DIR / f"{stamp}_full.png"
        try:
            await page.screenshot(path=str(full), full_page=False)
        except Exception:
            full = None

        # 各验证码容器单独截，便于直接喂给 OCR
        for sel in VERIFICATION_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el or not await el.is_visible():
                    continue
                box = await el.bounding_box()
                if not box or box["width"] < 20 or box["height"] < 20:
                    continue
                safe = sel.replace("'", "").replace("[", "").replace("]", "")
                safe = "".join(c for c in safe if c.isalnum() or c in "._-")[:40]
                shot = VERIFY_SHOT_DIR / f"{stamp}_{safe}.png"
                await el.screenshot(path=str(shot))
                logger.event("安全验证取证", 元素=sel, 文件=shot.name,
                             尺寸=f"{int(box['width'])}x{int(box['height'])}")
            except Exception:
                continue

        if full:
            logger.event("安全验证取证", 文件=full.name)
    except Exception as e:
        logger.debug(f"验证码截图失败(不影响流程): {e}")

BLOCKING_OVERLAY_SELECTORS = (
    ".topic-title",
    ".ss2077-custom-dialog",
)


async def has_visible_element(page: Page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            visible = await page.locator(selector).evaluate_all(
                """elements => elements.some(element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    const opacity = Number.parseFloat(style.opacity || "1");
                    return style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        opacity > 0.05 &&
                        rect.width > 0 && rect.height > 0 &&
                        rect.bottom > 0 && rect.right > 0 &&
                        rect.top < innerHeight && rect.left < innerWidth;
                })"""
            )
            if visible:
                return True
        except TargetClosedError:
            raise
        except Exception as exc:
            logger.debug_throttled(
                "has_visible_element",
                f"可见元素检测遇到页面切换({', '.join(selectors)}): "
                f"{logger.summarize_exception(exc)}",
            )
    return False


async def has_visible_verification(page: Page) -> bool:
    return await has_visible_element(page, VERIFICATION_SELECTORS)


async def has_blocking_overlay(page: Page) -> bool:
    return await has_visible_verification(page) or await has_visible_element(
        page, BLOCKING_OVERLAY_SELECTORS
    )


async def wait_until_verification_hidden(page: Page) -> None:
    while await has_visible_verification(page):
        await asyncio.sleep(0.5)


def is_expected_polling_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc)
    expected_signals = [
        "waiting for locator",
        "waiting for selector",
        "ElementHandle.press",
        "No node found for selector",
        "Execution context was destroyed",
    ]
    return any(signal in message for signal in expected_signals)


# --------------------------------------------------------------------------
# [本地改动] 验证码自动作答
#   设计要点：
#     1. 最多尝试 MAX_CAPTCHA_ATTEMPTS 次，超过就停下等人工
#     2. 求解器"没把握"时不猜，直接转人工（防连续失败触发风控）
#     3. 必须识别"切换其他验证方式"界面——它会让验证元素消失但并未通过，
#        只看"元素是否消失"会误判成已完成（实测踩过这个坑）
# --------------------------------------------------------------------------
_SOLVER = None
_SOLVER_TRIED = False


def _solver():
    """延迟导入求解器；缺依赖时返回 None，不影响主流程。"""
    global _SOLVER, _SOLVER_TRIED
    if _SOLVER_TRIED:
        return _SOLVER
    _SOLVER_TRIED = True
    try:
        from modules import captcha_solver
        _SOLVER = captcha_solver
        logger.debug("验证码求解器已加载")
    except Exception as e:
        logger.warn(f"验证码求解器不可用（将只走人工路径）: {e}")
        _SOLVER = None
    return _SOLVER


async def _first_visible(page: Page, selectors):
    """返回第一个真实可见的元素。用 has_visible_element 的严格判定，
    避免把屏幕外/透明的常驻容器当成命中。"""
    for sel in selectors:
        try:
            if await has_visible_element(page, (sel,)):
                el = await page.query_selector(sel)
                if el is not None:
                    return el, sel
        except Exception:
            continue
    return None, None


async def _has_verify_help(page: Page) -> bool:
    """是否出现「切换其他验证方式 / 帮助」界面。"""
    el, _ = await _first_visible(page, VERIFY_HELP_SELECTORS)
    return el is not None


async def _has_human_verify_ui(page: Page) -> bool:
    """是否真的显示着需要人工完成的验证码界面。

    必须复用 has_visible_element 的严格判定（检查 opacity 与视口位置）。
    坑：tcaptcha 会常驻一个 opacity=0、位置 y=-1000000 的隐藏容器，
        Playwright 的 ElementHandle.is_visible() 把它判为"可见"，
        会导致误认为验证码一直在，wait_for_human_verify 永久循环、
        自动化被 event_loop.clear() 一直挂起。
    """
    if await has_visible_element(page, HUMAN_VERIFY_SELECTORS):
        return True
    return await has_visible_verification(page)


async def _read_verify_prompt(page: Page) -> str:
    """读验证码题目文字，例如「请点击小写z朝方向一样的小写k」。

    要点：
      * 不能读 .yidun_modal__title —— 那是弹窗标题「请完成安全验证」
      * 真题在图片下方的提示里（.yidun_tips 等）
      * 验证码可能在内嵌 frame，需逐 frame 找，并优先选形如「请点击…」的文本
    """
    # 越靠前越可能是真题
    sels = (
        ".yidun_tips",
        ".yidun_tips__text",
        "[class*='yidun_tips']",
        ".yidun_modal__subtitle",
        ".yidun_modal__text",
        "[class*='yidun'][class*='content']",
    )
    candidates = []
    for frame in page.frames:
        for sel in sels:
            try:
                els = await frame.query_selector_all(sel)
            except Exception:
                continue
            for el in els:
                try:
                    if not await el.is_visible():
                        continue
                    # 注意：ElementHandle 没有 all_inner_texts()，那是 Locator 的 API
                    txt = (await el.inner_text() or "").strip()
                    if len(txt) >= 4:
                        candidates.append(txt)
                except Exception:
                    continue

    if not candidates:
        return ""

    # 优先取"请点击/请依序点击"这类真正的题面
    def score(t: str) -> int:
        s = 0
        if "点击" in t:
            s += 10
        if any(k in t for k in ("方向", "颜色", "一样", "一致", "正向", "相同")):
            s += 6
        if any(k in t for k in ("小写", "大写", "数字", "字母")):
            s += 4
        if t in ("请完成安全验证", "请进行验证", "安全验证"):
            s -= 20
        return s

    candidates.sort(key=score, reverse=True)
    best = candidates[0]
    return best if score(best) > 0 else ""


async def _verify_shot_box(page: Page):
    """取验证码区域元素及其大小，用于截图与相对坐标点击。

    同样要排除屏幕外的常驻容器，否则会截到一张空图、
    并让"相对坐标点击"算到错误的位置上。
    """
    for sel in (".yidun_popup .yidun_modal", ".yidun_modal", ".yidun_bgimg",
                "[id^='tcaptcha_transform']"):
        try:
            el = await page.query_selector(sel)
            if el is None:
                continue
            box = await el.bounding_box()
            if not box:
                continue
            if box["width"] < 100 or box["height"] < 60:
                continue
            # 屏幕外（常驻隐藏容器）直接跳过
            if box["x"] < -1000 or box["y"] < -1000:
                continue
            return el, box
        except Exception:
            continue
    return None, None


async def _capture_check(page: Page, tag: str) -> None:
    """把验证码画面存档，便于事后分析题型。"""
    try:
        VERIFY_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        el, _ = await _verify_shot_box(page)
        if el is not None:
            await el.screenshot(
                path=str(VERIFY_SHOT_DIR / f"{stamp}_{tag}_verify.png"))
        await page.screenshot(
            path=str(VERIFY_SHOT_DIR / f"{stamp}_{tag}_full.png"))
    except Exception as e:
        logger.debug(f"验证码截图失败: {e}")


async def try_auto_solve(page: Page, config) -> bool:
    """最多尝试 MAX_CAPTCHA_ATTEMPTS 次自动作答。

    返回 True=验证已通过；False=放弃，调用方转人工。
    """
    solver = _solver()
    if solver is None:
        logger.warn("求解器不可用，跳过自动作答。")
        return False

    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        try:
            if not await _has_human_verify_ui(page):
                if await _has_verify_help(page):
                    logger.warn("检测到「切换其他验证方式」界面："
                                "验证并未通过，转为人工。")
                    return False
                logger.info("验证界面已消失，视为已通过。")
                return True

            if await _has_verify_help(page):
                logger.warn("验证码下方出现辅助入口，停止自动作答以免误判。")
                return False

            prompt = ""
            for _ in range(5):
                prompt = await _read_verify_prompt(page)
                if prompt:
                    break
                await page.wait_for_timeout(900)
            if not prompt:
                logger.warn(f"第{attempt}次：读不到验证码题目，放弃自动作答。")
                return False

            el, box = await _verify_shot_box(page)
            if el is None:
                await page.wait_for_timeout(1500)
                continue

            await _capture_check(page, f"try{attempt}")
            shot_path = VERIFY_SHOT_DIR / f"_current_try{attempt}.png"
            await el.screenshot(path=str(shot_path))

            try:
                import cv2 as _cv2
            except ImportError:
                logger.warn("缺少 opencv，无法自动作答。")
                return False
            crop = _cv2.imread(str(shot_path))
            if crop is None:
                logger.warn(f"第{attempt}次：截图读取失败")
                return False

            res = solver.solve(crop, prompt,
                               debug_dir=VERIFY_SHOT_DIR / f"try{attempt}")
            logger.event("验证码自动作答",
                         尝试=f"{attempt}/{MAX_CAPTCHA_ATTEMPTS}",
                         题目=prompt[:40],
                         结果="成功" if res.ok else "放弃",
                         说明=res.reason[:60])

            if not res.ok:
                logger.warn(f"求解器放弃作答：{res.reason}")
                return False

            # 拟人化点击。
            # 实测踩坑：位置算对了但验证仍失败 —— tcaptcha 会采集鼠标轨迹，
            # 瞬时、像素级精确的点击会被判定为机器人。因此这里：
            #   1) 加人类级抖动（不精确到像素）
            #   2) 分多步移动，模拟手部轨迹
            #   3) 按下/松开之间留随机间隔
            import random
            rel_x = res.x / crop.shape[1]
            rel_y = res.y / crop.shape[0]
            tgt_x = box["x"] + rel_x * box["width"] + random.uniform(-3.5, 3.5)
            tgt_y = box["y"] + rel_y * box["height"] + random.uniform(-3.5, 3.5)

            start_x = tgt_x - random.uniform(90, 170)
            start_y = tgt_y - random.uniform(60, 130)
            await page.mouse.move(start_x, start_y)
            await page.wait_for_timeout(random.randint(90, 190))

            steps = random.randint(3, 5)
            for si in range(1, steps + 1):
                frac = si / steps
                await page.mouse.move(
                    start_x + (tgt_x - start_x) * frac + random.uniform(-4, 4),
                    start_y + (tgt_y - start_y) * frac + random.uniform(-4, 4),
                )
                await page.wait_for_timeout(random.randint(45, 115))

            # 到位后轻微对准，再按下
            await page.mouse.move(tgt_x, tgt_y)
            await page.wait_for_timeout(random.randint(120, 270))
            await page.mouse.down()
            await page.wait_for_timeout(random.randint(55, 135))
            await page.mouse.up()

            logger.event("验证码点击",
                         坐标=f"({res.x:.0f},{res.y:.0f})",
                         置信度=f"{res.confidence:.2f}",
                         方式="拟人轨迹")

            await page.wait_for_timeout(2500)

            if await _has_verify_help(page):
                logger.warn("作答后出现「切换其他验证方式」，判定为未通过。")
                return False
            if not await _has_human_verify_ui(page):
                logger.info("验证已通过。")
                return True

            logger.warn(f"第{attempt}次作答未被接受，验证码可能已刷新。")
            await page.wait_for_timeout(1200)

        except TargetClosedError:
            raise
        except Exception as e:
            logger.warn(f"第{attempt}次自动作答异常: "
                        f"{logger.summarize_exception(e)}")
            await page.wait_for_timeout(1500)

    logger.warn(f"已尝试 {MAX_CAPTCHA_ATTEMPTS} 次仍未通过，"
                "停止自动作答，转为等待人工完成。")
    return False


async def wait_for_human_verify(page: Page) -> None:
    """等人工把验证做完。

    两个坑（都实测踩过）：
      1. 用户可能点「切换其他验证方式」——验证控件同样会消失，但并未通过
      2. 验证控件消失 ≠ 页面已恢复。过早放行会让主流程找不到 video 而崩溃
    """
    while True:
        await asyncio.sleep(1.0)
        if await _has_verify_help(page):
            logger.warn("当前停留在「切换其他验证方式」界面，"
                        "请改回图片验证并完成。")
            continue
        if await _has_human_verify_ui(page):
            continue

        # 验证界面已消失，还需确认页面恢复正常（视频元素回来）才放行
        try:
            v = await page.query_selector("video")
            if v is not None:
                logger.info("人工验证已完成，页面已恢复。")
                return
            logger.debug("验证界面已消失，但视频尚未恢复，继续等待...")
        except Exception:
            pass


async def task_monitor(tasks: list[asyncio.Task]) -> None:
    checked_tasks = set()
    logger.info("任务监控已启动.")
    while any(not task.done() for task in tasks):
        for i, task in enumerate(tasks):
            if task.done() and task not in checked_tasks:
                checked_tasks.add(task)
                exc = task.exception()
                func_name = task.get_coro().__name__
                if exc is not None:
                    logger.log_exception(f"任务函数 {func_name} 出现异常.", exc, shift=True)
        await asyncio.sleep(1)
    logger.info("任务监控已退出.", shift=True)


async def video_optimize(page: Page, config: Config) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            await asyncio.sleep(2)
            try:
                await page.wait_for_selector("video", state="attached", timeout=3000)
            except TimeoutError:
                logger.debug_throttled(
                    "video:optimize",
                    "视频调节跳过: 页面未找到元素 video",
                )
                continue
            volume = await get_video_attr(page, "volume")
            rate = await get_video_attr(page, "playbackRate")
            changes = []
            if config.soundOff and volume != 0:
                await run_on(page, "video", "(el) => { el.volume = 0; }", "设置静音")
                await run_on(
                    page,
                    ".volumeBox",
                    '(el) => el.classList.add("volumeNone")',
                    "更新静音图标",
                )
                changes.append(f"音量 {volume}->0")
            if rate != config.limitSpeed:
                await run_on(
                    page,
                    "video",
                    f"(el) => {{ el.playbackRate = {config.limitSpeed}; }}",
                    "设置倍速",
                )
                await run_on(
                    page,
                    ".speedBox span",
                    f'(el) => {{ el.innerText = "X {config.limitSpeed}"; }}',
                    "更新倍速标签",
                )
                changes.append(f"倍速 {rate}->{config.limitSpeed}")
            if changes:
                logger.event("播放器调节", 项目=", ".join(changes))
        except TargetClosedError:
            logger.debug("浏览器已关闭, 视频调节模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "video_optimize",
                    f"视频调节模块轮询未命中: {logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("视频调节模块执行失败.", e)
            continue


async def play_video(
    page: Page, playback_enabled: asyncio.Event | None = None
) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            await asyncio.sleep(2)
            try:
                await page.wait_for_selector("video", state="attached", timeout=1000)
            except TimeoutError:
                logger.debug_throttled(
                    "video:play",
                    "视频播放跳过: 页面未找到元素 video",
                )
                continue
            if playback_enabled is not None and not playback_enabled.is_set():
                paused = await run_on(
                    page, "video", "(el) => el.paused", "读取视频暂停状态"
                )
                if paused is False:
                    await run_on(page, "video", "(el) => el.pause()", "暂停视频")
                continue
            state = await run_on(
                page,
                "video",
                """(el) => ({
                    paused: el.paused,
                    ended: el.ended,
                    currentTime: el.currentTime,
                    duration: el.duration,
                })""",
                "读取播放器状态",
            )
            if state is None:
                continue
            paused = state["paused"]
            blocked = await has_blocking_overlay(page)
            if blocked:
                if not paused:
                    await run_on(page, "video", "(el) => el.pause()", "遮罩层暂停视频")
                    logger.info("检测到遮罩层,已暂停视频等待处理.")
                    logger.event("遮罩层暂停", 播放器时间=round(state["currentTime"], 1))
                continue
            at_end = state["ended"] or video_at_end(
                state["currentTime"], state["duration"]
            )
            if paused and not at_end:
                logger.info("检测到视频暂停,正在尝试播放.")
                try:
                    await page.wait_for_selector(".videoArea", timeout=1000)
                except TimeoutError:
                    logger.debug_throttled(
                        "videoArea",
                        "尝试播放跳过: 页面未找到元素 .videoArea",
                    )
                    continue
                await run_on(page, "video", "(el) => el.play()", "恢复播放")
                logger.debug("视频已恢复播放.")
                logger.event(
                    "恢复播放",
                    播放器时间=round(state["currentTime"], 1),
                    总时长=round(state["duration"], 1),
                )
        except TargetClosedError:
            logger.debug("浏览器已关闭, 视频播放模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "play_video",
                    f"视频播放模块轮询未命中: {logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("视频播放模块执行失败.", e)
            continue


async def skip_questions(page: Page, event_loop) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            if "studywisdomh5.zhihuishu.com" in page.url:
                await asyncio.sleep(2)
                if not await has_visible_element(page, (".topic-title",)):
                    continue
                logger.warn("检测到新版课中弹题,请在浏览器中手动处理.", shift=True)
                logger.event("新版课中弹题", 处理方式="手动", 地址=page.url)
                while await has_visible_element(page, (".topic-title",)):
                    await asyncio.sleep(0.5)
                event_loop.set()
                continue
            if "hike.zhihuishu.com" in page.url:
                logger.warn("当前课程为新版本,不支持自动答题.", shift=True)
                logger.event("答题", 结果="跳过", 原因="课程版本不支持")
                return
            await asyncio.sleep(2)
            ques_element = await page.wait_for_selector(".el-scrollbar__view", state="attached", timeout=1000)
            total_ques = await ques_element.query_selector_all(".number")
            if total_ques:
                answered = 0
                for ques in total_ques:
                    await ques.click(timeout=500)
                    if not await page.query_selector(".answer"):
                        choices = await page.query_selector_all(".topic-item")
                        for each in choices[:2]:
                            await each.click(timeout=500)
                            await page.wait_for_timeout(100)
                        answered += 1
                logger.event("课中答题", 题目数=len(total_ques), 已作答=answered)
            await page.press(".el-dialog", "Escape", timeout=1000)
            event_loop.set()
        except TargetClosedError:
            logger.debug("浏览器已关闭, 答题模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "skip_questions",
                    f"答题模块轮询未命中(元素 .el-scrollbar__view/.el-dialog): "
                    f"{logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("答题模块执行失败.", e)
            if "fusioncourseh5" in page.url:
                not_finish_close = await page.query_selector(".el-dialog")
                if not_finish_close:
                    await page.press(".el-dialog", "Escape", timeout=1000)
            elif "hike.zhihuishu.com" in page.url:
                logger.warn("当前课程为新版本,不支持自动答题.", shift=True)
                return
            else:
                not_finish_close = await page.query_selector(".el-message-box__headerbtn")
                if not_finish_close:
                    await not_finish_close.click()
            continue


async def wait_for_verify(page: Page, config, event_loop) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            await asyncio.sleep(2)
            if not await has_visible_verification(page):
                continue

            event_loop.clear()
            logger.event("安全验证", 状态="开始", 地址=page.url)
            wait_start = time.time()
            # 把验证码画面存档，供事后分析题型
            await capture_verification(page)
            if config.enableHideWindow:
                await display_window(page)

            # [本地改动] 先尝试自动作答（最多 MAX_CAPTCHA_ATTEMPTS 次），
            # 失败则停下等人工。绝不在没把握时硬猜，避免连续失败触发风控。
            solved = await try_auto_solve(page, config)

            if solved:
                logger.info("安全验证已自动通过.", shift=True)
                logger.event("安全验证", 状态="自动通过",
                             耗时=f"{time.time() - wait_start:.1f}s")
            else:
                logger.warn("自动作答未成功，请手动完成验证...", shift=True)
                await wait_for_human_verify(page)

            event_loop.set()
            if config.enableHideWindow:
                await hide_window(page)
            logger.event("安全验证", 状态="结束",
                         耗时=f"{time.time() - wait_start:.1f}s")
            await asyncio.sleep(2)
        except TargetClosedError:
            logger.debug("浏览器已关闭, 安全验证模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "wait_for_verify",
                    f"安全验证模块轮询未命中(元素 .yidun_modal/.yidun_popup/tcaptcha): "
                    f"{logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("安全验证模块执行失败.", e)
            continue
