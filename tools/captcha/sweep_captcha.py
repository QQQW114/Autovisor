"""利用验证码的刷新按钮批量采集样本并测试求解器。

点刷新只换题、不计入验证失败，所以可以安全地大量采集，
用来统计识别准确率、发现新题型。

注意：全程只读 + 点刷新，不点击作答区域。
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

RUN = Path(r"D:\dsh_Dwork\zhihuishu-auto\avsrc")
sys.path.insert(0, str(RUN))

import cv2  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from modules import tasks, captcha_solver  # noqa: E402

OUT_ROOT = RUN / "verify_shots" / "_sweep"

REFRESH_SELECTORS = (
    ".yidun_refresh",
    ".yidun_modal__refresh",
    "[class*='yidun_refresh']",
    "[class*='refresh']",
)


async def find_refresh(page):
    """定位验证码右上角的刷新按钮。"""
    for f in page.frames:
        for sel in REFRESH_SELECTORS:
            try:
                els = await f.query_selector_all(sel)
            except Exception:
                continue
            for el in els:
                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                    if box and box["width"] >= 6 and box["height"] >= 6:
                        return el, box, sel
                except Exception:
                    continue
    return None, None, None


async def main(rounds: int):
    # 每次运行独立目录，避免覆盖历史样本
    OUT = OUT_ROOT / time.strftime("%Y%m%d_%H%M%S")
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {OUT}")
    records = []
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "zhihuishu" in p.url), None)
        if page is None:
            print("未找到页面")
            return

        el, box, sel = await find_refresh(page)
        if el is None:
            print("未找到刷新按钮，候选选择器都没命中。")
            return
        print(f"刷新按钮: {sel}  位置=({box['x']:.0f},{box['y']:.0f}) "
              f"{box['width']:.0f}x{box['height']:.0f}")

        vbox = None
        for r in range(1, rounds + 1):
            try:
                # 读题
                prompt = await tasks._read_verify_prompt(page)
                # 验证码区域截图
                cel, cbox = await tasks._verify_shot_box(page)
                if cel is None:
                    print(f"[{r}] 找不到验证码区域")
                    break
                vbox = cbox
                shot = OUT / f"r{r:02d}.png"
                await cel.screenshot(path=str(shot))
                crop = cv2.imread(str(shot))

                color, target, cand = captcha_solver.parse_prompt(prompt)
                res = None
                try:
                    res = captcha_solver.solve(crop, prompt)
                except Exception as e:
                    print(f"[{r}] solve 异常: {type(e).__name__}: {e}")

                rec = {
                    "round": r,
                    "prompt": prompt,
                    "color": color,
                    "target": target,
                    "candidate": cand,
                    "ok": bool(res.ok) if res else False,
                    "why": res.reason if res else "solve异常",
                    "pick": [round(res.x, 1), round(res.y, 1)] if res and res.ok else None,
                    "conf": round(res.confidence, 3) if res else 0,
                    "chars": [
                        {"t": c.text, "color": c.color, "sat": round(c.saturation),
                         "lean": round(c.lean_deg, 2), "pos": [round(c.cx), round(c.cy)]}
                        for c in (res.chars if res else [])
                    ],
                }
                records.append(rec)
                safe_prompt = re.sub(r"[^\x20-\x7e]", "?", prompt)
                print(f"[{r}] {safe_prompt[:44]:<46} "
                      f"{'OK ' if rec['ok'] else 'X  '} {rec['why'][:34]}")

                # 点刷新换题（只换题，不作答）
                await el.click()
                await page.wait_for_timeout(1600)

            except Exception as e:
                print(f"[{r}] 异常: {type(e).__name__}: {e}")
                await page.wait_for_timeout(1200)

    # 汇总
    (OUT / "records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(records)
    ok = sum(1 for r in records if r["ok"])
    print(f"\n=== 汇总 ===")
    print(f"采集 {total} 题，求解器给出结论 {ok} 题 "
          f"({ok / total * 100:.0f}%)，拒答 {total - ok} 题")

    # 题型分布
    kinds = {}
    for r in records:
        p = r["prompt"]
        if "颜色" in p:
            k = "颜色匹配"
        elif "方向" in p or "朝向" in p:
            k = "朝向匹配"
        elif "正向" in p:
            k = "正向筛选"
        elif p.count("点击") and not any(w in p for w in ("一样", "一致", "相同")):
            k = "直接识别"
        else:
            k = "其他"
        kinds[k] = kinds.get(k, 0) + 1
    print("题型分布:", kinds)
    print(f"记录: {OUT / 'records.json'}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(n))
