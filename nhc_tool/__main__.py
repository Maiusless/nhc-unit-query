"""程序入口：无参数启动 GUI；带参数走命令行批处理。

用法示例：
    nhc_query.exe                                       启动图形界面
    nhc_cli.exe --cli -p 11 -k 协和医院,肿瘤医院 -o 结果.xlsx
    nhc_cli.exe --cli -p 44 -f 关键词.txt -w 12 -o 结果.xlsx
    nhc_cli.exe --probe -f 令牌.txt                     检测代理池各 IP 状态
"""
import argparse
import csv
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nhc_tool import config
    from nhc_tool.engine import Engine, State, Task
    from nhc_tool.proxy_pool import ProxyPool, load_token
else:
    from . import config
    from .engine import Engine, State, Task
    from .proxy_pool import ProxyPool, load_token


def run_gui():
    from nhc_tool.gui import App
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


def _load_token_or_die(path, token=None):
    """读取令牌，失败给出可操作的提示（客户机上通常没有默认令牌文件）。"""
    if token:
        return token
    if not path or not os.path.exists(path):
        print("错误：未找到代理池令牌文件：%s" % (path or "(未指定)"))
        print("      请用 -f 指定令牌文件，或用 -t 直接给出令牌。")
        print("      若不需要代理池，请使用默认的直连模式（去掉 --mode proxy）。")
        sys.exit(2)
    return load_token(path)


def run_probe(token_file, token=None):
    tok = _load_token_or_die(token_file, token)
    pool = ProxyPool(tok)
    ps = pool.refresh()
    print("代理数量：%d" % len(ps))
    from nhc_tool.client import SiteClient
    from nhc_tool import captcha as CAP
    for p in ps:
        cli = SiteClient(p.url)
        ok = False
        for _ in range(12):
            r = cli.get_captcha()
            if not isinstance(r, tuple):
                continue
            ct, wl, png = r
            pts, _ = CAP.solve_local(png, wl)
            if pts and cli.check_captcha(ct, pts):
                ok = True
                break
        if not ok:
            print("%-22s 验证码求解失败" % p.label)
            continue
        r = cli.query_unit("11", "协和医院", ct, pts)
        print("%-22s %-9s %s" % (p.label, r.status, r.msg))


def run_cli(args):
    from nhc_tool import captcha as CAP
    if not CAP.engine_available():
        print("警告：本机无法加载验证码自动识别引擎")
        print("     原因：%s" % CAP.engine_error())
        print("     常见于 CPU 过老（不支持 AVX 指令集）。")
        print("     命令行模式没有人工输入验证码的兜底，无法完成查询。")
        print("     请改用图形界面（卫健查询工具.exe），它会弹窗让你手动点选验证码。")
        return 2

    kws = []
    if args.keywords:
        kws += [k.strip() for k in args.keywords.split(",") if k.strip()]
    if args.kwfile:
        with open(args.kwfile, "r", encoding="utf-8-sig", errors="ignore") as f:
            kws += [ln.strip() for ln in f if ln.strip()]
    kws = [k for k in dict.fromkeys(kws) if len(k) >= 4]
    if not kws:
        print("没有有效的关键字（需至少 4 个连续汉字）")
        return 1

    tasks = [Task(args.province, "", k) for k in kws]
    pool = None
    if args.mode == "proxy":
        pool = ProxyPool(_load_token_or_die(args.file, args.token))
        pool.refresh()
        print("代理池：%d 个 IP" % len(pool.proxies))

    eng = Engine(pool, tasks, mode=args.mode, workers=args.workers, state=State(),
                 log=lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m)),
                 solve_tries=args.tries, fetch_detail=args.detail,
                 allow_manual=False)
    t0 = time.time()
    eng.start()
    eng.join()
    el = time.time() - t0

    heads = ["关键字", "省份", "机构名称", "审批机关", "级别"]
    if args.detail:
        heads += ["地址", "登记号/备案编号", "诊疗科目/诊疗范围",
                  "法定代表人", "所有制形式", "经营性质"]
    heads += ["状态"]

    def detail_cells(t, u):
        if not args.detail:
            return []
        d = (t.detail or {}).get(u.get("unitGuid", "")) or {}
        return [d.get("unitAddr", ""), d.get("unitRegCode", ""),
                (d.get("unitSubjectName", "") or "").replace("\n", " "),
                d.get("unitLegalName", ""), d.get("unitHavingName", ""),
                d.get("unitNatureBusiName", "")]

    rows = []
    for t in tasks:
        if t.units:
            for u in t.units:
                rows.append([t.keyword, u.get("province", ""), u.get("unitName", ""),
                             u.get("unitOrganName", ""), u.get("levelName", "")]
                            + detail_cells(t, u) + ["成功"])
        else:
            rows.append([t.keyword, "", "", "", ""]
                        + (["", "", "", "", "", ""] if args.detail else [])
                        + [t.msg or t.status])
    if args.out:
        ext = os.path.splitext(args.out)[1].lower()
        if ext in (".xlsx", ".xls"):
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.append(heads)
            for r in rows:
                ws.append(r)
            wb.save(args.out)
        else:
            with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(heads)
                w.writerows(rows)
        print("已导出：%s" % args.out)
    s = eng.stats
    print("用时 %.1fs  共 %d 条  %.2fs/条  成功 %d  无结果 %d  不合规 %d  失败 %d"
          % (el, len(tasks), el / max(len(tasks), 1), s["ok"], s["empty"], s["invalid"], s["error"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description=config.APP_NAME)
    ap.add_argument("--cli", action="store_true", help="命令行模式")
    ap.add_argument("--probe", action="store_true", help="仅检测代理池状态")
    ap.add_argument("--mode", choices=["xff", "proxy"], default="xff",
                    help="出口方式：xff=直连伪造IP(默认)，proxy=代理池")
    ap.add_argument("-f", "--file", default=config.DEFAULT_TOKEN_FILE, help="代理池令牌文件")
    ap.add_argument("-t", "--token", help="直接指定令牌")
    ap.add_argument("-p", "--province", default="11", help="省份代码，如 11")
    ap.add_argument("-k", "--keywords", default="", help="关键字，逗号分隔")
    ap.add_argument("--kwfile", default="", help="关键字文件（每行一个）")
    ap.add_argument("-o", "--out", default="", help="导出文件 (.xlsx/.csv)")
    ap.add_argument("-w", "--workers", type=int, default=8, help="并发线程数")
    ap.add_argument("--tries", type=int, default=15, help="验证码最大尝试次数")
    ap.add_argument("--detail", action="store_true", help="同时抓取机构详情")
    args = ap.parse_args()

    if args.probe:
        run_probe(args.file, args.token)
    elif args.cli:
        sys.exit(run_cli(args))
    else:
        run_gui()


if __name__ == "__main__":
    main()
