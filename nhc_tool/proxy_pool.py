"""Webshare 代理池：读取令牌 -> 拉取 IP 列表 -> 维护可用性。"""
# 注解延迟求值：本文件用到 `list[Proxy]` / `Proxy | None`（PEP 585/604），
# Python 3.8 运行时无法直接求值。
from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.request

from . import config

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def parse_token(text: str) -> str:
    """从令牌文件内容里提取 token。"""
    text = text.strip()
    if not text:
        return ""
    # 形如： 令牌：xxxx   或   token: xxxx
    m = re.search(r"(?:令牌|token)\s*[:：]\s*([A-Za-z0-9_\-]{20,})", text, re.I)
    if m:
        return m.group(1)
    # 整段就是一个 token
    m = re.search(r"\b([A-Za-z0-9]{28,40})\b", text)
    if m:
        return m.group(1)
    # 可能是 url 里的参数
    m = re.search(r"[?&](?:token|key)=([A-Za-z0-9_\-]{20,})", text)
    return m.group(1) if m else ""


def load_token(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return parse_token(f.read())


class Proxy:
    __slots__ = ("address", "port", "username", "password", "country",
                 "url", "ok", "last_used", "fails", "cooldown_until")

    def __init__(self, address, port, username, password, country=""):
        self.address = address
        self.port = int(port)
        self.username = username
        self.password = password
        self.country = country
        self.url = "http://%s:%s@%s:%d" % (username, password, address, self.port)
        self.ok = True
        self.last_used = 0.0
        self.fails = 0
        self.cooldown_until = 0.0

    @property
    def label(self) -> str:
        return "%s:%d %s" % (self.address, self.port, self.country)

    def __repr__(self):
        return "<Proxy %s>" % self.label


class ProxyPool:
    """从 Webshare 拉取代理，并做简单的轮换 / 冷却管理。"""

    def __init__(self, token: str):
        self.token = token
        self.proxies: list[Proxy] = []
        self._lock = threading.Lock()
        self._rr = 0

    # ---------- 拉取 ----------
    def refresh(self, page_size: int = 100) -> list[Proxy]:
        out: list[Proxy] = []
        page = 1
        while True:
            url = "%s?mode=direct&page=%d&page_size=%d" % (config.WEBSHARE_API, page, page_size)
            req = urllib.request.Request(url, headers={
                "Authorization": "Token " + self.token,
                "Accept": "application/json",
            })
            raw = urllib.request.urlopen(req, timeout=30, context=_CTX).read().decode()
            js = json.loads(raw)
            for p in js.get("results", []):
                out.append(Proxy(p["proxy_address"], p["port"], p["username"],
                                 p["password"], p.get("country_code", "")))
            nxt = js.get("next")
            if not nxt or len(out) >= int(js.get("count", 0)):
                break
            page += 1
            if page > 50:
                break
        with self._lock:
            self.proxies = out
        return out

    # ---------- 调度 ----------
    def acquire(self, min_interval: float = 0.0) -> Proxy | None:
        """取一个当前可用（不在冷却中、且距上次使用超过 min_interval）的代理。"""
        now = time.time()
        with self._lock:
            cands = [p for p in self.proxies if p.ok and p.cooldown_until <= now]
            if not cands:
                return None
            # 优先选最久没用过的
            cands.sort(key=lambda p: p.last_used)
            for p in cands:
                if now - p.last_used >= min_interval:
                    p.last_used = now
                    return p
            # 全都刚用过，返回最早那个（调用方自行 sleep）
            p = cands[0]
            p.last_used = now
            return p

    def cool_down(self, proxy: Proxy, seconds: float):
        with self._lock:
            proxy.cooldown_until = max(proxy.cooldown_until, time.time() + seconds)

    def mark_bad(self, proxy: Proxy, seconds: float = 600):
        with self._lock:
            proxy.fails += 1
            if proxy.fails >= 3:
                proxy.ok = False
            proxy.cooldown_until = max(proxy.cooldown_until, time.time() + seconds)

    def stats(self) -> str:
        with self._lock:
            alive = sum(1 for p in self.proxies if p.ok)
            return "代理 %d 个（可用 %d）" % (len(self.proxies), alive)
