"""扫描 PE 文件的导入表，找出 Windows 7 上无法加载的依赖。

判定规则（客观、可复现）：
  1) Windows 7 上只有 KB2999226(UCRT) 提供的 api-ms-win-crt-*.dll；
     其余 api-ms-win-* 全是 Windows 8 才引入的 API 集，Win7 上不存在
     -> 只要某个 PE 导入它们，加载时就会报「找不到指定的模块」。
  2) 部分 DLL 本身是 Win8+ 才有的。
  3) 部分 kernel32 导出函数是 Win8+/Win10+ 才有的。

用法：
    python tools/win7check.py <目录或文件> [...]
"""
import os
import sys

import pefile

# ---- 规则 1：Win7 上不存在的 API 集 ----
API_SET_PREFIX = "api-ms-win-"
UCRT_API_SET_PREFIX = "api-ms-win-crt-"      # Win7 装了 UCRT 补丁后有

# ---- 规则 2：Win8+ 才有的普通 DLL ----
WIN8_ONLY_DLLS = {
    "d3d12.dll", "dxcore.dll", "dcomp.dll", "windows.web.dll",
    "windows.data.dll", "windows.storage.dll", "winrt.dll",
}

# ---- 规则 3：Win8+ 才有的 kernel32 导出（Win7 上不存在） ----
WIN8_KERNEL32_EXPORTS = {
    "GetSystemTimePreciseAsFileTime", "PathCchCanonicalizeEx",
    "PathCchCombineEx", "PathCchAppendEx", "PathCchAddBackslashEx",
    "GetPackageFamilyName", "GetPackageFullName", "SetThreadDescription",
    "WaitOnAddress", "WakeByAddressSingle", "WakeByAddressAll",
    "GetCurrentPackageFullName", "GetTempPath2W", "GetTempPath2A",
    "SetProcessMitigationPolicy", "GetProcessMitigationPolicy",
    "CreateFile2", "CompareStringOrdinal", "CopyFile2",
}

# ---- 需要一起打包的运行时（Win7 上可能缺失，但可随程序附带） ----
BUNDLE_NEEDED = ("ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                 "msvcp140.dll", "concrt140.dll")

SKIP_DIRS = {"build", "__pycache__", ".git"}


def scan(path):
    """返回 (blockers, bundle_needed, missing_ok)。"""
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                         pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    except Exception as e:
        return None, None, "解析失败: %s" % e

    imports = {}          # dll(小写) -> set(函数名)
    try:
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            dll = (entry.dll or b"").decode("ascii", "ignore").lower()
            imports.setdefault(dll, set())
            for imp in entry.imports:
                if imp.name:
                    imports[dll].add(imp.name.decode("ascii", "ignore"))
        for entry in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
            dll = (entry.dll or b"").decode("ascii", "ignore").lower()
            imports.setdefault(dll, set())
            for imp in entry.imports:
                if imp.name:
                    imports[dll].add(imp.name.decode("ascii", "ignore"))
    except Exception:
        pass
    finally:
        pe.close()

    blockers, bundle = [], set()
    for dll in sorted(imports):
        if dll.startswith(API_SET_PREFIX) and not dll.startswith(UCRT_API_SET_PREFIX):
            blockers.append("API集 %s" % dll)
        elif dll in WIN8_ONLY_DLLS:
            blockers.append("DLL %s" % dll)
        elif dll in BUNDLE_NEEDED:
            bundle.add(dll)
    for dll in ("kernel32.dll", "kernelbase.dll"):
        hit = imports.get(dll, set()) & WIN8_KERNEL32_EXPORTS
        for fn in sorted(hit):
            blockers.append("函数 %s!%s" % (dll, fn))
    return sorted(set(blockers)), sorted(bundle), None


def collect(targets):
    files = []
    for t in targets:
        if os.path.isfile(t):
            files.append(t)
        else:
            for root, dirs, names in os.walk(t):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for n in names:
                    if n.lower().endswith((".exe", ".dll", ".pyd", ".so")):
                        files.append(os.path.join(root, n))
    return sorted(files)


def main():
    targets = sys.argv[1:]
    if not targets:
        print("用法: python tools/win7check.py <目录或文件> [...]")
        return 2
    files = collect(targets)
    print("扫描 %d 个 PE 文件" % len(files))
    print()
    bad, need = [], set()
    for f in files:
        blockers, bundle, err = scan(f)
        rel = os.path.relpath(f)
        if err:
            print("  ?  %-58s %s" % (rel[:58], err))
            continue
        need.update(bundle)
        if blockers:
            bad.append((rel, blockers))
            print("  X  %s" % rel)
            for b in blockers:
                print("        - %s" % b)
    print()
    print("=" * 66)
    if bad:
        print("Win7 不兼容: %d / %d 个文件有问题" % (len(bad), len(files)))
    else:
        print("Win7 兼容: 全部 %d 个文件均无 Win8+ 依赖" % len(files))
    print("需要随程序附带的运行时: %s" % (", ".join(sorted(need)) or "无"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
