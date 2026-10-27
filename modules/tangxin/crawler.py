# -*- coding: utf-8 -*-
"""糖心爬虫：演员云 + 单演员视频列表。

实测确认的页面结构（2026-10-22）：
- 演员云 /zh-tw/a/ ：单页，约 805 位演员。
    演员条目正则：
      href="/(?:zh-tw/)?a/([^"]+)/"   -> slug(URL 编码)
      class="name" ... > 名字 </span>
      class="num"  ... > 作品数 </span>
- 演员页 /zh-tw/a/{slug}/ 与 /zh-tw/a/{slug}/{N}/ ：路径分页，每页 24 个 <article class="card">。
    下页链接： <link rel="next" href=".../{slug}/2"> （?page=N 无效！）
    视频卡片 <article class="card"> 内含：
      <a class="cover-link" href="/zh-tw/v/{id}/" aria-label="标题">
      <img src="https://t.5gcdn.xyz/videos/{id}/cover.jpg" ...>
      <h3 class="title"><a ...>标题</a></h3>
      <span class="duration">140:17</span>
"""
import re
from urllib.parse import quote, unquote

from . import config
from .models import Actor, Video
from .httpclient import HttpClient

# 演员云：slug + 名字 + 作品数
ACTOR_RE = re.compile(
    r'href="/(?:zh-tw/)?a/([^"]+)/"[^>]*>[\s\S]*?'
    r'class="name"[^>]*>\s*([^<]+?)\s*</span>[\s\S]*?'
    r'class="num"[^>]*>\s*(\d+)\s*</span>'
)

# 单个视频卡片
CARD_BLOCK_RE = re.compile(r'<article class="card".*?</article>', re.S)
VID_RE = re.compile(r'href="/(?:zh-tw/)?v/(\d+)/"')
TITLE_RE = re.compile(r'class="title"[^>]*>\s*<a[^>]*>([^<]+)</a>')
COVER_RE = re.compile(r'<img[^>]*src="(https://t\.5gcdn\.xyz/videos/\d+/cover\.jpg)"')
DURATION_RE = re.compile(r'class="duration"[^>]*>([\d:]+)<')


class TangXinCrawler:
    def __init__(self):
        self.client = HttpClient()

    # ---- 演员云 ----
    def fetch_actor_cloud(self, progress=None) -> list:
        """返回 List[Actor]（含 slug/name/count，不含 videos）。"""
        url = f"{config.ORIGIN}{config.LOCALE}/a/"
        if progress:
            progress("正在抓取演员云: %s" % url)
        html = self.client.get_text(url, is_cdn=False)
        found = ACTOR_RE.findall(html)
        actors = []
        for slug_enc, name_raw, count_raw in found:
            name = unquote(slug_enc)
            # 部分条目 name span 内容比 slug 解码更准（繁体），优先用 span 文本
            span_name = name_raw.strip()
            if span_name:
                name = span_name
            try:
                count = int(count_raw)
            except ValueError:
                count = 0
            actors.append(Actor(slug=slug_enc, name=name, count=count))
        if progress:
            progress("演员云抓取完成，共 %d 位演员" % len(actors))
        return actors

    # ---- 单演员视频 ----
    def fetch_actor_videos(self, actor: Actor, max_videos: int = 0, progress=None) -> list:
        """翻页抓取某演员的全部视频，写入 actor.videos 并返回。"""
        videos = []
        seen = set()
        page = 1
        while True:
            url = self._actor_page_url(actor.slug, page)
            try:
                html = self.client.get_text(url, is_cdn=False)
            except Exception as e:
                # ⭐ 404 = "没有下一页"（演员只有一页时第 2 页必然 404），
                #    这是**正常结尾**，别当警告刷屏；其它错误才提示。
                if "404" not in str(e) and progress:
                    progress("  [警告] 演员页 %s 失败: %s" % (url, e))
                break
            blocks = CARD_BLOCK_RE.findall(html)
            if not blocks:
                break  # 空页，到底了
            page_added = 0
            for b in blocks:
                m = VID_RE.search(b)
                if not m:
                    continue
                vid = m.group(1)
                if vid in seen:
                    continue
                seen.add(vid)
                t = TITLE_RE.search(b)
                title = t.group(1).strip() if t else (m.group(0) or vid)
                c = COVER_RE.search(b)
                cover = c.group(1) if c else ""
                d = DURATION_RE.search(b)
                duration = d.group(1) if d else ""
                v = Video(vid=vid, title=title, cover_url=cover,
                          duration=duration, actor=actor.name)
                videos.append(v)
                page_added += 1
                if max_videos and len(videos) >= max_videos:
                    break
            if progress:
                progress("  %s 第 %d 页 +%d (累计 %d)" %
                         (actor.name, page, page_added, len(videos)))
            if page_added == 0:
                break
            if max_videos and len(videos) >= max_videos:
                break
            page += 1
            if page > 200:  # 安全阀
                break
        actor.videos = videos
        if progress:
            progress("%s 共 %d 个视频" % (actor.name, len(videos)))
        return videos

    @staticmethod
    def _actor_page_url(slug_enc: str, page: int) -> str:
        base = "%s%s/a/%s" % (config.ORIGIN, config.LOCALE, slug_enc)
        if page <= 1:
            return base + "/"
        return "%s/%d/" % (base, page)

    def close(self):
        self.client.close()
