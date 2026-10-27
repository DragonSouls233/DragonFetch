# SPDX-License-Identifier: GPL-3.0-or-later
"""每个模特的下载记录 Markdown 生成与追加。

数据来源：``download_cache.downloads`` 表（model_name / downloaded_at / title / status）。
每个模特一个 MD 文件，存放在 ``data/download_records/<模特名>.md``：

- ``render_and_write``  ：从 DB 全量渲染（最新在前），用于批量转换历史记录 / 按需查看。
- ``append_record``     ：下载完成时低成本追加一行（保持文件常新，最新在前）。
- ``render_all``        ：把 DB 中全部模特的下载记录批量转换成 MD。
- ``md_to_html``        ：极简 Markdown→HTML（标题/引用/列表/加粗），供 GUI 查看器使用。
"""
import os
import re
import html
import sqlite3
import logging
from datetime import datetime

from multisite.core.utils.path_utils import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)

_ILLEGAL = re.compile(r'[\\/*?:"<>|]')


def sanitize_filename(name: str) -> str:
    """把模特名转成合法文件名（保留中文，替换非法字符）。"""
    if not name:
        return "unknown"
    s = _ILLEGAL.sub('_', name).strip().strip('.')
    return s[:120] or "unknown"


def get_records_dir() -> str:
    return ensure_data_dir('download_records')


def get_model_md_path(model_name: str) -> str:
    return os.path.join(get_records_dir(), sanitize_filename(model_name) + '.md')


def _db_path() -> str:
    return get_data_path('db', 'download_cache.db')


def _size_map() -> dict:
    """URL -> 文件大小（字节）。

    ``downloads.file_size`` 实测全为 0（2026-10-21 本地与服务端均如此），
    真实大小只落在 ``download_logs.db``，两表 URL 100% 重合，故按 URL 关联
    （同 URL 多条取最大值）。
    """
    path = get_data_path('db', 'download_logs.db')
    if not os.path.exists(path):
        return {}
    try:
        conn = sqlite3.connect(path, timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        cur = conn.cursor()
        cur.execute(
            "SELECT video_url, MAX(file_size) FROM download_logs "
            "WHERE video_url IS NOT NULL AND video_url != '' AND file_size > 0 "
            "GROUP BY video_url"
        )
        mapping = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
        conn.close()
        return mapping
    except Exception as e:
        logger.warning("读取下载大小失败: %s", e)
        return {}


def _model_country(model_name: str) -> str:
    """模特国籍（models.db 单条查询），查不到返回「未知」。"""
    path = get_data_path('db', 'models.db')
    if not os.path.exists(path) or not model_name:
        return '未知'
    try:
        conn = sqlite3.connect(path, timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        cur = conn.cursor()
        cur.execute("SELECT country FROM models WHERE name = ? LIMIT 1", (model_name,))
        row = cur.fetchone()
        conn.close()
        return (row[0] or '未知') if row else '未知'
    except Exception as e:
        logger.warning("读取模特国籍失败 %s: %s", model_name, e)
        return '未知'


def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def fetch_records(model_name: str, module: str = 'PORN') -> list:
    """返回该模特的已完成下载记录（最新在前，含大小——大小按 URL 从 download_logs 补）。"""
    path = _db_path()
    if not os.path.exists(path):
        return []
    try:
        sizes = _size_map()
        conn = sqlite3.connect(path, timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        cur = conn.cursor()
        cur.execute(
            "SELECT downloaded_at, title, url, file_path, file_size FROM downloads "
            "WHERE model_name = ? AND status = 'completed' ORDER BY downloaded_at DESC",
            (model_name,),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {
                'downloaded_at': r[0],
                'title': (r[1] or '(无标题)').replace('\n', ' ').strip(),
                'url': r[2] or '',
                'file_path': r[3] or '',
                'size': int(r[4] or 0) or int(sizes.get(r[2]) or 0),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("读取下载记录失败 %s: %s", model_name, e)
        return []


def _fmt_ts(ts) -> str:
    if not ts:
        return ''
    # 2026-05-18T14:28:43.356622 -> 2026-05-18 14:28:43
    return str(ts).replace('T', ' ')[:19]


def render_markdown(model_name: str, records: list, country: str = None) -> str:
    """渲染单个模特的下载记录 MD（国籍 + 统计 + 明细）。"""
    lines = [f"# 下载记录：{model_name}", ""]
    if not records:
        lines.append("> 暂无已完成下载记录。")
        return "\n".join(lines) + "\n"
    total_size = sum(r.get('size') or 0 for r in records)
    first = _fmt_ts(records[-1].get('downloaded_at'))
    last = _fmt_ts(records[0].get('downloaded_at'))
    head = f"> 国籍：{country or '未知'}｜共 {len(records)} 条已完成下载｜总大小 {_fmt_size(total_size)}"
    if first:
        head += f"｜{first} ~ {last}"
    lines.append(head)
    lines.append("")
    lines.append("## 记录明细")
    lines.append("")
    for rec in records:
        size = rec.get('size') or 0
        suffix = f"（{_fmt_size(size)}）" if size else ""
        lines.append(f"- **{_fmt_ts(rec.get('downloaded_at'))}** {rec.get('title', '')}{suffix}")
    return "\n".join(lines) + "\n"


def render_and_write(model_name: str, module: str = 'PORN', country: str = None) -> str:
    """从 DB 全量渲染并写入该模特的 MD 文件，返回路径。

    country 为空时自动从 models.db 查询该模特国籍。
    """
    records = fetch_records(model_name, module)
    if country is None:
        country = _model_country(model_name)
    path = get_model_md_path(model_name)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(render_markdown(model_name, records, country))
    except Exception as e:
        logger.warning("写入下载记录MD失败 %s: %s", path, e)
    return path


def append_record(
    model_name: str,
    title: str,
    url: str = '',
    file_path: str = '',
    downloaded_at=None,
) -> str:
    """下载完成时追加一行（最新在前）。文件不存在则新建。非致命。"""
    path = get_model_md_path(model_name)
    ts = _fmt_ts(downloaded_at) or _fmt_ts(datetime.now().isoformat())
    title = (title or '').replace('\n', ' ').strip() or '(无标题)'
    new_line = f"- **{ts}** {title}"
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            lines = content.split('\n')
            # 在第一个记录行之前插入，保持最新在前
            insert_at = len(lines)
            for i, ln in enumerate(lines):
                if ln.startswith('- '):
                    insert_at = i
                    break
            lines.insert(insert_at, new_line)
            content = '\n'.join(lines)
        else:
            content = (
                f"# 下载记录：{model_name}\n\n"
                f"> 由下载完成事件自动记录，最新在前\n\n{new_line}"
            )
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        logger.warning("追加下载记录MD失败 %s: %s", path, e)
    return path


def render_all(module: str = 'PORN') -> int:
    """批量把 DB 中全部模特的下载记录转换成 MD，返回生成文件数。"""
    path = _db_path()
    if not os.path.exists(path):
        return 0
    try:
        conn = sqlite3.connect(path, timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT model_name FROM downloads "
            "WHERE status='completed' AND model_name IS NOT NULL AND model_name != ''"
        )
        names = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        logger.warning("读取模特列表失败: %s", e)
        return 0
    n = 0
    for name in names:
        try:
            render_and_write(name, module)
            n += 1
        except Exception as e:
            logger.warning("批量生成失败 %s: %s", name, e)
    return n


def _esc(t: str) -> str:
    return html.escape(t)


def md_to_html(md: str) -> str:
    """极简 Markdown→HTML：# 标题 / > 引用 / - 列表 / **加粗**。无第三方依赖。"""
    lines = md.split('\n')
    out = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    for line in lines:
        s = line.rstrip()
        if s.startswith('# '):
            close_list()
            out.append(f'<h1>{_esc(s[2:])}</h1>')
        elif s.startswith('## '):
            close_list()
            out.append(f'<h2>{_esc(s[3:])}</h2>')
        elif s.startswith('> '):
            close_list()
            out.append(f'<p style="color:#666;font-size:12px">{_esc(s[2:])}</p>')
        elif s.startswith('- '):
            if not in_list:
                out.append('<ul>')
                in_list = True
            item = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', _esc(s[2:]))
            out.append(f'<li>{item}</li>')
        elif s.strip() == '':
            close_list()
        else:
            close_list()
            out.append(f'<p>{_esc(s)}</p>')
    close_list()
    return (
        '<html><head><meta charset="utf-8"></head>'
        '<body style="font-family:sans-serif;padding:12px;line-height:1.6">'
        + ''.join(out)
        + '</body></html>'
    )
