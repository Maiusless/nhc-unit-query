"""校验打包产物在 Windows 7 x64 上能否解析全部依赖。

核心判据（Win7 特有的两条事实）：
  A. Windows 7 上**不存在任何** api-ms-win-*.dll
     （API 集是 Win8 引入的；Win7 只有装 KB2999226 后才有 api-ms-win-crt-*）。
     => 包内任何 PE 引用的 api-ms-win-* 都必须在包根目录里有实体文件。
  B. Windows 7 上不存在 Win8+ 才引入的 API 集与 DLL。
     => 出现即判失败。

用法：
    python tools/bundlecheck.py <产物目录>
"""
import os
import sys
from collections import defaultdict

import pefile

API_SET = "api-ms-win-"

# Win8+ 才引入、Win7 上永远不存在的 API 集（装了 UCRT 也没有）
WIN8_PLUS_API_SETS = {
    "api-ms-win-core-path-l1-1-0.dll",
    "api-ms-win-core-heap-l2-1-0.dll",
    "api-ms-win-core-libraryloader-l1-2-0.dll",
    "api-ms-win-eventing-provider-l1-1-0.dll",
    "api-ms-win-core-winrt-l1-1-0.dll",
    "api-ms-win-core-com-l1-1-0.dll",
    "api-ms-win-core-file-l1-2-1.dll",
    "api-ms-win-core-memory-l1-1-1.dll",
}

WIN8_PLUS_DLLS = {"d3d12.dll", "dxcore.dll", "dcomp.dll", "windows.web.dll"}

SKIP_DIRS = {"__pycache__"}


def pe_imports(path):
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                         pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    except Exception as e:
        return None, str(e)
    out = set()
    try:
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []):
                d = (entry.dll or b"").decode("ascii", "ignore").lower()
                if d:
                    out.add(d)
    finally:
        pe.close()
    return out, None


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/bundlecheck.py <产物目录>")
        return 2
    root = sys.argv[1]

    # 包内所有文件（按文件名索引，DLL 查找是按名字）
    have = {}
    pes = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            p = os.path.join(dirpath, n)
            have.setdefault(n.lower(), []).append(p)
            if n.lower().endswith((".exe", ".dll", ".pyd")):
                pes.append(p)

    print("产物目录: %s" % root)
    print("包内文件 %d 个，其中 PE 文件 %d 个" % (len(have), len(pes)))
    print()

    # ---- 1) UCRT 是否齐全 ----
    print("【1】UCRT 运行时（Win7 缺 KB2999226 时必需）")
    ucrt_files = sorted(n for n in have if n == "ucrtbase.dll" or n.startswith(API_SET))
    print("   包内 UCRT 文件: %d 个" % len(ucrt_files))
    for k in ("ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll",
              "api-ms-win-crt-stdio-l1-1-0.dll", "api-ms-win-crt-heap-l1-1-0.dll"):
        print("     %-40s %s" % (k, "有" if k in have else "缺失 <<<"))

    # ---- 2) VC++ 运行时 ----
    print()
    print("【2】VC++ 运行时")
    for k in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
        print("     %-40s %s" % (k, "有" if k in have else "缺失（若无人引用则可不带）"))

    # ---- 3) 逐 PE 检查 ----
    print()
    print("【3】逐 PE 检查依赖（重点：api-ms-win-* 必须在包内）")
    bad_api, missing_api, other = [], [], defaultdict(set)
    for p in pes:
        deps, err = pe_imports(p)
        rel = os.path.relpath(p, root)
        if err:
            print("   ? %s 解析失败: %s" % (rel, err))
            continue
        for d in sorted(deps):
            if d in WIN8_PLUS_API_SETS or d in WIN8_PLUS_DLLS:
                bad_api.append((rel, d))
            elif d.startswith(API_SET):
                if d not in have:
                    missing_api.append((rel, d))
            elif d not in have:
                other[d].add(rel)

    if bad_api:
        print("   X 引用了 Win7 不存在的组件（致命）：")
        for rel, d in bad_api[:20]:
            print("       %-46s <- %s" % (d, rel))
    else:
        print("   OK 无 Win8+ 专属组件引用")

    if missing_api:
        print("   X 引用的 api-ms-win-* 在包内找不到（致命）：")
        for rel, d in missing_api[:20]:
            print("       %-46s <- %s" % (d, rel))
    else:
        print("   OK 所有 api-ms-win-* 引用均能在包内解析")

    print()
    print("【4】其它未随包附带的依赖（应为 Win7 自带的系统 DLL）")
    sysdll = sorted(d for d in other if not d.endswith(".pyd"))
    for d in sysdll:
        print("     %-42s 被 %d 个文件引用" % (d, len(other[d])))
    print("   共 %d 个（若其中有非系统 DLL，需要补进包里）" % len(sysdll))

    ok = not bad_api and not missing_api
    print()
    print("=" * 68)
    print("结论: %s" % ("Win7 依赖闭合，可交付" if ok else "存在问题，需修复"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
