"""图形界面 —— 仿官网极简交互，Sun Valley (Fluent) 主题。

结构：
    查询卡片（省份 + 机构名称 + 查询）
    工具条（批量 / 清空 / 导出 / 设置 / 主题）
    结果表（文本表，可拖动选中复制；「详细」为蓝色链接，点击看机构详情）
    模态实时进度窗（查询期间锁住主窗口，避免并发误操作）
"""
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


# ================= 人工验证码 =================
class CaptchaDialog(tk.Toplevel):
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
        center_window(self, master)
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


# ================= 实时进度（模态） =================
class ProgressDialog(tk.Toplevel):
    def __init__(self, master, total, on_stop):
        super().__init__(master)
        self.title("处理中")
        self.resizable(False, False)
        self.transient(master)
        self.total = max(1, int(total))
        self._on_stop = on_stop
        self._closed = False

        f = ttk.Frame(self, style="Card.TFrame", padding=20)
        f.pack(fill="both", expand=True)

        self.lbl_title = ttk.Label(f, text="正在查询…", style="H2.TLabel")
        self.lbl_title.pack(anchor="w")

        self.bar = ttk.Progressbar(f, mode="determinate", maximum=self.total, length=400)
        self.bar.pack(fill="x", pady=(14, 8))

        row = ttk.Frame(f, style="Card.TFrame")
        row.pack(fill="x")
        self.lbl_pct = ttk.Label(row, text="0%", style="AccentBig.TLabel")
        self.lbl_pct.pack(side="left")
        self.lbl_cnt = ttk.Label(row, text="0 / %d" % self.total, style="Sub.TLabel")
        self.lbl_cnt.pack(side="right")

        self.lbl_act = ttk.Label(f, text="准备中…", style="Sub.TLabel", width=52, anchor="w")
        self.lbl_act.pack(anchor="w", pady=(12, 0))

        self.lbl_stat = ttk.Label(f, text="成功 0   无结果 0   名称不合规 0   失败 0",
                                  style="Sub.TLabel")
        self.lbl_stat.pack(anchor="w", pady=(4, 0))

        bb = ttk.Frame(f, style="Card.TFrame")
        bb.pack(fill="x", pady=(18, 0))
        self.btn_stop = ttk.Button(bb, text="停 止", command=self._stop)
        self.btn_stop.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", lambda: None)
        # 居中到「主窗口」而不是屏幕：主窗口的位置是被记忆的，按屏幕居中会与之冲突
        center_window(self, master)
        self.grab_set()
        self.lift()
        self.focus_force()

    def _stop(self):
        self.btn_stop.config(state="disabled", text="正在停止…")
        self.lbl_title.config(text="正在停止…")
        self._on_stop()

    def update_state(self, done, total, activity, stats=None):
        if self._closed:
            return
        self.total = max(1, int(total))
        self.bar.config(maximum=self.total, value=done)
        self.lbl_pct.config(text="%d%%" % int(done * 100 / self.total))
        self.lbl_cnt.config(text="%d / %d" % (done, self.total))
        if activity:
            self.lbl_act.config(text="当前：%s" % activity)
        if stats:
            self.lbl_stat.config(text="成功 %d   无结果 %d   名称不合规 %d   失败 %d"
                                 % (stats.get("ok", 0), stats.get("empty", 0),
                                    stats.get("invalid", 0), stats.get("error", 0)))

    def finish(self):
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ================= 机构详情（可复制） =================
class DetailDialog(tk.Toplevel):
    def __init__(self, master, name, detail):
        super().__init__(master)
        self.title("机构详情")
        self.minsize(600, 460)
        self.transient(master)
        self._detail = detail or {}
        self._master = master

        head = ttk.Frame(self, style="Card.TFrame", padding=(20, 18, 20, 12))
        head.pack(fill="x")
        ttk.Label(head, text=name or self._detail.get("unitName", ""),
                  style="H2.TLabel", wraplength=680, justify="left").pack(anchor="w")
        ttk.Separator(self).pack(fill="x")

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        pal = self._pal()
        self.txt = tk.Text(outer, wrap="word", relief="flat", borderwidth=0,
                           font=("Microsoft YaHei UI", 10), cursor="xterm",
                           bg=pal["card"], fg=pal["text"],
                           padx=18, pady=12, spacing1=2, spacing3=4,
                           tabs=(96,), selectbackground=pal["sel"],
                           selectforeground=pal["selfg"], insertwidth=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        self.txt.tag_configure("grp", font=("Microsoft YaHei UI", 10, "bold"),
                               foreground=pal["accent"], spacing1=10, spacing3=4)
        self.txt.tag_configure("lbl", font=("Microsoft YaHei UI", 10),
                               foreground=pal["sub"])
        self.txt.tag_configure("val", font=("Microsoft YaHei UI", 10),
                               lmargin1=0, lmargin2=96)
        self.txt.tag_configure("sep", foreground=pal["line"])

        shown = set()
        for gname, fields in detail_groups(self._detail.get("unitType")):
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
            self.txt.insert("end", "其它（接口返回，官网未展示）\n", "grp")
            for key, val in rest:
                self.txt.insert("end", KEY_LABELS.get(key, key) + "\t", "lbl")
                self.txt.insert("end", _fmt(val, key) + "\n", "val")
        if not shown and not rest:
            self.txt.insert("end", "（该机构暂无更多详情字段）\n", "val")
        make_readonly(self.txt)
        add_copy_menu(self.txt, master)

        foot = ttk.Frame(self, padding=(20, 12))
        foot.pack(fill="x")
        ttk.Label(foot, text="可直接拖动鼠标选中，Ctrl+C 复制",
                  style="Sub.TLabel").pack(side="left")
        ttk.Button(foot, text="复制全部", command=self._copy).pack(side="right")
        ttk.Button(foot, text="关闭", style="Accent.TButton",
                   command=self.destroy).pack(side="right", padx=8)

        center_window(self, master, size=(760, 660))
        self.grab_set()
        self.lift()
        self.focus_force()
        self.txt.focus_set()

    def _pal(self):
        try:
            import sv_ttk
            return PALETTE[sv_ttk.get_theme()]
        except Exception:
            return PALETTE["light"]

    def _copy(self):
        """复制内容与窗口显示保持一致（含"官网未展示"的补充字段）。"""
        rows, shown = [], set()
        for _g, fields in detail_groups(self._detail.get("unitType")):
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
            messagebox.showinfo("已复制", "详情已复制到剪贴板", parent=self)
        except Exception:
            pass


# ================= 设置 =================
class SettingsDialog(tk.Toplevel):
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
        center_window(self, master)
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
        self.dlg_progress = None
        self._detail_win = None
        self._activity = ""
        self._row_by_line = {}
        self._line_no = 0

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
            d = dict(self._ui)
            d["geometry"] = self.winfo_geometry()
            d["theme"] = sv_ttk.get_theme()
            d["mode"] = self.var_mode.get()
            d["token_file"] = self.var_token_file.get()
            save_ui_state(d)
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
        s.configure("TButton", font=("Microsoft YaHei UI", 9))

    # ---------- 布局 ----------
    def _build(self):
        self.configure(bg=self._pal["bg"])

        head = ttk.Frame(self, padding=(20, 14, 20, 10))
        head.pack(fill="x")
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

        ttk.Label(card, text="注：至少输入机构名称中连续的 4 个字即可模糊查询。"
                             "例如“中国医学科学院北京协和医院”，输入“协和医院”即可。",
                  style="Sub.TLabel").grid(row=2, column=0, columnspan=3,
                                           sticky="w", pady=(10, 0))

        bar = ttk.Frame(self, padding=(20, 10, 20, 6))
        bar.pack(fill="x")
        self.bar = bar
        ttk.Label(bar, textvariable=self.var_count, style="H2.TLabel").pack(side="left")
        ttk.Button(bar, text="导出 Excel", style="Accent.TButton",
                   command=lambda: self._export("xlsx")).pack(side="right")
        ttk.Button(bar, text="导出 CSV",
                   command=lambda: self._export("csv")).pack(side="right", padx=6)
        ttk.Button(bar, text="清空", command=self._clear).pack(side="right")
        ttk.Button(bar, text="批量查询", command=self._toggle_batch).pack(side="right", padx=6)
        ttk.Button(bar, text="设置",
                   command=lambda: SettingsDialog(self, self)).pack(side="right")
        ttk.Button(bar, text="主题", command=self._toggle_theme).pack(side="right", padx=6)

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
    def _open_detail(self, row):
        if self._busy:
            return
        # 同一时刻只允许一个详情/加载窗口，防止快速连点堆出大量窗口
        cur = self._detail_win
        if cur is not None:
            try:
                if cur.winfo_exists():
                    cur.lift()
                    cur.focus_force()
                    return
            except Exception:
                pass
            self._detail_win = None

        name = row.get("unitName", "")
        guid, rt = row.get("unitGuid", ""), row.get("reqTime", "")
        if not guid:
            messagebox.showinfo("提示", "该行没有可查看的机构详情", parent=self)
            return
        if row.get("detail"):
            self._detail_win = self._show_detail(name, row["detail"])
            return

        win = tk.Toplevel(self)
        self._detail_win = win
        win.title("加载中")
        win.resizable(False, False)
        win.transient(self)
        ttk.Label(win, text="正在获取机构详情…\n（票据过期时会自动重新查询，请稍候）",
                  padding=26, justify="center").pack()
        center_window(win, self)
        win.grab_set()

        def run():
            try:
                d = fetch_unit_detail_fresh(guid, rt,
                                            row.get("provinceCode", ""),
                                            row.get("keyword", ""))
            except Exception:
                d = None
            self.after(0, lambda: done(d))

        def done(d):
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                return          # 主窗口已关闭
            self._detail_win = None
            self._refresh_ip()
            if not d:
                messagebox.showwarning(
                    "提示",
                    "多次重试仍未能获取该机构详情。\n"
                    "可稍后再点一次「详细」重试。",
                    parent=self)
                return
            row["detail"] = d
            self._detail_win = self._show_detail(name, d)

        threading.Thread(target=run, daemon=True).start()

    def _show_detail(self, name, detail):
        dlg = DetailDialog(self, name, detail)
        dlg.bind("<Destroy>", lambda e: setattr(self, "_detail_win", None)
                 if e.widget is dlg else None)
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

    def _run(self, tasks):
        if self.var_mode.get() == "proxy" and (not self.pool or not self.pool.proxies):
            if not self._ensure_pool():
                return
        self.tasks = tasks
        self._busy = True
        self._activity = ""
        self.engine = Engine(self.pool, tasks, mode=self.var_mode.get(),
                             workers=self.var_workers.get(),
                             log=lambda m: self.events.put(("log", m)),
                             state=self.state,
                             on_result=lambda t: self.events.put(("result", t)),
                             on_progress=lambda: self.events.put(("prog", None)),
                             on_activity=lambda kw, ph: self.events.put(("act", (kw, ph))),
                             solve_tries=self.var_tries.get(),
                             fetch_detail=False,
                             allow_manual=self.var_manual.get())
        self.dlg_progress = ProgressDialog(self, len(tasks), self._stop)
        self._refresh_ip()
        self._log("开始查询 %d 条（%s 模式，%d 并发）"
                  % (len(tasks), "直连" if self.var_mode.get() == "xff" else "代理池",
                     self.var_workers.get()))
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
        self.engine.start()
        self.engine.join()
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
        words = [w for w in dict.fromkeys(words) if len(w) >= 4]
        if not self._batch_open:
            self._toggle_batch()
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", "\n".join(words))
        self._log("导入 %d 个名称" % len(words))

    # ---------- 人工验证码 ----------
    def _manual_solve(self, png_bytes, wordlist):
        box = {"ev": threading.Event(), "res": None}

        def show():
            if self.dlg_progress is not None:
                try:
                    self.dlg_progress.grab_release()
                except Exception:
                    pass
            dlg = CaptchaDialog(self, png_bytes, wordlist)
            self.wait_window(dlg)
            box["res"] = dlg.result
            if self.dlg_progress is not None:
                try:
                    self.dlg_progress.grab_set()
                    self.dlg_progress.lift()
                except Exception:
                    pass
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
                    if self.dlg_progress is not None:
                        self.dlg_progress.finish()
                        self.dlg_progress = None
                    self._log("=== 全部结束 ===")
                    self._prog()
                    self._refresh_ip()
        except queue.Empty:
            pass
        if self._pump_running:
            try:
                self._pump_id = self.after(120, self._pump)
            except Exception:
                pass

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
        if self.dlg_progress is not None:
            self.dlg_progress.update_state(d, n, self._activity, s)

    def _add(self, t: Task):
        if t.status == "ok":
            for u in t.units:
                row = dict(u)
                row["keyword"] = t.keyword
                row["status"] = "成功"
                row["detail"] = (t.detail or {}).get(u.get("unitGuid", ""))
                self.rows.append(row)
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
        self.var_count.set("查询结果：%d 条记录" % len(self.rows))

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
        if with_detail and any(not r.get("detail") for r in data if r.get("unitGuid")):
            if not self._fetch_all_details(data):
                return

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

        default = "全国医疗机构查询_%s.%s" % (time.strftime("%Y%m%d_%H%M%S"), fmt)
        p = filedialog.asksaveasfilename(defaultextension="." + fmt, initialfile=default,
                                         filetypes=[("Excel", "*.xlsx")] if fmt == "xlsx"
                                         else [("CSV", "*.csv")])
        if not p:
            return
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
        """导出前补齐详情（带模态进度）。"""
        todo = [r for r in data if r.get("unitGuid") and not r.get("detail")]
        if not todo:
            return True
        self._detail_stop = False
        dlg = ProgressDialog(self, len(todo), self._request_detail_stop)
        dlg.lbl_title.config(text="正在抓取机构详情…")
        done = [0]
        failed = [0]
        lock = threading.Lock()

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
                safe(lambda: dlg.update_state(
                    n, len(todo), "抓取详情 - %s%s" % (nm, "" if not f else "  (失败 %d)" % f)))
                safe(self._refresh_ip)

            with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
                list(ex.map(one, todo))
            safe(dlg.finish)

        threading.Thread(target=work, daemon=True).start()
        self.wait_window(dlg)
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
