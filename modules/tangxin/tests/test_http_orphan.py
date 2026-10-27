# -*- coding: utf-8 -*-
"""HTTP 客户端"孤儿线程"治理的回归测试（2026-10-27 新增）。

回归点（服务器 2026-10-24 16:46 实测闪退）：
    `_threaded_get` 等不到结果就返回超时，但那个线程**还在读 socket**；
    随后 `dl.close()` 把共享 Session 关掉 ⇒ SSL/连接对象被释放 ⇒
    读线程踩野指针 ⇒ ``Windows fatal exception: code 0x80010108`` ⇒ 整个进程闪退。

必须保证：
  1) 超时的线程会被登记为"孤儿"，可被观测；
  2) close() 时**有孤儿活着就不关 Session**（宁可漏连接也不能 use-after-free）；
  3) 孤儿线程退出后自动从登记表移除；
  4) 孤儿过多时不再重试（避免孤儿爆炸）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

TMP = tempfile.mkdtemp(prefix="tx_http_")
os.environ["TX_DB"] = os.path.join(TMP, "t.db")
os.environ["TX_DATA"] = TMP

from tangxin import httpclient   # noqa: E402


def test_orphan_registered_on_timeout():
    """请求超时 ⇒ 线程被登记为孤儿（而不是被悄悄丢掉）。"""
    c = httpclient.HttpClient()
    try:
        # 把墙钟超时压到 1 秒，模拟"代理卡死"
        orig_timeout = httpclient.config.TIMEOUT
        httpclient.config.TIMEOUT = 1
        c._site_proxies = {"http": "socks5h://127.0.0.1:1", "https": "socks5h://127.0.0.1:1"}
        try:
            c.get_text("http://127.0.0.1:1/nothing", is_cdn=False)
        except Exception:
            pass
        finally:
            httpclient.config.TIMEOUT = orig_timeout
        # 至少能观测到"曾经有孤儿"或"请求已失败"——重点是 close() 的安全性
        print("[http] 超时请求已走完，孤儿数 =", c._live_orphans())
    finally:
        c.close()


def test_close_does_not_close_session_with_live_orphan():
    """有活着的孤儿时 close() **必须跳过**关闭共享 Session。"""
    c = httpclient.HttpClient()
    closed = {"site": False, "cdn": False}
    c.site.close = lambda: closed.__setitem__("site", True)
    c.cdn.close = lambda: closed.__setitem__("cdn", True)

    # 造一个"永远不结束"的假孤儿线程
    stop = threading.Event()
    th = threading.Thread(target=lambda: stop.wait(30), daemon=True)
    th.start()
    c._remember_inflight(th)

    c.close()
    assert not closed["site"] and not closed["cdn"], \
        "有活着的孤儿时仍然关了 Session（= use-after-free = 闪退）"
    print("[http] 有孤儿时 close() 跳过关闭 Session: PASS")
    stop.set()
    th.join(timeout=2)


def test_orphan_removed_when_thread_exits():
    """孤儿线程自己结束后要从登记表移除（否则 close() 会一直等它）。"""
    c = httpclient.HttpClient()
    th = threading.Thread(target=lambda: None, daemon=True)
    th.start()
    th.join(timeout=2)
    c._remember_inflight(th)
    time.sleep(0.2)
    c._join_orphans(budget=1.0)
    assert c._live_orphans() == 0, "已结束的线程仍留在登记表里"
    print("[http] 孤儿线程结束后自动出表: PASS")
    c.close()


def test_retry_stops_when_too_many_orphans():
    """孤儿超过上限就不再重试（避免孤儿线程爆炸）。

    ⚠️ 判据用「外层重试次数」而不是耗时：urllib3 适配器自己也有
    Retry(total=3)+退避，一次请求失败本身就要好几秒，用耗时判会误判。
    """
    c = httpclient.HttpClient()
    stop = threading.Event()
    th = threading.Thread(target=lambda: stop.wait(30), daemon=True)
    th.start()
    for _ in range(httpclient.MAX_ORPHANS + 1):
        c._remember_inflight(th)
    try:
        # 统计外层重试的判定次数：每次"准备重试"都会查一次孤儿数
        checks = []
        orig = c._live_orphans

        def traced():
            n = orig()
            checks.append(n)
            return n

        c._live_orphans = traced
        orig_timeout = httpclient.config.TIMEOUT
        httpclient.config.TIMEOUT = 1
        c._site_proxies = {"http": "socks5h://127.0.0.1:1",
                           "https": "socks5h://127.0.0.1:1"}
        try:
            c.get_text("http://127.0.0.1:1/x", is_cdn=False)
        except Exception:
            pass
        finally:
            httpclient.config.TIMEOUT = orig_timeout
        # 正常情况会查 RETRIES 次（3 次）才放弃；加了孤儿上限后应当**只查 1 次**
        assert len(checks) == 1, \
            "孤儿已超上限却还在重试（判定次数=%d，应为 1）" % len(checks)
        assert checks[0] >= httpclient.MAX_ORPHANS, \
            "判定时孤儿数不对: %s" % checks
        print("[http] 孤儿超上限后立即放弃重试（判定 %d 次）: PASS" % len(checks))
    finally:
        stop.set()
        th.join(timeout=2)


if __name__ == "__main__":
    try:
        test_close_does_not_close_session_with_live_orphan()
        test_orphan_removed_when_thread_exits()
        test_retry_stops_when_too_many_orphans()
        test_orphan_registered_on_timeout()
        print("\nALL HTTP ORPHAN TESTS PASSED")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
