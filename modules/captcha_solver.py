"""腾讯 tcaptcha「按方向匹配点选」求解器。

形态（实测于智慧树课中安全验证）：
    提示：请点击【灰色小写 v】朝方向一致的大写 Q
    图中有多个不同颜色、不同旋转角度的字符

算法（不依赖外部模型，纯本地 CV）：
    1. 按颜色分离字符（每个字符颜色不同，比形态学分割可靠）
    2. ddddocr 逐个识别字符  ← 注意：绝不能调用 set_ranges()，会破坏识别
    3. 用「模板旋转匹配」判定朝向：
       把字符旋转一圈，找与正立模板最吻合的角度 = 它相对正立姿态的偏差
    4. 从提示中解析出「目标字符」与「待选字符」
    5. 选偏差角最接近的候选 → 返回其坐标

风险控制（重要）：
    验证码连续失败可能升级或锁号，因此本模块返回 (坐标, 置信度)，
    调用方必须在置信度不足时【放弃自动作答、转人工】，绝不盲点。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import cv2
    import numpy as np
except ImportError:  # 允许在缺依赖的环境里导入本模块
    cv2 = None
    np = None

try:
    import ddddocr
except ImportError:
    ddddocr = None

TEMPLATE_SIZE = 96
MIN_CONFIDENCE = 0.45      # 目标字符与候选字符的最低形状匹配度
MAX_ANGLE_DIFF = 22.0      # 方向一致的最大角度差（度）


@dataclass
class CharBox:
    """一个识别出的字符。"""
    color: str
    text: str
    cx: float                  # 在裁剪图中的坐标
    cy: float
    w: int
    h: int
    area: int
    saturation: float
    # 各字符模板下的 (偏差角, 形状匹配度)，键为模板字符
    rots: dict = field(default_factory=dict)
    is_grey: bool = False
    lean: float = 0.0          # 倾斜量：顶部重心相对底部的横向偏移(px)
    lean_deg: float = 0.0      # 倾斜换算成角度(度)，正=右倾
    hue: float = 0.0           # 色相（用于"颜色一样"类题）
    value: float = 0.0         # 明度

    def get(self, ch: str) -> tuple[Optional[int], float]:
        """取该字符在模板 ch 下的 (偏差角, 匹配度)。"""
        return self.rots.get(ch, (None, -1.0))

    def best_for(self, ch: str) -> tuple[Optional[int], float]:
        """取最能代表字符 ch 的模板结果（大小写归一后择优选）。"""
        cands = [ch, ch.lower(), ch.upper()]
        if ch in "9q":
            cands = ["9", "q", "Q", "9"]
        best = (None, -1.0)
        for c in cands:
            a, sc = self.get(c)
            if a is not None and sc > best[1]:
                best = (a, sc)
        return best

    def __repr__(self) -> str:
        return (f"<{self.color}{self.text!r} ({self.cx:.0f},{self.cy:.0f}) "
                f"sat={self.saturation:.0f}>")


@dataclass
class SolveResult:
    ok: bool
    x: float = 0.0
    y: float = 0.0
    reason: str = ""
    prompt: str = ""
    chars: list = field(default_factory=list)
    confidence: float = 0.0


# --------------------------------------------------------------------------
# 图像处理
# --------------------------------------------------------------------------
# 需要预生成模板的字符集（覆盖题目中常见的数字与字母）
TEMPLATE_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _templates() -> dict:
    """生成正立字符模板（延迟构建并缓存）。"""
    global _TMPL_CACHE
    try:
        return _TMPL_CACHE
    except NameError:
        pass

    cache = {}
    font = cv2.FONT_HERSHEY_SIMPLEX
    for ch in TEMPLATE_CHARS:
        canvas = np.zeros((TEMPLATE_SIZE * 2, TEMPLATE_SIZE * 2), np.uint8)
        # 小写字母用略小字号，避免渲染尺寸与视觉大小偏离过多
        if ch.isupper() or ch.isdigit():
            scale, thick = 2.2, 6
        else:
            scale, thick = 2.0, 5
        (tw, th), _ = cv2.getTextSize(ch, font, scale, thick)
        cv2.putText(canvas, ch,
                    ((TEMPLATE_SIZE * 2 - tw) // 2, (TEMPLATE_SIZE * 2 + th) // 2),
                    font, scale, 255, thick, cv2.LINE_AA)
        ys, xs = np.nonzero(canvas)
        if len(xs) == 0:
            continue
        t = canvas[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        cache[ch] = cv2.resize(t, (TEMPLATE_SIZE, TEMPLATE_SIZE),
                               interpolation=cv2.INTER_AREA)
    _TMPL_CACHE = cache
    return cache


def _normalize(mask: np.ndarray) -> Optional[np.ndarray]:
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        return None
    sub = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    return cv2.resize(sub, (TEMPLATE_SIZE, TEMPLATE_SIZE),
                      interpolation=cv2.INTER_AREA)


def best_rotation(char_mask: np.ndarray, tmpl: np.ndarray,
                  step: int = 5, fine: bool = True) -> tuple[Optional[int], float]:
    """字符相对正立模板的偏差角。

    两级搜索：先按 step 粗搜，再在最优附近以 1 度精搜。
    这样把角度量化误差从 ±step/2 降到 ±0.5，提高相邻候选的区分度。
    返回 (角度, IoU 得分)。
    """
    norm = _normalize(char_mask)
    if norm is None or tmpl is None:
        return None, -1.0
    tn = tmpl > 100
    center = (TEMPLATE_SIZE / 2, TEMPLATE_SIZE / 2)

    def score_at(a: int) -> float:
        M = cv2.getRotationMatrix2D(center, -a, 1.0)
        rot = cv2.warpAffine(norm, M, (TEMPLATE_SIZE, TEMPLATE_SIZE),
                             flags=cv2.INTER_LINEAR)
        r = rot > 100
        inter = np.count_nonzero(r & tn)
        union = np.count_nonzero(r | tn)
        return inter / union if union else 0.0

    best_a, best_s = None, -1.0
    for a in range(-180, 180, step):
        sc = score_at(a)
        if sc > best_s:
            best_s, best_a = sc, a

    if fine and best_a is not None:
        for a in range(best_a - step + 1, best_a + step):
            sc = score_at(a)
            if sc > best_s:
                best_s, best_a = sc, a

    return best_a, best_s


def _color_groups(crop: np.ndarray) -> dict:
    """按颜色把前景分成若干组（每个字符颜色不同，按色相/饱和度切分最可靠）。

    色相经验值（OpenCV H 0~180）：
        黄 ≈ 18~40    绿 ≈ 40~95     蓝 ≈ 95~135
        红/粉 ≈ 0~12 或 165~180
        灰 = 低饱和
    """
    H, W = crop.shape[:2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    groups = {
        "黄":   (h >= 15) & (h < 42) & (s > 70) & (v > 140),
        "绿":   (h >= 42) & (h < 95) & (s > 55) & (v > 110),
        "蓝":   (h >= 95) & (h <= 135) & (s > 55) & (v > 100),
        "红粉": (((h <= 14) | (h >= 163)) & (s > 60) & (v > 100)),
        # 灰：低饱和且不是明亮的背景
        "灰":   (s < 55) & (v > 90) & (v < 212),
    }
    for k in groups:
        groups[k][0:45, max(0, W - 62):W] = False      # 去掉右上角刷新按钮
    return groups


def _split_component(crop: np.ndarray, mask: np.ndarray,
                     max_w: float, max_h: float) -> list:
    """把过大的连通块拆成多个（处理"黄色m与D粘连"这类情况）。

    做法：距离变换取峰值，用分水岭式的种子生长把区域切开。
    返回若干 (子掩码, 包围盒) 列表。
    """
    m = (mask.astype(np.uint8)) * 255
    dist = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    if dist.max() <= 0:
        return []

    # 找种子：距离变换的局部高峰
    thr = max(2.0, dist.max() * 0.55)
    seeds = (dist >= thr).astype(np.uint8)
    n_seed, seed_lab = cv2.connectedComponents(seeds, 8)

    # 种子不足两个说明不是粘连，交回调用方按单块处理
    if n_seed <= 2:
        return []

    out = []
    for sid in range(1, n_seed):
        seed_mask = (seed_lab == sid).astype(np.uint8) * 255
        # 以种子为标记做膨胀重建，近似分水岭
        grow = seed_mask.copy()
        prev = 0
        while True:
            grow = cv2.dilate(grow, np.ones((3, 3), np.uint8))
            grow = cv2.bitwise_and(grow, m)
            cur = int(np.count_nonzero(grow))
            if cur == prev:
                break
            prev = cur
        ys, xs = np.nonzero(grow)
        if len(xs) < 80:
            continue
        sub_mask = np.zeros_like(m, np.uint8)
        sub_mask[grow > 0] = 255
        out.append((sub_mask, (int(xs.min()), int(ys.min()),
                               int(xs.max()) - int(xs.min()) + 1,
                               int(ys.max()) - int(ys.min()) + 1)))
    return out if len(out) > 1 else []


def _iou_scaled(crop: np.ndarray, boxes: list) -> np.ndarray:
    """粗略估计字符的饱和度和亮度（备用）。"""
    return crop


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _name_color(hue: float, sat: float, val: float) -> str:
    """根据字符自身像素的 (色相, 饱和, 明度) 归纳颜色名。"""
    if sat < 55:
        return "灰"
    if hue < 15 or hue >= 163:
        return "红粉"
    if hue < 42:
        return "黄"
    if hue < 95:
        return "绿"
    if hue <= 135:
        return "蓝"
    return "其他"


def _foreground(crop: np.ndarray):
    """取前景掩码（排除浅灰背景与右上角刷新按钮）。

    注意：不要做 MORPH_CLOSE —— 那会把相邻字符连成一块，
    实测开运算之后直接用连通域，7 个字符能分出 7 块（仅 1 处对角粘连）。
    """
    H, W = crop.shape[:2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    bg = (s < 40) & (v > 210)
    fg = (~bg).astype(np.uint8) * 255
    fg[0:int(H * 0.18), max(0, W - 64):W] = 0     # 刷新按钮
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return fg


def _segment(mask: np.ndarray) -> list:
    """把前景切成互不重叠的字符块。

    相邻字符可能对角相接（如黄色 m 在左上、D 在右下），单方向矩形切分无法处理，
    因此用「距离变换取峰 + 各种子独立膨胀」做近似分水岭。
    种子阈值取 0.45*峰值——实测该值对"两字符对角粘连"恰好给出 2 个种子；
    再高会把两个峰并成一个，再低则把立体字符的一个笔画当成独立字符。
    """
    m = (mask > 0).astype(np.uint8)
    if not m.any():
        return []

    # 先按普通连通域拿到各个块
    n0, lab0, st0, _ = cv2.connectedComponentsWithStats(m * 255, 8)
    blocks = []
    for i in range(1, n0):
        x, y, w, h, area = st0[i]
        if area < 130:
            continue
        blocks.append((int(x), int(y), int(w), int(h), i, lab0))

    out = []
    for x, y, w, h, idx, lab in blocks:
        own = (lab == idx)
        # 尺寸正常 → 直接用
        if w <= 60 and h <= 60:
            ys, xs = np.nonzero(own)
            out.append((int(xs.min()), int(ys.min()),
                        int(xs.max() - xs.min() + 1),
                        int(ys.max() - ys.min() + 1), own))
            continue

        # 尺寸偏大 → 疑似多字符粘连，做分水岭
        sub = (own.astype(np.uint8)) * 255
        dist = cv2.distanceTransform(sub, cv2.DIST_L2, 5)
        peak = float(dist.max())
        if peak <= 0:
            continue
        seeds = (dist >= peak * 0.45).astype(np.uint8)
        n_seed, seed_lab = cv2.connectedComponents(seeds, 8)
        if n_seed <= 1:
            ys, xs = np.nonzero(own)
            out.append((int(xs.min()), int(ys.min()),
                        int(xs.max() - xs.min() + 1),
                        int(ys.max() - ys.min() + 1), own))
            continue

        grow = seed_lab.astype(np.int32)
        claimed = grow > 0
        kernel = np.ones((3, 3), np.uint8)
        for _ in range(max(w, h) + 5):
            grew = False
            for sid in range(1, n_seed):
                cur = (grow == sid).astype(np.uint8) * 255
                if not cur.any():
                    continue
                ext = (cv2.dilate(cur, kernel) > 0) & m.astype(bool) & (grow == 0)
                if ext.any():
                    grow[ext] = sid
                    claimed |= ext
                    grew = True
            if not grew:
                break

        for sid in range(1, n_seed):
            comp = (grow == sid)
            if comp.sum() < 80:
                continue
            ys, xs = np.nonzero(comp)
            out.append((int(xs.min()), int(ys.min()),
                        int(xs.max() - xs.min() + 1),
                        int(ys.max() - ys.min() + 1), comp))

    return out


def extract_chars(crop: np.ndarray, debug_dir: Optional[Path] = None) -> list[CharBox]:
    """从验证码图片中提取所有字符。

    流程：统一前景 → 分水岭切分（互不重叠）→ 逐块 OCR + 颜色 + 方向。
    不再按颜色预先分组，避免同一字符在多个色组里被重复识别。
    """
    if ddddocr is None:
        raise RuntimeError("未安装 ddddocr：pip install ddddocr")

    ocr = ddddocr.DdddOcr(show_ad=False)      # 关键：不要 set_ranges()
    tmpls = _templates()
    H, W = crop.shape[:2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h_chan, s_chan, v_chan = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    out: list[CharBox] = []
    vis = None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        vis = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

    fg = _foreground(crop)
    blocks = _segment(fg)
    # 兜底：分水岭没结果时用普通连通域
    if not blocks:
        n, labels, stats, cents = cv2.connectedComponentsWithStats(fg, 8)
        for i in range(1, n):
            x, y, w, hh, area = stats[i]
            if area < 150 or w > W * 0.5 or hh > H * 0.85:
                continue
            blocks.append((int(x), int(y), int(w), int(hh), labels == i))

    for x, y, w, hh, own in blocks:
        if w > W * 0.5 or hh > H * 0.85:
            continue
        # 右上角的刷新按钮会被当成字符（灰色），必须排除，
        # 否则在"找灰色字符"类题目里会干扰判断。
        # 实测按钮固定在图片区右上角（约 0.91W, 0.27H），字符不会出现在那里，
        # 因此直接按位置过滤，不依赖面积（按钮面积会随截图尺寸变化）。
        if x > W * 0.80 and y < H * 0.40:
            continue
        pad = 4
        sub = crop[max(0, y - pad):y + hh + pad, max(0, x - pad):x + w + pad]
        if sub.size == 0:
            continue

        # OCR：多方案投票。
        # 实测：直接放大原始彩色图最稳（4 倍最优），
        #      而"白底黑字"掩码法会把大写误判成小写（灰 D→'d'），只在必要时兜底。
        g_gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        _, b = cv2.threshold(g_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        votes = {}
        for tag, img in (
            ("raw4", cv2.resize(sub, None, fx=4, fy=4,
                                interpolation=cv2.INTER_CUBIC)),
            ("bin6", cv2.resize(b, None, fx=6, fy=6,
                                interpolation=cv2.INTER_CUBIC)),
        ):
            try:
                ok2, buf2 = cv2.imencode(".png", img)
                t = ocr.classification(buf2.tobytes()).strip()
            except Exception:
                t = ""
            t = re.sub(r"[^A-Za-z0-9]", "", t)
            if t:
                votes[t] = votes.get(t, 0) + 1
        if votes:
            text = max(votes.items(), key=lambda kv: (kv[1], len(kv[0])))[0][:1]
        else:
            text = ""
        if text and len(text) > 1:
            text = text[0]

        # 颜色：用该块自身像素的中位值判定
        hue_m = float(np.median(h_chan[own]))
        sat_m = float(np.median(s_chan[own]))
        val_m = float(np.median(v_chan[own]))
        cname = _name_color(hue_m, sat_m, val_m)

        ys, xs = np.nonzero(own)
        cx, cy = float(xs.mean()), float(ys.mean())

        # 倾斜：顶部重心相对底部的横向偏移，换算成角度
        hgt = ys.max() - ys.min() + 1
        top = ys < ys.min() + hgt * 0.25
        bot = ys > ys.max() - hgt * 0.25
        lean = float(xs[top].mean() - xs[bot].mean()) if (top.any() and bot.any()) else 0.0
        lean_deg = float(np.degrees(np.arctan2(lean, max(1.0, hgt * 0.5))))

        # 方向：OCR 结果 + 其大小写变体 + 常见字符兜底
        wanted = []
        if text and text.isalnum():
            wanted += [text, text.upper(), text.lower()]
        wanted += ["Q", "q", "V", "v", "N", "n", "9", "m"]
        seen_t = set()
        rots: dict = {}
        for ch in wanted:
            if ch in seen_t or ch not in tmpls:
                continue
            seen_t.add(ch)
            if len(seen_t) > 6:
                break
            a, sc = best_rotation(own, tmpls[ch])
            rots[ch] = (a, sc)

        out.append(CharBox(
            color=cname, text=text, cx=cx, cy=cy, w=int(w), h=int(hh),
            area=int(own.sum()), saturation=sat_m, rots=rots,
            is_grey=sat_m < 55,
            lean=lean, lean_deg=lean_deg,
            hue=hue_m, value=val_m,
        ))

        if vis is not None:
            cv2.rectangle(vis, (x * 3, y * 3), ((x + w) * 3, (y + hh) * 3),
                          (0, 0, 255), 2)
            cv2.putText(vis, f"{cname}{text}", (x * 3, max(16, y * 3 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

    if vis is not None and debug_dir:
        cv2.imwrite(str(debug_dir / "chars.png"), vis)
    return out


def parse_prompt(prompt: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """从提示文字中解析 (颜色, 目标字符, 候选字符)。

    支持的措辞形态（实测归纳）：
        请点击灰色小写 v 朝方向一致的大写 Q
        请点击灰色小写 n 朝方向一样的数字 9
        请点击【灰色小写v】朝方向一致的【大写Q】
    """
    if not prompt:
        return None, None, None
    p = prompt.replace(" ", "").replace("【", "").replace("】", "")

    colors = ["灰色", "灰", "黄色", "黄", "蓝色", "蓝", "红色", "红",
              "粉色", "粉", "绿色", "绿", "紫色", "紫", "橙色", "橙"]
    color = next((c for c in colors if c in p), None)

    # 把 [类型标记 + 字符] 全部找出来：小写v / 大写Q / 数字9 / 字母A
    TYPE_CH = re.compile(r"(小写|大写|数字|字母|字符)([A-Za-z0-9])")
    found = [m.group(2) for m in TYPE_CH.finditer(p)]
    if len(found) >= 2:
        target, candidate = found[0], found[-1]
    elif len(found) == 1:
        target, candidate = found[0], None
    else:
        target = candidate = None

    # 兜底：若只解析出一个，尝试从"数字X/大写X"里补出候选
    if candidate is None:
        m = re.search(r"数字([0-9])", p)
        if m and m.group(1) != target:
            candidate = m.group(1)

    # 目标与候选相同且句中出现两次，则取最后一次（避免 target == candidate）
    if target and candidate and target == candidate:
        cands = [m.group(2) for m in TYPE_CH.finditer(p)]
        if len(cands) >= 2 and cands[-1] != cands[0]:
            candidate = cands[-1]

    return color, target, candidate


def solve(crop: np.ndarray, prompt: str,
          debug_dir: Optional[Path] = None) -> SolveResult:
    """求解验证码，返回应点击的位置（裁剪图坐标系）。

    题型分两大类：
      直接识别   「请点击灰色小写n」「请点击灰色数字6」       → 只需找目标字符
      属性比对   「…朝向一样的大写Q」「…颜色一样的大写A」    → 需比较参照与候选
                 「请点击正向的大写P」                       → 选倾斜最小的候选

    调用方负责把裁剪图坐标换算成页面坐标，并在 ok=False 时转人工。
    """
    color, target, candidate = parse_prompt(prompt)
    chars = extract_chars(crop, debug_dir)
    res = SolveResult(ok=False, prompt=prompt, chars=chars)

    if not chars:
        res.reason = "未识别到任何字符"
        return res
    if not target:
        res.reason = f"提示解析失败: {prompt!r}"
        return res

    def match_strict(cb: CharBox, ch: str) -> bool:
        return bool(cb.text) and cb.text == ch

    def match_loose(cb: CharBox, ch: str) -> bool:
        if not cb.text:
            return False
        a, b = cb.text, ch
        if a.lower() == b.lower():
            return True
        # 易混字符组：OCR 在这几组之间经常认错。
        # 注意 pick() 是先严格匹配、严格没结果才走宽松，
        # 所以这里只影响"OCR 彻底认错"的情况，不会抢走精确命中的字符。
        groups = ("lI1", "oO0", "zZ2", "sS5", "gG9q")
        for g in groups:
            if a in g and b in g:
                return True
        return False

    def pick(text_ch: str, pool: list) -> list:
        strict = [c for c in pool if match_strict(c, text_ch)]
        return strict if strict else [c for c in pool if match_loose(c, text_ch)]

    def apply_color_limit(hits: list) -> list:
        """按题面里的颜色限定筛选候选。

        题面可能写「灰色」「黄色」「绿色」「蓝色」「红色」「粉色」，
        而 CharBox.color 用的是「灰/黄/绿/蓝/红粉」，需要做别名归一。
        早先只处理了灰色，导致"请点击黄色大写X"这类题会因多个同名候选而放弃。
        """
        if not color or not hits:
            return hits
        alias = {"灰": "灰", "黄": "黄", "绿": "绿", "蓝": "蓝",
                 "红": "红粉", "粉": "红粉", "紫": "紫", "橙": "黄"}
        key = alias.get(color[0])
        if not key:
            return hits
        matched = [c for c in hits if c.color == key]
        if matched:
            return matched
        # 灰色兜底：有些块的颜色名会归到"其他"
        if key == "灰":
            grey = [c for c in hits if c.is_grey]
            if grey:
                return grey
        return hits

    # ---------- 题型判定 ----------
    # 题面结构归纳（实测）：
    #   请点击[颜色][大小写]X                    → 直接识别（颜色限定目标自身）
    #   请点击[颜色][大小写]X朝向一样的大写Y     → 朝向比对（颜色限定参照X）
    #   请点击小写x颜色一样的大写X               → 颜色比对（题面里不写颜色名）
    #   请点击正向/侧向的[大小写]X               → 按倾斜筛选目标自身，无参照
    cmp_words = ("一样", "一致", "相同", "同样")
    wants_upright = "正向" in prompt
    wants_sideways = "侧向" in prompt
    tilt_select = wants_upright or wants_sideways
    has_compare = any(w in prompt for w in cmp_words)
    direct = (not has_compare) and (not tilt_select)
    # 只有目标、没有参照时，一律按直接识别处理
    if not direct and not tilt_select and candidate is None:
        direct = True

    # ---------- 题型A：直接识别 ----------
    if direct:
        hits = apply_color_limit(pick(target, chars))
        if not hits:
            res.reason = f"未找到目标字符 {target!r}"
            return res
        if len(hits) > 1:
            res.reason = f"找到 {len(hits)} 个 {target!r}，无法确定是哪一个"
            return res
        h = hits[0]
        res.ok = True
        res.x, res.y = h.cx, h.cy
        res.confidence = 0.90
        res.reason = f"直接识别: {h.color}{h.text!r}"
        return res

    # ---------- 题型B：正向 / 侧向（按旋转姿态筛选目标字符自身） ----------
    if tilt_select:
        # 判据用「模板旋转角」而非 lean：
        #   lean 只反映轻微倾斜；实测侧向字符能被旋转 ±90°，
        #   此时 lean 仍只有几度，而模板旋转角能正确反映姿态。
        text_hits = apply_color_limit(pick(target, chars))
        # 旋转后的字符常被 OCR 误读（实测侧向的 n 被读成 i），
        # 因此补充"目标模板匹配度尚可"的块一起参与姿态排序。
        pool = list(text_hits)
        for c in chars:
            if any(c is h for h in pool):
                continue
            a2, s2 = c.best_for(target)
            if a2 is not None and s2 >= 0.58:
                pool.append(c)
        if not pool:
            res.reason = f"未找到目标字符 {target!r}"
            return res

        scored = []
        for c in pool:
            ang, sc = c.best_for(target)
            if ang is None:
                continue
            # 折到 0~90：180° 旋转对多数字符等价，只需区分"正立/侧向"
            folded = min(abs(ang), 180 - abs(ang))
            scored.append((folded, sc, c))
        if not scored:
            res.reason = "目标字符无有效姿态测量"
            return res

        scored.sort(key=lambda t: t[0], reverse=wants_sideways)
        folded, sc, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else None

        if second is not None and abs(folded - second) < 6.0:
            res.reason = (f"姿态区分度不足（{folded:.0f}° vs {second:.0f}°），不确定")
            return res
        # 阈值按实测校准：这类验证码的"侧向"并非 90° 躺倒，
        # 实测是"略微歪斜"（正向 1~6°、侧向 14° 左右），所以门槛不能设高。
        if wants_upright and folded > 30:
            res.reason = f"未找到足够正立的 {target!r}（最小旋转 {folded:.0f}°）"
            return res
        if wants_sideways and folded < 8:
            res.reason = f"未找到足够侧向的 {target!r}（最大旋转 {folded:.0f}°）"
            return res

        res.ok = True
        res.x, res.y = best.cx, best.cy
        res.confidence = 0.70
        tag = "正向" if wants_upright else "侧向"
        extra = f" 次选{second:.0f}°" if second is not None else ""
        res.reason = (f"{tag}: {best.color}{best.text!r} 旋转{folded:.0f}° "
                      f"(模板分{sc:.2f}{extra})")
        return res

    if candidate is None:
        res.reason = f"提示解析失败: {prompt!r}"
        return res

    # ---------- 题型C：属性比对（颜色 / 朝向） ----------
    by_color = "颜色" in prompt
    # 题面里的颜色名描述的是「参照字符」（如"绿色大写Q朝向一样的大写G"里的绿色），
    # 候选字符不受该颜色约束，所以只对 targets 施加颜色筛选。
    targets = apply_color_limit(pick(target, chars))
    if not targets:
        res.reason = f"未找到参照字符 {target!r}"
        return res

    cands = pick(candidate, chars)
    if not cands:
        res.reason = f"未找到候选字符 {candidate!r}"
        return res

    # ---- C1：颜色一样 ----
    if by_color:
        # 参照字符取最"有颜色"的那个（灰度字符不参与颜色比对）
        colored = [c for c in targets if c.saturation >= 55]
        ref = max(colored, key=lambda c: c.saturation) if colored else targets[0]

        def hue_dist(a: float, b: float) -> float:
            d = abs(a - b) % 180
            return min(d, 180 - d)

        scored = []
        for c in cands:
            dh = hue_dist(c.hue, ref.hue)
            ds = abs(c.saturation - ref.saturation)
            scored.append((dh * 1.0 + ds * 0.35, dh, ds, c))

        if not scored:
            res.reason = "候选字符无有效颜色测量"
            return res
        scored.sort(key=lambda t: t[0])
        cost, dh, ds, best = scored[0]
        if len(scored) > 1 and (scored[1][0] - cost) < 6.0:
            res.reason = (f"候选色差过于接近 ({cost:.1f} vs {scored[1][0]:.1f})，不确定")
            return res
        if dh > 25:
            res.reason = f"无候选颜色与参照一致（最小色相差 {dh:.1f}）"
            return res
        res.ok = True
        res.x, res.y = best.cx, best.cy
        res.confidence = 0.75
        res.reason = (f"颜色匹配: {best.color}{best.text!r} "
                      f"色相差{dh:.1f} 饱和差{ds:.0f}")
        return res

    # ---- C2：朝向一样 ----
    # 倾向判据：优先用"倾斜角"（几何稳定），模板旋转角仅作参考。
    ref = max(targets, key=lambda c: c.w * c.h)
    ref_tilt = ref.lean_deg

    scored = []
    for c in cands:
        tilt = c.lean_deg
        # 模板角作为辅助：取最能代表该字符模板的偏差角
        ang, sc = c.best_for(candidate)
        d_tilt = abs(tilt - ref_tilt)
        d_tmpl = None
        if ang is not None and sc >= MIN_CONFIDENCE:
            t_ang, t_sc = ref.best_for(target)
            if t_ang is not None:
                d_tmpl = abs((ang - t_ang + 180) % 360 - 180)
        scored.append((d_tilt, d_tmpl, sc, c))

    if not scored:
        res.reason = "候选字符无有效方向测量"
        return res

    scored.sort(key=lambda t: t[0])
    d_tilt, d_tmpl, sc, best = scored[0]

    if len(scored) > 1 and (scored[1][0] - d_tilt) < 3.0:
        res.reason = (f"候选倾斜过于接近 ({d_tilt:.1f}° vs {scored[1][0]:.1f}°)，不确定")
        return res
    if d_tilt > 25.0:
        res.reason = f"无候选与参照朝向一致（最小倾斜差 {d_tilt:.1f}°）"
        return res

    res.ok = True
    res.x, res.y = best.cx, best.cy
    res.confidence = 0.70
    extra = f" 模板角差{d_tmpl:.1f}°" if d_tmpl is not None else ""
    res.reason = (f"朝向匹配: {best.color}{best.text!r} "
                  f"倾斜 {best.lean_deg:+.1f}° vs 参照 {ref_tilt:+.1f}°"
                  f" (差{d_tilt:.1f}°){extra}")
    return res


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
if __name__ == "__main__":
    SHOT = Path(r"D:\dsh_Dwork\zhihuishu-auto\avsrc\verify_shots"
                r"\20261007_172216_full.png")
    OUT = Path(r"D:\dsh_Dwork\zhihuishu-auto\avsrc\verify_shots\_ocr_test")
    img = cv2.imread(str(SHOT))
    crop = img[210:375, 480:800]

    CASES = [
        "请点击灰色小写 v 朝方向一致的大写 Q",
        "请点击灰色小写 n 朝方向一样的数字 9",
    ]
    for prompt in CASES:
        print("=" * 66)
        print("提示:", prompt)
        print("解析:", parse_prompt(prompt))
        r = solve(crop, prompt, debug_dir=OUT)
        for c in sorted(r.chars, key=lambda d: d.cx):
            aq = c.get("Q")
            details = " ".join(f"{k}:{v[0]:+d}/{v[1]:.2f}"
                               for k, v in sorted(c.rots.items()) if v[0] is not None)
            print(f"   {c.color}{c.text!r:<5} ({c.cx:5.0f},{c.cy:5.0f}) sat={c.saturation:5.0f}  {details}")
        print()
        print("  ok =", r.ok, " 坐标 =", f"({r.x:.0f}, {r.y:.0f})",
              f" 置信度 = {r.confidence:.2f}")
        print("  说明:", r.reason)
        print()
