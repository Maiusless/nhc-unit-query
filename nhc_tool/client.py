"""站点客户端。

两种出口模式：
  * xff  —— 直连，但每次请求伪造 X-Forwarded-For（实测服务端以其为客户端 IP，
             可绕过单 IP 每日额度与 60 秒间隔限制，无需代理）
  * proxy —— 走代理池（保留作为兜底）
"""
# 让注解延迟求值：本文件用到 `str | None`（PEP 604），Python 3.8 运行时无法直接求值。
# 加这一行后，同样的源码在 3.8 / 3.9+ 上都能正常工作。
from __future__ import annotations

import base64
import hashlib
import json
import random
import socket
import threading
import uuid

import requests

from . import config

_UA_HEADERS = {
    "User-Agent": config.UA,
    "Referer": config.BASE + "/unit",
    "Origin": config.BASE,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_PRIVATE = (10, 127, 172, 192, 169, 100, 198)


def random_ip() -> str:
    while True:
        a = random.randint(1, 223)
        if a in _PRIVATE:
            continue
        return "%d.%d.%d.%d" % (a, random.randint(0, 255),
                                random.randint(0, 255), random.randint(1, 254))


def machine_base_ip() -> str:
    """由本机标识（主机名 + 网卡 MAC）推导的稳定「默认初始 IP」。

    同一台机器每次启动结果一致；换机器则不同。用于会话起始 IP。
    """
    try:
        seed = "%s|%s" % (socket.gethostname(), uuid.getnode())
    except Exception:
        seed = "nhc-default"
    h = hashlib.md5(seed.encode("utf-8")).digest()
    a = 1 + h[0] % 223
    tries = 0
    while a in _PRIVATE and tries < 8:
        a = 1 + (a * 7 + 13) % 223
        tries += 1
    return "%d.%d.%d.%d" % (a, h[1], h[2], 1 + h[3] % 254)


class SessionIP:
    """会话级 IP 管理。

    * 程序启动时重置为「本机默认初始 IP」——进程重启即回到初始值。
    * 查询仍按次使用新的伪造 IP（这是绕过每日额度与 60 秒间隔的关键，
      不能固定）。
    * 详情抓取优先复用当前 IP，连续多次失败后才更换，尽量保证原有 IP。
    """

    def __init__(self):
        self.base = machine_base_ip()
        self.current = self.base
        self._used = False

    def reset(self):
        """重启会话：回到本机默认初始 IP。"""
        self.current = self.base
        self._used = False
        return self.current

    def next_query_ip(self):
        """查询用 IP：会话首个查询用本机默认初始 IP，之后每次轮换新 IP。

        必须轮换——固定 IP 会立刻撞上「每日额度」和「60 秒间隔」。
        """
        if not self._used:
            self._used = True
            return self.current
        self.current = random_ip()
        return self.current

    def detail_ip(self):
        return self.current

    def rotate(self):
        self.current = random_ip()
        return self.current

    def label(self):
        return self.current


# 进程级单例：每次启动都是新的，等价于「重启软件即重置」
SESSION_IP = SessionIP()


class SiteResult:
    OK = "ok"
    CAPTCHA = "captcha"
    LIMITED = "limited"
    INTERVAL = "interval"
    KEYWORD = "keyword"
    ERROR = "error"

    def __init__(self, status, msg="", data=None):
        self.status = status
        self.msg = msg
        self.data = data

    @property
    def ok(self):
        return self.status == self.OK

    def __repr__(self):
        return "<SiteResult %s %s>" % (self.status, self.msg)


# 一旦确认「系统代理不可用」，后续所有请求直接绕过代理。
# 很多机器装了代理软件（只对境外流量生效），系统代理会让本站请求直接失败；
# 首次失败后记住结论，避免每条查询都白等一次代理超时。
_DIRECT_ONLY = False


def _looks_like_proxy_error(exc) -> bool:
    """判断异常是否由系统代理引起。"""
    if isinstance(exc, requests.exceptions.ProxyError):
        return True
    return "proxy" in (str(exc) or "").lower()


class SiteClient:
    """一次查询 = 一个客户端实例（绑定一个伪造 IP 或一个代理）。"""

    _local = threading.local()

    def __init__(self, proxy_url: str | None = None, xff: str | None = None,
                 timeout: int | None = None):
        s = getattr(SiteClient._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update(_UA_HEADERS)
            SiteClient._local.s = s
        self.s = s
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.proxy_url = proxy_url
        self.xff = xff
        self.s.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}

    # ---------- 底层 ----------
    def _headers(self, ct=None, extra=None):
        h = {}
        if ct:
            h["Content-Type"] = ct
        if self.xff:
            h["X-Forwarded-For"] = self.xff
            h["X-Real-IP"] = self.xff
            h["Client-IP"] = self.xff
        if extra:
            h.update(extra)
        return h

    def _direct_session(self):
        """不走系统/环境代理的会话（系统代理不可用时兜底）。"""
        s = getattr(SiteClient._local, "ds", None)
        if s is None:
            s = requests.Session()
            s.headers.update(_UA_HEADERS)
            s.trust_env = False
            s.proxies = {}
            SiteClient._local.ds = s
        return s

    def _post(self, path, *, json_body=None, form=None, headers=None, method="POST"):
        url = config.DATA + path

        def once(sess):
            if method == "GET":
                return sess.get(url, headers=self._headers(extra=headers), timeout=self.timeout)
            if json_body is not None:
                return sess.post(url, data=json.dumps(json_body, ensure_ascii=False).encode("utf-8"),
                                 headers=self._headers("application/json", headers),
                                 timeout=self.timeout)
            return sess.post(url, data=form or {},
                             headers=self._headers("application/x-www-form-urlencoded", headers),
                             timeout=self.timeout)

        global _DIRECT_ONLY
        sess = self._direct_session() if _DIRECT_ONLY else self.s
        try:
            r = once(sess)
        except requests.RequestException as e:
            # 系统代理不可用 -> 记住结论并改直连重试一次
            if self.proxy_url or _DIRECT_ONLY or not _looks_like_proxy_error(e):
                return None, "NET:" + type(e).__name__
            _DIRECT_ONLY = True
            try:
                r = once(self._direct_session())
            except requests.RequestException as e2:
                return None, "NET:" + type(e2).__name__
        if r.status_code != 200:
            return None, "HTTP%d" % r.status_code
        try:
            return r.json(), None
        except Exception:
            return None, "BADJSON"

    # ---------- 业务 ----------
    def get_enddate(self):
        js, err = self._post(config.EP_ENDDATE, method="GET")
        return SiteResult(SiteResult.ERROR, err) if err else SiteResult(SiteResult.OK, data=js.get("data"))

    def get_provinces(self):
        js, err = self._post(config.EP_PROVINCE, form={})
        if err:
            return SiteResult(SiteResult.ERROR, err)
        if js.get("code") == "200":
            return SiteResult(SiteResult.OK, data=js.get("data"))
        return SiteResult(SiteResult.ERROR, js.get("msg", ""))

    def get_captcha(self):
        """返回 (token, word_list, png_bytes) 或 SiteResult(错误)。"""
        js, err = self._post(config.EP_CAPTCHA_IMG,
                             json_body={"width": config.CAP_W, "height": config.CAP_H})
        if err:
            return SiteResult(SiteResult.ERROR, err)
        if js.get("code") != "200":
            return SiteResult(SiteResult.ERROR, js.get("msg", ""))
        d = js.get("data") or {}
        return d.get("token"), d.get("wordList") or [], base64.b64decode(d.get("originalImageBase64", ""))

    def check_captcha(self, token: str, points: list) -> bool:
        js, err = self._post(config.EP_CAPTCHA_CHECK,
                             form={"token": token,
                                   "pointJson": json.dumps(points, separators=(",", ":"))})
        return (not err) and js.get("code") == "200"

    def query_unit(self, province_code: str, unit_name: str, captcha_token: str, points: list):
        h = {"checkWordToken": captcha_token,
             "pointJson": json.dumps(points, separators=(",", ":"))}
        js, err = self._post(config.EP_UNIT_LIST,
                             json_body={"provinceCode": province_code, "unitName": unit_name},
                             headers=h)
        if err:
            return SiteResult(SiteResult.ERROR, err)
        code, msg = js.get("code"), js.get("msg", "")
        if code == "200":
            return SiteResult(SiteResult.OK, msg, js.get("data"))
        if "验证码" in msg:
            return SiteResult(SiteResult.CAPTCHA, msg)
        if "受限" in msg or "明天" in msg:
            return SiteResult(SiteResult.LIMITED, msg)
        if "间隔" in msg:
            return SiteResult(SiteResult.INTERVAL, msg)
        if "关键字" in msg or "精确" in msg:
            return SiteResult(SiteResult.KEYWORD, msg)
        return SiteResult(SiteResult.ERROR, msg)

    def get_ticket(self, unit_guid: str, req_time: str):
        js, err = self._post(config.EP_TICKET, form={}, headers={"id": unit_guid, "reqTime": req_time})
        if err:
            return SiteResult(SiteResult.ERROR, err)
        if js.get("code") == "200":
            return SiteResult(SiteResult.OK, data=js.get("data"))
        return SiteResult(SiteResult.ERROR, js.get("msg", ""))

    def get_detail(self, ticket: str):
        js, err = self._post(config.EP_UNIT_DETAIL + ticket, form={})
        if err:
            return SiteResult(SiteResult.ERROR, err)
        if js.get("code") == "200":
            return SiteResult(SiteResult.OK, data=js.get("data"))
        return SiteResult(SiteResult.ERROR, js.get("msg", ""))
