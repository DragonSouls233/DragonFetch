# -*- coding: utf-8 -*-
"""内置代理管理（exe 自带 Xray 内核，启动时自动拉起）。

与主平台 ``modules/core/v2ray_controller.py`` 保持同一套拓扑，但**不 import
主平台代码** —— 糖心是独立部署，自带内核 + 自带节点配置即可跑。

端口布局（与主平台一致）：
    10808  = balancer 入口（多出口 roundRobin），抓取/无状态请求用
    10809+ = 节点专属入口，第 i 个节点固定一个端口

⚠️ 为什么下载必须用专属入口、不能用 10808：
   balancer 的 roundRobin 是**逐连接**换出口的，而媒体直链里常带
   ``?ip=<出口IP>`` —— 页面走 A 节点、分片走 B 节点时 CDN 会直接拒。
   主平台线上就踩过（一开负载均衡下载全挂）。所以下载按任务**固定**一个
   专属入口，保证同一次下载的所有请求走同一个出口。

使用流程：
    1) 启动时 ``ensure_running()``：先探测 10808，已在监听就复用外部实例；
       否则生成配置 → ``xray -test`` 校验 → 拉起自带内核。
    2) 运行中定期 ``health_check()``，挂了就 ``restart()``（僵尸自愈）。
"""
import json
import os
import socket
import subprocess
import sys
import time

from . import config, logger

# 与主平台保持一致
SOCKS_PORT = 10808
LB_TAG = "proxy-balance"


def _bundle_root() -> str:
    """打包资源根目录。

    ⚠️ onefile 模式下 ``--add-binary`` / ``--add-data`` 的资源会被解压到
       ``sys._MEIPASS`` 指向的**临时目录**，而不是 exe 同级目录 —— 所以找
       Xray 内核 / 节点配置时必须同时看这里，否则打包后一律「找不到内核」。
    """
    return getattr(sys, "_MEIPASS", "") or config.RUNTIME_ROOT


def find_xray_exe() -> str:
    """定位 Xray 内核可执行文件。

    顺序：环境变量 ``TX_XRAY_EXE`` → 部署根 ``bin/xray/xray.exe``（用户自放）
    → **打包资源目录** ``_MEIPASS/bin/xray/xray.exe`` → 开发期项目
    ``v2rayN/bin/xray/xray.exe`` → PATH 里的 ``xray``。
    """
    cands = []
    env = os.environ.get("TX_XRAY_EXE")
    if env:
        cands.append(env)
    root = config.RUNTIME_ROOT
    cands.append(os.path.join(root, "bin", "xray", "xray.exe"))
    cands.append(os.path.join(root, "v2rayN", "bin", "xray", "xray.exe"))
    # 打包自带（onefile 解压目录）
    cands.append(os.path.join(_bundle_root(), "bin", "xray", "xray.exe"))
    # 开发期：包在 tangxin/ 下，其上一级即项目根
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands.append(os.path.join(pkg_parent, "v2rayN", "bin", "xray", "xray.exe"))
    cands.extend(["xray", "xray.exe"])

    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        exe = c
        if os.path.basename(c) in ("xray", "xray.exe"):
            exe = shutil_which(c)
            if not exe:
                continue
        if os.path.isfile(exe):
            return exe
    return ""


def shutil_which(name: str):
    try:
        import shutil
        return shutil.which(name)
    except Exception:
        return None


def load_nodes() -> list:
    """加载节点列表。

    顺序：环境变量 ``TX_NODES`` 指向的 JSON → 部署根 ``config/nodes.json``
    （用户可改，优先）→ **打包资源目录** ``_MEIPASS/config/nodes.json``
    → 包内 ``resources/config/nodes.json``（开发期/兜底）。
    """
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.environ.get("TX_NODES"),
        os.path.join(config.RUNTIME_ROOT, "config", "nodes.json"),
        os.path.join(_bundle_root(), "config", "nodes.json"),
        os.path.join(pkg_dir, "resources", "config", "nodes.json"),
    ]
    for p in cands:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            nodes = data.get("nodes") if isinstance(data, dict) else data
            if isinstance(nodes, list) and nodes:
                logger.get_logger().info("载入 %d 个节点：%s", len(nodes), p)
                return nodes
        except Exception as e:
            logger.get_logger().warning("读取节点配置失败 %s: %s", p, e)
    return []


def _build_outbound(node: dict, idx: int) -> dict:
    """把节点编译成 Xray outbound（VLESS + Reality，与主平台一致）。"""
    tag = "proxy-%d" % idx
    flow = node.get("flow") or ""
    user = {
        "id": node.get("id") or "",
        "encryption": "none",
        "level": 0,
    }
    if flow:
        user["flow"] = flow

    reality = {
        "show": False,
        "fingerprint": node.get("fingerprint") or "chrome",
        "serverName": node.get("sni") or "",
        "publicKey": node.get("public_key") or "",
        "shortId": node.get("short_id") or "",
        "spiderX": node.get("spider_x") or "/",
    }

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": node.get("address") or "",
                "port": int(node.get("port") or 443),
                "users": [user],
            }]
        },
        "streamSettings": {
            "network": node.get("network") or "tcp",
            "security": "reality",
            "realitySettings": reality,
        },
        "mux": {"enabled": False},
    }


def build_config(nodes: list, socks_port: int = SOCKS_PORT) -> dict:
    """生成完整 Xray 配置：balancer 入口 + 每节点专属入口。

    routing 规则顺序很关键：
      1. 先放「专属入口 → 指定节点」的精确规则；
      2. **必须**放一条「纯兜底」规则（除 type/network/balancerTag 外无任何
         限定条件），否则未命中的流量会走**第一个 outbound**（= proxy-1），
         等于绕过负载均衡。这条坑主平台线上踩过。
    """
    outbounds = []
    names = []
    for i, n in enumerate(nodes, 1):
        outbounds.append(_build_outbound(n, i))
        names.append(str(n.get("remarks") or "proxy-%d" % i))

    keep = [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ]
    outbounds.extend(keep)

    inbounds = [{
        "tag": "socks",
        "port": socks_port,
        "listen": "127.0.0.1",
        "protocol": "socks",
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": False},
        "settings": {"auth": "noauth", "udp": True, "allowTransparent": False},
    }]
    rules = []
    for i in range(len(nodes)):
        tag = "socks-node-%d" % i
        port = socks_port + 1 + i
        inbounds.append({
            "tag": tag,
            "port": port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": False},
            "settings": {"auth": "noauth", "udp": True, "allowTransparent": False},
        })
        rules.append({"type": "field", "inboundTag": [tag], "outboundTag": "proxy-%d" % (i + 1)})

    # 纯兜底：所有未命中规则的流量走 balancer
    rules.append({"type": "field", "network": "tcp,udp", "balancerTag": LB_TAG})

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "rules": rules,
            "balancers": [{"tag": LB_TAG, "selector": ["proxy-"]}],
        },
    }


class ProxyManager:
    """自带 Xray 内核的代理管理器（探测 → 自启 → 健康检查 → 自愈重启）。"""

    def __init__(self, nodes: list = None, socks_port: int = SOCKS_PORT):
        self.socks_port = socks_port
        self.exe = find_xray_exe()
        self.nodes = nodes if nodes is not None else load_nodes()
        self._process = None
        self._config_path = os.path.join(
            config.RUNTIME_ROOT, "config", "xray_config.json")
        self._log = logger.get_logger()

    # ---------- 探测 ----------
    def is_listening(self, port: int = None, host: str = "127.0.0.1",
                     timeout: float = 1.0) -> bool:
        """TCP 探测端口是否在监听。"""
        p = port or self.socks_port
        try:
            with socket.create_connection((host, int(p)), timeout=timeout):
                return True
        except OSError:
            return False

    def listening_ports(self) -> list:
        """返回当前在监听的代理端口列表。"""
        ports = [self.socks_port] + [self.socks_port + 1 + i
                                     for i in range(len(self.nodes))]
        return [p for p in ports if self.is_listening(p)]

    # ---------- 配置 ----------
    def _write_config(self) -> bool:
        """生成并写出 Xray 配置。"""
        cfg = build_config(self.nodes, self.socks_port)
        d = os.path.dirname(self._config_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True

    def _test_config(self) -> tuple:
        """用 ``xray -test`` 校验配置；返回 (ok, msg)。"""
        try:
            r = subprocess.run(
                [self.exe, "run", "-test", "-c", self._config_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                cwd=os.path.dirname(self.exe),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = (r.stdout or b"").decode("utf-8", "ignore") + \
                  (r.stderr or b"").decode("utf-8", "ignore")
            if r.returncode == 0:
                return True, out.strip()[:300]
            return False, out.strip()[:800]
        except Exception as e:
            return False, str(e)

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """启动自带 Xray 内核（若端口已在监听则直接复用）。"""
        if self.is_listening():
            self._log.info("检测到 127.0.0.1:%d 已在监听，复用现有代理实例",
                           self.socks_port)
            return True
        if not self.exe:
            self._log.error("未找到 Xray 内核（TX_XRAY_EXE 或 bin/xray/xray.exe）")
            return False
        if not self.nodes:
            self._log.error("没有可用节点（检查 config/nodes.json）")
            return False

        self._write_config()
        ok, msg = self._test_config()
        if not ok:
            self._log.error("Xray 配置校验失败，未启动：%s", msg)
            return False

        try:
            self._process = subprocess.Popen(
                [self.exe, "-c", self._config_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(self.exe),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self._log.error("启动 Xray 内核失败: %s", e)
            return False

        for _ in range(30):                       # 最多等 30s
            if self.is_listening():
                self._log.info("内置代理已就绪: 127.0.0.1:%d（%d 个节点）",
                             self.socks_port, len(self.nodes))
                return True
            time.sleep(1)
        self._log.error("Xray 启动后 30s 内端口仍未监听")
        return False

    def ensure_running(self) -> bool:
        """确保代理在跑：已在监听则复用，否则自启。"""
        if self.is_listening():
            return True
        return self.start()

    def stop(self) -> None:
        """停止自带内核（仅停自己拉起的进程，不动外部实例）。"""
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._process = None

    def restart(self) -> bool:
        """重启自带内核（僵尸自愈）。"""
        self._log.warning("重启内置代理内核…")
        self.stop()
        time.sleep(1)
        return self.start()

    def health_check(self) -> bool:
        """健康探测：端口不监听即为僵尸，自动重启。"""
        if self.is_listening():
            return True
        self._log.warning("代理端口 127.0.0.1:%d 未监听（疑似僵尸进程）",
                          self.socks_port)
        return self.restart()

    # ---------- 代理地址 ----------
    def proxy_url(self, port: int = None) -> str:
        """返回 SOCKS5 代理 URL（``socks5h`` = 远程解析 DNS，必须）。"""
        return "socks5h://127.0.0.1:%d" % (port or self.socks_port)

    def pinned_ports(self) -> list:
        """节点专属入口端口列表（下载时按任务挑一个固定使用）。"""
        return [self.socks_port + 1 + i for i in range(len(self.nodes))]

    def pinned_proxy_url(self, task_index: int = 0) -> str:
        """按任务序号固定取一个专属入口，保证同一次下载走同一出口。

        ⚠️ **回退是必须的，不是可选项**：当 10808 已被**外部**代理实例占用时，
           为避免端口冲突我们不拉起自带内核 —— 于是 10809+ 专属入口**并不存在**。
           此时若照旧返回专属端口，下载会全部 ``WinError 10061 目标计算机积极
           拒绝``（2026-10-23 真机 QA 实测踩到）。所以先探测，不通就回退
           balancer；糖心 CDN 直链不带 ``ip=`` 出口绑定，走 balancer 是安全的。
        """
        ports = self.pinned_ports()
        if ports:
            p = ports[task_index % len(ports)]
            if self.is_listening(p):
                return self.proxy_url(p)
            self._log.info("节点专属入口 %s 未监听（复用外部实例），回退 balancer %s",
                           p, self.socks_port)
        return self.proxy_url()
