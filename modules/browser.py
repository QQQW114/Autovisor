from dataclasses import dataclass
from pathlib import Path
import sys
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright._impl._errors import TargetClosedError


@dataclass
class BrowserSession:
    browser: Browser
    context: BrowserContext
    page: Page
    attached: bool

    async def close(self) -> None:
        """Close only resources owned by Autovisor."""
        try:
            if self.attached:
                if not self.page.is_closed():
                    await self.page.close()
                return
            await self.browser.close()
        except TargetClosedError:
            return


def get_effective_driver(config_driver: str) -> str:
    return str(config_driver).strip().lower()


def resolve_browser_channel(driver: str) -> str | None:
    if driver == "edge":
        return "msedge"
    if driver == "chromium":
        return None
    return driver


def resolve_executable_path(driver: str, configured_path: str) -> str | None:
    path = configured_path.strip().strip("\"'")
    if not path or driver == "chromium":
        return None
    if path.endswith(".app"):
        app_name = Path(path).stem
        return str(Path(path) / "Contents" / "MacOS" / app_name)
    return path


def resolve_cdp_endpoint(
    configured_url: str,
    active_port_path: Path | None = None,
) -> str:
    configured_url = configured_url.strip()
    default_url = "http://127.0.0.1:9222"
    if configured_url and configured_url != default_url:
        return configured_url

    if active_port_path is None and sys.platform == "darwin":
        active_port_path = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "DevToolsActivePort"
        )
    if active_port_path and active_port_path.is_file():
        lines = active_port_path.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith("/devtools/browser/"):
            return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
    return configured_url or default_url


def is_loopback_cdp_endpoint(endpoint: str) -> bool:
    host = (urlparse(endpoint).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "profile"


def _launch_args(config) -> dict:
    driver = get_effective_driver(config.driver)
    channel = resolve_browser_channel(driver)
    executable_path = resolve_executable_path(driver, config.exe_path)
    args = {
        "headless": False,
        "args": [
            "--window-size=1600,900",
            "--window-position=100,100",
        ],
    }
    # [本地改动] 固定 user-data-dir，让登录态在两次运行之间保留。
    # 原生实现不传此项，Playwright 每次新建临时 profile，
    # 导致每轮启动都要重新登录、反复卡在登录页。
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args["user_data_dir"] = str(PROFILE_DIR)

    if executable_path:
        args["executable_path"] = executable_path
    elif channel:
        args["channel"] = channel
    return args


async def _launch_browser(playwright: Playwright, config, logger):
    """[本地改动] 返回持久化 context。

    playwright.chromium.launch() 不接受 user_data_dir，会每次新建临时 profile，
    登录态无法保留。改用 launch_persistent_context()，把 profile 固定在
    avsrc/data/profile，登录一次即可长期复用。
    返回值语义随之从 Browser 变成 BrowserContext。
    """
    launch_args = _launch_args(config)
    try:
        return await playwright.chromium.launch_persistent_context(**launch_args)
    except TargetClosedError as exc:
        logger.log_exception("首次启动浏览器失败,准备重试.", exc)
        return await playwright.chromium.launch_persistent_context(**launch_args)


async def create_browser_session(
    playwright: Playwright,
    config,
    cookies,
    logger,
) -> BrowserSession:
    if config.attach_existing_chrome:
        cdp_endpoint = resolve_cdp_endpoint(config.cdp_url)
        if not is_loopback_cdp_endpoint(cdp_endpoint):
            raise RuntimeError("为避免凭据泄露，CDP 端点只允许连接本机 loopback 地址")
        logger.info("正在连接现有 Chrome 的远程调试端点...")
        page = None
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
            if not browser.contexts:
                raise RuntimeError("Chrome CDP 连接成功,但没有可用浏览器上下文")
            context = browser.contexts[0]
            page = await context.new_page()
            attached = True
            logger.info("已连接现有 Chrome,将复用当前登录状态.")
            await _prepare_page(page, logger)
            return BrowserSession(browser, context, page, attached)
        except BaseException as exc:
            if page is not None and not page.is_closed():
                try:
                    await page.close()
                except TargetClosedError:
                    pass
            if not isinstance(exc, Exception) or isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(
                "无法连接现有 Chrome; 请启用 chrome://inspect/#remote-debugging, "
                "或将 attachExistingChrome 设为 False"
            ) from exc
    else:
        logger.info(f"正在启动 {config.driver} 浏览器...")
        # [本地改动] _launch_browser 现在返回持久化 context（BrowserContext），
        # 其中已包含默认页面，不能再调用 browser.new_context()。
        context = await _launch_browser(playwright, config, logger)
        try:
            if cookies:
                await context.add_cookies(cookies)
                logger.info("已加载 Cookies!")
            else:
                logger.info("未找到 Cookies,将跳转至登录页.")
            page = context.pages[0] if context.pages else await context.new_page()
            await _prepare_page(page, logger)
            # browser 位传 context，保证 close() 时能真正关掉持久化浏览器
            return BrowserSession(context, context, page, False)
        except BaseException:
            try:
                await context.close()
            except TargetClosedError:
                pass
            raise


async def _prepare_page(page: Page, logger) -> None:
    stealth_path = Path(logger.runtime_root) / "res" / "stealth.min.js"
    if stealth_path.is_file():
        # [本地改动] 挂到 context 而非 page。
        # page.add_init_script 只覆盖当前这一个 Page 对象，页面跳转或
        # 浏览器自行开的新标签不会被注入，navgator.webdriver 等特征会裸奔。
        # context 级注入对新开的每个文档都生效。
        await page.context.add_init_script(path=str(stealth_path))
        logger.debug("stealth.js 已注入（context 级）")
    page.set_default_timeout(24 * 3600 * 1000)
