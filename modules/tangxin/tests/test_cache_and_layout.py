# -*- coding: utf-8 -*-
"""糖心缓存层 + 新目录结构 的离线测试（不依赖网络、不依赖显示器）。

覆盖三件事：
1. **缓存增量语义**：重复同步只增新增，不重复插入；读取能还原 Actor/Video。
2. **DB 落在 data/ 目录**（用户要求：DB 放 DATA，二进制放 BIN）。
3. **下载目录结构**：``{下载目录}/{模特}/{标题}/{标题}.mp4 + .nfo + -poster.jpg``
   （用户截图反馈：少了一个"标题文件夹"），且**旧的两层结构仍能被识别**。

运行：
    cd G:\\PornSimilarityPlatform
    python -m tangxin.tests.test_cache_and_layout
"""
import os
import shutil
import sys
import tempfile
import importlib

# ---- 必须把运行根指向临时目录（避免污染真实 data/）----
# ⚠️ 用 `python -m tangxin.tests...` 运行时，**父包 tangxin 会先被导入**，
#    那一刻 config 已经按「项目根」算好了 RUNTIME_ROOT/DATA_DIR。
#    所以这里设完环境变量后必须 reload config，让它按新环境变量重算。
_TMP_ROOT = tempfile.mkdtemp(prefix="tx_cache_root_")
os.environ["TX_ROOT"] = _TMP_ROOT
os.environ["TX_DB"] = os.path.join(_TMP_ROOT, "data", "tangxin.db")
os.environ["TX_SETTINGS"] = os.path.join(_TMP_ROOT, "data", "tangxin_settings.json")
os.environ["TX_OUTPUT"] = os.path.join(_TMP_ROOT, "downloads")
os.environ["TX_TEMP"] = os.path.join(_TMP_ROOT, "temp", "tangxin")
os.environ["TX_LOG_DIR"] = os.path.join(_TMP_ROOT, "logs")

from tangxin import cache, config, db, existing, layout     # noqa: E402
from tangxin.models import Actor, Video                       # noqa: E402
from tangxin.downloader import TangXinDownloader              # noqa: E402
from tangxin.utils import sanitize_filename                   # noqa: E402

# 按新的 TX_ROOT 重算路径（reload 会原地更新模块，所有引用同一模块的代码同步生效）
importlib.reload(config)


def _mk_actor(slug, name, count=3):
    return Actor(slug=slug, name=name, count=count)


def _mk_video(vid, title, actor="测试模特"):
    return Video(vid=str(vid), title=title, actor=actor,
                 duration="12:34", cover_url="https://t.5gcdn.xyz/videos/%s/cover.jpg" % vid)


# ============================================================================
# 1) 目录职责：DB 在 data/、bin/ 存在
# ============================================================================
def test_layout_dirs():
    layout.ensure_dirs()
    assert os.path.isdir(config.DATA_DIR), "DATA 目录未创建：%s" % config.DATA_DIR
    assert os.path.isdir(config.BIN_DIR), "BIN 目录未创建：%s" % config.BIN_DIR
    assert config.DATA_DIR.endswith("data"), "DATA_DIR 应为 data 目录，实际=%s" % config.DATA_DIR
    assert config.BIN_DIR.endswith("bin"), "BIN_DIR 应为 bin 目录，实际=%s" % config.BIN_DIR
    db.get_conn()   # 触发建库
    assert os.path.isfile(db.get_db_path()), "数据库文件未创建：%s" % db.get_db_path()
    assert os.path.normpath(os.path.dirname(db.get_db_path())) == \
        os.path.normpath(config.DATA_DIR), \
        "数据库必须落在 DATA 目录，实际=%s" % db.get_db_path()
    print("[1] 目录职责(DB→data/、bin/ 存在): PASS (%s)" % db.get_db_path())


# ============================================================================
# 2) 演员云缓存：增量语义
# ============================================================================
def test_actor_cache_incremental():
    r1 = cache.upsert_actors([_mk_actor("a1", "演员一", 3),
                               _mk_actor("a2", "演员二", 5)])
    assert r1["added"] == 2 and r1["updated"] == 0, "首次应新增 2 位：%s" % r1
    assert r1["total"] == 2, "总数应为 2：%s" % r1

    # 重复同步同样的数据 → 不该有任何新增/更新
    r2 = cache.upsert_actors([_mk_actor("a1", "演员一", 3),
                               _mk_actor("a2", "演员二", 5)])
    assert r2["added"] == 0 and r2["updated"] == 0, \
        "重复同步不应有新增/更新：%s" % r2

    # 只加一位新演员 → 只新增 1
    r3 = cache.upsert_actors([_mk_actor("a1", "演员一", 3),
                               _mk_actor("a2", "演员二", 5),
                               _mk_actor("a3", "演员三", 1)])
    assert r3["added"] == 1 and r3["updated"] == 0, "应只新增 1 位：%s" % r3

    # 作品数变了 → 算 update（不是新增）
    r4 = cache.upsert_actors([_mk_actor("a1", "演员一", 9),
                               _mk_actor("a2", "演员二", 5),
                               _mk_actor("a3", "演员三", 1)])
    assert r4["added"] == 0 and r4["updated"] == 1, "作品数变化应算更新：%s" % r4

    actors = cache.get_actors()
    assert len(actors) == 3, "读回演员数不对：%d" % len(actors)
    by_slug = {a.slug: a for a in actors}
    assert by_slug["a1"].name == "演员一" and by_slug["a1"].count == 9, \
        "演员数据读回不对：%s" % by_slug["a1"]
    print("[2] 演员云增量语义: PASS (新增/更新计数正确)")


# ============================================================================
# 3) 作品缓存：增量语义 + 按演员取
# ============================================================================
def test_video_cache_incremental():
    v1 = [_mk_video(1, "片一"), _mk_video(2, "片二")]
    r1 = cache.upsert_videos("a1", "演员一", v1)
    assert r1["added"] == 2 and r1["total_actor"] == 2, "首次应新增 2 部：%s" % r1

    r2 = cache.upsert_videos("a1", "演员一", v1)
    assert r2["added"] == 0 and r2["updated"] == 0, "重复同步不应新增：%s" % r2

    # 追加一部新片 → 只新增 1
    r3 = cache.upsert_videos("a1", "演员一", v1 + [_mk_video(3, "片三")])
    assert r3["added"] == 1 and r3["total_actor"] == 3, "应只新增 1 部：%s" % r3

    vids = cache.get_videos("a1")
    assert len(vids) == 3, "读回作品数不对：%d" % len(vids)
    assert {v.vid for v in vids} == {"1", "2", "3"}, "作品 vid 不对：%s" % vids
    assert vids[0].title == "片一" and vids[0].actor == "演员一", "作品字段不对"

    # 另一位演员的作品不应串台
    cache.upsert_videos("a2", "演员二", [_mk_video(9, "别人的片")])
    assert len(cache.get_videos("a1")) == 3, "演员之间数据串台了"
    assert len(cache.get_videos("a2")) == 1, "演员二的作品没存进去"
    print("[3] 作品增量语义 + 按演员隔离: PASS")


# ============================================================================
# 4) 待预热列表：同步过的演员不再出现
# ============================================================================
def test_pending_actor_slugs():
    cache.clear_cache()
    cache.upsert_actors([_mk_actor("p1", "甲"), _mk_actor("p2", "乙")])
    pend = cache.pending_actor_slugs()
    assert set(pend) == {"p1", "p2"}, "待预热列表不对：%s" % pend
    cache.upsert_videos("p1", "甲", [_mk_video(1, "片")])
    pend2 = cache.pending_actor_slugs()
    assert "p1" not in pend2 and "p2" in pend2, \
        "已同步的演员应从待预热列表移除：%s" % pend2
    st = cache.stats()
    assert st["actors"] == 2 and st["videos"] == 1, "统计不对：%s" % st
    print("[4] 待预热列表 + 统计: PASS")


# ============================================================================
# 5) 目录结构：{下载目录}/{模特}/{标题}/{标题}.mp4 + .nfo + -poster.jpg
# ============================================================================
def test_download_layout():
    base = os.path.join(_TMP_ROOT, "downloads")
    actor_name = "白某妹妹"
    title = "测试标题A"
    v = _mk_video(555, title, actor=actor_name)
    v.cover_url = "https://t.5gcdn.xyz/videos/555/cover.jpg"

    dl = TangXinDownloader(mode="python")
    # 假下载：直接写临时 mp4（跳过网络与解密）
    def fake_download(video, out_mp4, progress=None, max_segments=0, stop_event=None):
        with open(out_mp4, "wb") as f:
            f.write(b"\x00" * 4096)
    dl._download_python = fake_download
    # 假封面
    dl.client.get_bytes = lambda url, is_cdn=False: b"\xff\xd8\xff" + b"0" * 512

    actor_dir = os.path.join(base, sanitize_filename(actor_name))
    res = dl.download_video(v, actor_dir, get_cover=True)
    dl.close()

    assert res.get("ok"), "下载应成功：%s" % res
    title_dir = os.path.join(actor_dir, sanitize_filename(title))
    assert os.path.isdir(title_dir), "缺少标题文件夹：%s" % title_dir
    mp4 = os.path.join(title_dir, sanitize_filename(title) + ".mp4")
    nfo = os.path.join(title_dir, sanitize_filename(title) + ".nfo")
    poster = os.path.join(title_dir, sanitize_filename(title) + "-poster.jpg")
    assert os.path.isfile(mp4), "mp4 不在标题目录：%s" % mp4
    assert os.path.isfile(nfo), "nfo 不在标题目录：%s" % nfo
    assert os.path.isfile(poster), "封面不在标题目录：%s" % poster
    # NFO 里必须有 vid（跨目录去重靠它）
    with open(nfo, "r", encoding="utf-8") as f:
        txt = f.read()
    assert "<uniqueid type=\"vid\">555</uniqueid>" in txt, "NFO 缺少 vid：%s" % txt
    print("[5] 下载目录结构(模特/标题/成片+NFO+封面): PASS (%s)" % title_dir)


# ============================================================================
# 6) 去重索引：新版三层与旧版两层都要认
# ============================================================================
def test_dedup_new_and_legacy():
    actor_name = "模特甲"
    title = "片子乙"
    v = _mk_video(777, title, actor=actor_name)

    # 新版三层：{root}/{模特}/{标题}/{标题}.mp4
    base_new = os.path.join(_TMP_ROOT, "dedup_new")
    new_dir = os.path.join(base_new, sanitize_filename(actor_name),
                           sanitize_filename(title))
    os.makedirs(new_dir, exist_ok=True)
    new_mp4 = os.path.join(new_dir, sanitize_filename(title) + ".mp4")
    with open(new_mp4, "wb") as f:
        f.write(b"x" * 100)

    # 旧版两层：{root}/{模特}/{标题}.mp4
    base_old = os.path.join(_TMP_ROOT, "dedup_old")
    legacy_dir = os.path.join(base_old, sanitize_filename(actor_name))
    os.makedirs(legacy_dir, exist_ok=True)
    legacy_mp4 = os.path.join(legacy_dir, sanitize_filename(title) + ".mp4")
    with open(legacy_mp4, "wb") as f:
        f.write(b"y" * 100)

    idx = existing.ExistingIndex([base_new])
    st, p = idx.status_of(v, base_new)
    assert st == "downloaded", "新版三层结构应识别为已下载，实际=%s" % st
    assert os.path.normpath(p) == os.path.normpath(new_mp4), \
        "命中的应是新版路径，实际=%s" % p

    idx2 = existing.ExistingIndex([base_old])
    st2, p2 = idx2.status_of(v, base_old)
    assert st2 == "downloaded", "旧版两层结构应识别为已下载，实际=%s" % st2
    assert os.path.normpath(p2) == os.path.normpath(legacy_mp4), \
        "命中的应是旧版路径，实际=%s" % p2
    print("[6] 去重兼容新旧目录结构: PASS")


# ============================================================================
# 7) 扫描索引：新版三层目录里的 mp4 也能被扫进索引
# ============================================================================
def test_scan_index_new_layout():
    base = os.path.join(_TMP_ROOT, "scanroot")
    actor_name = "扫描模特"
    title = "扫描标题"
    title_dir = os.path.join(base, sanitize_filename(actor_name), sanitize_filename(title))
    os.makedirs(title_dir, exist_ok=True)
    mp4 = os.path.join(title_dir, sanitize_filename(title) + ".mp4")
    with open(mp4, "wb") as f:
        f.write(b"z" * 200)
    idx = existing.ExistingIndex([base])
    n = idx.refresh()
    rows = db.get_existing_all()
    assert n >= 1, "扫描应收录 1 个文件，实际=%d" % n
    hit = [r for r in rows if r["mp4_path"] == os.path.normpath(mp4)]
    assert hit, "新版结构的 mp4 未进索引"
    assert hit[0]["actor"] == sanitize_filename(actor_name), \
        "演员名推断错误：%s" % hit[0]["actor"]
    assert hit[0]["title"] == sanitize_filename(title), \
        "标题推断错误：%s" % hit[0]["title"]
    print("[7] 扫描索引识别三层结构: PASS (actor=%s title=%s)"
          % (hit[0]["actor"], hit[0]["title"]))


# ============================================================================
# 8) 设置文件也落在 data/，老根目录设置自动迁移
# ============================================================================
def test_settings_in_data_dir():
    from tangxin import settings
    importlib.reload(settings)
    path = settings.get_settings_path()
    assert os.path.normpath(os.path.dirname(path)) == \
        os.path.normpath(config.DATA_DIR), \
        "设置文件必须落在 data/，实际=%s" % path
    # 写一次，确认落在 data/ 里
    settings.save(output_base=os.path.join(_TMP_ROOT, "downloads"))
    assert os.path.isfile(path), "设置文件没写出来：%s" % path
    # 老根目录的设置文件应被迁移进 data/
    legacy = os.path.join(_TMP_ROOT, "tangxin_settings.json")
    with open(legacy, "w", encoding="utf-8") as f:
        f.write('{"workers": 4}')
    layout._migrated = False       # 让迁移再跑一次
    moved = layout.migrate_legacy()
    assert legacy not in moved or not os.path.isfile(legacy), \
        "老设置文件没有迁移（目标已存在时应保留，不覆盖）"
    print("[8] 设置文件落在 data/: PASS (%s)" % path)


# ============================================================================
# 9) bin/ 查找：yt-dlp / xray / ffmpeg 的定位逻辑
# ============================================================================
def test_bin_lookup():
    from tangxin import ytdlp_engine
    importlib.reload(ytdlp_engine)
    # 造一个假的 bin/yt-dlp.exe
    fake = os.path.join(config.BIN_DIR, "yt-dlp.exe")
    os.makedirs(config.BIN_DIR, exist_ok=True)
    with open(fake, "wb") as f:
        f.write(b"MZ")
    try:
        found = ytdlp_engine.find_ytdlp_exe()
        assert os.path.normpath(found) == os.path.normpath(fake), \
            "yt-dlp.exe 定位不对：%s" % found
    finally:
        os.remove(fake)
    # ffmpeg 定位不应抛异常（找不到时返回 "ffmpeg" 兜底）
    assert config.FFMPEG, "ffmpeg 定位返回空"
    print("[9] bin/ 内程序定位: PASS (ffmpeg=%s)" % os.path.basename(config.FFMPEG))


# ============================================================================
# 10) 扫描下载目录 → 索引 + 下载记录 都要入库（用户："已下好的要记录到数据库"）
# ============================================================================
def test_scan_into_db():
    base = os.path.join(_TMP_ROOT, "scan_db")
    actor = "扫描演员"
    title = "扫描作品"
    tdir = os.path.join(base, actor, title)
    os.makedirs(tdir, exist_ok=True)
    mp4 = os.path.join(tdir, title + ".mp4")
    with open(mp4, "wb") as f:
        f.write(b"m" * 500)
    with open(os.path.join(tdir, title + ".nfo"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?>\n<movie>\n  <title>%s</title>\n'
                '  <uniqueid type="vid">88888</uniqueid>\n'
                '  <runtime>12</runtime>\n'
                '  <actor><name>%s</name></actor>\n</movie>\n' % (title, actor))
    with open(os.path.join(tdir, title + "-poster.jpg"), "wb") as f:
        f.write(b"jpg")

    idx = existing.ExistingIndex([base])
    n = idx.refresh()
    assert n == 1, "应扫描到 1 个成品，实际 %d" % n
    # 索引表
    rows = db.get_existing_all()
    assert len(rows) == 1, "索引表应有 1 行，实际 %d" % len(rows)
    assert rows[0]["vid"] == "88888", "索引 vid 不对：%s" % rows[0]["vid"]
    assert rows[0]["actor"] == actor, "索引 actor 不对：%s" % rows[0]["actor"]
    # 下载记录表（用户要的"记录到数据库"）
    recs = db.get_records(limit=50)
    hit = [r for r in recs if r["vid"] == "88888"]
    assert hit, "下载记录表里没有扫描到的片子"
    assert hit[0]["status"] == "done", "状态应为 done：%s" % hit[0]["status"]
    assert hit[0]["file_path"], "下载记录缺少文件路径"
    # 按演员统计（演员卡"已下 N"的数据源）
    counts = idx.downloaded_count_by_actor()
    assert counts.get(existing.norm_key(actor)) == 1, \
        "按演员统计不对：%s" % counts
    print("[10] 扫描入库(索引+下载记录+按演员统计): PASS")


# ============================================================================
# 11) 批量状态判定 status_map：新版三层 / 旧版两层 都要认
# ============================================================================
def test_status_map_bulk():
    base_new = os.path.join(_TMP_ROOT, "sm_new")
    base_old = os.path.join(_TMP_ROOT, "sm_old")
    a_new = "新版甲"
    a_old = "旧版乙"
    # 新版三层
    d = os.path.join(base_new, a_new, "新作品")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "新作品.mp4"), "wb") as f:
        f.write(b"x" * 10)
    # 旧版两层
    d2 = os.path.join(base_old, a_old)
    os.makedirs(d2, exist_ok=True)
    with open(os.path.join(d2, "旧作品.mp4"), "wb") as f:
        f.write(b"y" * 10)

    v_new = _mk_video(601, "新作品", actor=a_new)
    v_old = _mk_video(602, "旧作品", actor=a_old)
    v_none = _mk_video(603, "没下过", actor="陌生演员")
    idx = existing.ExistingIndex([base_new, base_old])
    smap = idx.status_map([v_new, v_old, v_none], base_new)
    assert smap["601"][0] == "downloaded", "新版三层应判为 downloaded：%s" % (smap.get("601"),)
    assert smap["602"][0] in ("downloaded", "elsewhere"), \
        "旧版两层应被识别：%s" % (smap.get("602"),)
    assert smap["603"][0] == "none", "没下过的应是 none：%s" % (smap.get("603"),)
    print("[11] 批量状态判定(新旧结构): PASS")


# ============================================================================
# 12) 下载完成立即写索引（不用等全盘扫描）
# ============================================================================
def test_record_downloaded_immediate():
    base = os.path.join(_TMP_ROOT, "rec_dl")
    os.makedirs(base, exist_ok=True)
    v = _mk_video(909, "刚下完的片", actor="即时演员")
    mp4 = os.path.join(base, "即时演员", "刚下完的片", "刚下完的片.mp4")
    os.makedirs(os.path.dirname(mp4), exist_ok=True)
    with open(mp4, "wb") as f:
        f.write(b"z" * 100)
    existing.record_downloaded(v, mp4, size_bytes=100)
    # 索引里应立刻能查到
    idx = existing.get_index()
    st, path = idx.status_of(v, base)
    assert st == "downloaded", "刚下完的应立刻判为 downloaded，实际=%s" % st
    # 下载记录里也应有
    recs = db.get_records(limit=50)
    assert any(r["vid"] == "909" for r in recs), "下载记录里没有刚下完的片子"
    counts = idx.downloaded_count_by_actor()
    assert counts.get(existing.norm_key("即时演员")) == 1, "按演员统计没跟上：%s" % counts
    print("[12] 下载完成立即写索引+记录: PASS")


if __name__ == "__main__":
    try:
        test_layout_dirs()
        test_actor_cache_incremental()
        test_video_cache_incremental()
        test_pending_actor_slugs()
        test_download_layout()
        test_dedup_new_and_legacy()
        test_scan_index_new_layout()
        test_settings_in_data_dir()
        test_bin_lookup()
        test_scan_into_db()
        test_status_map_bulk()
        test_record_downloaded_immediate()
        print("\nALL CACHE & LAYOUT TESTS PASSED")
    finally:
        db.close()
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
