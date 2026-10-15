"""查询调度引擎。

默认 xff 模式：每条查询使用一个随机伪造 IP，服务端据此判定客户端 IP，
因此每日额度与 60 秒间隔限制均不生效 —— 可无限制并发查询。

proxy 模式保留作为兜底（若站点修复 XFF 信任问题）。
"""
# 注解延迟求值：本文件用到 `str | None`（PEP 604），Python 3.8 运行时无法直接求值。
from __future__ import annotations

import json
import os
import threading
import time

from . import captcha as CAP
from . import config
from .client import SiteClient, SiteResult


# ---------------- 任务 / 结果 ----------------
class Task:
    __slots__ = ("province_code", "province_name", "keyword", "status", "msg",
                 "units", "ip", "tries", "ts", "detail")

    def __init__(self, province_code, province_name, keyword):
        self.province_code = province_code
        self.province_name = province_name
        self.keyword = keyword
        self.status = "pending"      # pending / ok / empty / invalid / error
        self.msg = ""
        self.units = []
        self.ip = ""
        self.tries = 0
        self.ts = ""
        self.detail = {}


class State:
    """记录当日已用尽的代理 IP（仅 proxy 模式使用）。"""

    def __init__(self, path=None):
        self.path = path or os.path.join(config.data_dir(), "state.json")
        self.limited = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.limited = json.load(f).get("limited", {})
        except Exception:
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"limited": self.limited}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def today():
        return time.strftime("%Y-%m-%d")

    def is_limited(self, ip):
        return self.limited.get(ip) == self.today()

    def mark_limited(self, ip):
        self.limited[ip] = self.today()
        self.save()


# ---------------- 引擎 ----------------
class Engine:
    def __init__(self, pool, tasks, *, mode="xff", workers=8, log=None, state=None,
                 on_result=None, on_progress=None, on_activity=None, solve_tries=15,
                 fetch_detail=False, allow_manual=True):
        self.pool = pool
        self.tasks = list(tasks)
        self.mode = mode
        self.workers = max(1, int(workers))
        self.log = log or (lambda *a, **k: None)
        self.state = state or State()
        self.on_result = on_result or (lambda t: None)
        self.on_progress = on_progress or (lambda *a: None)
        self.on_activity = on_activity or (lambda *a: None)
        self.solve_tries = int(solve_tries)
        self.fetch_detail = fetch_detail
        self.allow_manual = allow_manual

        self._lock = threading.Lock()
        self._q = list(self.tasks)
        self._stop = threading.Event()
        self._ths = []
        self.stats = {"done": 0, "ok": 0, "empty": 0, "invalid": 0, "error": 0,
                      "limited_ips": 0, "captcha_fail": 0}
    # ---------- 队列 ----------
    def _next(self):
        with self._lock:
            return self._q.pop(0) if self._q else None

    def _requeue(self, t):
        with self._lock:
            self._q.append(t)

    # ---------- 一次完整查询 ----------
    def _act(self, task, phase):
        try:
            self.on_activity(task.keyword, phase)
        except Exception:
            pass

    def _one_query(self, cli: SiteClient, task: Task):
        """返回 SiteResult（已含验证码重试）。"""
        for _ in range(self.solve_tries):
            if self._stop.is_set():
                return None
            self._act(task, "获取验证码")
            r = cli.get_captcha()
            if not isinstance(r, tuple):
                continue
            tok, wl, png = r
            if not tok:
                continue
            self._act(task, "识别验证码")
            pts, src = CAP.solve(png, wl, allow_manual=self.allow_manual)
            if not pts:
                continue
            if src == "manual":
                self.log("[%s] 已使用人工输入验证码" % task.keyword)
            self._act(task, "校验验证码")
            if not cli.check_captcha(tok, pts):
                continue
            self._act(task, "查询中")
            res = cli.query_unit(task.province_code, task.keyword, tok, pts)
            if res.status == SiteResult.CAPTCHA:
                continue
            return res
        return SiteResult(SiteResult.CAPTCHA, "验证码自动识别失败")

    # ---------- 取代理 ----------
    def _acquire_proxy(self):
        """返回 (proxy, err)。err 非空表示当前无可用代理。"""
        for _ in range(60):
            if self._stop.is_set():
                return None, "已停止"
            avail = [x for x in self.pool.proxies
                     if x.ok and not self.state.is_limited(x.address)]
            if not avail:
                self._stop.set()
                return None, "代理池所有 IP 当日额度已用尽，请明日再试或更换代理池"
            cand = self.pool.acquire(min_interval=config.MIN_INTERVAL)
            if cand is None:
                time.sleep(0.5)
                continue
            if self.state.is_limited(cand.address):
                continue
            return cand, ""
        return None, "代理池长时间无可用 IP"

    # ---------- 单条任务（可重试） ----------
    def _handle(self, task: Task):
        task.tries += 1
        if self.mode == "xff":
            from .client import SESSION_IP
            ip = SESSION_IP.next_query_ip()   # 首个查询用本机默认初始 IP
            cli = SiteClient(None, xff=ip)
            task.ip = ip
        else:
            p, err = self._acquire_proxy()
            if p is None:
                self.log(err)
                if err.startswith("代理池所有"):
                    task.status, task.msg = "error", err
                    self._finish(task)
                    return True
                return False
            cli = SiteClient(p.url)
            task.ip = p.address

        res = self._one_query(cli, task)
        if res is None:
            return False

        if res.status == SiteResult.OK:
            d = res.data or {}
            task.units = d.get("unitInfoList") or []
            task.status = "ok" if task.units else "empty"
            task.msg = ("共 %s 条，返回 %s 条"
                        % ((d.get("summary") or {}).get("total", 0), len(task.units)))
            task.ts = time.strftime("%H:%M:%S")
            if self.fetch_detail and task.units:
                self._fetch_details(cli, task)
            self._finish(task)
            return True

        if res.status == SiteResult.KEYWORD:
            task.status, task.msg = "invalid", res.msg
            self._finish(task)
            return True

        if res.status == SiteResult.LIMITED:
            if self.mode == "proxy":
                self.state.mark_limited(task.ip)
                self.stats["limited_ips"] += 1
                self.log("[%s] IP %s 额度用尽，换 IP" % (task.keyword, task.ip))
                return False
            return False   # xff 模式下换一个新伪造 IP 即可

        if res.status == SiteResult.INTERVAL:
            time.sleep(1.0)
            return False

        if res.status == SiteResult.CAPTCHA:
            self.stats["captcha_fail"] += 1
            self.log("[%s] 验证码未能通过，重试" % task.keyword)
            return False

        self.log("[%s] 请求异常：%s" % (task.keyword, res.msg))
        time.sleep(0.6)
        return False

    def _fetch_details(self, cli: SiteClient, task: Task):
        for u in task.units:
            self._act(task, "抓取详情")
            try:
                d = fetch_unit_detail_fresh(u.get("unitGuid", ""), u.get("reqTime", ""),
                                            task.province_code, task.keyword)
                if d:
                    task.detail[u.get("unitGuid", "")] = d
            except Exception:
                pass

    def _finish(self, task):
        kind = task.status if task.status in ("ok", "empty", "invalid", "error") else "error"
        with self._lock:
            self.stats["done"] += 1
            self.stats[kind] = self.stats.get(kind, 0) + 1
        self.log("[%s] %s %s" % (task.keyword, "完成" if kind in ("ok", "empty") else "结束", task.msg))
        self.on_result(task)
        self.on_progress()

    # ---------- 工作线程 ----------
    def _worker(self):
        while not self._stop.is_set():
            t = self._next()
            if t is None:
                return
            ok = False
            try:
                ok = self._handle(t)
            except Exception as e:
                self.log("[%s] 内部异常 %s" % (t.keyword, type(e).__name__))
            if not ok and not self._stop.is_set():
                if t.tries >= 6:
                    t.status, t.msg = "error", t.msg or "多次重试失败"
                    self._finish(t)
                else:
                    time.sleep(0.5)
                    self._requeue(t)

    def start(self):
        # 注意：这里不 reset()。会话 IP 只在「进程启动」时回到本机默认初始 IP
        # （SESSION_IP 是进程级单例，重启软件自然重置）。若每次开始查询都 reset，
        # 本机默认 IP 会被反复使用并累积额度，反而更快撞上限。
        self._ths = [threading.Thread(target=self._worker, daemon=True)
                     for _ in range(self.workers)]
        for t in self._ths:
            t.start()

    def join(self):
        for t in self._ths:
            t.join()

    def stop(self):
        self._stop.set()

    def progress(self):
        return self.stats["done"], len(self.tasks)


def fetch_unit_detail(unit_guid: str, req_time: str, xff: str | None = None,
                      tries: int = 9, switch_after: int = 3):
    """按需抓取单个机构详情（界面点开「详细」时使用）。

    票据与详情分两步请求，任一环节抖动都可能失败。策略：
      先复用当前会话 IP 连续重试 switch_after 次，仍失败才更换 IP；
      避免一有抖动就换 IP（用户要求尽量保证原有 IP）。
    """
    from .client import SESSION_IP
    ip = xff or SESSION_IP.detail_ip()
    streak = 0
    for _ in range(max(1, int(tries))):
        try:
            cli = SiteClient(None, xff=ip)
            t = cli.get_ticket(unit_guid, req_time)
            if t.ok and t.data:
                d = cli.get_detail(t.data)
                if d.ok and d.data:
                    return d.data
        except Exception:
            pass
        streak += 1
        if streak >= max(1, int(switch_after)):
            ip = SESSION_IP.rotate()
            streak = 0
    return None


_REQ_CACHE = {}          # (省份, 关键字) -> (时间戳, unitInfoList)
_REQ_TTL = 60.0          # 秒；票据有效期 2~5 分钟，缓存 60 秒是安全的


def requery_units(province_code: str, keyword: str, tries: int = 15):
    """重新执行一次查询，返回最新的 unitInfoList（其中的 reqTime 是新鲜的）。

    用于「详情票据 reqTime 过期」时换取新票据。同一批次的多个机构会命中
    短 TTL 缓存，避免逐条重复查询。
    """
    key = (province_code, keyword)
    now = time.time()
    hit = _REQ_CACHE.get(key)
    if hit and now - hit[0] < _REQ_TTL:
        return hit[1]

    from .client import SESSION_IP
    cli = SiteClient(None, xff=SESSION_IP.next_query_ip())
    for _ in range(max(1, int(tries))):
        r = cli.get_captcha()
        if not isinstance(r, tuple):
            continue
        tok, wl, png = r
        pts, _src = CAP.solve_local(png, wl)
        if not pts or not cli.check_captcha(tok, pts):
            continue
        res = cli.query_unit(province_code, keyword, tok, pts)
        if res.ok:
            units = res.data.get("unitInfoList") or []
            _REQ_CACHE[key] = (time.time(), units)
            return units
    return None


def fetch_unit_detail_fresh(unit_guid: str, req_time: str,
                            province_code: str = "", keyword: str = ""):
    """取机构详情，并对「reqTime 过期」做自愈。

    实测站点票据用的 reqTime 约 2~5 分钟后失效（过期后 post4Ticket 拿不到票据）。
    因此第一次失败时，用同样的省份+关键字重新查询一次，拿到该机构最新的
    reqTime 再取详情。
    """
    d = fetch_unit_detail(unit_guid, req_time, tries=4)
    if d:
        return d
    if not (province_code and keyword):
        return None
    units = requery_units(province_code, keyword)
    if not units:
        return None
    for u in units:
        if u.get("unitGuid") == unit_guid:
            return fetch_unit_detail(unit_guid, u.get("reqTime", ""), tries=6)
    return None
