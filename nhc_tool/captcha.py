"""验证码求解：点选文字。

策略：
  det 检测字符框 -> 多增强投票分类 -> 结合 wordList 约束做指派
  （精确匹配优先，其次用模板 IoU 兜底）

设计要点：验证码校验失败不消耗查询额度，因此上层可循环重试。
"""
import io
import os
import threading

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_ANG = (-15, -8, 0, 8, 15)
_PADS = (2, 5)
_SCALES = (0.92, 1.0, 1.08)
FONTS = ["Dengb.ttf", "msyhbd.ttc", "simhei.ttf"]

_lock = threading.Lock()
_det = None
_ocr = None
_tcache = {}
_TCACHE_MAX = 6000       # 模板缓存上限（约 24MB），超出整体清空
_ENGINE_ERR = None       # 模型不可用时的原因（如老 CPU 不支持 AVX）


def _ensure():
    """加载识别模型。返回 True 表示可用。

    官方 onnxruntime 预编译包假设 CPU 支持 AVX/AVX2；极老的 CPU（部分
    Atom/Celeron）会加载失败。这里捕获异常并降级为「只能手动输入验证码」，
    避免整个程序不可用。
    """
    global _det, _ocr, _ENGINE_ERR
    with _lock:
        if _det is not None:
            return True
        if _ENGINE_ERR:
            return False
        try:
            import ddddocr
            _det = ddddocr.DdddOcr(det=True, show_ad=False)
            _ocr = ddddocr.DdddOcr(show_ad=False)
            return True
        except Exception as e:
            _ENGINE_ERR = "%s: %s" % (type(e).__name__, str(e)[:140])
            return False


def engine_available() -> bool:
    """自动识别引擎是否可用（首次调用会加载模型）。"""
    return _ensure()


def engine_error():
    return _ENGINE_ERR


def _font(name):
    p = os.path.join("C:/Windows/Fonts", name)
    return p if os.path.exists(p) else None


def _render(ch, fp, size=64, angle=0.0):
    key = (ch, fp, size, angle)
    v = _tcache.get(key)
    if v is not None:
        return v
    img = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(fp, int(size * 0.88))
    bb = d.textbbox((0, 0), ch, font=f)
    d.text(((size - (bb[2] - bb[0])) / 2 - bb[0], (size - (bb[3] - bb[1])) / 2 - bb[1]),
           ch, font=f, fill=255)
    if angle:
        img = img.rotate(angle, expand=False, fillcolor=0, resample=Image.BICUBIC)
    a = (np.asarray(img, dtype=np.float32) / 255.0) > 0.5
    # 缓存上限：模板随时可重算，绝不能无上限增长
    # （每条约 4KB，6000 条 ≈ 24MB；不设上限时 1000 次识别会涨到 400MB+）
    if len(_tcache) >= _TCACHE_MAX:
        _tcache.clear()
    _tcache[key] = a
    return a


def _fg_mask(crop, size=64):
    a = np.asarray(crop.convert("RGB"), dtype=np.float32)
    border = np.concatenate([a[0, :, :], a[-1, :, :], a[:, 0, :], a[:, -1, :]])
    bg = np.median(border, axis=0)
    dist = np.linalg.norm(a - bg, axis=2)
    thr = max(30.0, float(dist.mean() + 0.8 * dist.std()))
    m = dist > thr
    ys, xs = np.where(m)
    if len(ys) == 0:
        return np.zeros((size, size), dtype=bool)
    m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray((m * 255).astype(np.uint8)).resize((size, size), Image.BILINEAR)
    return (np.asarray(im, dtype=np.float32) / 255.0) > 0.4


def _iou(mask, ch):
    best = 0.0
    for fn in FONTS:
        fp = _font(fn)
        if not fp:
            continue
        for a in range(-60, 61, 15):
            t = _render(ch, fp, angle=a)
            inter = np.logical_and(t, mask).sum()
            if inter == 0:
                continue
            u = np.logical_or(t, mask).sum()
            s = inter / u
            if s > best:
                best = s
    return best


def _crop_bytes(im, box, pad, angle, scale):
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w, h = (x2 - x1) + pad * 2, (y2 - y1) + pad * 2
    c = im.crop((int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2)))
    if scale != 1.0:
        c = c.resize((max(1, int(c.width * scale)), max(1, int(c.height * scale))), Image.LANCZOS)
    if angle:
        c = c.rotate(angle, expand=True, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    buf = io.BytesIO()
    c.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def detect(png_bytes):
    _ensure()
    out = []
    for x1, y1, x2, y2 in _det.detection(png_bytes):
        if x2 - x1 >= 16 and y2 - y1 >= 16:
            out.append((int(x1), int(y1), int(x2), int(y2)))
    return out


def solve_local(png_bytes, wordlist, use_iou=True):
    """返回 (points, info)。points 为按 wordlist 顺序的坐标列表。"""
    if not _ensure():
        return None, {"err": "engine_unavailable", "detail": _ENGINE_ERR}
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    boxes = detect(png_bytes)
    if not boxes:
        return None, {"err": "no_box"}
    info = []
    for b in boxes:
        votes = {}
        for a in _ANG:
            for p in _PADS:
                for s in _SCALES:
                    try:
                        r = _ocr.classification(_crop_bytes(im, b, p, a, s))
                    except Exception:
                        continue
                    if r:
                        votes[r] = votes.get(r, 0) + 1
        pred, conf = ("", 0.0)
        if votes:
            pred, n = max(votes.items(), key=lambda kv: kv[1])
            conf = n / sum(votes.values())
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        info.append({"box": b, "pred": pred, "conf": conf, "pt": {"x": cx, "y": cy}})

    masks = None
    if use_iou:
        masks = [_fg_mask(im.crop((b[0] - 6, b[1] - 6, b[2] + 6, b[3] + 6))) for b in boxes]

    used = set()
    pts, detail = [], []
    for t in wordlist:
        pick, why = None, ""
        # 1) 精确匹配
        cand = [(i, info[i]["conf"]) for i in range(len(boxes))
                if i not in used and info[i]["pred"] == t]
        if cand:
            pick = max(cand, key=lambda x: x[1])[0]
            why = "exact"
        elif use_iou and masks:
            # 2) 模板 IoU 兜底
            sc = [(_iou(masks[i], t), i) for i in range(len(boxes)) if i not in used]
            if sc:
                sc.sort(reverse=True)
                pick = sc[0][1]
                why = "iou%.2f" % sc[0][0]
        if pick is None:
            rest = [i for i in range(len(boxes)) if i not in used]
            if not rest:
                return None, info
            pick = rest[0]
            why = "fallback"
        used.add(pick)
        pts.append(info[pick]["pt"])
        detail.append({"target": t, "pred": info[pick]["pred"], "why": why,
                       "box": info[pick]["box"]})
    return pts, {"boxes": info, "detail": detail}


# ---------------- 统一入口（含人工兜底） ----------------
MANUAL_HOOK = None   # 由 GUI 注入: fn(png_bytes, wordlist) -> points | None


def solve(png_bytes, wordlist, *, use_iou=True, allow_manual=True):
    """先自动识别；识别不出来且允许时，调用人工兜底。

    返回 (points, source)，source ∈ {'auto','manual',None}

    若本机 CPU 不支持自动识别引擎（onnxruntime 需要 AVX），会自动跳过自动
    识别、直接走人工输入，保证程序仍然可用。
    """
    if _ensure():
        pts, info = solve_local(png_bytes, wordlist, use_iou=use_iou)
        if pts and _plausible(pts, info):
            return pts, "auto"
    else:
        pts = None
    if allow_manual and MANUAL_HOOK is not None:
        try:
            pts2 = MANUAL_HOOK(png_bytes, wordlist)
        except Exception:
            pts2 = None
        if pts2:
            return pts2, "manual"
    return pts, "auto" if pts else None


def _plausible(pts, info):
    """粗略判断自动结果是否可信：所有点都落在框内且互不重合。"""
    if not pts:
        return False
    if len(pts) != len(set((p["x"], p["y"]) for p in pts)):
        return False
    for p in pts:
        if not (0 <= p["x"] <= 320 and 0 <= p["y"] <= 170):
            return False
    return True

