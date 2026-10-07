from urllib.parse import urlparse

from playwright.async_api import Page


LOGIN_HOSTS = {"login.zhihuishu.com", "passport.zhihuishu.com"}
LOGIN_USERNAME_SELECTOR = "#lUsername, input[name='mobile']"
LOGIN_PASSWORD_SELECTOR = "#lPassword, input[type='password']"
LOGIN_SUBMIT_SELECTOR = ".wall-sub-btn, .btn-block__grandient_login, button[type='submit']"

# [本地改动] 学号登录通道。
# 默认的"账号登录"页签对纯数字学号不适用，
# 必须切到"学号登录"并补上学校/机构字段。
STUDENT_TAB_TEXTS = ("学号登录", "学号/工号登录")
STUDENT_ID_SELECTOR = "input[placeholder*='学号']"
PASSWORD_ANY_SELECTOR = "input[type='password']"
# 实测：下拉项是 .el-select-dropdown__item；隐藏态空态是 .el-select-dropdown__empty
SCHOOL_OPTION_SELECTOR = ".el-select-dropdown__item, .el-autocomplete-suggestion li"
SCHOOL_OPTION_ANY = (".el-select-dropdown__item, .el-autocomplete-suggestion li, "
                     ".el-scrollbar__view li, li[class*='option']")

# [本地改动] 学校选择框没有 placeholder、没有 id/name，无法用属性定位。
# 实测学号登录态下的三个输入框依次是：
#   [0] text      ph=''              学校/机构（el-select 的筛选输入）
#   [1] text      ph='请输入您的学号'
#   [2] password  ph='请输入密码'
# 因此学校框 = 页面上可见的第一个文本输入框。
JS_FIND_SCHOOL = r"""() => {
  const pick = () => {
    const all = [...document.querySelectorAll('input')].filter(el => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return el.type === 'text' && r.width > 40 && r.height > 12 &&
             st.display !== 'none' && st.visibility !== 'hidden';
    });
    if (!all.length) return null;
    // 学校框不含"学号"字样；优先取第一个
    const school = all.find(el => !/学号/.test(el.placeholder || '')) || all[0];
    if (!school.dataset.zsSchoolTag) {
      school.dataset.zsSchoolTag = '1';
    }
    return school;
  };
  const el = pick();
  if (!el) return false;
  el.setAttribute('data-zs-school', '1');
  return true;
}"""
SCHOOL_TAGGED_SELECTOR = "input[data-zs-school='1']"


def is_login_page(url: str) -> bool:
    """判断 URL 是否仍处于智慧树登录流程。"""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in LOGIN_HOSTS


async def wait_for_login_complete(
    page: Page, timeout: float = 24 * 3600 * 1000
) -> None:
    """等待页面离开智慧树登录域名。"""
    if not is_login_page(page.url):
        return
    await page.wait_for_url(
        lambda url: not is_login_page(str(url)),
        wait_until="commit",
        timeout=timeout,
    )


JS_CHECK_STATE = """() => {
  const el = document.querySelector('input.el-checkbox__original');
  return el ? !!el.checked : null;
}"""


async def _checkbox_checked(page: Page):
    try:
        return await page.evaluate(JS_CHECK_STATE)
    except Exception:
        return None


async def accept_login_terms(page: Page, want: bool = True, tries: int = 4) -> bool:
    """勾选用户协议复选框。

    Element UI 的原始 checkbox 被渲染成 0x0 隐藏元素，只能靠 JS 触发 click()。
    关键：每次点击都会【切换】状态，所以不能盲点——必须点一次查一次，
    只沿着目标状态修，否则会点成"取消勾选"。
    """
    if await _checkbox_checked(page) is None:
        return False

    for _ in range(tries):
        checked = await _checkbox_checked(page)
        if checked == want:
            return True
        try:
            clicked = await page.evaluate(
                "() => { const el = document.querySelector('input.el-checkbox__original');"
                " if (!el) return false; el.click(); return true; }"
            )
            if not clicked:
                return False
        except Exception:
            return False
        await page.wait_for_timeout(600)

    return (await _checkbox_checked(page)) == want


# --------------------------------------------------------------------------
# [本地改动] 学号登录流程
# --------------------------------------------------------------------------
async def switch_to_student_tab(page: Page, retries: int = 12) -> bool:
    """切到「学号登录」页签。多轮重试，因为登录页是异步渲染的。

    实测：页签为 .el-tabs__item，顺序 [账号登录, 学号登录, 工号登录, 扫码]。
    返回 False 时由调用方回退账号登录。
    """
    for attempt in range(retries):
        try:
            tabs = page.locator(".el-tabs__item")
            n = await tabs.count()
            for i in range(n):
                t = tabs.nth(i)
                txt = (await t.text_content() or "").strip()
                if "学号" in txt:
                    if await t.is_visible():
                        await t.click(timeout=4000)
                        await page.wait_for_timeout(1200)
                        return True
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    return False


async def pick_school(page: Page, school_name: str) -> bool:
    """在「请选择您的学校或者机构」里输入学校并选中下拉项。

    两个要点：
      1. 学校框无 placeholder，靠 JS 打标记定位（见 JS_FIND_SCHOOL）。
      2. Element UI 必须键盘逐字输入才会触发 Vue 的 input 事件与远程搜索，
         直接 fill() 只改 value，下拉不会弹出。
    """
    if not school_name:
        return False

    for attempt in range(3):
        try:
            tagged = await page.evaluate(JS_FIND_SCHOOL)
            if not tagged:
                await page.wait_for_timeout(1500)
                continue

            box = page.locator(SCHOOL_TAGGED_SELECTOR).first
            await box.wait_for(state="visible", timeout=10000)
            await box.click(timeout=5000)
            await page.wait_for_timeout(600)

            await box.press("Control+a")
            await box.press("Delete")
            await page.wait_for_timeout(300)

            await box.type(school_name, delay=150)
            await page.wait_for_timeout(2500)

            opt = page.locator(SCHOOL_OPTION_SELECTOR).filter(has_text=school_name).first
            if await opt.count():
                try:
                    if await opt.is_visible():
                        await opt.click(timeout=5000)
                        await page.wait_for_timeout(1000)
                        return True
                except Exception:
                    pass

            # 兜底：键盘选中首项
            await box.press("ArrowDown")
            await page.wait_for_timeout(300)
            await box.press("Enter")
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            await page.wait_for_timeout(1200)

    return False


async def fill_student_login(page: Page, school: str, student_id: str, password: str) -> bool:
    """按「学号登录」三件套填表：学校 → 学号 → 密码。

    学校必须先选定（否则登录按钮保持禁用）。
    """
    ok_school = await pick_school(page, school)
    if not ok_school:
        return False

    id_box = page.locator(STUDENT_ID_SELECTOR).first
    await id_box.wait_for(state="visible", timeout=15000)
    await id_box.click()
    await id_box.press("Control+a")
    await id_box.press("Delete")
    await id_box.type(student_id, delay=90)
    await page.wait_for_timeout(400)

    pwd_box = page.locator(PASSWORD_ANY_SELECTOR).first
    await pwd_box.wait_for(state="visible", timeout=15000)
    await pwd_box.click()
    await pwd_box.press("Control+a")
    await pwd_box.press("Delete")
    await pwd_box.type(password, delay=90)
    await page.wait_for_timeout(600)

    return True


async def submit_login(page: Page) -> None:
    """点提交按钮。优先用可见的登录按钮。"""
    for sel in (
        "button:has-text('登录')",
        "button[type='submit']",
        LOGIN_SUBMIT_SELECTOR,
    ):
        try:
            btn = page.locator(sel).last
            if await btn.count():
                await btn.click(timeout=6000)
                return
        except Exception:
            continue
    # 兜底：直接在密码框回车
    try:
        await page.locator(PASSWORD_ANY_SELECTOR).first.press("Enter")
    except Exception:
        pass


async def fill_login_form_for_manual(
    page: Page, mode: str, school: str, username: str, password: str
) -> bool:
    """[本地改动] 手动协作模式：把这些都做好，只把滑块留给用户。

    做完的事：
      - 切到「学号登录」
      - 填学校 / 学号 / 密码
      - 勾选用户协议
    不做的事：
      - 不点登录、不碰人机验证（易盾是反自动化设计，硬试会被反复重置题目）

    之后由调用方等待用户拖动滑块并点登录；登录态存进固定 profile，
    下次运行即可免登录。
    """
    if mode == "student":
        if not await switch_to_student_tab(page):
            return False
        filled = await fill_student_login(page, school, username, password)
    else:
        try:
            u = page.locator(LOGIN_USERNAME_SELECTOR).first
            p = page.locator(LOGIN_PASSWORD_SELECTOR).first
            await u.wait_for(state="visible", timeout=20000)
            await u.fill(username)
            await p.fill(password)
            filled = True
        except Exception:
            filled = False

    await accept_login_terms(page)
    return filled
