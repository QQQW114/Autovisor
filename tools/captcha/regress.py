"""离线回归测试：对数据集里的每题跑求解器，输出报告与标注图。

用来在不需要现场触发验证码的情况下找 bug：
  - 崩溃 / 异常
  - 题面解析失败
  - 拒答原因是否合理
  - 选中位置是否对（配合标注图人工核对）
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

RUN = Path(r"D:\dsh_Dwork\zhihuishu-auto\avsrc")
sys.path.insert(0, str(RUN))

import cv2  # noqa: E402
from modules import captcha_solver  # noqa: E402

DS = RUN / "verify_shots" / "_dataset"
OUT = DS / "_report"
OUT.mkdir(parents=True, exist_ok=True)


def a(s, n=60):
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


def annotate(img, res, path):
    vis = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    for c in res.chars:
        x1 = int((c.cx - c.w / 2) * 3)
        y1 = int((c.cy - c.h / 2) * 3)
        x2 = int((c.cx + c.w / 2) * 3)
        y2 = int((c.cy + c.h / 2) * 3)
        cv2.rectangle(vis, (x1, y1), (x2, y2), (150, 150, 150), 1)
        cv2.putText(vis, f"{c.text or '?'}{c.lean_deg:+.0f}",
                    (x1, max(16, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (0, 0, 200), 1)
    if res.ok:
        px, py = int(res.x * 3), int(res.y * 3)
        cv2.drawMarker(vis, (px, py), (0, 0, 255), cv2.MARKER_CROSS, 34, 3)
        cv2.circle(vis, (px, py), 30, (0, 0, 255), 3)
    cv2.imwrite(str(path), vis)


def main():
    data = json.loads((DS / "dataset.json").read_text(encoding="utf-8"))
    rows = []
    for i, item in enumerate(data):
        local = DS / item.get("local", "")
        row = {"idx": i, "prompt": item["prompt"], "img": item.get("local"),
               "ok": False, "why": "", "pick": None, "nchar": 0, "err": None}
        if not local.is_file():
            row["err"] = "图片缺失"
            rows.append(row)
            continue
        img = cv2.imread(str(local))
        if img is None:
            row["err"] = "读图失败"
            rows.append(row)
            continue
        try:
            res = captcha_solver.solve(img, item["prompt"])
            row["ok"] = res.ok
            row["why"] = res.reason
            row["nchar"] = len(res.chars)
            if res.ok:
                row["pick"] = [round(res.x), round(res.y)]
            annotate(img, res, OUT / f"case{i:03d}.png")
        except Exception as e:
            row["err"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    print(f"{'#':<4}{'题面':<34}{'结果':<6}{'字符':<5}{'说明'}")
    print("-" * 96)
    for r in rows:
        pr = a(r["prompt"], 32)
        st = "OK" if r["ok"] else ("ERR" if r["err"] else "X")
        why = a(r["err"] or r["why"], 44)
        print(f"{r['idx']:<4}{pr:<34}{st:<6}{r['nchar']:<5}{why}")

    total = len(rows)
    oks = sum(1 for r in rows if r["ok"])
    errs = sum(1 for r in rows if r["err"])
    print()
    print(f"合计 {total} 题：给出结论 {oks}，异常 {errs}，拒答 {total - oks - errs}")

    reasons = Counter()
    for r in rows:
        if r["ok"] or r["err"]:
            continue
        w = r["why"]
        if "提示解析失败" in w or "未解析出候选" in w:
            reasons["解析失败"] += 1
        elif "未找到目标字符" in w or "未找到参照字符" in w or "未找到候选字符" in w:
            reasons["字符未找到(OCR)"] += 1
        elif "无法确定" in w:
            reasons["多个同名候选"] += 1
        elif "接近" in w:
            reasons["区分度不足"] += 1
        elif "无候选" in w:
            reasons["无匹配"] += 1
        else:
            reasons["其他"] += 1
    print("拒答原因分布:", dict(reasons))
    print(f"标注图: {OUT}")

    # 落一份 UTF-8 报表，便于阅读中文题面
    lines = ["# 求解器离线回归报告", "",
             f"样本 {total} 题：给出结论 {oks}，异常 {errs}，拒答 {total - oks - errs}", "",
             "| # | 题面 | 结果 | 字符数 | 说明 |", "|---|---|---|---|---|"]
    for r in rows:
        st = "OK" if r["ok"] else ("**异常**" if r["err"] else "拒答")
        desc = (r["err"] or r["why"]).replace("|", "/")
        lines.append(f"| {r['idx']} | {r['prompt']} | {st} | {r['nchar']} | {desc} |")
    lines += ["", "## 拒答原因分布", "", str(dict(reasons))]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报表: {OUT / 'summary.md'}")


if __name__ == "__main__":
    main()
