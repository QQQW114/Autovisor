"""首次运行引导 / 配置向导。

仓库里不携带任何凭据与课程链接，所以第一次运行时会问你要。

用法::

    python setup_wizard.py              # 只补空缺项（首次运行会自动调用）
    python setup_wizard.py --account    # 重新输入账号
    python setup_wizard.py --course     # 重新输入课程链接（换课程用）
    python setup_wizard.py --all        # 全部重新输入
    python setup_wizard.py --show       # 只查看当前配置

由「启动.bat」在启动主程序前自动调用；也可以单独运行。
已有的值默认不会被覆盖 —— 只补空缺项。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# 控制台编码加固：Windows 默认代码页是 GBK，
# 直接 print 非 GBK 字符会抛 UnicodeEncodeError 把程序打断。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"
EXAMPLE = ROOT / "config.ini.example"

SEC_ACCOUNT = "user-account"
SEC_COURSE = "course-url"


# --------------------------------------------------------------------------
# config.ini 读写（按行改写，保留注释与原排版）
# --------------------------------------------------------------------------
def ensure_config() -> None:
    if CONFIG.is_file():
        return
    if not EXAMPLE.is_file():
        print(f"[ERROR] 找不到 {EXAMPLE.name}，无法生成配置文件。")
        sys.exit(1)
    shutil.copy2(EXAMPLE, CONFIG)
    print(f"已根据 {EXAMPLE.name} 生成 config.ini")


def _section_bounds(lines: list[str], section: str):
    start = end = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None:
                return start, i
            if s.lower() == f"[{section}]":
                start = i
    return start, (len(lines) if start is not None else None)


def read_config() -> dict[str, dict[str, str]]:
    """读出全部段落 -> 键值。"""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    data: dict[str, dict[str, str]] = {}
    cur = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s.strip("[]").strip().lower()
            data.setdefault(cur, {})
            continue
        if cur is None or s.startswith((";", "#")) or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        data[cur][k.strip().lower()] = v.strip()
    return data


def read_value(section: str, key: str) -> str:
    return read_config().get(section.lower(), {}).get(key.lower(), "")


def write_value(section: str, key: str, value: str) -> None:
    """按行改写，保留注释（configparser 写回会丢掉注释）。"""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    start, end = _section_bounds(lines, section)

    if start is None:
        lines += ["", f"[{section}]", f"{key} = {value}"]
        CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    assert end is not None
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            # 保留原有的键名写法（配置里是 URL1，就不要写成 url1）
            old_key = lines[i].split("=", 1)[0].rstrip()
            lines[i] = f"{old_key} = {value}"
            break
    else:
        lines.insert(end, f"{key} = {value}")

    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 交互
# --------------------------------------------------------------------------
def ask(label: str, secret: bool = False) -> str:
    while True:
        try:
            # 只有真正接在终端上才用 getpass（能隐藏输入）。
            # stdin 是管道/重定向时 getpass 会去读控制台而卡死。
            if secret and sys.stdin.isatty():
                import getpass
                raw = getpass.getpass(f"  {label}: ")
            else:
                raw = input(f"  {label}: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未做修改。")
            sys.exit(130)
        val = raw.strip()
        if val:
            return val
        print("    不能为空，请重新输入。")


def ask_course_url() -> str:
    """课程播放页地址，做最基本的格式校验。"""
    while True:
        val = ask("课程播放页地址（浏览器打开课程后复制地址栏整条链接）")
        if not re.match(r"^https?://", val, re.IGNORECASE):
            print("    看起来不是网址（应以 http:// 或 https:// 开头），请重新输入。")
            continue
        return val


def banner(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print()


# --------------------------------------------------------------------------
# 各部分
# --------------------------------------------------------------------------
def prompt_account(force: bool = False) -> int:
    """账号部分。force=True 时无条件重新询问。"""
    login_mode = (read_value(SEC_ACCOUNT, "login_mode") or "student").strip().lower()
    need_school = login_mode == "student"

    todo: list[tuple[str, str, bool]] = []
    if need_school and (force or not read_value(SEC_ACCOUNT, "school")):
        todo.append(("school", "学校全称（例：XX职业技术学院）", False))
    if force or not read_value(SEC_ACCOUNT, "username"):
        todo.append(("username", "智慧树账号（手机号 / 邮箱 / 纯学号）", False))
    if force or not read_value(SEC_ACCOUNT, "password"):
        todo.append(("password", "密码", True))

    if not todo:
        return 0

    banner("填写账号信息" if not force else "重新填写账号信息")
    for key, label, secret in todo:
        write_value(SEC_ACCOUNT, key, ask(label, secret))
        print(f"    [OK] 已写入 {key}")
        print()
    return len(todo)


def prompt_course(force: bool = False) -> int:
    """课程部分。"""
    if not force and read_value(SEC_COURSE, "url1"):
        return 0

    banner("填写课程链接" if not force else "更换课程链接")
    print("  提示：地址必须是【视频播放页】，浏览器打开课程后点进播放页再复制。")
    print("        不要填课程介绍页 / 学堂首页，否则识别不到课程目录。")
    print()
    write_value(SEC_COURSE, "url1", ask_course_url())
    print("    [OK] 已写入 URL1")
    print()
    print("    如需一次跑多门课：编辑 config.ini 的 [course-url] 段，")
    print("    依次填 URL1 / URL2 / URL3…（程序会一门一门串行跑完）。")
    print()
    return 1


def show_config() -> None:
    data = read_config()
    banner("当前配置")
    acc = data.get(SEC_ACCOUNT, {})
    crs = data.get(SEC_COURSE, {})

    def mask(v: str) -> str:
        if not v:
            return "(空)"
        return v if len(v) <= 4 else v[:2] + "*" * (len(v) - 4) + v[-2:]

    print("  [账号]")
    print(f"    登录方式 : {acc.get('login_mode', '(未设置)')}")
    print(f"    学校     : {acc.get('school') or '(空)'}")
    print(f"    账号     : {mask(acc.get('username', ''))}")
    print(f"    密码     : {'(已设置)' if acc.get('password') else '(空)'}")
    print()
    print("  [课程]")
    for k in ("url1", "url2", "url3", "url4", "urln"):
        v = crs.get(k, "")
        if v:
            print(f"    {k.upper():<5}: {v[:70]}{'…' if len(v) > 70 else ''}")
    if not any(crs.get(k) for k in ("url1", "url2", "url3", "url4", "urln")):
        print("    (还没有填任何课程链接)")
    print()


# --------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ensure_config()

    args = {a.lower() for a in argv[1:]}
    if "--show" in args or "-s" in args:
        show_config()
        return 0

    force_account = bool(args & {"--account", "-a", "--all"})
    force_course = bool(args & {"--course", "-c", "--all"})

    n = 0
    n += prompt_account(force=force_account)
    n += prompt_course(force=force_course)

    if n:
        print("配置已保存到 config.ini，开始启动。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
