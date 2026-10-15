"""数据保真审计：验证详情窗字段/标签与官网页面的条件规则一致。

官网页面的规则（逆向自 index-52f8b012.js）：
  审批类(unitType 空或"0")：审批机关 / 登记号 / 级别 / 执业许可证有效期
  备案类(unitType≠"0")    ：备案机关 / 备案编号 / 所有制形式 / 经营性质 / 备案日期
  unitType=="2"           ：诊疗范围（否则诊疗科目）
  unitType=="4"           ：额外显示 服务方式

用法（在仓库根目录执行）：
    .venv38\\Scripts\\python.exe tools/datacheck.py
"""
import os
import sys

# 本文件在 tools/ 下，需要把仓库根目录加入 sys.path 才能 import nhc_tool
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
P = lambda *a: print(*a, flush=True)

from nhc_tool.client import SiteClient, random_ip
from nhc_tool import captcha as CAP
import nhc_tool.gui as G

G.messagebox.showinfo = lambda *a, **k: None
G.messagebox.showwarning = lambda *a, **k: None


def query(kw, pc="35"):
    cli = SiteClient(None, xff=random_ip())
    for _ in range(15):
        r = cli.get_captcha()
        if not isinstance(r, tuple):
            continue
        tok, wl, png = r
        pts, _ = CAP.solve(png, wl, allow_manual=False)
        if pts and cli.check_captcha(tok, pts):
            return cli, cli.query_unit(pc, kw, tok, pts)
    return None, None


def detail(u):
    cli = SiteClient(None, xff=random_ip())
    t = cli.get_ticket(u["unitGuid"], u["reqTime"])
    if not (t.ok and t.data):
        return None
    d = cli.get_detail(t.data)
    return d.data if d.ok else None


def render(d):
    """把 DetailDialog 的渲染结果取出来。"""
    app = G.App()
    app.update_idletasks()
    dlg = G.DetailDialog(app, d.get("unitName", ""), d)
    app.update_idletasks()
    app.update()
    txt = dlg.txt.get("1.0", "end-1c")
    dlg.destroy()
    app._pump_running = False
    app.destroy()
    return txt


# ---------- 1) _fmt 的字段级行为 ----------
P("【1】_fmt 字段级格式化（防 13 位编码被误转日期）")
cases = [("1234567890123", "unitRegCode", "应为原样"),
         ("1234567890123", "unitPassDate", "应为日期"),
         ("1791856841896", "unitDateValidBegin", "应为日期"),
         ("MA346JF8135011117D1102", "unitRegCode", "应为原样")]
for v, k, expect in cases:
    got = G._fmt(v, k)
    P("     %-24s key=%-20s -> %-14s (%s)" % (v, k, got, expect))
P("     不传 key（默认）:", G._fmt("1234567890123"), "← 应为原样")

# ---------- 2) 取真实样本 ----------
P()
P("【2】取真实样本")
approve, filing = None, None
for kw in ("门诊部", "诊所"):
    cli, q = query(kw)
    units = (q.data or {}).get("unitInfoList") or []
    for u in units:
        d = detail(u)
        if not d:
            continue
        if d.get("unitType") == "0" and approve is None:
            approve = d
        elif d.get("unitType") not in (None, "", "0") and filing is None:
            filing = d
    if approve and filing:
        break
P("     审批类样本:", approve and approve.get("unitName"), "unitType=",
  approve and approve.get("unitType"))
P("     备案类样本:", filing and filing.get("unitName"), "unitType=",
  filing and filing.get("unitType"))

# ---------- 3) 渲染并比对 ----------
CHECKS = {
    "0": {"must": ["审批机关", "登记号", "级别", "有效期起", "有效期止", "诊疗科目"],
          "must_not": ["备案机关：", "备案编号："]},
    "filing": {"must": ["备案机关", "备案编号", "所有制形式", "经营性质", "备案日期"],
               "must_not": ["级别\t"]},
}

for tag, d in (("审批类", approve), ("备案类", filing)):
    if not d:
        P()
        P("【3】%s 样本缺失，跳过" % tag)
        continue
    P()
    P("【3】%s 渲染结果（unitType=%s）" % (tag, d.get("unitType")))
    txt = render(d)
    for line in txt.splitlines():
        P("     | " + line)
    spec = CHECKS["0"] if d.get("unitType") == "0" else CHECKS["filing"]
    ok = True
    for w in spec["must"]:
        if w not in txt:
            P("     X 缺少: %s" % w)
            ok = False
    for w in spec["must_not"]:
        if w in txt:
            P("     X 不应出现: %s" % w)
            ok = False
    P("     结论:", "符合官网规则" if ok else "不符")

# ---------- 4) 导出表头 ----------
P()
P("【4】导出列定义")
P("     ", [c[1] for c in G.EXPORT_DETAIL_COLS])
P()
P("=== 完成 ===")
