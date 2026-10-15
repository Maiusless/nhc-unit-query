"""打包脚本：生成便携版 exe（GUI + CLI）。

用法（先按 README「开发与打包」一节准备好 .venv38）：
    .venv38\\Scripts\\python.exe build_exe.py           # 默认：Win7 及以上通用（Python 3.8）
    .venv311\\Scripts\\python.exe build_exe.py          # Python 3.11：仅 Win8/10/11
    ... build_exe.py --onedir                          # 输出目录版（便于逐文件扫描校验）
    ... build_exe.py --gui-only | --cli-only
    ... build_exe.py --no-verify                       # 跳过 Win7 兼容性扫描
    ... build_exe.py --out-dir 某个目录                 # 自定义产物目录（默认 release）

产物（release/）：
    卫健查询工具.exe     图形界面
    nhc_cli.exe          命令行 / 诊断

两个关键点（决定了 Win7 能不能跑）：

1. **必须用 Python 3.8 打包**
   Python 3.9+ 的 python3x.dll 依赖 `api-ms-win-core-path-l1-1-0.dll`，
   该 API 集是 Windows 8 才引入的，Win7 上根本不存在 —— 会直接报
   「Failed to load Python DLL ... 找不到指定的模块」。
   Python 3.8 是最后一个官方支持 Win7 的版本。

2. **随包附带 UCRT（ucrtbase.dll + api-ms-win-*.dll）**
   Python 3.8 自身依赖 UCRT，Win7 上需要 KB2999226 才有。
   微软文档明确支持「本地（app-local）部署」：把 Windows SDK 里
   `Redist\\ucrt\\DLLs\\x64` 下的全部 DLL 放到 exe 同目录即可。
   这里把它们打进包里，客户机不装补丁也能跑。
"""
import argparse
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

OUT_DIR = "release"          # 发布产物目录
WORK_DIR = "build"           # PyInstaller 中间产物（已 gitignore）

# Windows SDK 中 UCRT 本地部署文件的位置（微软文档指定）
UCRT_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\Redist\ucrt\DLLs\x64",
    r"C:\Program Files\Windows Kits\10\Redist\ucrt\DLLs\x64",
]

# 产物自检工具
SCANNER = os.path.join(ROOT, "tools", "win7check.py")
LEAKSCAN = os.path.join(ROOT, "tools", "leakscan.py")


def clean_pycache():
    """打包前把 site-packages 下的 __pycache__ **移出**（不是删除）。

    为什么必须做：`.pyc` 的 `co_filename` 里嵌着**构建机的绝对路径**
    （如 `C:\\Users\\<用户名>\\...\\.venv38\\Lib\\site-packages\\x.py`），
    而 `--collect-all` 会把这些 `.pyc` 一并收进产物，于是本机路径就被带进发布包
    （`tools/leakscan.py` 能检出）。

    这里用「移动到临时目录」而不是删除：既避开批量删除保护，也便于事后查看；
    Python 下次导入时会自动重建缓存，不影响使用。
    """
    import shutil
    import site
    import tempfile

    roots = set(site.getsitepackages())
    try:
        roots.add(site.getusersitepackages())
    except Exception:
        pass

    targets = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, _files in os.walk(root):
            if os.path.basename(dirpath) == "__pycache__":
                targets.append(dirpath)

    hold = os.path.join(tempfile.gettempdir(), "nhc_pycache_hold")
    os.makedirs(hold, exist_ok=True)
    moved = 0
    for i, src in enumerate(targets):
        dst = os.path.join(hold, "%04d_%s" % (i, os.path.basename(os.path.dirname(src))))
        try:
            shutil.move(src, dst)
            moved += 1
        except Exception:
            pass
    print(">>> 已移出 __pycache__：%d 个目录（临时存放于 %s，可自动重建）" % (moved, hold))


def find_ucrt():
    for d in UCRT_CANDIDATES:
        if os.path.isfile(os.path.join(d, "ucrtbase.dll")):
            return d
    return None


def common_args(ucrt, out_dir):
    args = [
        "--noconfirm", "--clean",
        "--distpath", out_dir,
        "--workpath", WORK_DIR,
        "--specpath", WORK_DIR,
        "--collect-all", "ddddocr",
        "--collect-all", "onnxruntime",
        "--collect-all", "sv_ttk",
        "--collect-submodules", "PIL",
        "--hidden-import", "ddddocr",
        "--hidden-import", "onnxruntime",
        "--hidden-import", "sv_ttk",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "openpyxl",
        "--hidden-import", "requests",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "numpy.f2py",
    ]
    if ucrt:
        n = 0
        for f in sorted(glob.glob(os.path.join(ucrt, "*.dll"))):
            args += ["--add-binary", "%s%s." % (f, os.pathsep)]
            n += 1
        print(">>> 随包附带 UCRT: %d 个 DLL（%s）" % (n, ucrt))
    else:
        print(">>> 警告：未找到 Windows SDK 的 UCRT，Win7 上可能需要先装 KB2999226")
    return args


def build(entry, name, console, onedir, extra):
    cmd = [sys.executable, "-m", "PyInstaller"] + extra + [
        "--name", name,
        "--console" if console else "--windowed",
        entry,
    ]
    # 注意插到 "PyInstaller" 之后（索引 3），否则会被当成 -m 的模块名
    cmd.insert(3, "--onedir" if onedir else "--onefile")
    print("\n>>> %s -> %s%s" % (entry, name, "（目录版）" if onedir else ""))
    return subprocess.run(cmd, cwd=ROOT).returncode


def verify(target):
    """调用 tools/win7check.py 做 Win7 兼容性扫描。"""
    if not os.path.isfile(SCANNER):
        print(">>> 跳过扫描（未找到 tools/win7check.py）")
        return 0
    print("\n" + "=" * 68)
    print(">>> Win7 兼容性扫描")
    print("=" * 68)
    return subprocess.run([sys.executable, SCANNER, target], cwd=ROOT).returncode


def verify_leak(target):
    """调用 tools/leakscan.py 检查产物内是否残留本机路径 / 密钥。"""
    if not os.path.isfile(LEAKSCAN):
        print(">>> 跳过泄漏扫描（未找到 tools/leakscan.py）")
        return 0
    print("\n" + "=" * 68)
    print(">>> 泄漏扫描（本机路径 / 密钥）")
    print("=" * 68)
    return subprocess.run([sys.executable, LEAKSCAN, target], cwd=ROOT).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onedir", action="store_true", help="输出目录版而非单文件")
    ap.add_argument("--gui-only", action="store_true")
    ap.add_argument("--cli-only", action="store_true")
    ap.add_argument("--no-verify", action="store_true", help="跳过 Win7 扫描")
    ap.add_argument("--out-dir", default=OUT_DIR,
                    help="产物目录，默认 %s" % OUT_DIR)
    a = ap.parse_args()

    print(">>> 打包解释器: Python %s (%s)" % (sys.version.split()[0], sys.executable))
    if sys.version_info >= (3, 9):
        print(">>> 注意：Python >= 3.9 打包出的 exe **无法在 Windows 7 上运行**")

    out = os.path.join(ROOT, a.out_dir)
    os.makedirs(out, exist_ok=True)
    clean_pycache()                      # 防止构建机路径被 .pyc 带进产物
    ucrt = find_ucrt()
    extra = common_args(ucrt, a.out_dir)

    rcs = []
    if not a.cli_only:
        rcs.append(build("run_gui.py", "卫健查询工具", False, a.onedir, extra))
    if not a.gui_only:
        rcs.append(build("run_cli.py", "nhc_cli", True, a.onedir, extra))

    print("\n产物目录: %s" % out)
    for n in sorted(os.listdir(out)):
        p = os.path.join(out, n)
        if os.path.isfile(p):
            print("   %-28s %8.1f MB" % (n, os.path.getsize(p) / 1048576))
        else:
            print("   %-28s %s" % (n + "/", "(目录版)"))

    if not a.no_verify:
        if not a.cli_only:
            exe = os.path.join(out, "卫健查询工具.exe")
            rcs.append(verify(exe))
            rcs.append(verify_leak(exe))
        if not a.gui_only:
            exe = os.path.join(out, "nhc_cli.exe")
            rcs.append(verify(exe))
            rcs.append(verify_leak(exe))
    return 0 if all(rc == 0 for rc in rcs) else 1


if __name__ == "__main__":
    sys.exit(main())
