"""全局配置与常量。"""
import os
import sys

APP_NAME = "卫健信用查询工具"
APP_VER = "1.0.0"

BASE = "https://zgcx.nhc.gov.cn"
DATA = BASE + "/DATA"

# 站点接口（已实测）
EP_ENDDATE = "/searchdate/enddate"           # GET  数据截止时间
EP_PROVINCE = "/province/list"               # POST form -> [{code,value}]
EP_CAPTCHA_IMG = "/image/validate/post4ValidImg"   # POST json {width,height}
EP_CAPTCHA_CHECK = "/image/validate/check"         # POST form {token,pointJson}
EP_UNIT_LIST = "/web/unit/list"              # POST json {provinceCode,unitName} + 头 checkWordToken/pointJson
EP_TICKET = "/post4Ticket"                   # POST 头 id/reqTime -> ticket
EP_UNIT_DETAIL = "/web/unit/detail/"         # POST + ticket

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 验证码画布尺寸（服务端按此尺寸返回图片，点击坐标即此坐标系）
CAP_W, CAP_H = 300, 150

# 站点限流
MIN_INTERVAL = 60.0          # 同一 IP 两次查询最小间隔（秒）
REQUEST_TIMEOUT = 40
MAX_RETRY = 3

# Webshare
WEBSHARE_API = "https://proxy.webshare.io/api/v2/proxy/list/"

# 默认令牌文件：留空 = 不预设，由用户自行选择。
# 用户选过的路径会记录在数据目录的 ui.json 里，下次自动带出；
# 命令行请用 -f 指定（缺失时会给出提示并以退出码 2 结束）。
DEFAULT_TOKEN_FILE = ""


def app_dir() -> str:
    """程序所在目录（打包后为 exe 同级目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _documents_dir() -> str:
    """Windows「文档」文件夹（自动处理重定向 / 中文系统）。

    读注册表 User Shell Folders\\Personal，比硬编码 %USERPROFILE%\\Documents
    更可靠（用户可能把文档目录改到 D 盘）。
    """
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        v, _ = winreg.QueryValueEx(k, "Personal")
        winreg.CloseKey(k)
        p = os.path.expandvars(v)
        if p:
            return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


_DATA_DIR = None


def data_dir() -> str:
    """可写数据目录（首次运行时自动创建）。

    依次尝试，全部都不需要管理员权限：
      1. 用户「文档」\\<APP_NAME>
      2. %LOCALAPPDATA%\\<APP_NAME>
      3. exe 同级 nhc_data（绿色版兜底）
      4. 用户主目录
    """
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR
    cands = [
        os.path.join(_documents_dir(), APP_NAME),
        os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME),
        os.path.join(app_dir(), "nhc_data"),
        os.path.join(os.path.expanduser("~"), APP_NAME),
    ]
    for d in cands:
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                _DATA_DIR = d
                return d
        except Exception:
            continue
    _DATA_DIR = os.path.join(os.path.expanduser("~"), APP_NAME)
    return _DATA_DIR
