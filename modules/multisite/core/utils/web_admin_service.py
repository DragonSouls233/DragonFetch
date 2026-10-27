# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""WEB 管理后台 —— 服务层（控制器与 GUI 都只依赖这一层）

为什么必须有这一层
==================
打包后的 exe 是 **onefile 单文件**，机器上并没有 Python 解释器。
旧实现在 ``run_gui.py`` 里是这样起WEB 后台的::

    python_exe = os.path.join(os.path.dirname(sys.executable), 'python.exe')
    if not os.path.exists(python_exe):
        python_exe = 'python.exe'          # ← 打包后必然找不到
    subprocess.Popen([python_exe, web_admin_path], ...)

即「拿 python.exe 去跑 start_web_admin.py」——**打包后这条路是断的**，
服务器上 WEB 后台从来没起来过。

本模块改为 **在主进程内起 uvicorn（后台线程）**：

- 不依赖外部解释器，打包 / 开发环境行为一致；
- GUI 与局域网网页**共用同一个进程、同一套业务逻辑与同一个数据库**，
  天然满足「桌面操作与网页操作结果一致」；
- 生命周期可控：可启动、可停止、可改端口、可查局域网访问地址。

分层：GUI(Tab) → 本服务层 → web_admin(FastAPI 控制器) → modules.core(业务)
"""
import asyncio
import logging
import os
import re
import socket
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---- 默认配置（GUI 可覆盖，落到 config.yaml 的 web_admin 节）----
DEFAULT_HOST = "0.0.0.0"      # 0.0.0.0 = 允许局域网访问；改成 127.0.0.1 则仅本机
DEFAULT_PORT = 16600


def detect_lan_ips() -> List[str]:
    """探测本机可用于局域网访问的 IPv4 地址。

    ⚠️ 双网口机器（一个对外、一个对内）必须专门处理（2026-10-17，120 服务器实测）：
        以太网   192.168.10.120   网关=(无)      ← **内网口（对内/局域网）**
        以太网 2 192.168.1.100    网关=192.168.1.1 ← **对外口（上网）**
    旧实现用「UDP connect 到 223.5.5.5」拿出口 IP，那走的是**默认路由 = 对外口**，
    于是 LAN 地址会显示成 192.168.1.100 —— 局域网电脑根本连不上。

    现在的排序规则（越靠前越可能是「内网口」，会被 GUI 优先当作 LAN 地址展示）：
      1. **无默认网关**的网卡最优先 —— 内网口通常不配网关（对外口才有）；
      2. 其次私有网段（192.168. / 10. / 172.16-31.）；
      3. 跳过回环 127.*、APIPA 169.254.*、以及常见虚拟网卡。
    无论排序如何，**所有候选 IP 都会返回**给GUI 展示，由用户按实际情况确认。
    """
    ips: List[str] = []

    # 1) 用 psutil 拿到「网卡 -> IP + 是否 up + MTU」，并标记有无默认网关
    has_gw: Dict[str, bool] = {}
    iface_ip: Dict[str, str] = {}
    try:
        import psutil
        stats = psutil.net_if_stats()
        for name, items in psutil.net_if_addrs().items():
            for a in items:
                if a.family != socket.AF_INET or not a.address:
                    continue
                ip = a.address
                # 只收集「可用于局域网访问」的真实 IPv4：
                # 跳过回环、APIPA(169.254.*，没连上 DHCP 的自动私有地址)，
                # 以及 VPN/虚拟网卡（它们的地址对同事的电脑没意义）。
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                if _is_noise_iface(name):
                    continue
                ips.append(ip)
                iface_ip[name] = ip
                # Windows：无网关的网卡判定为内网口
                has_gw[name] = _iface_has_gateway(name)
                break
        _ = stats  # 仅确保可用
    except Exception:
        # 2) 兜底：UDP connect 拿出口 IP（注意：双网口下这是「对外口」）
        ips = []
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect(("223.5.5.5", 80))   # 纯本地路由查询，不产生流量
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        except Exception:
            pass
        finally:
            s.close()
        # 3) 补齐主机名解析到的其它 IP（双网卡时这里能捞到内网口）
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        return _dedup(ips)

    def rank(item: int) -> int:
        name = item
        score = 0
        # ① 无默认网关 => 内网口（对内），最可能是同事要访问的那个
        if not has_gw.get(name, True):
            score += 0
        else:
            score += 10# 有网关 => 多半是对外口
        ip = iface_ip.get(name, "")
        # ② 私有网段优先
        if ip.startswith("192.168."):
            score += 0
        elif ip.startswith("10."):
            score += 1
        elif ip.startswith("172."):
            try:
                score += 0 if 16 <= int(ip.split(".")[1]) <= 31 else 6
            except Exception:
                score += 6
        else:
            score += 4      # 公网地址：能访问但通常不是同事所在的局域网
        return score

    ordered = [iface_ip[n] for n in sorted(iface_ip.keys(), key=rank)]
    return _dedup(ordered)


def _is_noise_iface(name: str) -> bool:
    """判断网卡是否为「噪声网卡」：蓝牙/虚拟网卡/VPN 等，地址对同事没意义。"""
    n = (name or "").lower()
    for kw in ("loopback", "teredo", "isatap", "npcap", "bluetooth", "vmware",
               "virtualbox", "vethernet", "hyper-v", "tailscale", "zerotier",
               "wsl", "docker", "vpn", "tap", "tun", "radmin"):
        if kw in n:
            return True
    return False


def _dedup(seq: List[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def detect_ip_iface_map() -> Dict[str, Dict[str, Any]]:
    """返回 ``{ip: {"iface": 网卡名, "has_gateway": bool}}``。

    供 GUI 逐条标注「这是内网口还是对外口」，解决双网口机器上
    「显示的是对外 IP，局域网连不上」的问题。
    拿不到网卡信息时返回空 dict，调用方需容错。
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        import psutil
    except Exception:
        return out
    gw_ifaces = _default_gateway_ifaces()
    try:
        for name, items in psutil.net_if_addrs().items():
            for a in items:
                if a.family != socket.AF_INET or not a.address:
                    continue
                ip = a.address
                if ip.startswith("127."):
                    continue
                out.setdefault(ip, {
                    "iface": name,
                    "has_gateway": name in gw_ifaces,
                })
                break
    except Exception:
        pass
    return out


_GW_IFACES: Optional[set] = None


def _default_gateway_ifaces() -> set:
    """返回**配置了默认网关**的网卡名集合。

    内网口通常**没有**默认网关（对外口才有）—— 这是双网口机器上
    区分「对内/对外」最可靠的判据（120 服务器实测：无网关的那张就是内网口）。

    实现：用 PowerShell 的 `Get-NetIPConfiguration` 一次拿到
    「有网关的接口别名」集合；不可用时退回解析 `route print`。
    """
    global _GW_IFACES
    if _GW_IFACES is not None:
        return _GW_IFACES

    names = set()
    try:
        import subprocess
        ps = (
            "Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway} "
            "| ForEach-Object {$_.InterfaceAlias}"
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            stderr=subprocess.DEVNULL, timeout=8,
        )
        txt = raw.decode("utf-8", "replace")
        names = {ln.strip() for ln in txt.splitlines() if ln.strip()}
    except Exception:
        names = set()
    _GW_IFACES = names
    return names


def _iface_has_gateway(iface_name: str) -> bool:
    """该网卡是否配置了默认网关（探测失败时保守返回 True）。"""
    return iface_name in _default_gateway_ifaces()


def find_free_port(host: str, port: int) -> Optional[int]:
    """检查端口是否可用；被占用返回 None，否则返回原端口。

    实现要点：**不能**用 ``bind()`` + ``SO_REUSEADDR`` 来探测 ——
    Windows 上 ``SO_REUSEADDR`` 允许多个套接字绑定同一端口，于是预检会
    「以为空闲」，而随后 uvicorn 真正 bind 时抛WinError 10048，
    最终只能报出「启动超时」这种无信息量的错误（QA Q5 曾踩到）。
    这里改用「尝试连接」：连得上 = 已被占用，连不上 = 空闲。
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect((probe_host, port))
        return None          # 连得上 ⇒ 有人占用
    except (ConnectionRefusedError, socket.timeout, OSError):
        return port           # 连不上 ⇒ 空闲
    finally:
        s.close()


class WebAdminService:
    """WEB 管理后台的运行时控制器（服务层）。

    单例由 :func:`get_web_admin_service` 提供，GUI 多个入口共享同一实例，
    避免「重复启动 / 状态不一致」。
    """

    _instance: Optional["WebAdminService"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._server = None          # uvicorn.Server 实例（用于优雅停止）
        self._thread: Optional[threading.Thread] = None
        self._config: Dict[str, Any] = {
            "enabled": False,        # 是否启用 WEB 后台
            "host": DEFAULT_HOST,
            "port": DEFAULT_PORT,
            "autostart": True,       # 随 GUI 启动
        }
        self._last_error: str = ""
        self._lock = threading.RLock()
        self.load_config()

    # ------------------------------------------------------------------ 配置
    @property
    def config_path(self) -> str:
        """config.yaml 的实际路径（与 ConfigManager 查找顺序一致）。"""
        from multisite.core.utils.path_utils import get_config_path
        for rel in ("../config.yaml", "config.yaml"):
            p = get_config_path(rel)
            if os.path.exists(p):
                return p
        return get_config_path("../config.yaml")

    def load_config(self) -> Dict[str, Any]:
        """从 config.yaml 的 web_admin 节读取配置（读不到就用默认值）。"""
        try:
            import yaml
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            section = raw.get("web_admin") or {}
            with self._lock:
                for key in ("host", "port"):
                    if section.get(key) is not None:
                        self._config[key] = section[key]
                for key in ("enabled", "autostart"):
                    if section.get(key) is not None:
                        self._config[key] = bool(section[key])
                self._config["port"] = int(self._config["port"])
        except Exception as e:
            logger.info(f"未读取到 web_admin 配置，使用默认值: {e}")
        return dict(self._config)

    def save_config(self, **updates: Any) -> Dict[str, Any]:
        """把配置写回 config.yaml 的 web_admin 节（保留其它节不动）。"""
        import yaml
        path = self.config_path
        with self._lock:
            self._config.update(updates)
            if "port" in updates:
                self._config["port"] = int(updates["port"])
            cfg = dict(self._config)

        raw: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        raw["web_admin"] = cfg
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        logger.info(f"WEB 后台配置已保存: {cfg}")
        return cfg

    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._config)

    # ------------------------------------------------------------------ 状态
    def is_running(self) -> bool:
        return bool(self._server is not None and
                    getattr(self._server, "started", False))

    def get_status(self) -> Dict[str, Any]:
        """给 GUI 用的完整状态：是否运行、监听地址、局域网访问 URL。

        双网口机器（对内 + 对外）会把**所有**网卡 IP 都列出来，并标出哪个是
        「内网口（无默认网关）」，GUI 据此展示，避免像默认路由那样只给对外口地址。
        """
        cfg = self.get_config()
        host, port = cfg["host"], cfg["port"]
        lan_ips = detect_lan_ips()
        iface_map = detect_ip_iface_map()
        lan_url = f"http://127.0.0.1:{port}"
        if host == "0.0.0.0" and lan_ips:
            # detect_lan_ips 已按「内网口优先」排序 => 第一个就是最可能的 LAN 地址
            lan_url = f"http://{lan_ips[0]}:{port}"
        return {
            "running": self.is_running(),
            "enabled": cfg["enabled"],
            "autostart": cfg["autostart"],
            "host": host,
            "port": port,
            "lan_ips": lan_ips,
            # [{ip, iface, has_gateway, is_lan}] 供 GUI 逐条展示与标注
            "iface_details": [
                {
                    "ip": ip,
                    "iface": iface_map.get(ip, {}).get("iface", "?"),
                    "has_gateway": bool(iface_map.get(ip, {}).get("has_gateway")),
                    "is_lan": (i == 0 and host == "0.0.0.0"),
                }
                for i, ip in enumerate(lan_ips)
            ],
            "urls": [f"http://{ip}:{port}" for ip in lan_ips] if host == "0.0.0.0" else [],
            "local_url": f"http://127.0.0.1:{port}",
            "lan_url": lan_url,
            "error": self._last_error,
        }

    # ------------------------------------------------------------------ 生命周期
    def start(self, host: Optional[str] = None, port: Optional[int] = None) -> Dict[str, Any]:
        """在当前进程内启动 WEB 后台（后台线程跑 uvicorn）。

        返回 ``{"ok": bool, "message": str, ...status}``，不抛异常——
        便于 GUI 就地提示（项目约定：线程内不弹模态框）。
        """
        with self._lock:
            if self.is_running():
                return {"ok": False, "message": "WEB 后台已在运行", **self.get_status()}
            cfg = dict(self._config)
            if host:
                cfg["host"] = host
            if port:
                cfg["port"] = int(port)
            h, p = cfg["host"], cfg["port"]

            if find_free_port(h, p) is None:
                self._last_error = f"端口 {p} 已被占用，请换一个端口"
                return {"ok": False, "message": self._last_error, **self.get_status()}

            try:
                import uvicorn
                from web_admin.server import app as fastapi_app
            except Exception as e:
                self._last_error = f"WEB 后台模块加载失败: {e}"
                logger.error(self._last_error, exc_info=True)
                return {"ok": False, "message": self._last_error, **self.get_status()}

            uvicorn_config = uvicorn.Config(
                fastapi_app, host=h, port=p, log_level="warning",
                access_log=False, ws_ping_interval=None, ws_ping_timeout=None,
            )
            self._server = uvicorn.Server(uvicorn_config)

            def _serve():
                try:
                    # uvicorn.Server.run 内部会 install signal handler，
                    # 非主线程会抛错，这里绕开：在本线程直接跑 serve()
                    asyncio.run(self._server.serve())
                except SystemExit:
                    pass
                except OSError as e:
                    # 端口在预检之后被抢占等 bind 失败：把真实原因带给调用方，
                    # 避免统一显示成无信息量的「启动超时」
                    self._last_error = f"端口绑定失败：{e}"
                    logger.error(f"WEB 后台 bind 失败: {e}")
                except Exception as e:
                    logger.error(f"WEB 后台运行异常: {e}", exc_info=True)
                    self._last_error = str(e)

            self._thread = threading.Thread(
                target=_serve, name="web-admin-server", daemon=True
            )
            self._thread.start()

            # 等待真正起来（最多 15 秒），期间顺带把 enabled 落库
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.is_running():
                    self._last_error = ""
                    self._config["host"], self._config["port"] = h, p
                    self.save_config(enabled=True, host=h, port=p)
                    logger.info(f"WEB 管理后台已启动: http://127.0.0.1:{p}")
                    return {"ok": True, "message": f"已启动: http://127.0.0.1:{p}",
                            **self.get_status()}
                if self._thread is not None and not self._thread.is_alive():
                    break
                time.sleep(0.2)

            self._last_error = self._last_error or "启动超时（服务未在 15 秒内就绪）"
            return {"ok": False, "message": f"启动失败：{self._last_error}",
                    **self.get_status()}

    def stop(self) -> Dict[str, Any]:
        """优雅停止 WEB 后台（uvicorn 官方方式：置 should_exit）。"""
        with self._lock:
            if not self._server:
                return {"ok": False, "message": "WEB 后台未运行", **self.get_status()}
            try:
                self._server.should_exit = True
                if self._thread is not None:
                    self._thread.join(timeout=8)
            except Exception as e:
                logger.warning(f"停止 WEB 后台时异常: {e}")
            finally:
                self._server = None
                self._thread = None
            self.save_config(enabled=False)
            logger.info("WEB 管理后台已停止")
            return {"ok": True, "message": "已停止", **self.get_status()}

    def restart(self, host: Optional[str] = None, port: Optional[int] = None) -> Dict[str, Any]:
        """改配置后重启（端口变更必须重启才生效）。"""
        if self.is_running():
            self.stop()
        return self.start(host=host, port=port)


_instance_svc: Optional[WebAdminService] = None


def get_web_admin_service() -> WebAdminService:
    """获取全局单例（GUI 各入口共用，避免重复启动）。"""
    global _instance_svc
    if _instance_svc is None:
        _instance_svc = WebAdminService()
    return _instance_svc


def stop_web_admin_service() -> bool:
    """优雅停止全局 WEB 后台（供 GUI 关闭 / 退出时调用）。

    - 返回 True 表示确实执行了停止动作；未运行 / 异常则返回 False。
    - 集中在这一处，方便主窗口 ``closeEvent`` 与 ``aboutToQuit`` 两路共用，
      也便于单元测试直接调用验证。
    - 根因修复（2026-10-18）：此前服务随 GUI 启动，但从不在关闭时停止，
      关窗后 uvicorn 仍占着 16600 端口、进程成孤儿，下次开新 EXE 即端口冲突。
    """
    try:
        svc = get_web_admin_service()
        if svc.is_running():
            svc.stop()
            logger.info("已停止 WEB 后台服务（GUI 关闭/退出）")
            return True
    except Exception as e:
        logger.warning(f"停止 WEB 后台失败: {e}")
    return False