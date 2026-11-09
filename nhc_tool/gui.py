"""图形界面 —— 仿官网极简交互，Sun Valley (Fluent) 主题。

结构：
    查询卡片（省份 + 机构名称 + 查询）
    工具条（批量 / 清空 / 导出 / 设置 / 主题）
    结果表（文本表，可拖动选中复制；「详细」为蓝色链接，点击看机构详情）
    模态实时进度窗（查询期间锁住主窗口，避免并发误操作）
"""
import base64
import csv
import io
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
import zlib
from tkinter import filedialog, messagebox, ttk

from . import captcha as CAP
from . import config
from .engine import Engine, State, Task, fetch_unit_detail_fresh
from .proxy_pool import ProxyPool, load_token

PROVINCES = [
    ("11", "北京市"), ("12", "天津市"), ("13", "河北省"), ("14", "山西省"), ("15", "内蒙古自治区"),
    ("21", "辽宁省"), ("22", "吉林省"), ("23", "黑龙江省"), ("31", "上海市"), ("32", "江苏省"),
    ("33", "浙江省"), ("34", "安徽省"), ("35", "福建省"), ("36", "江西省"), ("37", "山东省"),
    ("41", "河南省"), ("42", "湖北省"), ("43", "湖南省"), ("44", "广东省"), ("45", "广西壮族自治区"),
    ("46", "海南省"), ("50", "重庆市"), ("51", "四川省"), ("52", "贵州省"), ("53", "云南省"),
    ("54", "西藏自治区"), ("61", "陕西省"), ("62", "甘肃省"), ("63", "青海省"),
    ("64", "宁夏回族自治区"), ("65", "新疆维吾尔自治区"),
]
PROV_LABELS = ["%s %s" % (c, n) for c, n in PROVINCES]
DEFAULT_PROV = "35 福建省"

# ---- 详情字段（标签与可见性随 unitType 变化，逆向自官网前端 index-52f8b012.js）----
# 官网详情页的条件规则（原文逻辑）：
#   unitType 为空或 "0"  → 审批类：显示「审批机关 / 登记号 / 级别 / 执业许可证有效期」
#   unitType 非空且≠"0"  → 备案类：显示「备案机关 / 备案编号 / 所有制形式 / 经营性质 / 备案日期」
#   unitType == "2"      → 标签用「诊疗范围」（否则「诊疗科目」）
#   unitType == "4"      → 额外显示「服务方式」
#   有效期 与 备案日期 互斥（官网用 `||` 二选一）
DETAIL_ALL_KEYS = ("province", "unitOrganName", "unitRegCode", "unitAddr",
                   "unitSubjectName", "unitLevelName", "unitHavingName",
                   "unitNatureBusiName", "unitServiceType", "unitLegalName",
                   "unitChargeName", "unitDateValidBegin", "unitDateValidEnd",
                   "unitPassDate")

# 官网只对这三个日期字段做 yyyy/MM/dd 规范化，其余字段一律原样显示
DATE_FIELDS = ("unitDateValidBegin", "unitDateValidEnd", "unitPassDate")

# 字段中文名（用于「官网未展示」区，避免出现英文原名）
KEY_LABELS = {
    "province": "省份", "unitOrganName": "机关", "unitRegCode": "编号",
    "unitAddr": "地址", "unitLevelName": "级别", "unitSubjectName": "科目/范围",
    "unitLegalName": "法定代表人", "unitChargeName": "主要负责人",
    "unitHavingName": "所有制形式", "unitNatureBusiName": "经营性质",
    "unitServiceType": "服务方式", "unitDateValidBegin": "有效期起",
    "unitDateValidEnd": "有效期止", "unitPassDate": "备案日期",
}


def is_filing(unit_type):
    """是否备案类机构（诊所备案等）：unitType 非空且不是 "0"。"""
    return bool(unit_type) and str(unit_type) != "0"


def detail_groups(unit_type):
    """按 unitType 生成与官网详情页**一致的字段与标签**。

    返回的每个分组里的字段就是官网在该 unitType 下会显示的行
    （官网对空值字段同样显示标签，所以这里不做空值过滤）。
    """
    filing = is_filing(unit_type)
    sub_label = "诊疗范围" if str(unit_type) == "2" else "诊疗科目"
    basic = [("province", "省份"),
             ("unitOrganName", "备案机关" if filing else "审批机关"),
             ("unitRegCode", "备案编号" if filing else "登记号"),
             ("unitAddr", "地址")]
    groups = [("基本信息", basic)]
    if filing:
        # 官网：备案类显示 所有制形式 / 经营性质，仅 unitType=4 才显示 服务方式
        rows = [("unitHavingName", "所有制形式"), ("unitNatureBusiName", "经营性质")]
        if str(unit_type) == "4":
            rows.append(("unitServiceType", "服务方式"))
        groups.append(("经营信息", rows))
    else:
        # 官网：级别仅审批类显示
        basic.append(("unitLevelName", "级别"))
    groups.append(("负责人", [("unitLegalName", "法定代表人"),
                            ("unitChargeName", "主要负责人")]))
    if filing:
        groups.append(("备案信息", [("unitPassDate", "备案日期")]))
    else:
        groups.append(("执业许可", [("unitDateValidBegin", "有效期起"),
                                  ("unitDateValidEnd", "有效期止")]))
    groups.append((sub_label, [("unitSubjectName", sub_label)]))
    return groups


# 导出用的详情列：标签写成"两种口径"，避免备案类被误标为登记号/诊疗科目
EXPORT_DETAIL_COLS = [
    ("unitAddr", "地址"), ("unitRegCode", "登记号/备案编号"),
    ("unitSubjectName", "诊疗科目/诊疗范围"),
    ("unitLegalName", "法定代表人"), ("unitHavingName", "所有制形式"),
    ("unitNatureBusiName", "经营性质"),
]

# 结果表列定义： (key, 表头, 像素宽)
TABLE_COLS = [
    ("province", "省份", 110),
    ("unitName", "机构名称", 420),
    ("unitOrganName", "审批机关", 280),
    ("levelName", "级别", 90),
    ("act", "操作", 80),
]

PALETTE = {
    "light": {"bg": "#f3f3f3", "card": "#ffffff", "line": "#e5e5e5", "text": "#1c1c1c",
              "sub": "#616161", "accent": "#0067c0", "row2": "#f5f8fc",
              "sel": "#bcdcf7", "selfg": "#0a2b45", "ok": "#0f7b0f"},
    "dark": {"bg": "#202020", "card": "#2b2b2b", "line": "#3a3a3a", "text": "#ffffff",
             "sub": "#a0a0a0", "accent": "#4cc2ff", "row2": "#262626",
             "sel": "#2f5d7d", "selfg": "#ffffff", "ok": "#6ccb5f"},
}


def _fmt(v, key=""):
    """字段取值显示。

    官网只对三个日期字段做 yyyy/MM/dd 规范化，其余字段一律原样显示。
    所以这里也只对日期字段处理 13 位毫秒时间戳 —— 否则 13 位的
    登记号 / 备案编号 之类会被误转成日期（原实现的隐患）。
    """
    if v is None or v == "":
        return "—"
    s = str(v).strip()
    if key in DATE_FIELDS and s.isdigit() and len(s) == 13:
        try:
            return time.strftime("%Y/%m/%d", time.localtime(int(s) / 1000))
        except Exception:
            return s
    return s


def _cell(v, key=""):
    """导出用取值：空值留空。

    界面里空值显示为「—」便于阅读，但导出到 Excel 时留空更忠实于官网
    （官网空字段就是空），也便于筛选 / 排序 / 比对。
    """
    if v is None or v == "":
        return ""
    return _fmt(v, key)


# 诊疗科目配色：同一科室在任何机构里都用同一个颜色（crc32 取模，跨进程稳定），
# 让「内科 / 外科 / 检验科」在不同记录间形成固定的视觉锚点。
SUBJECT_COLORS = {
    "light": ("#0b63b8", "#0d7a6a", "#7a3ea8", "#a0521a", "#1f6f3d", "#8a1f4b"),
    "dark": ("#7bb8ff", "#54d2b4", "#c9a6ff", "#ffb473", "#8be0a8", "#ff9fc6"),
}

# 科室语义图标：按关键词匹配（**顺序有意义**，"小儿/妇女保健"等特例要排在前面）。
# 只选 Unicode 6.0 / BMP 时代的老字符 —— Win7 的 Segoe UI Symbol 才有字形，
# 新 emoji（🫀🦷🩺🧠…）在 Win7 上会显示成方块。
# 图标统一用中性灰（不跟随科室颜色），想换某个科直接改这张表。
SUBJECT_ICONS = (
    ("中西医结合", "☯"),
    ("中医", "🌿"),
    ("妇女保健", "♀"),
    ("妇产", "♀"),
    ("小儿", "👶"),
    ("儿童保健", "🍼"),
    ("儿科", "👶"),
    ("预防", "💉"),
    ("全科", "🏥"),
    ("内科", "❤"),
    ("外科", "✚"),
    ("眼科", "👓"),
    ("耳鼻咽喉", "👂"),
    ("耳鼻喉", "👂"),
    ("口腔", "👄"),
    ("皮肤", "✨"),
    ("美容", "💅"),
    ("心理", "💬"),
    ("结核", "☣"),
    ("传染", "😷"),
    ("地方病", "🌍"),
    ("肿瘤", "💊"),
    ("急诊", "🚑"),
    ("康复", "💆"),
    ("职业", "🏭"),
    ("临终", "💤"),
    ("麻醉", "💤"),
    ("疼痛", "⚡"),
    ("重症", "💉"),
    ("检验", "🔬"),
    ("病理", "🔍"),
    ("影像", "📷"),
    ("放射", "📷"),
)
SUBJECT_ICON_DEFAULT = "🏥"


def subject_icon(name):
    """按科室名挑一个语义相近的图标；匹配不到就用医院图标兜底。"""
    s = name or ""
    for key, icon in SUBJECT_ICONS:
        if key in s:
            return icon
    return SUBJECT_ICON_DEFAULT


# 彩色图标缓存：key = (Tk 解释器, 字符, 像素尺寸)
_ICON_CACHE = {}
_ICON_CROSS_COLOR = (211, 47, 47)     # 外科的红十字


def _emoji_font_path():
    """彩色 emoji 字体（Win8.1+ 自带）。Win7 只有单色 Segoe UI Symbol，返回 None。"""
    root = os.environ.get("WINDIR") or r"C:\Windows"
    p = os.path.join(root, "Fonts", "seguiemj.ttf")
    return p if os.path.exists(p) else None


def _draw_cross(px, color=_ICON_CROSS_COLOR):
    """外科没有合适的 emoji（✚ 不是彩色字符），直接画一个红十字。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    arm = max(4, int(round(px * 0.32)))
    th = max(3, int(round(px * 0.32)))
    c = px // 2
    r = max(1, th // 3)
    d.rounded_rectangle([c - th / 2.0, c - arm, c + th / 2.0, c + arm],
                        radius=r, fill=color + (255,))
    d.rounded_rectangle([c - arm, c - th / 2.0, c + arm, c + th / 2.0],
                        radius=r, fill=color + (255,))
    return img


def _subject_icon_photo(master, ch, px):
    """把 emoji 渲染成彩色 PNG（Tk 8.6 的文字控件不支持彩色 emoji）。

    返回 PhotoImage；环境不支持彩色渲染（Win7 / Pillow 异常）时返回 None，
    调用方退回单色文字图标。
    """
    try:
        key = (master.tk.interpaddr(), ch, px)
    except Exception:
        key = (id(master.tk), ch, px)
    hit = _ICON_CACHE.get(key)
    if hit is not None:
        try:
            hit.width()          # 解释器重开后旧 PhotoImage 会失效，别复用它
            return hit
        except Exception:
            _ICON_CACHE.pop(key, None)
    try:
        from PIL import Image, ImageDraw, ImageFont
        if ch == "✚":
            img = _draw_cross(px)
        else:
            fp = _emoji_font_path()
            if not fp:
                return None
            font = ImageFont.truetype(fp, int(px))
            img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            bb = d.textbbox((0, 0), ch, font=font, embedded_color=True)
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if w <= 0 or h <= 0:
                return None
            d.text(((px - w) / 2.0 - bb[0], (px - h) / 2.0 - bb[1]), ch,
                   font=font, embedded_color=True)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        photo = tk.PhotoImage(
            master=master,
            data=base64.b64encode(buf.getvalue()).decode("ascii"))
        _ICON_CACHE[key] = photo
        return photo
    except Exception:
        return None


def _is_blank(v):
    return str(v if v is not None else "").strip() in ("", "—", "-", "/", "无")


def _split_outside_brackets(s, seps):
    """按 seps 拆分，但括号内的分隔符不拆（"内科（A、B）" 保持完整）。"""
    parts, buf, depth = [], [], 0
    for ch in s:
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth = max(0, depth - 1)
        if depth == 0 and ch in seps:
            p = "".join(buf).strip()
            if p:
                parts.append(p)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def split_subjects(text):
    """把官网的诊疗科目串拆成 [(科室名, [子专业...]), ...]。

    官网实际格式（实测）：
        预防保健科 | 内科;呼吸内科专业;消化内科专业 | 眼科 | ... ******
    即「科室之间用 | 分隔，科室内用 ; 分隔主科与子专业」，末尾带一串 *。
    也兼容「预防保健科、全科医疗科」这种纯顿号写法，以及括号 / 冒号写法。

    纯展示用：复制与导出仍保留官网原文，不做任何改写。
    """
    s = "" if text is None else str(text).strip()
    if not s or s in ("—", "-", "/", "无"):
        return []
    s = re.sub(r"\*{2,}\s*$", "", s).strip()     # 去掉末尾的 ******

    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
    elif re.search(r"[、，,\n\r]", s):
        parts = _split_outside_brackets(s, "、，,\n\r")
    else:
        parts = [s]

    # 「内科：A、B、外科：C」这种冒号写法：冒号后直到下一个冒号前的都算子专业
    if "|" not in s and any("：" in p for p in parts):
        merged = []
        for p in parts:
            if "：" in p:
                a, b = p.split("：", 1)
                row = [a.strip()]
                if b.strip():
                    row.append(b.strip())
                merged.append(row)
            elif merged:
                merged[-1].append(p)
            else:
                merged.append([p])
        parts = [";".join(x) for x in merged]

    out = []
    for p in parts:
        p = p.strip().strip("*").strip()
        if not p:
            continue
        # 段内：第一个 ; 之前是科室名，之后都是它的子专业
        segs = [x.strip() for x in re.split(r"[；;]", p) if x.strip()]
        if not segs:
            continue
        name, subs = segs[0], segs[1:]

        if not subs:
            m = re.match(r"^(.*?)[（(](.+)[)）]\s*$", name)
            if m and m.group(1).strip():
                name = m.group(1).strip()
                subs = [x.strip() for x in re.split(r"[、，,；;]", m.group(2))
                        if x.strip()]
        if not subs and "：" in name:
            a, b = name.split("：", 1)
            if a.strip() and b.strip():
                name = a.strip()
                subs = [x.strip() for x in re.split(r"[、，,；;]", b) if x.strip()]
        out.append((name, subs))
    return out


def make_readonly(txt: tk.Text):
    """只读但保留鼠标选中 / Ctrl+C / Ctrl+A / Ctrl+X 与方向键导航。

    除导航键与这三个 Ctrl 组合外，其余按键一律拦截（含退格、删除、粘贴），
    避免只读内容被误改。
    """
    nav = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
           "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
           "Escape", "Tab", "ISO_Left_Tab", "Caps_Lock", "Num_Lock"}

    def on_key(e):
        if e.state & 0x4:
            return None if e.keysym.lower() in ("c", "a", "x", "insert") else "break"
        return None if e.keysym in nav else "break"

    txt.bind("<Key>", on_key)
    txt.bind("<<Paste>>", lambda e: "break")
    txt.bind("<<Cut>>", lambda e: "break")
    txt.bind("<Button-2>", lambda e: "break")


def add_copy_menu(widget: tk.Text, parent):
    """右键菜单：复制 / 全选 / 复制全部。"""
    m = tk.Menu(widget, tearoff=0)

    def copy_sel():
        try:
            widget.event_generate("<<Copy>>")
        except Exception:
            pass

    def copy_all():
        try:
            parent.clipboard_clear()
            parent.clipboard_append(widget.get("1.0", "end-1c"))
        except Exception:
            pass

    m.add_command(label="复制选中", command=copy_sel)
    m.add_command(label="复制全部", command=copy_all)
    m.add_separator()
    m.add_command(label="全选", command=lambda: (widget.tag_add("sel", "1.0", "end-1c"),
                                                 "break"))
    widget.bind("<Button-3>", lambda e: m.tk_popup(e.x_root, e.y_root))
    return m


def center_window(win, parent=None, size=None):
    """把窗口居中到父窗口（无父窗口则居中到屏幕）。

    注意：Tk 的 geometry("+X+Y") 设的是「窗口外框」坐标，而 winfo_rootx/y
    返回的是「客户区」坐标，两者相差标题栏与边框的高度/宽度。所以先按外框
    定位一次，等窗口真正映射后再量出装饰偏移并补偿，做到视觉上精确居中。
    """
    try:
        win.update_idletasks()
        if size:
            w, h = size
        else:
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        if parent is not None and parent.winfo_viewable():
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            if pw <= 1 or ph <= 1:              # 父窗口还没完成布局
                px, py, pw, ph = 0, 0, sw, sh
        else:
            px, py = 0, 0
            pw, ph = sw, sh
        tx = max(0, min(px + (pw - w) // 2, sw - w))
        ty = max(0, min(py + (ph - h) // 2, sh - h))
        win.geometry("%dx%d+%d+%d" % (w, h, tx, ty))

        def compensate(tries=10):
            try:
                if not win.winfo_ismapped() and tries > 0:
                    win.after(20, lambda: compensate(tries - 1))
                    return
                dx = win.winfo_rootx() - tx
                dy = win.winfo_rooty() - ty
                if dx or dy:
                    win.geometry("%dx%d+%d+%d"
                                 % (w, h, max(0, tx - dx), max(0, ty - dy)))
            except Exception:
                pass

        win.after(25, compensate)
        win.after(170, compensate)      # 兜底再来一次，防窗口管理器后置调整
    except Exception:
        pass


# ================= 界面状态记忆 =================
_UI_FILE = None


def _ui_path():
    global _UI_FILE
    if _UI_FILE is None:
        _UI_FILE = os.path.join(config.data_dir(), "ui.json")
    return _UI_FILE


def load_ui_state():
    try:
        with open(_ui_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_ui_state(d):
    try:
        p = _ui_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        pass


_GEO_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


def valid_geometry(geo, minsize, screen):
    """校验保存的窗口几何。窗口跑到屏幕外、或尺寸过小则返回 None（改用居中）。"""
    m = _GEO_RE.match(str(geo or ""))
    if not m:
        return None
    w, h, x, y = (int(m.group(i)) for i in range(1, 5))
    sw, sh = screen
    if w < minsize[0] or h < minsize[1]:
        return None
    if x > sw - 120 or y > sh - 80 or x + w < 120 or y + h < 80:
        return None
    x = max(0, min(x, max(0, sw - w)))
    y = max(0, min(y, max(0, sh - h)))
    return "%dx%d+%d+%d" % (w, h, x, y)


# ---------- 弹窗位置记忆 ----------
# 规则：某个弹窗第一次出现时居中；之后每次出现在上次关闭时的位置
#（详情窗允许缩放，所以尺寸也一起记住）。
def window_geometry(key):
    try:
        wins = load_ui_state().get("windows")
        if isinstance(wins, dict):
            v = wins.get(key)
            return v if isinstance(v, str) else None
    except Exception:
        pass
    return None


def remember_window(key, geo):
    """写回 ui.json 里该弹窗的几何。

    每次重新读盘再写，避免与主窗口 _save_ui 的缓存互相覆盖。
    """
    try:
        d = load_ui_state()
        wins = d.get("windows")
        if not isinstance(wins, dict):
            wins = {}
        wins[key] = geo
        d["windows"] = wins
        save_ui_state(d)
    except Exception:
        pass


_POS_RE = re.compile(r"^([+-]\d+)([+-]\d+)$")


def _bad_geo(geo):
    """+0+0 视为无效：多为窗口映射前就被记下的默认位置。"""
    g = str(geo or "")
    m = _GEO_RE.match(g)
    if m:
        return int(m.group(3)) == 0 and int(m.group(4)) == 0
    m = _POS_RE.match(g)
    return bool(m and int(m.group(1)) == 0 and int(m.group(2)) == 0)


def place_memo_window(win, key, parent=None, size=None, keep_size=True,
                      minsize=(200, 120)):
    """第一次居中，之后回到上次位置（keep_size=False 时只记位置不记尺寸）。"""
    try:
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        saved = window_geometry(key)
        if saved and _bad_geo(saved):
            saved = None            # 历史脏数据（+0+0），当作没有记录
        geo = valid_geometry(saved, minsize, (sw, sh))
        if not geo and saved:
            # 只存了位置（尺寸固定的弹窗）：用当前自然尺寸补全
            m = _POS_RE.match(saved)
            if m:
                w, h = win.winfo_reqwidth(), win.winfo_reqheight()
                x, y = int(m.group(1)), int(m.group(2))
                x = max(0, min(x, max(0, sw - w)))
                y = max(0, min(y, max(0, sh - h)))
                geo = "%dx%d+%d+%d" % (w, h, x, y)
        if geo:
            if not keep_size:
                m = _GEO_RE.match(geo)
                if m:
                    geo = "%s%s" % (m.group(3), m.group(4))
            win.geometry(geo)
            return True
    except Exception:
        pass
    center_window(win, parent, size=size)
    return False


def place_inside_remembered(win, key, parent=None):
    """把一个小窗摆在某个已记忆窗口的中心（没有记录时退回居中）。

    详情加载窗用：让它从「详情窗将要出现的位置」冒出，避免先闪一下屏幕中央。
    """
    try:
        win.update_idletasks()
        m = _GEO_RE.match(window_geometry(key) or "")
        if m:
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x = int(m.group(3)) + (int(m.group(1)) - w) // 2
            y = int(m.group(4)) + (int(m.group(2)) - h) // 2
            x = max(0, min(x, max(0, sw - w)))
            y = max(0, min(y, max(0, sh - h)))
            win.geometry("%dx%d+%d+%d" % (w, h, x, y))
            return True
    except Exception:
        pass
    center_window(win, parent)
    return False


class MemoWindow(tk.Toplevel):
    """关闭时把位置（可含尺寸）写回 ui.json 的 Toplevel。"""

    geo_key = None
    geo_keep_size = True

    def _save_geo(self):
        if not self.geo_key:
            return
        try:
            st = self.state()
            if st in ("iconic", "withdrawn") or not self.winfo_ismapped():
                return                     # 最小化/还没显示出来，不覆盖上次的位置
            g = self.winfo_geometry()
            if _bad_geo(g):
                return
            if not self.geo_keep_size:
                m = _GEO_RE.match(g)
                if not m:
                    return
                g = "%s%s" % (m.group(3), m.group(4))
            remember_window(self.geo_key, g)
        except Exception:
            pass

    def destroy(self):
        self._save_geo()
        super().destroy()


# ================= 人工验证码 =================
class CaptchaDialog(MemoWindow):
    geo_key = "captcha"
    geo_keep_size = False

    def __init__(self, master, png_bytes, wordlist):
        super().__init__(master)
        self.title("验证码")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self.points = []
        self.wordlist = list(wordlist)

        from PIL import Image, ImageTk
        self._pil = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        wrap = ttk.Frame(self, style="Card.TFrame", padding=16)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="请在图片上按顺序点击：", style="Sub.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="【 %s 】" % "  ".join(self.wordlist),
                  style="AccentBig.TLabel").pack(anchor="w", pady=(2, 10))

        self.canvas = tk.Canvas(wrap, width=self._pil.width, height=self._pil.height,
                                highlightthickness=1, highlightbackground="#c8c8c8",
                                bg="#ffffff")
        self.canvas.pack()
        self.photo = ImageTk.PhotoImage(self._pil)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self._click)

        self.lbl = ttk.Label(wrap, text="已点击 0 / %d" % len(self.wordlist), style="Sub.TLabel")
        self.lbl.pack(anchor="w", pady=(8, 0))

        bar = ttk.Frame(wrap, style="Card.TFrame")
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="重来", command=self._reset).pack(side="left")
        ttk.Button(bar, text="放弃本次", command=self._cancel).pack(side="left", padx=8)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        place_memo_window(self, self.geo_key, master)
        self.grab_set()
        self.lift()
        self.focus_force()

    def _click(self, ev):
        if len(self.points) >= len(self.wordlist):
            return
        x, y = int(ev.x), int(ev.y)
        self.points.append({"x": x, "y": y})
        n = len(self.points)
        self.canvas.create_oval(x - 13, y - 13, x + 13, y + 13, outline="#0f7b0f", width=2)
        self.canvas.create_text(x, y, text=str(n), fill="#0f7b0f",
                                font=("Microsoft YaHei", 10, "bold"))
        self.lbl.config(text="已点击 %d / %d" % (n, len(self.wordlist)))
        if n == len(self.wordlist):
            self.after(150, self._ok)

    def _reset(self):
        self.points = []
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.lbl.config(text="已点击 0 / %d" % len(self.wordlist))

    def _ok(self):
        self.result = self.points
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ================= 查询中的窗内覆盖层 =================
class BusyOverlay:
    """盖在主窗口内容区的查询进度层（替代原来的独立模态进度窗）。

    * 不创建 Toplevel、不 grab、不抢任务栏 —— 不会出现"最小化后找不到"的问题
    * 只用 Frame.place，随主窗口缩放；Win7 上也不依赖任何透明/合成特性
    * 转圈用 Canvas 画弧线旋转，批量时进度条显示真实百分比
    """

    def __init__(self, app, total, on_stop, title="正在查询…"):
        self.app = app
        self.total = max(1, int(total))
        self._on_stop = on_stop
        self._closed = False
        self._angle = 0
        self._job = None
        pal = app._pal

        try:
            head_h = app.head.winfo_height()
        except Exception:
            head_h = 0

        # 盖住标题栏以下的所有内容（标题与「访问官网」保持可见）
        self.frame = tk.Frame(app, bg=pal["bg"], takefocus=1)
        self.frame.place(x=0, y=head_h, relwidth=1, relheight=1, height=-head_h)
        self.frame.lift()
        self.frame.bind("<Key>", lambda e: "break")   # 吞掉键盘，避免误改输入框

        # 用 tk.Frame 自绘卡片：边框、底色完全可控，不依赖 ttk 主题的边框细节
        card = tk.Frame(self.frame, bg=pal["card"],
                        highlightbackground=pal["line"], highlightthickness=1)
        card.place(relx=0.5, rely=0.46, anchor="center")
        inner = tk.Frame(card, bg=pal["card"], padx=26, pady=22)
        inner.pack()

        top = tk.Frame(inner, bg=pal["card"])
        top.pack(fill="x")
        self.cv = tk.Canvas(top, width=42, height=42, bg=pal["card"],
                            highlightthickness=0, takefocus=0)
        self.cv.pack(side="left", padx=(0, 16))
        self.arc = self.cv.create_arc(5, 5, 37, 37, start=0, extent=270,
                                      style="arc", width=4, outline=pal["accent"])

        txt = tk.Frame(top, bg=pal["card"])
        txt.pack(side="left", fill="x", expand=True)
        self.lbl_title = tk.Label(txt, text=title, bg=pal["card"], fg=pal["text"],
                                  font=("Microsoft YaHei UI", 13, "bold"), anchor="w")
        self.lbl_title.pack(anchor="w")
        self.lbl_act = tk.Label(txt, text="0 / %d · 准备中…" % self.total,
                                bg=pal["card"], fg=pal["sub"],
                                font=("Microsoft YaHei UI", 9), anchor="w")
        self.lbl_act.pack(anchor="w", pady=(3, 0))

        try:
            ttk.Style(self.app).configure("Busy.Horizontal.TProgressbar", thickness=6)
        except Exception:
            pass
        self.bar = ttk.Progressbar(
            inner, style="Busy.Horizontal.TProgressbar",
            mode="indeterminate" if self.total <= 1 else "determinate",
            maximum=self.total, length=430)
        self.bar.pack(fill="x", pady=(18, 12))
        if self.total <= 1:
            self.bar.start(12)

        row = tk.Frame(inner, bg=pal["card"])
        row.pack(fill="x")
        self.lbl_stat = tk.Label(row, text="成功 0    无结果 0    名称不合规 0    失败 0",
                                 bg=pal["card"], fg=pal["sub"],
                                 font=("Microsoft YaHei UI", 9), anchor="w")
        self.lbl_stat.pack(side="left")
        self.btn_stop = ttk.Button(row, text="停 止", command=self._stop)
        self.btn_stop.pack(side="right")

        self._tick()
        self.frame.focus_set()

    # ---- 动画 ----
    def _tick(self):
        if self._closed:
            return
        try:
            self._angle = (self._angle - 24) % 360
            self.cv.itemconfigure(self.arc, start=self._angle)
            self._job = self.frame.after(70, self._tick)
        except Exception:
            pass

    # ---- 状态 ----
    def _stop(self):
        try:
            self.btn_stop.config(state="disabled", text="正在停止…")
            self.lbl_title.config(text="正在停止…")
        except Exception:
            pass
        try:
            self._on_stop()
        except Exception:
            pass

    def update_state(self, done, total, activity, stats=None):
        if self._closed:
            return
        try:
            self.total = max(1, int(total))
            if self.total > 1:
                self.bar.config(maximum=self.total, value=done)
                head = "%d / %d（%d%%）" % (done, self.total,
                                           int(done * 100 / self.total))
            else:
                head = "正在处理…" if done < 1 else "即将完成…"
            if activity:
                head += " · 当前：%s" % activity
            self.lbl_act.config(text=head)
            if stats:
                self.lbl_stat.config(
                    text="成功 %d    无结果 %d    名称不合规 %d    失败 %d"
                         % (stats.get("ok", 0), stats.get("empty", 0),
                            stats.get("invalid", 0), stats.get("error", 0)))
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._job is not None:
            try:
                self.frame.after_cancel(self._job)
            except Exception:
                pass
        try:
            self.bar.stop()
        except Exception:
            pass
        try:
            self.frame.destroy()
        except Exception:
            pass
        try:
            self.app.ent.focus_set()
        except Exception:
            pass


# ================= 机构详情（独立窗口 · 可复制） =================
class DetailDialog(MemoWindow):
    """独立的机构详情窗口。

    刻意**不设 transient、不 grab_set**：
      * transient 子窗不会出现在任务栏，主窗口最小化时会被一起隐藏，
        恢复后可能既看不见、又仍握着 grab —— 整个程序就像卡死一样；
      * 独立窗口有自己的最小化 / 最大化 / 关闭按钮，可以和主窗口各管各的，
        也便于同时开两个机构的详情做对照。
    """

    geo_key = "detail"
    geo_keep_size = True

    def __init__(self, master, name, detail, cascade=0):
        super().__init__(master)
        self._detail = detail or {}
        self._master = master
        self._base_title = name or self._detail.get("unitName", "") or "机构详情"
        short = self._base_title if len(self._base_title) <= 28 \
            else self._base_title[:27] + "…"
        self.title("机构详情 - %s" % short)
        self.minsize(620, 460)

        pal = self._pal()
        colors = SUBJECT_COLORS[self._theme_name()]

        # ---- 顶部：机构名称（可选中 / Ctrl+C / 右键 / 一键复制）----
        head = ttk.Frame(self, style="Card.TFrame", padding=(20, 16, 20, 10))
        head.pack(fill="x")
        row = ttk.Frame(head, style="Card.TFrame")
        row.pack(fill="x")
        self._head, self._row = head, row

        self.name_txt = tk.Text(row, wrap="word", relief="flat", borderwidth=0,
                                height=1, font=("Microsoft YaHei UI", 15, "bold"),
                                cursor="xterm", bg=pal["card"], fg=pal["text"],
                                padx=0, pady=0, spacing1=0, spacing3=0,
                                highlightthickness=0, insertwidth=0,
                                selectbackground=pal["sel"],
                                selectforeground=pal["selfg"])
        self.name_txt.insert("1.0", self._base_title)
        make_readonly(self.name_txt)
        self._build_name_menu()

        # 先 pack 右侧按钮：否则先 pack 的 expand 文本会把它挤成 0 宽
        self.btn_name = ttk.Label(row, text="复制名称", style="Link.TLabel",
                                  cursor="hand2")
        self.btn_name.pack(side="right", padx=(12, 0), anchor="n")
        self.btn_name.bind("<Button-1>", lambda e: self._copy_name())
        self.name_txt.pack(side="left", fill="x", expand=True)

        ttk.Separator(self).pack(fill="x")
        self.name_txt.bind("<Configure>", self._fit_name_height)
        self.after_idle(self._fit_name_height)

        # ---- 正文 ----
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.txt = tk.Text(outer, wrap="word", relief="flat", borderwidth=0,
                           font=("Microsoft YaHei UI", 10), cursor="xterm",
                           bg=pal["card"], fg=pal["text"],
                           padx=18, pady=12, spacing1=2, spacing3=4,
                           tabs=(96,), selectbackground=pal["sel"],
                           selectforeground=pal["selfg"], insertwidth=0,
                           highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        # 彩色图标按屏幕 DPI 生成，125%/150% 缩放下不会显小
        try:
            self._icon_px = max(14, int(round(18 * float(self.winfo_fpixels("1i")) / 96.0)))
        except Exception:
            self._icon_px = 18

        self.txt.tag_configure("grp", font=("Microsoft YaHei UI", 10, "bold"),
                               foreground=pal["accent"], spacing1=6, spacing3=4)
        self.txt.tag_configure("lbl", font=("Microsoft YaHei UI", 10),
                               foreground=pal["sub"])
        self.txt.tag_configure("val", font=("Microsoft YaHei UI", 10),
                               lmargin1=0, lmargin2=96)
        self.txt.tag_configure("sep", foreground=pal["line"])
        # 诊疗科目：每个科一行，彩色色块 + 加粗放大的科名，子专业缩进灰字
        for i, c in enumerate(colors):
            self.txt.tag_configure("sc%d" % i, foreground=c)
        self.txt.tag_configure("sname", font=("Microsoft YaHei UI", 11, "bold"),
                               spacing1=6, spacing3=1)
        self.txt.tag_configure("ssub", font=("Microsoft YaHei UI", 9),
                               foreground=pal["sub"], lmargin1=26, lmargin2=26,
                               spacing3=6)
        self.txt.tag_configure("scount", font=("Microsoft YaHei UI", 9),
                               foreground=pal["sub"], spacing3=2)
        # 科室图标：比科名大 1pt 更清楚，但保持中性灰、不跟随科室颜色
        self.txt.tag_configure("sicon", font=("Microsoft YaHei UI", 12),
                               foreground=pal["sub"])

        # 分栏分割线用内嵌 Frame 实现（Text 里没法画真正的横线）：
        # 记下所有 Frame，窗口变宽变窄时统一改宽度
        self._seps = []
        self.txt.bind("<Configure>", self._sync_seps, add="+")

        shown = set()
        first_group = True
        for gname, fields in detail_groups(self._detail.get("unitType")):
            if len(fields) == 1 and fields[0][0] == "unitSubjectName":
                shown.add("unitSubjectName")
                raw = self._detail.get("unitSubjectName")
                if _is_blank(raw):
                    continue        # 官网对空科目整段不显示，这里保持一致
                if not first_group:
                    self._add_sep()
                first_group = False
                self._insert_subjects(gname, raw)
                continue
            if not first_group:
                self._add_sep()
            first_group = False
            self.txt.insert("end", gname + "\n", "grp")
            for key, label in fields:
                # 官网对空值字段同样显示标签，这里保持一致（空值显示为 —）
                self.txt.insert("end", label + "\t", "lbl")
                self.txt.insert("end", _fmt(self._detail.get(key), key) + "\n", "val")
                shown.add(key)
        # 接口返回、但官网在该 unitType 下不展示的字段：单独列出，不冒充官网字段
        rest = [(k, v) for k, v in self._detail.items()
                if k not in shown
                and k not in ("unitGuid", "unitType", "unitName")
                and v not in (None, "")]
        if rest:
            if not first_group:
                self._add_sep()
            first_group = False
            self.txt.insert("end", "其它（接口返回，官网未展示）\n", "grp")
            for key, val in rest:
                self.txt.insert("end", KEY_LABELS.get(key, key) + "\t", "lbl")
                self.txt.insert("end", _fmt(val, key) + "\n", "val")
        if not shown and not rest:
            self.txt.insert("end", "（该机构暂无更多详情字段）\n", "val")
        make_readonly(self.txt)
        add_copy_menu(self.txt, self)

        foot = ttk.Frame(self, padding=(20, 12))
        # before=outer：底栏要先分到固定高度，否则窗口不够高时它会被正文挤压
        # （正文 Text 请求 24 行，默认 680 高的窗口里底栏只剩 2px，按钮直接看不见）
        foot.pack(side="bottom", fill="x", before=outer)
        ttk.Label(foot, text="可拖动选中复制；科目按科分色，复制内容仍为官网原文",
                  style="Sub.TLabel").pack(side="left")
        self.btn_copy = ttk.Button(foot, text="复制全部", command=self._copy)
        self.btn_copy.pack(side="right")
        ttk.Button(foot, text="关闭", style="Accent.TButton",
                   command=self.destroy).pack(side="right", padx=8)

        place_memo_window(self, self.geo_key, master, size=(780, 680),
                          keep_size=True, minsize=(620, 460))
        if cascade:
            # 等居中的装饰补偿（25/170ms）跑完再错位，否则会被它拽回原位
            self.after(260, lambda: self._cascade(cascade))
        self.lift()
        self.focus_force()
        self.txt.focus_set()

    # ---- 名称区 ----
    def _fit_name_height(self, _ev=None):
        """名称有多长就占几行（最多 3 行），不靠固定高度硬扛。"""
        try:
            n = self.name_txt.count("1.0", "end-1c", "displaylines")
            if isinstance(n, (tuple, list)):
                n = n[0] if n else 1
            n = max(1, min(int(n or 1), 3))
            if int(self.name_txt.cget("height")) != n:
                self.name_txt.configure(height=n)
        except Exception:
            pass

    def _build_name_menu(self):
        m = tk.Menu(self.name_txt, tearoff=0)
        m.add_command(label="复制名称", command=self._copy_name)
        m.add_command(label="复制选中",
                      command=lambda: self.name_txt.event_generate("<<Copy>>"))
        m.add_separator()
        m.add_command(label="全选",
                      command=lambda: self.name_txt.tag_add("sel", "1.0", "end-1c"))
        self.name_txt.bind("<Button-3>", lambda e: m.tk_popup(e.x_root, e.y_root))

    def _copy_name(self):
        txt = self.name_txt.get("1.0", "end-1c").strip()
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.btn_name.config(text="已复制 ✓")
            self.after(1500, self._reset_name_btn)
        except Exception:
            pass

    def _reset_name_btn(self):
        try:
            if self.winfo_exists():
                self.btn_name.config(text="复制名称")
        except Exception:
            pass

    def _cascade(self, n):
        try:
            x, y = self.winfo_x(), self.winfo_y()
            self.geometry("+%d+%d" % (x + 26 * n, y + 26 * n))
        except Exception:
            pass

    # ---- 主题 / 配色 ----
    def restyle(self):
        """主窗口切主题时，已开着的详情窗跟着换色（它是独立窗口，不会自动重绘）。"""
        pal, colors = self._pal(), SUBJECT_COLORS[self._theme_name()]
        try:
            self.name_txt.configure(bg=pal["card"], fg=pal["text"],
                                    selectbackground=pal["sel"],
                                    selectforeground=pal["selfg"])
            self.txt.configure(bg=pal["card"], fg=pal["text"],
                               selectbackground=pal["sel"],
                               selectforeground=pal["selfg"])
            self.txt.tag_configure("grp", foreground=pal["accent"])
            self.txt.tag_configure("lbl", foreground=pal["sub"])
            self.txt.tag_configure("sep", foreground=pal["line"])
            self.txt.tag_configure("ssub", foreground=pal["sub"])
            self.txt.tag_configure("scount", foreground=pal["sub"])
            self.txt.tag_configure("sicon", foreground=pal["sub"])
            for i, c in enumerate(colors):
                self.txt.tag_configure("sc%d" % i, foreground=c)
            for line in getattr(self, "_seps", []):
                line.configure(bg=pal["line"])
        except Exception:
            pass

    def _theme_name(self):
        try:
            import sv_ttk
            t = sv_ttk.get_theme()
            return t if t in SUBJECT_COLORS else "light"
        except Exception:
            return "light"

    def _pal(self):
        return PALETTE[self._theme_name()]

    # ---- 分栏分割线 ----
    def _add_sep(self):
        """在两组字段之间插一条 1px 分割线。

        Text 控件画不了真正的横线，这里内嵌一个高度 1px 的 Frame；
        宽度在 `_sync_seps()` 里跟着窗口宽度调整，视觉上就是通栏细线。
        内嵌窗口不参与 `get()`，复制文字不会带出多余字符。
        """
        try:
            line = tk.Frame(self.txt, height=1, bg=self._pal()["line"],
                            bd=0, highlightthickness=0, width=200)
            self._seps.append(line)
            self.txt.window_create("end", window=line, align="center",
                                   padx=0, pady=9)
            self.txt.insert("end", "\n")
            self._sync_seps()
        except Exception:
            pass

    def _sync_seps(self, _ev=None):
        """让分割线铺满正文宽度（减去左右内边距与滚动条）。"""
        try:
            width = max(120, self.txt.winfo_width() - 48)
        except Exception:
            return
        for line in self._seps:
            try:
                line.configure(width=width)
            except Exception:
                pass

    # ---- 诊疗科目 ----
    def _insert_subjects(self, title, raw):
        items = split_subjects(raw)
        self.txt.insert("end", title, "grp")
        if not items:
            self.txt.insert("end", "\n", "grp")
            self.txt.insert("end", _fmt(raw) + "\n", "val")
            return
        self.txt.insert("end", "    共 %d 项\n" % len(items), "scount")
        n = len(SUBJECT_COLORS["light"])
        prev = -1
        for name, subs in items:
            i = zlib.crc32(name.encode("utf-8")) % n
            if i == prev:                  # 相邻两科不撞色
                i = (i + 1) % n
            prev = i
            tag = "sc%d" % i
            icon = subject_icon(name)
            photo = _subject_icon_photo(self.txt, icon, self._icon_px)
            if photo is not None:
                # 图片不参与文字内容：拖动选中复制时不会带出图标
                self.txt.image_create("end", image=photo, align="center", padx=5)
            else:
                # Win7（无彩色 emoji 字体）退回单色文字图标
                self.txt.insert("end", icon + " ", "sicon")
            self.txt.insert("end", name + "\n", ("sname", tag))
            if subs:
                self.txt.insert("end", "    " + " · ".join(subs) + "\n", "ssub")

    def _copy(self):
        """复制内容与窗口显示保持一致（含"官网未展示"的补充字段）。"""
        rows, shown = [], set()
        for _g, fields in detail_groups(self._detail.get("unitType")):
            # 空科目在界面上整段隐藏，复制也保持一致，不输出「诊疗科目：—」
            if len(fields) == 1 and fields[0][0] == "unitSubjectName" \
                    and _is_blank(self._detail.get("unitSubjectName")):
                shown.add("unitSubjectName")
                continue
            for k, lbl in fields:
                rows.append("%s：%s" % (lbl, _fmt(self._detail.get(k), k)))
                shown.add(k)
        for k, v in self._detail.items():
            if k not in shown and k not in ("unitGuid", "unitType", "unitName") \
                    and v not in (None, ""):
                rows.append("%s：%s" % (KEY_LABELS.get(k, k), _fmt(v, k)))
        txt = "\n".join(rows)
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
            # 不弹提示框（复制这种高频动作弹窗打断感太强），直接在按钮上反馈
            self.btn_copy.config(text="已复制 ✓")
            self.after(1500, self._reset_copy_btn)
        except Exception:
            pass

    def _reset_copy_btn(self):
        try:
            if self.winfo_exists():
                self.btn_copy.config(text="复制全部")
        except Exception:
            pass


# ================= 设置 =================
class SettingsDialog(MemoWindow):
    geo_key = "settings"
    geo_keep_size = False

    def __init__(self, master, app):
        super().__init__(master)
        self.title("设置")
        self.resizable(False, False)
        self.transient(master)
        self.app = app

        f = ttk.Frame(self, style="Card.TFrame", padding=(24, 20, 24, 18))
        f.pack(fill="both", expand=True)

        def section(title):
            ttk.Label(f, text=title, style="H2.TLabel").pack(anchor="w")
            box = ttk.Frame(f, style="Card.TFrame")
            box.pack(fill="x", pady=(8, 16))
            return box

        def row(parent, label, factory, unit=""):
            """factory(row) 创建控件——必须用 row 作 master，否则会挂到外层容器上。"""
            r = ttk.Frame(parent, style="Card.TFrame")
            r.pack(fill="x", pady=3)
            ttk.Label(r, text=label, width=16, anchor="w").pack(side="left")
            factory(r).pack(side="left")
            if unit:
                ttk.Label(r, text=unit, style="Sub.TLabel").pack(side="left", padx=(6, 0))

        # ---- 1. 查询性能 ----
        b1 = section("查询性能")
        row(b1, "并发线程数",
            lambda p: ttk.Spinbox(p, from_=1, to=32, width=6, textvariable=app.var_workers),
            "个")
        row(b1, "验证码尝试次数",
            lambda p: ttk.Spinbox(p, from_=1, to=60, width=6, textvariable=app.var_tries),
            "次")
        ttk.Label(b1, text="并发越高越快，建议 8 ~ 20；验证码失败会自动换图重试，"
                           "不消耗查询额度。",
                  style="Sub.TLabel", wraplength=460, justify="left").pack(anchor="w", pady=(6, 0))

        # ---- 2. 验证码 ----
        b2 = section("验证码")
        ttk.Checkbutton(b2, text="自动识别失败时，弹窗让我手动点击",
                        variable=app.var_manual).pack(anchor="w")
        ttk.Label(b2, text="默认全自动识别；仅当连续重试仍失败时才弹窗，"
                           "不勾选则该条记录为失败并跳过。",
                  style="Sub.TLabel", wraplength=460, justify="left").pack(anchor="w", pady=(6, 0))

        # ---- 3. 网络出口 ----
        b3 = section("网络出口")
        r = ttk.Frame(b3, style="Card.TFrame")
        r.pack(fill="x")
        ttk.Radiobutton(r, text="直连（推荐）", variable=app.var_mode, value="xff",
                        command=self._sync_token_row).pack(side="left")
        ttk.Radiobutton(r, text="代理池（备用）", variable=app.var_mode, value="proxy",
                        command=self._sync_token_row).pack(side="left", padx=18)

        # 令牌文件选择 —— 仅「代理池」模式需要
        tr = ttk.Frame(b3, style="Card.TFrame")
        tr.pack(fill="x", pady=(10, 0))
        self.lbl_tok = ttk.Label(tr, text="令牌文件", width=10, anchor="w")
        self.lbl_tok.pack(side="left")
        self.ent_tok = ttk.Entry(tr, textvariable=app.var_token_file, width=32)
        self.ent_tok.pack(side="left", fill="x", expand=True)
        self.btn_tok = ttk.Button(tr, text="上传/选择…", width=11,
                                  command=self._pick_token)
        self.btn_tok.pack(side="left", padx=(6, 0))
        self.lbl_tip = ttk.Label(
            b3, text="选择代理池令牌 .txt 文件（Webshare 等服务商提供）。"
                     "选好后点「关闭」，下次查询即用代理池；直连模式下此项无需设置。",
            style="Sub.TLabel", wraplength=470, justify="left")
        self.lbl_tip.pack(anchor="w", pady=(4, 0))

        ttk.Label(b3, text="直连模式会为每次请求伪造来源 IP，服务端据此判定客户端，"
                           "因此不受“每日额度”和“60 秒间隔”限制，可高并发无限制查询。"
                           "详情抓取会优先复用当前 IP 重试，多次失败后才更换。",
                  style="Sub.TLabel", wraplength=470, justify="left").pack(anchor="w", pady=(8, 0))

        ttk.Separator(f).pack(fill="x", pady=(0, 14))
        ttk.Button(f, text="关闭", style="Accent.TButton",
                   command=self.destroy).pack(anchor="e")

        self._sync_token_row()
        place_memo_window(self, self.geo_key, master)
        self.grab_set()
        self.lift()
        self.focus_force()

    def _sync_token_row(self):
        """只有选「代理池」时，令牌文件那一行才可编辑。"""
        on = self.app.var_mode.get() == "proxy"
        st = "normal" if on else "disabled"
        try:
            self.ent_tok.config(state=st)
            self.btn_tok.config(state=st)
            self.lbl_tok.config(foreground=self.app._pal["text"] if on
                                else self.app._pal["sub"])
        except Exception:
            pass

    def _pick_token(self):
        cur = self.app.var_token_file.get().strip()
        kw = {"title": "选择代理池令牌文件", "parent": self,
              "filetypes": [("文本文件", "*.txt"), ("所有文件", "*.*")]}
        if cur:
            d = os.path.dirname(cur)
            if os.path.isdir(d):
                kw["initialdir"] = d
        p = filedialog.askopenfilename(**kw)
        if p:
            self.app.var_token_file.set(p)
            self.app._save_ui()


# ================= 主窗口 =================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(config.APP_NAME)
        self.minsize(980, 640)

        self._ui = load_ui_state()
        self.pool = None
        self.engine = None
        self.state = State()
        self.tasks = []
        self.rows = []
        self.events = queue.Queue()
        self._pump_running = True
        self._pump_id = None
        self._engine_id = None
        self._after_ids = set()
        self._batch_open = False
        self._log_open = False
        self._busy = False
        self._detail_stop = False
        self.overlay = None
        self._detail_wins = {}          # unitGuid -> 详情/加载窗口（同一机构只开一个）
        self._activity = ""
        self._row_by_line = {}
        self._line_no = 0
        self._unit_count = 0            # 实际展示的机构条数
        self._match_total = 0           # 官网报告的匹配总数（接口每次只回前 20 条）

        self.var_prov = tk.StringVar(value=DEFAULT_PROV)
        self.var_kw = tk.StringVar()
        self.var_workers = tk.IntVar(value=10)
        self.var_tries = tk.IntVar(value=15)
        self.var_manual = tk.BooleanVar(value=True)
        self.var_mode = tk.StringVar(value=self._ui.get("mode") or "xff")
        self.var_token_file = tk.StringVar(
            value=self._ui.get("token_file")
            or (config.DEFAULT_TOKEN_FILE if os.path.exists(config.DEFAULT_TOKEN_FILE) else ""))
        self.var_status = tk.StringVar(value="就绪")
        self.var_cutoff = tk.StringVar(value="")
        self.var_count = tk.StringVar(value="")
        self.var_ip = tk.StringVar(value="")

        self._init_theme()
        self._styles()
        self._build()
        self._apply_geometry()
        CAP.MANUAL_HOOK = self._manual_solve
        self._pump()
        self._after(200, self._load_cutoff)
        self._after(400, self._check_engine)
        self._refresh_ip()

    def _after(self, ms, fn):
        """登记定时回调，关窗时统一取消，避免残留回调报 invalid command name。"""
        try:
            aid = self.after(ms, fn)
            self._after_ids.add(aid)
            return aid
        except Exception:
            return None

    # ---------- 窗口几何记忆 ----------
    def _apply_geometry(self):
        """有保存的位置/尺寸就还原（并校验没跑到屏幕外），否则默认居中。"""
        try:
            self.update_idletasks()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            geo = valid_geometry(self._ui.get("geometry"), (980, 640), (sw, sh))
            if geo:
                self.geometry(geo)
                return
            w, h = 1120, 740
            self.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                           max(0, (sh - h) // 3)))
        except Exception:
            pass

    def _save_ui(self):
        try:
            import sv_ttk
            # 先读盘再合并：各弹窗也会往 ui.json 写自己的位置，
            # 直接用启动时的缓存会把这些记录覆盖掉。
            d = load_ui_state()
            d["geometry"] = self.winfo_geometry()
            d["theme"] = sv_ttk.get_theme()
            d["mode"] = self.var_mode.get()
            d["token_file"] = self.var_token_file.get()
            wins = d.get("windows")
            if isinstance(wins, dict):
                wins.pop("progress", None)   # 进度窗已改为窗内覆盖层
            save_ui_state(d)
            self._ui = d
        except Exception:
            pass

    def _check_engine(self):
        """启动自检：验证码识别引擎是否可用（老 CPU 可能不支持 AVX）。"""
        def run():
            ok = CAP.engine_available()
            self.events.put(("engine", (ok, CAP.engine_error())))
        threading.Thread(target=run, daemon=True).start()

    # ---------- 主题 ----------
    def _init_theme(self):
        import sv_ttk
        t = self._ui.get("theme")
        sv_ttk.set_theme(t if t in ("light", "dark") else "light")
        self._pal = PALETTE[sv_ttk.get_theme()]

    def _toggle_theme(self):
        try:
            import sv_ttk
            sv_ttk.toggle_theme()
            self._pal = PALETTE[sv_ttk.get_theme()]
            self._styles()
            self._repaint()
            for w in list(self._detail_wins.values()):
                try:
                    if w.winfo_exists() and hasattr(w, "restyle"):
                        w.restyle()
                except Exception:
                    pass
        except Exception as e:
            self._log("主题切换失败：%s" % type(e).__name__)

    def _repaint(self):
        p = self._pal
        for w in (self, getattr(self, "batch", None), getattr(self, "bottom", None),
                  getattr(self, "logframe", None), getattr(self, "st", None)):
            if w is None:
                continue
            try:
                w.configure(bg=p["bg"])
            except Exception:
                pass
        for w in (getattr(self, "txt", None), getattr(self, "logtxt", None)):
            if w is None:
                continue
            try:
                w.configure(bg=p["bg"], fg=p["text"], insertbackground=p["text"],
                            selectbackground=p["sel"], selectforeground=p["selfg"])
            except Exception:
                pass
        for t in (getattr(self, "res", None), getattr(self, "hdr", None)):
            if t is None:
                continue
            try:
                t.configure(bg=p["card"], fg=p["text"],
                            selectbackground=p["sel"], selectforeground=p["selfg"],
                            insertbackground=p["card"])
                t.tag_configure("link", foreground=p["accent"], underline=True)
                t.tag_configure("odd", background=p["row2"])
                t.tag_configure("head", foreground=p["sub"])
                if t is self.res:
                    t.tag_raise("sel")
            except Exception:
                pass

    def _styles(self):
        s = ttk.Style(self)
        p = self._pal
        s.configure("H1.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        s.configure("H2.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        s.configure("Sub.TLabel", font=("Microsoft YaHei UI", 9))
        s.configure("AccentBig.TLabel", font=("Microsoft YaHei UI", 15, "bold"),
                    foreground=p["accent"])
        s.configure("Field.TLabel", font=("Microsoft YaHei UI", 9), foreground=p["sub"])
        s.configure("Value.TLabel", font=("Microsoft YaHei UI", 10))
        s.configure("Link.TLabel", font=("Microsoft YaHei UI", 9), foreground=p["accent"])
        s.configure("Whisper.TLabel", font=("Microsoft YaHei UI", 9),
                    foreground="#9b9b9b" if p["bg"] == PALETTE["light"]["bg"] else "#6e6e6e")
        s.configure("TButton", font=("Microsoft YaHei UI", 9))

    # ---------- 布局 ----------
    def _build(self):
        self.configure(bg=self._pal["bg"])

        head = ttk.Frame(self, padding=(20, 14, 20, 10))
        head.pack(fill="x")
        self.head = head
        ttk.Label(head, text="全国医疗机构查询", style="H1.TLabel").pack(side="left")
        # 「访问官网」按钮：用系统默认浏览器打开官网查询页
        self.btn_site = ttk.Button(head, text="访问官网", style="Accent.TButton",
                                   cursor="hand2", command=self._open_site)
        self.btn_site.pack(side="right", padx=(10, 0))
        self.lbl_cutoff = ttk.Label(head, textvariable=self.var_cutoff, style="Link.TLabel",
                                    cursor="hand2")
        self.lbl_cutoff.pack(side="right")
        self.lbl_cutoff.bind("<Button-1>", lambda e: self._explain_cutoff())

        card = ttk.Frame(self, style="Card.TFrame", padding=(20, 16, 20, 16))
        card.pack(fill="x", padx=16)
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="所在省份").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="医疗机构名称").grid(row=0, column=1, sticky="w", padx=(16, 10))

        self.cmb = ttk.Combobox(card, textvariable=self.var_prov, values=PROV_LABELS,
                                state="readonly", width=14, font=("Microsoft YaHei UI", 10))
        self.cmb.grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.ent = ttk.Entry(card, textvariable=self.var_kw, font=("Microsoft YaHei UI", 11))
        self.ent.grid(row=1, column=1, sticky="we", padx=(16, 12), pady=(4, 0), ipady=3)
        self.ent.bind("<Return>", lambda e: self._search())

        self.btn = ttk.Button(card, text="查 询", style="Accent.TButton",
                              width=12, command=self._search)
        self.btn.grid(row=1, column=2, sticky="ns", pady=(4, 0))

        ttk.Label(card, text="注：至少输入机构名称中连续的 4 个字即可模糊查询，"
                             "例如“中国医学科学院北京协和医院”，输入“协和医院”即可；"
                             "官网接口每次最多返回前 20 条，可用更多字缩小范围。",
                  style="Sub.TLabel").grid(row=2, column=0, columnspan=3,
                                           sticky="w", pady=(10, 0))

        bar = ttk.Frame(self, padding=(20, 10, 20, 6))
        bar.pack(fill="x")
        self.bar = bar
        # 先排右侧按钮：窗口变窄时被压缩的是左侧文字，而不是把按钮挤没
        ttk.Button(bar, text="导出 Excel", style="Accent.TButton",
                   command=lambda: self._export("xlsx")).pack(side="right")
        ttk.Button(bar, text="导出 CSV",
                   command=lambda: self._export("csv")).pack(side="right", padx=6)
        ttk.Button(bar, text="清空", command=self._clear).pack(side="right")
        ttk.Button(bar, text="批量查询", command=self._toggle_batch).pack(side="right", padx=6)
        ttk.Button(bar, text="设置",
                   command=lambda: SettingsDialog(self, self)).pack(side="right")
        ttk.Button(bar, text="主题", command=self._toggle_theme).pack(side="right", padx=6)
        self.lbl_count = ttk.Label(bar, textvariable=self.var_count, style="H2.TLabel")
        self.lbl_count.pack(side="left")

        self.batch = ttk.Frame(self, style="Card.TFrame", padding=14)
        ttk.Label(self.batch, text="批量查询：每行一个医疗机构名称（至少 4 个字）",
                  style="Sub.TLabel").pack(anchor="w")
        wrapb = ttk.Frame(self.batch, style="Card.TFrame")
        wrapb.pack(fill="both", expand=True, pady=(6, 8))
        self.txt = tk.Text(wrapb, height=6, wrap="none", relief="flat",
                           font=("Microsoft YaHei UI", 10), bg=self._pal["bg"],
                           fg=self._pal["text"], insertbackground=self._pal["text"],
                           selectbackground=self._pal["sel"],
                           selectforeground=self._pal["selfg"])
        self.txt.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(wrapb, command=self.txt.yview)
        sb.pack(side="left", fill="y")
        self.txt.config(yscrollcommand=sb.set)
        bb = ttk.Frame(self.batch, style="Card.TFrame")
        bb.pack(fill="x")
        ttk.Button(bb, text="导入 txt/csv/xlsx", command=self._import_kw).pack(side="left")
        ttk.Button(bb, text="开始批量查询", style="Accent.TButton",
                   command=self._search_batch).pack(side="left", padx=10)

        # 底部区域先占位：保证它先于结果表分配空间，日志面板不会盖住状态栏
        self.bottom = ttk.Frame(self)
        self.bottom.pack(fill="x", side="bottom")

        self._build_table()

        self.logframe = ttk.Frame(self.bottom, style="Card.TFrame", padding=(14, 10, 14, 10))
        lb = ttk.Frame(self.logframe, style="Card.TFrame")
        lb.pack(fill="x", pady=(0, 6))
        ttk.Label(lb, text="运行日志", style="H2.TLabel").pack(side="left")
        ttk.Button(lb, text="收起", command=self._toggle_log).pack(side="right")
        ttk.Button(lb, text="清空", command=self._clear_log).pack(side="right", padx=6)
        self.logtxt = tk.Text(self.logframe, height=6, state="disabled", wrap="word",
                              relief="flat", font=("Consolas", 8),
                              bg=self._pal["bg"], fg=self._pal["sub"],
                              selectbackground=self._pal["sel"],
                              selectforeground=self._pal["selfg"])
        self.logtxt.pack(fill="both", expand=True)

        self.st = ttk.Frame(self.bottom, padding=(20, 6))
        self.st.pack(fill="x")
        ttk.Label(self.st, textvariable=self.var_status, style="Sub.TLabel").pack(side="left")
        ttk.Label(self.st, textvariable=self.var_ip, style="Sub.TLabel").pack(side="right")
        ttk.Button(self.st, text="日志", command=self._toggle_log).pack(side="right", padx=(8, 14))
        # 「日志」按钮左边的一行小字（刻意做得不起眼）
        # Tk 8.6 的文字控件渲染不出彩色 emoji，这里把 🥰 用 Pillow 画成图片再贴上去；
        # 没有彩色字体（Win7）时退回单色文字，保证不会变成方块。
        self.lbl_whisper = ttk.Frame(self.st)
        self.lbl_whisper.pack(side="right", padx=(0, 12))
        try:
            px = max(11, int(round(12.5 * float(self.winfo_fpixels("1i")) / 96.0)))
        except Exception:
            px = 14
        ttk.Label(self.lbl_whisper, text="今天也要准时下班哦",
                  style="Whisper.TLabel").pack(side="left")
        for _ in range(2):
            photo = _subject_icon_photo(self.lbl_whisper, "🥰", px)
            if photo is not None:
                ttk.Label(self.lbl_whisper, image=photo,
                          style="Whisper.TLabel").pack(side="left", padx=(1, 0))
            else:
                ttk.Label(self.lbl_whisper, text="🥰",
                          style="Whisper.TLabel").pack(side="left")
        ttk.Label(self.lbl_whisper, text="~",
                  style="Whisper.TLabel").pack(side="left")

    def _build_table(self):
        """结果表：用 Text 实现——支持拖动选中与 Ctrl+C 复制，且可做蓝色链接。"""
        pal = self._pal
        self.tbl_font = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.tbl_bold = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
        stops, x = [], 0
        for _k, _t, w in TABLE_COLS:
            x += w
            stops.append(x)
        self._tabs = tuple(stops)

        self.rf = ttk.Frame(self, style="Card.TFrame")
        self.rf.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        self.hdr = tk.Text(self.rf, height=1, wrap="none", relief="flat", borderwidth=0,
                           font=self.tbl_bold, bg=pal["card"], fg=pal["sub"],
                           padx=10, pady=5, tabs=self._tabs, cursor="arrow",
                           selectbackground=pal["sel"], selectforeground=pal["selfg"],
                           highlightthickness=0)
        self.hdr.pack(fill="x")
        self.hdr.insert("end", "\t".join(t for _k, t, _w in TABLE_COLS))
        make_readonly(self.hdr)

        body = ttk.Frame(self.rf, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        self.res = tk.Text(body, wrap="none", relief="flat", borderwidth=0,
                           font=self.tbl_font, cursor="xterm",
                           bg=pal["card"], fg=pal["text"],
                           padx=10, pady=6, tabs=self._tabs,
                           selectbackground=pal["sel"], selectforeground=pal["selfg"],
                           highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.res.yview)
        self.res.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.res.pack(side="left", fill="both", expand=True)

        self.res.tag_configure("link", foreground=pal["accent"], underline=True)
        self.res.tag_configure("odd", background=pal["row2"])
        # 关键：把选中标签提到自定义底色之上，否则隔行底色会盖住选中高亮
        self.res.tag_raise("sel")
        # 按下记录、松开校验：必须「按下的链接」和「松开的链接」是同一个才触发，
        # 避免按住某一行的「详细」拖到另一行松开时错开成那一行的详情。
        self.res.bind("<Button-1>", self._on_table_press)
        self.res.bind("<ButtonRelease-1>", self._on_table_release)
        self.res.bind("<Motion>", self._on_table_motion)
        self.res.bind("<Leave>", lambda e: self.res.configure(cursor="xterm"))
        make_readonly(self.res)
        add_copy_menu(self.res, self)

    # ---------- 结果表 ----------
    def _fit(self, s, maxpx):
        s = "" if s is None else str(s)
        # 折叠所有空白（含 \t \n）——否则单元格里的制表符/换行会撑坏表格对齐，
        # 并让「行号 -> 数据行」的映射错位
        s = " ".join(s.split())
        if self.tbl_font.measure(s) <= maxpx:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.tbl_font.measure(s[:mid] + "…") <= maxpx:
                lo = mid
            else:
                hi = mid - 1
        return s[:lo] + "…"

    def _clear_table(self):
        self.res.delete("1.0", "end")
        self._row_by_line = {}
        self._line_no = 0

    def _add_table_row(self, row, cells, has_link):
        self._line_no += 1
        line = self._line_no
        for i, (_k, _t, w) in enumerate(TABLE_COLS):
            if i:
                self.res.insert("end", "\t")
            txt = self._fit(cells[i], w - 18) if i < len(cells) else ""
            if i == len(TABLE_COLS) - 1 and has_link:
                i0 = self.res.index("end-1c")
                self.res.insert("end", "详细")
                self.res.tag_add("link", i0, self.res.index("end-1c"))
            else:
                self.res.insert("end", txt)
        self.res.insert("end", "\n")
        if line % 2 == 0:
            self.res.tag_add("odd", "%d.0" % line, "%d.end" % line)
        self._row_by_line[line] = row
        self.res.see("end")

    def _on_table_motion(self, ev):
        idx = self.res.index("@%d,%d" % (ev.x, ev.y))
        try:
            link = "link" in self.res.tag_names(idx)
        except Exception:
            link = False
        self.res.configure(cursor="hand2" if link else "xterm")

    def _link_at(self, x, y):
        """返回 (行号, 链接范围) —— 该屏幕坐标若落在「详细」链接上，否则 None。"""
        try:
            idx = self.res.index("@%d,%d" % (x, y))
            if "link" not in self.res.tag_names(idx):
                return None
            rng = self.res.tag_ranges("link")
            for i in range(0, len(rng), 2):
                s, e = str(rng[i]), str(rng[i + 1])
                if self.res.compare(idx, ">=", s) and self.res.compare(idx, "<", e):
                    return int(str(idx).split(".")[0]), (s, e)
        except Exception:
            pass
        return None

    def _on_table_press(self, ev):
        self._press_link = self._link_at(ev.x, ev.y)

    def _on_table_release(self, ev):
        start = getattr(self, "_press_link", None)
        self._press_link = None
        if start is None:
            return                       # 按下时不在链接上 -> 只是拖选文字
        cur = self._link_at(ev.x, ev.y)
        if cur is None or cur != start:
            return                       # 拖到别处/别的行才松开 -> 不触发
        line, _rng = cur
        row = self._row_by_line.get(line)
        if row:
            self._open_detail(row)

    # ---------- 面板 ----------
    def _toggle_batch(self):
        if self._busy:
            return
        if self._batch_open:
            self.batch.pack_forget()
        else:
            self.batch.pack(fill="x", padx=16, pady=(0, 6), after=self.bar)
        self._batch_open = not self._batch_open

    def _toggle_log(self):
        if self._log_open:
            self.logframe.pack_forget()
        else:
            # 在底部容器内插到状态栏之前 —— 顺序确定，不会盖住状态栏
            self.logframe.pack(fill="x", side="top", before=self.st)
        self._log_open = not self._log_open

    def _clear_log(self):
        self.logtxt.config(state="normal")
        self.logtxt.delete("1.0", "end")
        self.logtxt.config(state="disabled")

    def _refresh_ip(self):
        try:
            from .client import SESSION_IP
            self.var_ip.set("会话IP：%s" % SESSION_IP.label())
        except Exception:
            pass

    # ---------- 详情 ----------
    def _live_detail(self, guid):
        """该机构已有窗口就把它恢复出来（最小化也能还原），没有则返回 None。"""
        win = self._detail_wins.get(guid)
        if win is None:
            return None
        try:
            if not win.winfo_exists():
                raise tk.TclError("gone")
            if win.state() in ("iconic", "withdrawn"):
                win.deiconify()          # 关键：lift() 对最小化的窗口无效
            win.lift()
            win.focus_force()
            return win
        except Exception:
            self._detail_wins.pop(guid, None)
            return None

    def _open_detail(self, row):
        if self._busy:
            return
        name = row.get("unitName", "")
        guid, rt = row.get("unitGuid", ""), row.get("reqTime", "")
        if not guid:
            messagebox.showinfo("提示", "该行没有可查看的机构详情", parent=self)
            return
        if self._live_detail(guid) is not None:
            return

        if row.get("detail"):
            self._show_detail(name, row["detail"], guid)
            return

        # 加载窗同样是独立窗口：不 transient、不抢 grab，
        # 免得主窗口最小化后整个程序被一个看不见的模态窗卡住。
        win = tk.Toplevel(self)
        self._detail_wins[guid] = win
        win.title("机构详情 - 正在加载")
        win.resizable(False, False)
        ttk.Label(win, text="正在获取机构详情…\n（票据过期时会自动重新查询，请稍候）",
                  padding=26, justify="center").pack()
        place_inside_remembered(win, "detail", self)

        def cancel():
            self._detail_wins.pop(guid, None)
            try:
                win.destroy()
            except Exception:
                pass

        ttk.Button(win, text="取消", command=cancel).pack(pady=(0, 14))
        win.protocol("WM_DELETE_WINDOW", cancel)

        def run():
            try:
                d = fetch_unit_detail_fresh(guid, rt,
                                            row.get("provinceCode", ""),
                                            row.get("keyword", ""))
            except Exception:
                d = None
            try:
                self.after(0, lambda: done(d))
            except Exception:
                pass                # 主窗口已关闭

        def done(d):
            # 用户中途点了「取消」/ 关了窗口：不再弹详情
            if self._detail_wins.get(guid) is not win:
                return
            self._detail_wins.pop(guid, None)
            try:
                win.destroy()
            except Exception:
                return
            self._refresh_ip()
            if not d:
                messagebox.showwarning(
                    "提示",
                    "多次重试仍未能获取该机构详情。\n"
                    "可稍后再点一次「详细」重试。",
                    parent=self)
                return
            row["detail"] = d
            self._show_detail(name, d, guid)

        threading.Thread(target=run, daemon=True).start()

    def _show_detail(self, name, detail, guid=""):
        # 已开着别的机构详情时错开一点，避免新窗完全盖住旧窗
        n = 0
        for w in list(self._detail_wins.values()):
            try:
                if w.winfo_exists():
                    n += 1
            except Exception:
                pass
        dlg = DetailDialog(self, name, detail, cascade=min(n, 6))
        self._detail_wins[guid] = dlg

        def gone(e):
            if e.widget is dlg and self._detail_wins.get(guid) is dlg:
                self._detail_wins.pop(guid, None)

        dlg.bind("<Destroy>", gone, add="+")
        return dlg

    # ---------- 数据 ----------
    def _load_cutoff(self):
        def run():
            try:
                from .client import SiteClient
                r = SiteClient().get_enddate()
                if r.ok and r.data:
                    d = r.data
                    self.events.put(("cutoff", "官网数据截止 %s年%s月%s日%s时（点击说明）"
                                     % (d.get("year"), d.get("month"),
                                        d.get("day"), d.get("hour"))))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _open_site(self):
        """用系统默认浏览器打开官网查询页。"""
        url = config.BASE + "/unit"
        ok = False
        try:
            ok = bool(webbrowser.open(url, new=2))
        except Exception:
            ok = False
        if not ok and hasattr(os, "startfile"):
            try:
                os.startfile(url)          # Windows：直接交给系统打开
                ok = True
            except Exception:
                ok = False
        if ok:
            self._log("已打开官网：%s" % url)
        else:
            messagebox.showwarning(
                "提示", "未能自动打开浏览器，请手动访问：\n%s" % url, parent=self)

    def _explain_cutoff(self):
        messagebox.showinfo(
            "数据来源说明",
            "本工具不保存任何数据，每次查询都实时访问国家卫生健康委官网接口，\n"
            "结果与网页完全一致，无需维护、无需更新。\n\n"
            "右上角显示的是【官网自己声明的数据截止时间】\n"
            "（接口 /DATA/searchdate/enddate），官网页面显示的也是同一个值。\n\n"
            "含义：卫健委的公示数据更新到该时间点为止。\n"
            "该日期之后新登记或变更的机构，要等官网更新数据后才能查到——\n"
            "这与用浏览器打开官网查询的结果完全一致。\n\n"
            "点右上角「访问官网」按钮，可直接用浏览器打开官网查询页。",
            parent=self)

    def _prov(self):
        v = self.var_prov.get().split(" ", 1)
        return v[0], (v[1] if len(v) > 1 else "")

    def _search(self):
        if self._busy:
            return
        kw = self.var_kw.get().strip()
        if not kw:
            messagebox.showwarning("提示", "请输入医疗机构名称", parent=self)
            return
        if len(kw) < 4:
            messagebox.showwarning("提示", "医疗机构名称至少需要连续的 4 个字", parent=self)
            return
        self._clear()
        pc, pn = self._prov()
        self._run([Task(pc, pn, kw)])

    def _search_batch(self):
        if self._busy:
            return
        kws = [ln.strip() for ln in self.txt.get("1.0", "end").splitlines() if ln.strip()]
        kws = list(dict.fromkeys(kws))
        if not kws:
            messagebox.showwarning("提示", "请在批量框里输入名称（每行一个）", parent=self)
            return
        bad = [k for k in kws if len(k) < 4]
        if bad:
            messagebox.showwarning("提示", "以下名称少于 4 个字：\n" + "、".join(bad[:8]),
                                   parent=self)
            return
        self._clear()
        pc, pn = self._prov()
        if self._batch_open:
            self._toggle_batch()
        self._run([Task(pc, pn, k) for k in kws])

    def _int_setting(self, var, default, lo, hi):
        """读取数值设置；Spinbox 里被输入了非数字时退回默认值（否则 .get() 抛 TclError）。"""
        try:
            v = int(var.get())
        except Exception:
            v = default
        v = max(lo, min(hi, v))
        try:
            var.set(v)          # 把纠正后的值写回界面，用户能看见实际生效值
        except Exception:
            pass
        return v

    def _run(self, tasks):
        workers = self._int_setting(self.var_workers, 10, 1, 32)
        tries = self._int_setting(self.var_tries, 15, 1, 60)
        if self.var_mode.get() == "proxy" and (not self.pool or not self.pool.proxies):
            if not self._ensure_pool():
                return
        # 先把引擎建好再置 _busy：构造失败时不会把界面卡在「查询中」
        try:
            self.engine = Engine(self.pool, tasks, mode=self.var_mode.get(),
                                 workers=workers,
                                 log=lambda m: self.events.put(("log", m)),
                                 state=self.state,
                                 on_result=lambda t: self.events.put(("result", t)),
                                 on_progress=lambda: self.events.put(("prog", None)),
                                 on_activity=lambda kw, ph: self.events.put(("act", (kw, ph))),
                                 solve_tries=tries,
                                 fetch_detail=False,
                                 allow_manual=self.var_manual.get())
        except Exception as e:
            messagebox.showerror("错误", "无法启动查询：%s" % e, parent=self)
            return
        self.tasks = tasks
        self._busy = True
        self._activity = ""
        self.overlay = BusyOverlay(self, len(tasks), self._stop)
        self._refresh_ip()
        self._log("开始查询 %d 条（%s 模式，%d 并发）"
                  % (len(tasks), "直连" if self.var_mode.get() == "xff" else "代理池",
                     workers))
        threading.Thread(target=self._run_engine, daemon=True).start()

    def _ensure_pool(self):
        path = self.var_token_file.get().strip()
        if not path or not os.path.exists(path):
            if not messagebox.askyesno(
                    "代理池模式",
                    "还没有选择代理池令牌文件。\n\n"
                    "是否现在选择？（也可在「设置 → 网络出口」里选择）\n"
                    "若不需要代理池，请切回「直连」模式。",
                    parent=self):
                return False
            path = filedialog.askopenfilename(
                title="选择代理池令牌文件",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
            if not path:
                return False
            self.var_token_file.set(path)
        try:
            self.pool = ProxyPool(load_token(path))
            self.pool.refresh()
            return True
        except Exception as e:
            messagebox.showerror("错误", "代理池加载失败：%s" % e, parent=self)
            return False

    def _run_engine(self):
        try:
            self.engine.start()
            self.engine.join()
        finally:
            # 无论引擎怎么退出，都要发 done —— 否则覆盖层会一直挂着
            self.events.put(("done", None))

    def _stop(self):
        if self.engine:
            self.engine.stop()
            self._log("已请求停止…")

    def _clear(self):
        if self._busy:
            return
        self._clear_table()
        self.rows = []
        self._unit_count = 0
        self._match_total = 0
        self.var_count.set("")

    def _import_kw(self):
        p = filedialog.askopenfilename(title="导入名称列表",
                                       filetypes=[("表格", "*.xlsx *.xls *.csv"),
                                                  ("文本", "*.txt"), ("所有文件", "*.*")])
        if not p:
            return
        words = []
        try:
            ext = os.path.splitext(p)[1].lower()
            if ext in (".xlsx", ".xls"):
                from openpyxl import load_workbook
                wb = load_workbook(p, read_only=True, data_only=True)
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        for c in row:
                            if isinstance(c, str) and c.strip():
                                words.append(c.strip())
            elif ext == ".csv":
                with open(p, "r", encoding="utf-8-sig", errors="ignore") as f:
                    for row in csv.reader(f):
                        for c in row:
                            if c.strip():
                                words.append(c.strip())
            else:
                with open(p, "r", encoding="utf-8-sig", errors="ignore") as f:
                    words = [ln.strip() for ln in f if ln.strip()]
        except Exception as e:
            messagebox.showerror("错误", "导入失败：%s" % e, parent=self)
            return
        uniq = list(dict.fromkeys(words))
        words = [w for w in uniq if len(w) >= 4]
        dropped = len(uniq) - len(words)
        if not words:
            messagebox.showwarning("提示", "没有可用的名称：每个名称至少需要连续的 4 个字",
                                   parent=self)
            return
        if not self._batch_open:
            self._toggle_batch()
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", "\n".join(words))
        self._log("导入 %d 个名称%s" % (len(words),
                                       "（%d 个少于 4 字，已忽略）" % dropped if dropped else ""))

    # ---------- 人工验证码 ----------
    def _manual_solve(self, png_bytes, wordlist):
        box = {"ev": threading.Event(), "res": None}

        def show():
            dlg = CaptchaDialog(self, png_bytes, wordlist)
            self.wait_window(dlg)
            box["res"] = dlg.result
            box["ev"].set()

        self.after(0, show)
        box["ev"].wait()
        return box["res"]

    # ---------- 事件泵 ----------
    def _pump(self):
        if not self._pump_running:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                # 单条事件处理失败不能拖垮整个泵：否则后续结果 / done 都收不到，
                # 界面会永远停在「查询中」且所有按钮被 _busy 挡住。
                try:
                    self._handle_event(kind, payload)
                except Exception as e:
                    try:
                        self._log("⚠ 处理「%s」事件出错：%s %s"
                                  % (kind, type(e).__name__, str(e)[:80]))
                    except Exception:
                        pass
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            if self._pump_running:
                try:
                    self._pump_id = self.after(120, self._pump)
                except Exception:
                    pass

    def _handle_event(self, kind, payload):
        if kind == "log":
            self._log(payload)
        elif kind == "cutoff":
            self.var_cutoff.set(payload)
        elif kind == "result":
            self._add(payload)
        elif kind == "prog":
            self._prog()
        elif kind == "act":
            kw, ph = payload
            self._activity = "%s - %s" % (ph, kw)
        elif kind == "engine":
            ok, err = payload
            if ok:
                self._log("验证码自动识别引擎已就绪")
            else:
                self.var_manual.set(True)
                self._log("⚠ 无法加载验证码自动识别引擎：%s" % err)
                self._log("  已自动切换为「手动输入验证码」，其余功能不受影响。")
                messagebox.showwarning(
                    "验证码引擎不可用",
                    "本机无法加载验证码自动识别引擎：\n%s\n\n"
                    "常见原因：CPU 过老（不支持 AVX 指令集）。\n\n"
                    "已自动切换为手动输入模式 —— 查询时会弹窗让你点选验证码，\n"
                    "批量查询、机构详情、导出等功能均不受影响。\n"
                    "建议把「并发线程数」调小（如 2~4）。" % err,
                    parent=self)
        elif kind == "done":
            self._busy = False
            self.btn.config(state="normal")
            self.var_status.set("完成")
            if self.overlay is not None:
                self.overlay.close()
                self.overlay = None
            self._log("=== 全部结束 ===")
            self._prog()
            self._refresh_ip()

    def _log(self, msg):
        self.logtxt.config(state="normal")
        self.logtxt.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        self.logtxt.see("end")
        self.logtxt.config(state="disabled")

    def _prog(self):
        if not self.engine:
            return
        d, n = self.engine.progress()
        s = self.engine.stats
        self.var_status.set("已完成 %d/%d   成功 %d   无结果 %d   名称不合规 %d   失败 %d"
                            % (d, n, s["ok"], s["empty"], s["invalid"], s["error"]))
        if self.overlay is not None:
            self.overlay.update_state(d, n, self._activity, s)

    def _add(self, t: Task):
        if t.status == "ok":
            total = int(getattr(t, "total", 0) or 0)
            self._match_total += total
            if total > len(t.units):
                self._log("[%s] 官网共匹配 %d 条，接口每次只返回前 %d 条；"
                          "补充关键字可缩小范围" % (t.keyword, total, len(t.units)))
            for u in t.units:
                row = dict(u)
                row["keyword"] = t.keyword
                row["status"] = "成功"
                row["detail"] = (t.detail or {}).get(u.get("unitGuid", ""))
                self.rows.append(row)
                self._unit_count += 1
                self._add_table_row(row,
                                    [u.get("province", t.province_name),
                                     u.get("unitName", ""),
                                     u.get("unitOrganName", ""),
                                     u.get("levelName", ""), ""],
                                    has_link=True)
        else:
            label = {"empty": "未查询到符合条件的机构",
                     "invalid": t.msg,
                     "error": "查询失败：%s" % t.msg}.get(t.status, t.msg)
            row = {"keyword": t.keyword, "unitName": "【%s】%s" % (t.keyword, label),
                   "province": t.province_name, "unitOrganName": "",
                   "levelName": "", "detail": None, "status": label}
            self.rows.append(row)
            self._add_table_row(row, [t.province_name,
                                      "【%s】%s" % (t.keyword, label), "", "", ""],
                                has_link=False)
        extra = ""
        if self._match_total > self._unit_count:
            extra = "（官网匹配 %d 条，仅显示前 20 条）" % self._match_total
        self.var_count.set("查询结果：%d 条%s" % (len(self.rows), extra))

    # ---------- 导出（自适应详情列） ----------
    def _export(self, fmt):
        if self._busy:
            return
        data = list(self.rows)
        if not data:
            messagebox.showinfo("提示", "没有可导出的结果", parent=self)
            return
        with_detail = any(r.get("detail") for r in data)
        if not with_detail and any(r.get("unitGuid") for r in data):
            with_detail = messagebox.askyesno(
                "导出选项",
                "是否一并抓取并导出机构详情？\n"
                "（地址 / 登记号 / 诊疗科目 / 法定代表人 / 所有制形式 / 经营性质）",
                parent=self)

        # 先问保存路径，再抓详情：否则用户等抓取跑完又点「取消」，白等一场
        default = "全国医疗机构查询_%s.%s" % (time.strftime("%Y%m%d_%H%M%S"), fmt)
        p = filedialog.asksaveasfilename(defaultextension="." + fmt, initialfile=default,
                                         filetypes=[("Excel", "*.xlsx")] if fmt == "xlsx"
                                         else [("CSV", "*.csv")])
        if not p:
            return

        if with_detail and any(not r.get("detail") for r in data if r.get("unitGuid")):
            if not self._fetch_all_details(data):
                return
            if not self.winfo_exists():
                return          # 抓详情期间用户把窗口关了，别再往下走（否则弹框/保存会报错）

        cols = [("keyword", "关键字", 20), ("province", "省份", 12),
                ("unitName", "机构名称", 42), ("unitOrganName", "审批机关", 32),
                ("levelName", "级别", 10)]
        if with_detail:
            cols += [(k, lbl, 44 if k == "unitAddr" else
                      (36 if k == "unitSubjectName" else 16))
                     for k, lbl in EXPORT_DETAIL_COLS]
        cols += [("status", "状态", 22)]

        rows = []
        for r in data:
            d = r.get("detail") or {}
            cells = []
            for key, _lbl, _w in cols:
                if key == "status":
                    cells.append(r.get("status", ""))
                elif r.get(key) not in (None, ""):
                    cells.append(_cell(r.get(key), key))
                else:
                    cells.append(_cell(d.get(key, ""), key))
            rows.append(cells)

        try:
            if fmt == "xlsx":
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                ws.title = "查询结果"
                ws.append([c[1] for c in cols])
                for r in rows:
                    ws.append(r)
                for i, (_k, _l, w) in enumerate(cols, start=1):
                    ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
                wb.save(p)
            else:
                with open(p, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow([c[1] for c in cols])
                    w.writerows(rows)
            self._log("已导出 %d 行 -> %s" % (len(rows), p))
            messagebox.showinfo("完成", "已导出到：\n%s" % p, parent=self)
        except Exception as e:
            messagebox.showerror("错误", "导出失败：%s" % e, parent=self)

    def _fetch_all_details(self, data):
        """导出前补齐详情（用窗内覆盖层显示进度）。"""
        todo = [r for r in data if r.get("unitGuid") and not r.get("detail")]
        if not todo:
            return True
        self._detail_stop = False
        ov = BusyOverlay(self, len(todo), self._request_detail_stop,
                         title="正在抓取机构详情…")
        self.overlay = ov
        done = [0]
        failed = [0]
        lock = threading.Lock()
        finished = tk.BooleanVar(master=self, value=False)

        def work():
            from concurrent.futures import ThreadPoolExecutor

            def safe(fn):
                """主窗口可能已被关闭，回调要能安全失败。"""
                try:
                    self.after(0, fn)
                except Exception:
                    pass

            def one(r):
                if self._detail_stop:
                    return
                try:
                    r["detail"] = fetch_unit_detail_fresh(
                        r.get("unitGuid", ""), r.get("reqTime", ""),
                        r.get("provinceCode", ""), r.get("keyword", "")) or {}
                except Exception:
                    r["detail"] = {}
                with lock:
                    done[0] += 1
                    if not r.get("detail"):
                        failed[0] += 1
                    n, f = done[0], failed[0]
                    nm = r.get("unitName", "")[:22]
                safe(lambda: ov.update_state(
                    n, len(todo), "抓取详情 - %s%s" % (nm, "" if not f else "  (失败 %d)" % f)))
                safe(self._refresh_ip)

            try:
                with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
                    list(ex.map(one, todo))
            finally:
                # 即使抓取过程中抛异常，也要关掉覆盖层并放行主线程
                safe(ov.close)
                safe(lambda: finished.set(True))

        threading.Thread(target=work, daemon=True).start()
        try:
            self.wait_variable(finished)      # 保持事件循环，转圈不卡
        except Exception:
            pass
        self.overlay = None
        return True

    def _request_detail_stop(self):
        self._detail_stop = True

    def on_close(self):
        if self._busy:
            if not messagebox.askyesno("确认", "查询正在进行，确定要退出吗？", parent=self):
                return
            if self.engine:
                self.engine.stop()
        self._pump_running = False
        if self.overlay is not None:
            try:
                self.overlay.close()          # 先停掉转圈的 after，避免退出时报 invalid command
            except Exception:
                pass
            self.overlay = None
        for aid in list(self._after_ids) + [self._pump_id, self._engine_id]:
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
        self._after_ids.clear()
        self._pump_id = self._engine_id = None
        self.state.save()
        self._save_ui()
        self.destroy()
