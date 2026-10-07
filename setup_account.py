"""首次运行引导：config.ini 里账号信息为空时，交互式询问并写回。

仓库里不携带任何凭据，所以 fork 出去的项目第一次运行时问用户要。
已有的值不会被覆盖 —— 只补空缺项。

由「启动.bat」在启动主程序前自动调用，也可以单独运行：
    python setup_account.py
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# 控制台编码加固：Windows 默认代码页是 GBK，
# 直接 print 非 GBK 字符（如 ✓）会抛 UnicodeEncodeError 把程序打断。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"
EXAMPLE = ROOT / "config.ini.example"

SECTION = "user-account"


def ensure_config() -> None:
    """没有 config.ini 就从模板生成一份。"""
    if CONFIG.is_file():
        return
    if not EXAMPLE.is_file():
        print(f"[ERROR] 找不到 {EXAMPLE.name}，无法生成配置文件。")
        sys.exit(1)
    shutil.copy2(EXAMPLE, CONFIG)
    print(f"已根据 {EXAMPLE.name} 生成 config.ini")


def _section_bounds(lines: list[str]) -> tuple[int | None, int | None]:
    start = end = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None:
                return start, i
            if s.lower() == f"[{SECTION}]":
                start = i
    return start, (len(lines) if start is not None else None)


def read_config() -> tuple[dict, str]:
    text = CONFIG.read_text(encoding="utf-8")
    lines = text.splitlines()
    start, end = _section_bounds(lines)
    vals: dict[str, str] = {}
    if start is not None and end is not None:
        for ln in lines[start + 1:end]:
            if "=" in ln and not ln.strip().startswith((";", "#")):
                k, _, v = ln.partition("=")
                vals[k.strip().lower()] = v.strip()
    return vals, text


def write_value(key: str, value: str) -> None:
    """按行改写，保留注释与原排版（configparser 写回会丢掉注释）。"""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    start, end = _section_bounds(lines)

    if start is None:
        lines += ["", f"[{SECTION}]", f"{key} = {value}"]
        CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    assert end is not None
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.insert(end, f"{key} = {value}")

    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask(label: str, secret: bool = False) -> str:
    while True:
        try:
            # 只有真正接在终端上才用 getpass（它读控制台、能隐藏输入）。
            # 若 stdin 是管道/重定向（无人值守、脚本调用），
            # getpass 会去读控制台而卡死 —— 此时退化为普通 input。
            use_getpass = secret and sys.stdin.isatty()
            if use_getpass:
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


def main() -> int:
    ensure_config()
    vals, _ = read_config()

    login_mode = (vals.get("login_mode") or "student").strip().lower()
    need_school = login_mode == "student"

    todo: list[tuple[str, str, bool]] = []
    if need_school and not vals.get("school"):
        todo.append(("school", "学校全称（例：XX职业技术学院）", False))
    if not vals.get("username"):
        todo.append(("username", "智慧树账号（手机号 / 邮箱 / 纯学号）", False))
    if not vals.get("password"):
        todo.append(("password", "密码", True))

    if not todo:
        return 0

    print()
    print("=" * 56)
    print("  首次运行：请填写账号信息")
    print("  （仓库不携带任何凭据，填一次就会存进 config.ini）")
    print("=" * 56)
    print()

    for key, label, secret in todo:
        write_value(key, ask(label, secret))
        print(f"    [OK] 已写入 {key}")
        print()

    print("账号信息已保存到 config.ini，开始启动。")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
