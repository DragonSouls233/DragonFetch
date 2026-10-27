# -*- coding: utf-8 -*-
"""统一的 HTTP 客户端：带 socks5h 代理 + Referer + 重试 + 关证书校验。"""
import re
import ssl
import threading
import time as _time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from . import config
from . import logger

# ⭐ 超时后仍在后台跑的"孤儿线程"治理参数
#    场景：`_threaded_get` 等不到结果就返回超时，但那个线程**还在读 socket**。
#    如果此时把共享 Session 关掉，SSL/连接对象会被释放 → 读线程踩野指针 →
#    Windows fatal exception 0x80010108 → 整个进程闪退（2026-10-24 服务器实测）。
MAX_ORPHANS = 6          # 同时存活的孤儿线程上限（超过就不再重试，别再制造）
ORPHAN_JOIN_BUDGET = 5.0  # close() 时等待孤儿退出的总预算（秒）


# HTML/XML 内声明的编码（比 HTTP 头可靠：头里常常压根没有 charset）
_XML_ENC_RE = re.compile(rb"<\?xml[^>]+encoding=[\"']([\w\-]+)[\"']", re.I)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""", re.I)


def _decode_text(content: bytes, header_encoding=None) -> str:
    """把响应字节解成文本。

    ⚠️ 不能直接用 ``requests`` 的 ``r.text``：Astro SSR 站点返回的 HTML
    响应头里**不带 charset**，requests 便按 RFC 2616 回退 ``ISO-8859-1``，
    于是 UTF-8 的中日文昵称被逐字节解码，显示成 ``æ‰‹º¤μ†`` 这种乱码
    （演员云实测：FC2 / JB_Diary 等 ASCII 名正常，中文日文名全乱）。

    优先级：HTML/XML 内声明 > 响应头 charset > UTF-8 兜底。
    """
    enc = None
    m = _XML_ENC_RE.search(content[:4096]) or _META_CHARSET_RE.search(content[:4096])
    if m:
        enc = m.group(1).decode("ascii", "ignore")
    elif header_encoding:
        # requests 对无 charset 的 text/* 默认给 iso-8859-1 —— 这个默认值不可信
        if header_encoding.lower() not in ("iso-8859-1", "latin-1", "latin1", "none"):
            enc = header_encoding
    if not enc:
        enc = "utf-8"
    for cand in (enc, "utf-8", "gb18030"):
        try:
            return content.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("latin-1", errors="replace")


def _build_session(proxy: bool) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": config.UA,
        "Referer": config.REFERER,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    retry = Retry(
        total=config.RETRIES,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    if proxy:
        s.proxies.update({"http": config.PROXY, "https": config.PROXY})
    return s


class HttpClient:
    """区分 SITE(需代理) 与 CDN(默认也代理) 两种会话。

    注意：requests 的 session.proxies 在部分环境下会被忽略（实测走直连导致
    SSL EOF），因此这里把代理以「每次请求参数」形式传入，与已验证可用的探针一致。
    """

    def __init__(self):
        self.site = _build_session(proxy=True)
        self.cdn = _build_session(proxy=config.CDN_USE_PROXY)
        self._site_proxies = {"http": config.PROXY, "https": config.PROXY} if True else None
        self._cdn_proxies = ({"http": config.PROXY, "https": config.PROXY}
                             if config.CDN_USE_PROXY else None)
        # ⭐ 超时后仍在后台跑的"孤儿线程"登记表（见文件头说明）。
        #    close() 必须先等它们退出，再关共享 Session，否则 use-after-free 闪退。
        self._inflight = []
        self._inflight_lock = threading.Lock()
        self._closed = False

    # ---------------- 孤儿线程登记 ----------------
    def _remember_inflight(self, th):
        with self._inflight_lock:
            self._inflight = [t for t in self._inflight if t.is_alive()]
            self._inflight.append(th)

    def _forget_inflight(self, th):
        with self._inflight_lock:
            self._inflight = [t for t in self._inflight if t is not th]

    def _live_orphans(self):
        with self._inflight_lock:
            self._inflight = [t for t in self._inflight if t.is_alive()]
            return len(self._inflight)

    def _join_orphans(self, budget=ORPHAN_JOIN_BUDGET):
        """等待孤儿线程退出，返回**仍然活着**的数量。"""
        end = _time.time() + budget
        with self._inflight_lock:
            threads = list(self._inflight)
        for th in threads:
            left = end - _time.time()
            if left <= 0:
                break
            try:
                th.join(timeout=left)
            except Exception:
                pass
        return self._live_orphans()

    def get_text(self, url: str, is_cdn: bool = False) -> str:
        return self._request(url, is_cdn, as_bytes=False)

    def get_bytes(self, url: str, is_cdn: bool = False) -> bytes:
        return self._request(url, is_cdn, as_bytes=True)

    # ---------- 代理控制 ----------
    def set_proxy(self, proxy: str, for_cdn: bool = True) -> None:
        """切换代理（用于按任务固定到某个节点专属入口）。"""
        if not proxy:
            return
        self._site_proxies = {"http": proxy, "https": proxy}
        if for_cdn:
            self._cdn_proxies = {"http": proxy, "https": proxy}
        self.site.proxies.update({"http": proxy, "https": proxy})
        if for_cdn:
            self.cdn.proxies.update({"http": proxy, "https": proxy})

    def proxy_url(self, cdn: bool = False) -> str:
        """返回当前生效的代理 URL。"""
        p = (self._cdn_proxies if cdn else self._site_proxies) or {}
        return p.get("https") or p.get("http") or config.PROXY

    def _request(self, url: str, is_cdn: bool, as_bytes: bool):
        import time as _t
        s = self.cdn if is_cdn else self.site
        proxies = self._cdn_proxies if is_cdn else self._site_proxies
        wall = max(config.TIMEOUT, 25)  # 防止 socks 代理卡死无超时
        if self._closed:
            raise RuntimeError("HTTP 客户端已关闭")

        def _threaded_get():
            out = {}

            def run():
                try:
                    r = s.get(url, timeout=config.TIMEOUT, verify=False,
                              proxies=proxies)
                    r.raise_for_status()
                    if as_bytes:
                        out["data"] = r.content
                    else:
                        # 自行判定编码：r.text 会回退 ISO-8859-1 导致中文乱码
                        out["data"] = _decode_text(r.content, r.encoding)
                except Exception as e:  # 代理偶发卡死/超时，重试以自愈
                    out["err"] = e
                finally:
                    self._forget_inflight(th)

            th = threading.Thread(target=run, daemon=True)
            th.start()
            th.join(timeout=wall)
            if "data" in out:
                return out["data"], None
            if "err" in out:
                return None, out["err"]
            # ⭐ 超时：这个线程**还在跑**（可能正卡在 SSL 读）⇒ 登记为孤儿。
            #    绝不能让它读到已被 close 的连接（那会 use-after-free 闪退）。
            self._remember_inflight(th)
            return None, TimeoutError("请求在 %ds 内无响应（疑似代理卡死）" % wall)

        last = None
        for attempt in range(config.RETRIES + 1):
            data, err = _threaded_get()
            if err is None:
                return data
            last = err
            if attempt < config.RETRIES:
                # ⭐ 孤儿线程太多就别再重试了：每重试一次就多留一个孤儿，
                #    全部挤在同一个共享 Session 上，越多越容易踩 use-after-free。
                if self._live_orphans() >= MAX_ORPHANS:
                    logger.get_logger().warning(
                        "HTTP 孤儿线程已达 %d 个（上限），放弃重试：%s",
                        MAX_ORPHANS, url)
                    raise last
                _t.sleep(1.0 + attempt)
        raise last

    def close(self):
        """关闭会话。

        ⚠️ 顺序是关键：必须**先等孤儿线程退出**（它们还持有共享 Session 的
        socket 正在读），再关 Session。反过来就会把 SSL/连接对象释放掉，
        读线程踩野指针 → Windows fatal exception 0x80010108 → 进程闪退。
        """
        self._closed = True
        alive = self._join_orphans()
        if alive:
            # 还有线程在读：**宁可漏掉连接也不关**（进程退出时由 OS 回收），
            # 绝不能用 use-after-free 换一次干净关闭。
            logger.get_logger().warning(
                "仍有 %d 个 HTTP 线程在读连接，跳过关闭 Session（避免闪退）", alive)
            return
        try:
            self.site.close()
        except Exception:
            pass
        try:
            self.cdn.close()
        except Exception:
            pass


# 关闭 urllib3 的证书警告（我们故意 verify=False）
try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass
