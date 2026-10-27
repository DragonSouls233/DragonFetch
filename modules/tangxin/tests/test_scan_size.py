# -*- coding: utf-8 -*-
"""扫描入库的体积正确性测试（2026-10-24 新增）。

回归点：同一个 mp4 会被「.nfo 行」和「.mp4 行」各扫到一次，去重时若
nfo 行胜出，``size_bytes`` 就变成 nfo 的几百字节 ⇒「已下总大小」显示 0.00 GB
（服务器上 307 部全部如此，真实约 220 GB）。修复后**必须让 mp4 行胜出**，
同时把 nfo 提供的 vid/title 合并回 mp4 行。
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

TMP = tempfile.mkdtemp(prefix="tx_scan_size_")
# ⭐ 必须在 import tangxin.db 之前设好（config 在 import 时读环境变量）
os.environ["TX_DB"] = os.path.join(TMP, "test.db")
os.environ["TX_DATA"] = TMP

from tangxin import existing   # noqa: E402

ACTOR = "测试演员"
TITLE = "测试影片"
MP4_SIZE = 5 * 1024 * 1024


def test_scan_keeps_real_size():
    base = os.path.join(TMP, "media")
    d = os.path.join(base, ACTOR, TITLE)
    os.makedirs(d)
    with open(os.path.join(d, TITLE + ".mp4"), "wb") as f:
        f.write(b"\x00" * MP4_SIZE)
    with open(os.path.join(d, TITLE + ".nfo"), "w", encoding="utf-8") as f:
        f.write("<movie><title>%s</title><actor><name>%s</name></actor>"
                "<uniqueid type=\"vid\">12345</uniqueid>"
                "<runtime>120</runtime></movie>\n" % (TITLE, ACTOR))

    idx = existing.ExistingIndex([base])
    n = idx.refresh()
    assert n == 1, "去重后应只剩 1 行，实际 %d" % n

    c = sqlite3.connect(os.environ["TX_DB"])
    rows = c.execute("SELECT size_bytes, vid, title FROM existing_files").fetchall()
    c.close()

    assert len(rows) == 1, "库中应有 1 行，实际 %d" % len(rows)
    size, vid, title = rows[0]
    assert size == MP4_SIZE, \
        "size_bytes 不是真实 mp4 体积（nfo 行胜出）: %s != %s" % (size, MP4_SIZE)
    assert vid == "12345", "nfo 提供的 vid 没合并进来: %r" % vid
    assert title == TITLE, "标题不对: %r" % title
    print("[scan] 扫描入库：体积真实(%d) + nfo 的 vid 已合并: PASS" % size)


if __name__ == "__main__":
    try:
        test_scan_keeps_real_size()
        print("\nALL SCAN-SIZE TESTS PASSED")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
