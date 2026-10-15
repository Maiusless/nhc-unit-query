"""扫描 PyInstaller 打包产物内部，查找可能泄漏的敏感信息。

为什么要做：源码里删掉的本机路径 / 令牌文件名，仍可能残留在**已编译的字节码**里
（PyInstaller 会把 .pyc 压进 PYZ 归档），公开分发前必须确认干净。

做法：用 PyInstaller 自带的读取器解开 CArchive（外层）与 PYZ（内层），
把每个条目的字节流按 UTF-8 / UTF-16 解码后做模式匹配。

用法：
    uv tool run --from pyinstaller python tools/leakscan.py <exe> [...]
    # 或先装好 pyinstaller 的任意 Python：
    python tools/leakscan.py release/卫健查询工具.exe

退出码：0 = 未发现泄漏；1 = 命中可疑内容
"""
import os
import re
import sys
import tempfile

# (正则, 说明) —— 命中即打印
PATTERNS = [
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub Token"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub OAuth Token"),
    (r"ghs_[A-Za-z0-9]{20,}", "GitHub App Token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub PAT"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI 风格密钥"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"[A-Za-z]:\\Users\\[A-Za-z0-9_.\-]+", "Windows 用户目录"),
    (r"/Users/[A-Za-z0-9_.\-]+/", "macOS 用户目录"),
    (r"/home/[A-Za-z0-9_.\-]+/", "Linux 用户目录"),
    (r"[A-Za-z0-9._%+\-]+@(?:gmail|qq|163|126|outlook|hotmail|foxmail)\.[A-Za-z]{2,}", "个人邮箱"),
    (r"(?i)authorization:\s*bearer\s+\S+", "硬编码 Bearer 头"),
]

# 允许出现、不必报警的字符串（避免误报）
ALLOW = (
    "users\\administrator\\appdata\\local\\temp",   # PyInstaller 运行期临时目录说明
    "python.org",
)

SKIP_EXT = (".onnx", ".png", ".jpg", ".ico", ".dll", ".pyd", ".so", ".exe")

# 第三方开源许可 / 声明文件：里面本来就有上游开发者的邮箱，
# 属于必须随包分发的许可内容，不是泄漏。
# base_library.zip 是 CPython 自带的标准库归档，同理。
SKIP_ENTRY = re.compile(
    r"(?i)(third.?party|licen[cs]e|notice|copying|authors|changelog|readme|"
    r"base_library\.zip)", re.I)


def scan_blob(name, data, hits):
    """在字节流里做模式匹配（同时尝试 UTF-8 与 UTF-16）。"""
    texts = []
    try:
        texts.append(data.decode("utf-8", "ignore"))
    except Exception:
        pass
    try:
        texts.append(data.decode("utf-16-le", "ignore"))
    except Exception:
        pass
    for text in texts:
        low = text.lower()
        for pat, label in PATTERNS:
            for m in set(re.findall(pat, text)):
                if any(a in m.lower() for a in ALLOW):
                    continue
                hits.append((name, label, m[:100]))


def iter_entries(exe):
    """逐个产出 (条目名, 字节) —— 含 PYZ 内部的每个模块。"""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    ca = CArchiveReader(exe)
    for name in list(ca.toc):
        try:
            data = ca.extract(name)
        except Exception:
            continue
        if not data:
            continue
        yield name, data
        if not name.lower().endswith(".pyz"):
            continue
        # 把内嵌的 PYZ 落盘，再用 PyInstaller 的读取器逐个取出模块字节码
        tmp = os.path.join(tempfile.gettempdir(), "_leakscan_%d.pyz" % abs(hash(name)))
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            za = ZlibArchiveReader(tmp)
            for mod in list(za.toc):
                try:
                    blob = za.extract(mod)
                except Exception:
                    continue
                if blob:
                    yield "PYZ::" + mod, blob
        except Exception as e:
            print("  ! PYZ 解析失败: %s" % e)
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass


def scan_exe(exe):
    print("=" * 70)
    print("扫描: %s (%.1f MB)" % (os.path.basename(exe), os.path.getsize(exe) / 1048576))
    hits, n = [], 0
    skipped = 0
    for name, data in iter_entries(exe):
        n += 1
        if name.lower().endswith(SKIP_EXT):
            continue
        if SKIP_ENTRY.search(os.path.basename(name)):
            skipped += 1
            continue
        scan_blob(name, data, hits)
    print("  条目数: %d（跳过第三方许可文件 %d 个）" % (n, skipped))
    if hits:
        seen = set()
        for name, label, frag in hits:
            key = (label, frag)
            if key in seen:
                continue
            seen.add(key)
            print("  !! [%s] %s" % (label, frag))
            print("       位置: %s" % name)
    else:
        print("  未发现可疑内容 ✅")
    return hits


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/leakscan.py <exe> [...]")
        return 2
    total = []
    for exe in sys.argv[1:]:
        if not os.path.isfile(exe):
            print("跳过（不存在）: %s" % exe)
            continue
        total += scan_exe(exe)
    print()
    print("=" * 70)
    print("结论: %s" % ("发现 %d 处可疑内容" % len(total) if total else "全部干净 ✅"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
