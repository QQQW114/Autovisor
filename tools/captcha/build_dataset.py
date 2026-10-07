"""从日志与取证截图中重建「题目+图片」配对数据集，用于离线回归测试。

来源：
  1. logs/*.txt 里的「安全验证取证」行给出图片文件名与尺寸
     紧邻的「验证码自动作答 | ... 题目=...」行给出题面
  2. verify_shots/_sweep/<ts>/ 下 records.json 与 rNN.png 的对应关系

产物：verify_shots/_dataset/dataset.json + 归集的图片
"""
import json
import re
import shutil
from pathlib import Path

RUN = Path(r"D:\dsh_Dwork\zhihuishu-auto\avsrc")
SHOTS = RUN / "verify_shots"
LOGS = RUN / "logs"
OUT = SHOTS / "_dataset"
OUT.mkdir(parents=True, exist_ok=True)

# 取证件：元素=.yidun_popup .yidun_modal 文件=xxx.png 尺寸=WxH
RE_SHOT = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\.\d+\].*安全验证取证 \| 元素=\.yidun_popup \.yidun_modal "
    r"文件=(\S+\.png) 尺寸=(\d+)x(\d+)")
# 作答件：题目=...
RE_SOLVE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\.\d+\].*验证码自动作答 \| 尝试=(\d+)/(\d+) 题目=(.+?) 结果=(成功|放弃)")
# 取证整页件
RE_FULL = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\.\d+\].*安全验证取证 \| 文件=(\S+\.png)$")


def secs(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def collect_from_logs():
    pairs = []
    for lf in sorted(LOGS.glob("Log*.txt")):
        try:
            lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        shots = []      # (secs, filename, w, h)
        solves = []     # (secs, prompt, attempt)
        for ln in lines:
            m = RE_SHOT.match(ln)
            if m:
                shots.append((secs(m.group(1)), m.group(2),
                              int(m.group(3)), int(m.group(4))))
                continue
            m = RE_SOLVE.match(ln)
            if m:
                solves.append((secs(m.group(1)), m.group(4).strip(),
                               int(m.group(2))))
        # 配对：取证后 30 秒内出现的第一次作答
        for s_sec, fname, w, h in shots:
            cand = [x for x in solves if 0 <= x[0] - s_sec <= 30]
            if not cand:
                continue
            cand.sort(key=lambda x: x[0])
            pairs.append({
                "img": fname, "prompt": cand[0][1], "attempt": cand[0][2],
                "size": [w, h], "log": lf.name,
            })
    return pairs


def collect_from_sweeps():
    pairs = []
    sweep_root = SHOTS / "_sweep"
    # 兼容两种布局：新（按时间戳子目录）与旧（直接平铺）
    candidates = []
    for rec in sweep_root.glob("*/records.json"):
        candidates.append((rec.parent, rec))
    flat = sweep_root / "records.json"
    if flat.is_file():
        candidates.append((sweep_root, flat))

    for base, rec in candidates:
        try:
            data = json.loads(rec.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in data:
            img = base / f"r{r['round']:02d}.png"
            if not img.is_file():
                continue
            pairs.append({
                "img": str(img.relative_to(SHOTS)),
                "prompt": r["prompt"], "attempt": 1,
                "size": None, "log": base.name,
                "prev_ok": r.get("ok"), "prev_why": r.get("why"),
            })
    return pairs


def is_real_prompt(t: str) -> bool:
    """过滤掉误配进来的弹窗标题。

    早期版本的读题逻辑会读到 .yidun_modal__title（「请完成安全验证」），
    这类条目不是真题面，会把回归统计拉偏。
    """
    if not t or "点击" not in t:
        return False
    if t.strip() in ("请完成安全验证", "请进行验证", "安全验证"):
        return False
    return True


def main():
    logs = [p for p in collect_from_logs() if is_real_prompt(p["prompt"])]
    sweeps = [p for p in collect_from_sweeps() if is_real_prompt(p["prompt"])]
    print(f"来自日志: {len(logs)} 对")
    print(f"来自抓题: {len(sweeps)} 对")

    # 去重（同图同题只留一份）
    seen = set()
    merged = []
    for p in logs + sweeps:
        key = (Path(p["img"]).name, p["prompt"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)

    # 归集图片
    ok = 0
    for i, p in enumerate(merged):
        src = SHOTS / p["img"]
        if not src.is_file():
            p["missing"] = True
            continue
        ext = src.suffix
        dst = OUT / f"cap{i:03d}{ext}"
        try:
            shutil.copy2(src, dst)
            p["local"] = dst.name
            ok += 1
        except Exception as e:
            p["copy_error"] = str(e)

    (OUT / "dataset.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"归集成功 {ok} / {len(merged)} 张")
    print(f"数据集: {OUT / 'dataset.json'}")

    # 题型分布
    kinds = {}
    for p in merged:
        q = p["prompt"]
        if "颜色" in q:
            k = "颜色匹配"
        elif "正向" in q:
            k = "正向筛选"
        elif any(w in q for w in ("方向", "朝向")):
            k = "朝向匹配"
        else:
            k = "直接识别"
        kinds[k] = kinds.get(k, 0) + 1
    print("题型分布:", kinds)


if __name__ == "__main__":
    main()
