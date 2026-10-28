# -*- coding: utf-8 -*-
"""已下载文件识别（跨根目录去重）——「本机先下，后续转移服务器」场景的核心模块。

## 要解决的问题
用户现在在**本机**下载，之后要把成果转移到**服务器**。两边的目录结构完全一样
（`{下载目录}/{演员}/{标题}.mp4`），只是**盘符/根路径不同**（如 `G:\\TEST\\糖心Vlog`
vs `E:\\TangXinVlog\\downloads`）。

原先的幂等只检查**当前 output_dir 下**有没有同名文件 —— 换个根目录就认为没下过，
于是把同一部片子再下一遍，浪费带宽和时间。

## 识别策略（三道，由快到慢）
1. **路径映射快查**（零成本）：目录结构一致 ⇒ 把每个已知根目录拼上
   `{演员}/{标题}/{标题}.mp4`（新版三层）或 `{演员}/{标题}.mp4`（旧版两层）
   直接 `os.path.isfile` 判断。不需要扫描，毫秒级。
2. **vid 精确匹配**：NFO 里写了 `<uniqueid type="vid">`，扫描时解析出来建索引，
   按 vid 命中（最可靠，标题被改名也不怕）。
3. **(演员, 标题) 宽松匹配**：对文件名做 NFKC + casefold + 去空白标准化，
   识别"Errno22 降级改名"等造成的命名差异。

## 目录结构（用户定，新版）
    {下载目录}/{模特文件夹}/{标题文件夹}/{标题}.mp4 / .nfo / -poster.jpg
旧版是 `{下载目录}/{模特}/{标题}.mp4` —— **两种都要认**（老片不能因为
改了结构就变成"没下过"而重复下载）。

## 持久化
扫描结果写进 SQLite 表 `existing_files`（root/vid/actor/title/mp4_path/size/mtime）。
避免每次启动都全盘扫；超过 `STALE_HOURS` 或用户点「重新扫描」才重扫。
扫描在后台线程进行，带进度回调，**绝不阻塞下载主流程**（扫不完就先按没命中处理）。

## 使用
    idx = existing.get_index()
    idx.configure(known_dirs=[r"G:\\TEST\\糖心Vlog"])   # 或从 settings 读
    idx.ensure_ready()                                   # 需要时扫描（后台）
    hit = idx.lookup(video)                              # → 已存在的 mp4 路径或 None
"""
import os
import re
import sqlite3
import threading
import time
import unicodedata

from . import config, db, logger

# 索引超过这个时长视为过期，下次 ensure_ready 会重扫
STALE_HOURS = 12

_VID_RE = re.compile(r'<uniqueid[^>]*type="vid"[^>]*>([^<]+)</uniqueid>', re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_ACTOR_RE = re.compile(r"<actor>\s*<name>(.*?)</name>", re.S | re.I)
_RUNTIME_RE = re.compile(r"<runtime>(\d+)</runtime>", re.I)


def norm_key(text) -> str:
    """标准化匹配键（转发 ``utils.norm_key``，行为一致）。

    ⚠️ 实现放在 utils，因为 db 也要用（existing 依赖 db，放这里会循环导入）。
    """
    from .utils import norm_key as _nk
    return _nk(text)


def _degraded_names(name: str):
    """给定一个文件名 stem，返回可能的降级变体（Errno22 时 transfer 会改名）。"""
    out = {name}
    cleaned = "".join(c for c in name if ord(c) <= 0xFFFF)
    cleaned = "".join(c for c in cleaned if c not in '<>:"|*?/\\').strip().rstrip(". ")
    if cleaned:
        out.add(cleaned)
        if len(cleaned) > 120:
            out.add(cleaned[:120].rstrip(". ").strip())
    return out


class ExistingIndex:
    """已下载文件索引（多根目录 + 去重）。"""

    # 哨兵：表示"本实例还没配置过目录"，用于区分「首次配置」与「用户改配置」
    _UNSET = object()

    def __init__(self, known_dirs=None):
        self._lock = threading.Lock()
        self._scanning = False
        self._mem_vid = {}          # vid -> mp4_path
        self._mem_key = {}          # (actor_key, title_key) -> mp4_path
        self._loaded_at = 0.0
        self._configured_dirs = self._UNSET
        self.known_dirs = []
        self._log = logger.get_logger()
        if known_dirs:
            self.configure(known_dirs)

    # ---------- 配置 ----------
    def configure(self, known_dirs):
        """设置已知根目录列表（会去重、去空）。

        ⚠️ 配置**发生变化**就清空库里的旧索引：否则用户删掉某个目录后，
           旧索引仍会命中并跳过下载，行为不可预期（实测踩到）。
           清空后由 ensure_ready / refresh 按新配置重建。
        """
        seen, out = set(), []
        for d in known_dirs or []:
            d = (d or "").strip()
            if not d:
                continue
            n = os.path.normpath(d)
            if n not in seen:
                seen.add(n)
                out.append(n)
        if out != getattr(self, "_configured_dirs", self._UNSET):
            # 只有「曾经配过、现在变了」才清库；首次配置要复用上次进程留下的索引
            # （否则重启后每次都得重扫一遍 NAS，实测踩到）。
            if getattr(self, "_configured_dirs", self._UNSET) is not self._UNSET:
                try:
                    db.replace_existing([])      # 旧索引不再可信
                except Exception as e:
                    self._log.warning("清理旧索引失败：%s", e)
            self._configured_dirs = out
            with self._lock:
                self._loaded_at = 0.0
        self.known_dirs = out

    def add_dir(self, path):
        if not path:
            return
        n = os.path.normpath(path.strip())
        if n and n not in self.known_dirs:
            self.known_dirs.append(n)
            with self._lock:
                self._loaded_at = 0.0
            try:
                db.replace_existing([])      # 目录变了，旧索引不可信
            except Exception:
                pass

    # ---------- 内存索引 ----------
    def _ensure_mem(self):
        """把 DB 里的索引载入内存（每次进程只做一次）。"""
        with self._lock:
            if self._loaded_at:
                return
            try:
                rows = db.get_existing_all()
            except Exception as e:
                self._log.warning("读取已下载索引失败：%s", e)
                rows = []
            self._mem_vid = {}
            self._mem_key = {}
            for r in rows:
                if r.get("vid"):
                    self._mem_vid[str(r["vid"])] = r["mp4_path"]
                ak = norm_key(r.get("actor"))
                tk = norm_key(r.get("title"))
                if ak or tk:
                    self._mem_key[(ak, tk)] = r["mp4_path"]
            self._loaded_at = time.time()

    def stats(self):
        self._ensure_mem()
        with self._lock:
            return {"dirs": len(self.known_dirs),
                    "files": len(set(list(self._mem_vid.values()) +
                                     list(self._mem_key.values()))),
                    "by_vid": len(self._mem_vid)}

    # ---------- 扫描 ----------
    def _scan_one(self, root, on_progress=None):
        """扫描单个根目录，返回行列表。

        行内除索引字段外，还带 ``nfo_path`` / ``poster_path`` / ``duration_sec``，
        供写进**下载记录**表（用户要"已下好的记录到数据库"）。
        """
        rows = []
        if not os.path.isdir(root):
            self._log.info("已下载目录不存在，跳过：%s", root)
            return rows
        n_files = 0
        for dp, dn, fs in os.walk(root):
            # 跳过明显不是媒体库的目录
            if os.path.basename(dp).lower() in ("logs", "temp", "tangxin", "_runtime"):
                continue
            for fn in fs:
                low = fn.lower()
                if not (low.endswith(".mp4") or low.endswith(".nfo")):
                    continue
                full = os.path.join(dp, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                n_files += 1
                if on_progress and n_files % 200 == 0:
                    on_progress(root, n_files)
                if low.endswith(".nfo"):
                    info = self._parse_nfo(full)
                    if not info:
                        continue
                    mp4 = os.path.splitext(full)[0] + ".mp4"
                    if not os.path.isfile(mp4):
                        continue
                    rows.append({
                        "root": root, "vid": info.get("vid") or "",
                        "actor": info.get("actor") or os.path.basename(dp),
                        "title": info.get("title") or os.path.splitext(fn)[0],
                        "mp4_path": mp4, "size_bytes": st.st_size,
                        "mtime": st.st_mtime,
                        "nfo_path": full,
                        "poster_path": self._find_poster(mp4),
                        "duration_sec": (info.get("runtime_min") or 0) * 60,
                        # ⚠️ 标记来源：nfo 行与 mp4 行指向**同一个 mp4_path**，
                        #    去重时必须让 mp4 行胜出，否则 size_bytes 会变成
                    #    nfo 的几百字节（实测 307 部全显示 0.00 GB）。
                        "_is_nfo": True,
                    })
                else:
                    # 没有 NFO 的 mp4：用目录结构推演员/标题
                    #   新版 {root}/{演员}/{标题}/{标题}.mp4 → 目录名==文件名
                    #   旧版 {root}/{演员}/{标题}.mp4
                    stem = os.path.splitext(fn)[0]
                    actor, title = _infer_actor_title(dp, stem)
                    nfo_cand = os.path.splitext(full)[0] + ".nfo"
                    rows.append({
                        "root": root, "vid": "",
                        "actor": actor,
                        "title": title,
                        "mp4_path": full, "size_bytes": st.st_size,
                        "mtime": st.st_mtime,
                        "nfo_path": nfo_cand if os.path.isfile(nfo_cand) else "",
                        "poster_path": self._find_poster(full),
                        "duration_sec": 0,
                        "_is_nfo": False,
                    })
        return rows

    @staticmethod
    def _find_poster(mp4_path: str) -> str:
        """找同目录同名的 ``-poster.jpg``（有就返回，没有空串）。"""
        try:
            stem = os.path.splitext(mp4_path)[0]
            cand = stem + "-poster.jpg"
            if os.path.isfile(cand):
                return cand
            # 有些平台用 {stem}.jpg
            cand2 = stem + ".jpg"
            if os.path.isfile(cand2):
                return cand2
        except OSError:
            pass
        return ""

    @staticmethod
    def _parse_nfo(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read(20000)
        except OSError:
            return None
        m = _VID_RE.search(txt)
        vid = m.group(1).strip() if m else ""
        t = _TITLE_RE.search(txt)
        a = _ACTOR_RE.search(txt)
        r = _RUNTIME_RE.search(txt)
        try:
            runtime_min = int(r.group(1)) if r else 0
        except (TypeError, ValueError):
            runtime_min = 0
        return {"vid": vid,
                "title": (t.group(1).strip() if t else ""),
                "actor": (a.group(1).strip() if a else ""),
                "runtime_min": runtime_min}

    def refresh(self, on_progress=None):
        """强制重扫所有已知目录并写库。返回纳入索引的文件数。

        扫描结果**同时**写进下载记录表（status='done'）——
        用户需求：「识别到下载目录已经下好的，需要记录到数据库里」。
        """
        with self._lock:
            if self._scanning:
                return 0
            self._scanning = True
        try:
            all_rows = []
            for root in list(self.known_dirs):
                all_rows.extend(self._scan_one(root, on_progress))
            # 同一个 mp4 可能同时被 .nfo 与 .mp4 两条记录覆盖到，
            # 按路径去重后再写库/计数，否则数量会虚高（实测 5 vs 真实 3）。
            # ⚠️ 去重时必须**让 mp4 行胜出**：nfo 行虽然 vid/title 更准，
            #    但它的 size_bytes 是 nfo 的几百字节，胜出就会把
            #    「已下总大小」算成 0（2026-10-24 服务器上 307 部全显示 0.00 GB）。
            #    折中：保留 mp4 行的体积，同时把 nfo 行的 vid/title 补回去。
            uniq = {}
            for r in all_rows:
                prev = uniq.get(r["mp4_path"])
                if prev is None:
                    uniq[r["mp4_path"]] = r
                    continue
                if prev.get("_is_nfo") and not r.get("_is_nfo"):
                    # r 是 mp4 行：体积/时间用它，nfo 提供的 vid/title 保留
                    if prev.get("vid") and not r.get("vid"):
                        r["vid"] = prev["vid"]
                    if prev.get("title") and not r.get("title"):
                        r["title"] = prev["title"]
                    if prev.get("duration_sec") and not r.get("duration_sec"):
                        r["duration_sec"] = prev["duration_sec"]
                    uniq[r["mp4_path"]] = r
                elif not prev.get("_is_nfo") and r.get("_is_nfo"):
                    # prev 是 mp4 行、r 是 nfo 行：把 nfo 的 vid/title 补给 prev
                    if r.get("vid") and not prev.get("vid"):
                        prev["vid"] = r["vid"]
                    if r.get("title") and not prev.get("title"):
                        prev["title"] = r["title"]
                    if r.get("duration_sec") and not prev.get("duration_sec"):
                        prev["duration_sec"] = r["duration_sec"]
            rows = list(uniq.values())
            # ⚠️⚠️ **保护：扫到 0 个文件时绝不清空已有索引**。
            #    扫描目录很可能是"暂时看不见"（网络盘在别的会话没挂载 /
            #    权限掉了 / 盘符变了）——这时 refresh() 的 DELETE+INSERT
            #    会把用户几十上百条已下载记录一次性抹光。
            #    2026-10-24 服务器上就踩过：SSH 无桌面会话看不到 R:，
            #    扫到 0 个 → 索引被清空 → "已下载"TAB 全空。
            try:
                old_cnt = db.count_existing()
            except Exception:
                old_cnt = 0
            if not rows and old_cnt:
                self._log.warning(
                    "扫描未找到任何文件（%d 个根目录），**保留原有 %d 条索引不覆盖**"
                    "（多半是目录暂时不可访问：网络盘未映射/掉线/权限问题）",
                    len(self.known_dirs), old_cnt)
                return old_cnt
            db.replace_existing(rows)
            # ⭐ 写下载记录（已下好的片子要能在「下载记录」里查到）
            try:
                n_rec = db.upsert_downloads_from_scan(rows)
            except Exception as e:
                n_rec = 0
                self._log.warning("写下载记录失败（不影响索引）：%s", e)
            with self._lock:
                self._loaded_at = 0.0
            self._ensure_mem()
            self._log.info("已下载索引重建：%d 个根目录，%d 个文件（下载记录 %d 条）",
                           len(self.known_dirs), len(rows), n_rec)
            return len(rows)
        except Exception as e:
            self._log.warning("重建已下载索引失败：%s", e)
            return 0
        finally:
            with self._lock:
                self._scanning = False

    def ensure_ready(self, max_age_hours=STALE_HOURS, on_progress=None, on_done=None):
        """需要时才扫描（过期/空索引且配置了目录）。后台线程执行，不阻塞调用方。

        ``on_done(count)``：扫描完成后回调（**在后台线程里**，UI 层要自己切回主线程）。
        """
        if not self.known_dirs:
            return False
        try:
            cnt = db.count_existing()
        except Exception:
            cnt = 0
        stale = (time.time() - self._db_mtime_or_zero()) > max_age_hours * 3600
        if cnt and not stale:
            self._ensure_mem()
            if on_done:
                try:
                    on_done(cnt)
                except Exception:
                    pass
            return False
        if cnt == 0 and self._scanning:
            return False

        def _worker():
            n = 0
            try:
                n = self.refresh(on_progress)
            except Exception:
                n = 0
            if on_done:
                try:
                    on_done(n)
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return True

    def _db_mtime_or_zero(self):
        try:
            p = db.get_db_path()
            return os.path.getmtime(p) if os.path.isfile(p) else 0.0
        except OSError:
            return 0.0

    # ---------- 查询 ----------
    def lookup(self, video, extra_dirs=None):
        """查这个视频是否已在（其它）目录下过。返回 mp4 路径或 None。

        ⚠️ 顺序很重要：**先查内存索引**（DB，毫秒级），再走路径快查。
           早前是"路径快查优先"，每个作品要试 3~6 个候选路径的 ``isfile``，
           在 NAS 上一次往返几十毫秒 ⇒ 23 行能卡好几秒，而且网络抖动时
           ``isfile`` 抛异常被静默当成"没下过" ⇒ 「已下 7/23」就是这么来的。
        """
        if video is None:
            return None
        # 1) 内存索引（vid 优先，其次 演员+标题）
        self._ensure_mem()
        with self._lock:
            hit = self._mem_vid.get(str(video.vid))
            if hit and _safe_isfile(hit):
                return hit
            ak = norm_key(video.actor)
            tk = norm_key(video.title)
            hit = self._mem_key.get((ak, tk))
            if hit and _safe_isfile(hit):
                return hit
        # 2) 路径快查兜底（索引还没建 / 刚下完还没扫到）
        dirs = list(self.known_dirs) + [d for d in (extra_dirs or []) if d]
        if not dirs:
            # 没有任何已知目录 ⇒ 不做跨目录去重（用户没配就是没开这个能力）
            return None
        for root in dirs:
            for cand in candidate_paths(root, video.actor or "", video.title or ""):
                if _safe_isfile(cand):
                    return os.path.normpath(cand)
        return None

    def status_map(self, videos, output_base):
        """**批量**判定多个作品的已下载状态 → ``{vid: (status, path)}``。

        为什么批量：逐条 ``isfile`` 在 NAS 上是"每个作品一次网络往返"，
        248 行能把界面卡死好几秒。改成"每个演员目录 listdir 一次"，
        剩下的靠内存索引 ⇒ 几十行也只有几次网络访问。

        status 取值与 ``status_of`` 一致：
          ``downloaded`` / ``elsewhere`` / ``none``
        """
        videos = [v for v in (videos or []) if v is not None]
        if not videos:
            return {}
        out = {}
        self._ensure_mem()
        base_norm = os.path.normpath(output_base or "")

        # ---- 1) 索引命中（不额外访问磁盘）----
        with self._lock:
            for v in videos:
                hit = self._mem_vid.get(str(v.vid))
                if not hit:
                    hit = self._mem_key.get((norm_key(v.actor), norm_key(v.title)))
                if hit:
                    p = os.path.normpath(hit)
                    # 在当前下载目录里 → downloaded；在别的目录 → elsewhere
                    if base_norm and p.startswith(base_norm):
                        out[str(v.vid)] = ("downloaded", p)
                    else:
                        out[str(v.vid)] = ("elsewhere", p)

        # ---- 2) 按演员分组，listdir 一次看有没有对应成品 ----
        roots = []
        if output_base:
            roots.append(output_base)
        for r in self.known_dirs:
            if r not in roots:
                roots.append(r)
        by_actor = {}
        for v in videos:
            if str(v.vid) in out:
                continue
            by_actor.setdefault(v.actor or "", []).append(v)

        for actor, vs in by_actor.items():
            titles = {}          # 标题(清洗后) -> [根目录, ...]
            for root in roots:
                actor_dir = os.path.join(root, actor or "")
                try:
                    names = os.listdir(actor_dir)
                except OSError:
                    continue
                for n in names:
                    # 新版是**目录**「标题」，旧版是**文件**「标题.mp4」
                    # → 统一去掉扩展名后再比对
                    stem = os.path.splitext(n)[0]
                    titles.setdefault(stem, []).append(root)
            for v in vs:
                t = _safe(v.title)
                roots_hit = titles.get(t)
                if not roots_hit:
                    continue
                # 先在**当前下载目录**里找（roots 第一个就是它），
                # 找到 → downloaded；只在别的目录有 → elsewhere
                found = ""
                for root in roots_hit:
                    for cand in (os.path.join(root, actor or "", t, t + ".mp4"),
                                 os.path.join(root, actor or "", t + ".mp4")):
                        if _safe_isfile(cand):
                            found = cand
                            break
                    if found:
                        break
                if found:
                    p = os.path.normpath(found)
                    st = ("downloaded" if (base_norm and p.startswith(base_norm))
                          else "elsewhere")
                    out[str(v.vid)] = (st, p)
                else:
                    # 目录在但文件不在（可能下到一半）→ 仍算"其它目录已存在"
                    out[str(v.vid)] = ("elsewhere", os.path.join(
                        roots_hit[0], actor or "", t))
        # 没命中的补 none
        for v in videos:
            out.setdefault(str(v.vid), ("none", None))
        return out

    def downloaded_count_by_actor(self):
        """按演员统计已下载数量 → ``{规范化演员名: n}``（用于演员卡"已下 N"）。"""
        return db.count_existing_by_actor()

    def status_of(self, video, output_base):
        """判断某视频的"已下载状态"，给作品表打标记用。

        返回 ``(status, path)``：
          - ``("downloaded",  path)``   → 当前下载目录里已有成品（本地下完的）
          - ``("elsewhere",   path)``   → 其它已知目录已存在（下载时会跳过）
          - ``("none",       None)``    → 还没下过

        ⚠️ 判定顺序：**索引优先**（毫秒级、稳）→ 路径快查兜底。
           路径命中当前下载目录 ⇒ downloaded；命中其它目录 ⇒ elsewhere。
        """
        if video is None:
            return ("none", None)
        base_norm = os.path.normpath(output_base or "")
        # 1) 内存索引
        self._ensure_mem()
        with self._lock:
            hit = self._mem_vid.get(str(video.vid))
            if not hit:
                hit = self._mem_key.get((norm_key(video.actor), norm_key(video.title)))
        if hit and _safe_isfile(hit):
            p = os.path.normpath(hit)
            if base_norm and p.startswith(base_norm):
                return ("downloaded", p)
            return ("elsewhere", p)
        # 2) 路径快查兜底（索引还没建 / 刚下完还没扫到）
        for cand in candidate_paths(output_base or "", video.actor or "",
                                    video.title or ""):
            if _safe_isfile(cand):
                return ("downloaded", os.path.normpath(cand))
        # 3) 其它已知目录
        try:
            hit2 = self.lookup(video, extra_dirs=[output_base])
        except Exception:
            hit2 = None
        if hit2:
            return ("elsewhere", os.path.normpath(hit2))
        return ("none", None)

    def add_dir_no_clear(self, path):
        """追加一个已知目录用于路径映射快查，**不**清理已建索引。

        用于"改下载目录"场景：把新旧两个下载根都纳入查找，使两边已下的片子
        都能被识别为"已存在"而跳过；旧索引（扫描自其它目录）仍有效，避免每次
        改目录都触发整盘重扫。
        """
        if not path:
            return
        n = os.path.normpath(path.strip())
        if n and n not in self.known_dirs:
            self.known_dirs.append(n)
            self._log.info("去重索引追加查找根（不清库）：%s", n)


def _safe(name):
    try:
        from .utils import sanitize_filename
        return sanitize_filename(name)
    except Exception:
        return re.sub(r'[<>:"/\\|?*]', "_", str(name or ""))


def _safe_isfile(path: str) -> bool:
    """``os.path.isfile`` 的安全版：**异常不当成"存在"，也不静默丢掉**。

    ⚠️ NAS 上 ``isfile`` 可能抛 OSError（断连/路径过长/权限）。
       早前直接用 ``os.path.isfile``，异常被上层 catch 成 "没下过" ⇒
       统计偏小（实测「已下 7/23」）。这里把异常单独记一笔。
    """
    if not path:
        return False
    try:
        if not os.path.isfile(path):
            return False
        try:
            return os.path.getsize(path) > 0
        except OSError:
            return True      # 文件在但取不到大小（权限/断连）→ 保守当存在
    except OSError:
        return False


def candidate_paths(root: str, actor: str, title: str) -> list:
    """某视频在某个根目录下的**候选成品路径**（按优先级）。

    新版：``{root}/{演员}/{标题}/{标题}.mp4``
    旧版：``{root}/{演员}/{标题}.mp4``

    两种都要给 —— 老片已经下在新版之前，改结构后不能变成"没下过"。
    """
    out = []
    actor_dir = os.path.normpath(os.path.join(root, actor or ""))
    for name in _degraded_names(_safe(title)):
        out.append(os.path.normpath(os.path.join(actor_dir, name, name + ".mp4")))
        out.append(os.path.normpath(os.path.join(actor_dir, name + ".mp4")))
    return out


def _infer_actor_title(dirpath: str, stem: str):
    """从文件所在目录推断 (演员, 标题)。

    新版结构：``{root}/{演员}/{标题}/{标题}.mp4`` → 目录名 == 文件名 ⇒
    演员 = 上一级目录名，标题 = 目录名。
    旧版结构：``{root}/{演员}/{标题}.mp4`` → 演员 = 目录名，标题 = 文件名。
    """
    dir_name = os.path.basename(dirpath)
    if dir_name == stem:
        parent = os.path.dirname(dirpath)
        actor = os.path.basename(parent) if parent else ""
        return actor, dir_name
    return dir_name, stem


# ---------- 进程级单例 ----------
_INDEX = None
_INDEX_LOCK = threading.Lock()


def _load_known_dirs() -> list:
    """读已知扫描目录：**数据库优先**，读不到才回落 settings.json。

    2026-10-29：用户要求"扫描文件夹目录要记录到数据库"。以前只存在
    settings.json 里，换台机器/重装后配置就丢了，索引也就建不起来 ——
    于是每个作品都被判成"未下载"，重复下载。现在以 tangxin.db 的 meta 表
    为准，首次启动把 settings 里的旧值迁移进库，之后一律走库。
    """
    import json

    dirs: list = []
    try:
        from . import cache

        raw = cache.get_meta("known_dirs", "")
        if raw:
            got = json.loads(raw)
            if isinstance(got, list):
                dirs = [str(d) for d in got if str(d or "").strip()]
    except Exception:
        dirs = []
    if not dirs:
        try:
            from . import settings

            data = settings.load(apply_to_config=False)
            dirs = [str(d) for d in (data.get("known_dirs") or [])
                    if str(d or "").strip()]
        except Exception:
            dirs = []
        if dirs:
            # 迁移：把 settings 里的旧配置写进库，以后就以库为准
            try:
                save_known_dirs(dirs)
            except Exception:
                pass
    return dirs


def save_known_dirs(dirs) -> list:
    """把已知扫描目录**持久化到数据库**（tangxin.db 的 meta 表）。"""
    import json

    from . import cache as _cache

    out = []
    for d in dirs or []:
        d = str(d or "").strip()
        if d and d not in out:
            out.append(d)
    try:
        _cache.set_meta("known_dirs", json.dumps(out, ensure_ascii=False))
    except Exception:
        pass
    # settings 里也留一份（旧版本/其它模块还在读它，避免行为不一致）
    try:
        from . import settings

        s = settings.load(apply_to_config=False)
        s["known_dirs"] = out
        settings.save(s)
    except Exception:
        pass
    return out


def get_index() -> ExistingIndex:
    """进程级单例。

    ⚠️ 会自动把 ``config.OUTPUT_BASE``（当前下载根目录）也纳入已知目录：
       服务器上 NAS 往往映射成**本机的一个盘符**，而这正是最该被扫描的根。
       即使不手动配置任何目录，仅凭它也能识别"已经下到这份 NAS 上"的片子。
    """
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            dirs = _load_known_dirs()
            out = getattr(config, "OUTPUT_BASE", "") or ""
            if out:
                norm = [os.path.normpath(d) for d in dirs]
                if os.path.normpath(out) not in norm:
                    dirs.append(out)
            _INDEX = ExistingIndex(dirs)
        return _INDEX


def apply_known_dirs(dirs) -> ExistingIndex:
    """用户**主动改动**已知目录时调用：写库 + 配置索引（自动并入 OUTPUT_BASE）。

    与 ``settings.apply()`` 的区别：那个只写 config，不碰索引（否则每次启动
    都会因列表不一致而清库重扫）；这个才是真正的"用户改了配置"入口。
    """
    all_dirs = [d for d in (dirs or []) if d]
    save_known_dirs(all_dirs)          # 持久化到 tangxin.db
    out = getattr(config, "OUTPUT_BASE", "") or ""
    if out and os.path.normpath(out) not in [os.path.normpath(d) for d in all_dirs]:
        all_dirs.append(out)
    idx = get_index()
    idx.configure(all_dirs)
    return idx


def record_downloaded(video, mp4_path, size_bytes=0, nfo_path="",
                      poster_path="", duration_sec=0, root=None):
    """刚下载完的成品**立即**写进索引 + 下载记录（不用等全盘扫描）。

    这样「已下 N/M」的统计、跨目录去重、「下载记录」立刻就是对的。
    """
    if not mp4_path:
        return False
    root = root or getattr(config, "OUTPUT_BASE", "") or ""
    try:
        st = os.stat(mp4_path)
        size_bytes = size_bytes or st.st_size
        mtime = st.st_mtime
    except OSError:
        mtime = 0.0
    row = {
        "root": root,
        "vid": str(getattr(video, "vid", "") or ""),
        "actor": getattr(video, "actor", "") or "",
        "title": getattr(video, "title", "") or "",
        "mp4_path": os.path.normpath(mp4_path),
        "size_bytes": size_bytes,
        "mtime": mtime,
        "nfo_path": nfo_path or "",
        "poster_path": poster_path or "",
        "duration_sec": duration_sec or 0,
    }
    try:
        db.upsert_existing(row)
    except Exception:
        pass
    try:
        db.upsert_downloads_from_scan([row])
    except Exception:
        pass
    # 内存索引同步更新（下一个查询立刻能命中）
    try:
        idx = get_index()
        with idx._lock:
            if row["vid"]:
                idx._mem_vid[row["vid"]] = row["mp4_path"]
            ak, tk = norm_key(row["actor"]), norm_key(row["title"])
            if ak or tk:
                idx._mem_key[(ak, tk)] = row["mp4_path"]
    except Exception:
        pass
    return True


def reset_index():
    """测试用：清空单例。"""
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None
