# -*- coding: utf-8 -*-
"""站点注册表：支持多个「黄果」系列站点，彼此可区分。

每个站点定义:
  - id / name          标识与展示名（UI 区分用）
  - domain            主域名（用于优先排序）
  - base_url          站点根（detail/play/分类拼接）
  - home_url          列表首页地址（可带路径，如 /go-home/）
  - candidates        候选入口（自动探测用，须是「真站点」地址）
  - publish_pages     发布页 / 中转页（只用于读取最新域名，不可作 base_url）
  - markers           HTML 标记（用来识别该站点）
  - categories        [(标签, 路径), ...]（路径基于 base_url 拼接）
  - pager             分页规则 {style, segment, total_attr}（翻页拉全量用）
  - download_subdir   下载子目录（区分存储）
  - cover_encrypted   封面是否 AES 加密
  - backend           后端模块名（core.backend_xxx），实现 site_api 接口
  - color             区分主题色（UI 标签）
  - note              备注（设置面板展示）
"""

from typing import Dict, List, Optional

# ---------------------------------------------------------------- 站点定义

SITE_HUANGUOAI = {
    "id": "huangguoai",
    "name": "黄果AI短剧",
    "domain": "huangguoai.com",
    "base_url": "https://huangguoai.com",
    # 真正的列表首页是根域；/go-home/ 是「最新地址发布页」，仅作存活探测候选
    "home_url": "https://huangguoai.com/",
    # 备用域名（镜像）：来自官方「回家的路」发布页（go-home），直连无需代理。
    # 主域被墙时，启动检测 / 下载前会自动从这些里挑一个可达的，跳过被屏蔽的。
    #
    # ⚠ 只放「真站点」地址（页面含 hg-drama-card，可直接解析列表/详情/分片）。
    #   2026-10-19 实测更新：*.vqojfzoq.cc 三个子域已全部连接超时（失效），
    #   替换为发布页当前的国内直连地址。发布页会按访问者 IP 轮换，
    #   这里只是「首次启动就大概率可用」的兜底，真实可用集合以
    #   mirror_refresh 运行时刷新结果为准。
    "mirrors": [
        "https://nw85x.ye28r31s.cc",
        "https://b9h02.ye28r31s.cc",
        "https://crb4g.ye28r31s.cc",
    ],
    # 发布页 / 中转页：**不是剧集站**（HTTP 200 但页面无 hg-drama-card），
    # 因此绝不能当作 base_url（会把列表解析成 0 条）。它们只用于
    # core/mirror_refresh.py「读取最新域名」——即「回家的路」的入口。
    "publish_pages": [
        "https://huangguoai.ai/",
        "https://huangguo10.com/",
        "https://huangguo8.com/",
        "https://huangguoai.pages.dev/",
        "https://huangguoai.gitlab.io/",
        "https://huangguoai.com/go-home/",
    ],
"candidates": [
    # 真站点镜像排在前面（列表页评分高，会被优先选中）
        "https://nw85x.ye28r31s.cc/",
        "https://b9h02.ye28r31s.cc/",
        "https://crb4g.ye28r31s.cc/",
        "https://huangguoai.com/",
        "https://huangguoai.com/go-home/",
        "https://huangguoai.ai/",
    ],
    "markers": ["hg-drama-card", "video_list"],
    # 准入镜像用的强标记：**必须出现卡片容器**才算真站点。
    # 仅靠 markers（任一命中）会把「官方最新地址入口」这类发布页误收——
    # 实测 hgai5.com: hg-drama-card=0 但 video_list=1，正是踩这个坑。
    "strong_markers": ["hg-drama-card"],
    # 必须全部命中才认定为该站点。
    # 加入 "huangguoai.com" 是为了与「黄果漫剧」(huangguoac.com) 区分：两站同属
    # 一套前端框架，都含 hg-drama-card；而本站页面必带自己的域名，漫剧站为 0 次。
    "required": ["hg-drama-card", "huangguoai.com"],
    "categories": [
        ("短剧", "/ai-duanju/"),
        ("漫剧", "/ai-manju/"),
        ("换脸", "/ai-huanlian/"),
        ("魔改", "/ai-mogai/"),
        ("排名", "/ranks/hot/"),
        ("专题", "/topics/"),
    ],
    "download_subdir": "huangguoai",
    "cover_encrypted": True,
    "cover_crypt": "aes",           # 封面加密算法：AES-128-CBC
    # 分页：/ai-duanju/{page}/（模板来自 data-jump-tpl）
    "pager": {"style": "path", "segment": "", "total_attr": "data-pages"},
    "backend": "backend_huangguoai",
    "color": "#e64340",
    "note": "AI 短剧原创站（免费 m3u8，可直接下载）",
}

SITE_HUANGUOME = {
    "id": "huangguome",
    "name": "黄果(me)",
    "domain": "huangguo.me",
    "base_url": "https://huangguo.me",
    "home_url": "https://huangguo.me/",
    "candidates": [
        "https://huangguo.me/",
    ],
    "markers": ["poster-card", "poster-cover-img"],
    "strong_markers": ["poster-card"],
    "required": ["poster-cover-img"],    # 排除只有 poster-card 的宣传落地页
    # 分类名取自首页区块真实标题（不是推测）：
    #   精选推荐 -> /category/huangguojx/、今日上新 -> /category/mrgx/、
    #   追剧热榜 -> /category/rebang/、片库 -> /list/ai-short-play/
    "categories": [
        ("精选推荐", "/category/huangguojx/"),
        ("今日上新", "/category/mrgx/"),
        ("追剧热榜", "/category/rebang/"),
        ("片库", "/list/ai-short-play/"),
    ],
    "pager": {"style": "path", "segment": "page", "total_attr": "data-total"},
    "download_subdir": "huangguome",
    "cover_encrypted": True,
    "cover_crypt": "xor",           # 封面加密算法：前 100 字节 XOR（key=2019ysapp7527）
    "backend": "backend_huangguome",
    "color": "#2f7de1",
    "note": "黄果(me) 站（多数分集需金币/会员解锁，仅前 1-2 集免费可下载）",
}

SITE_HUANGGUOAC = {
    "id": "huangguoac",
    "name": "黄果漫剧",
    "domain": "huangguoac.com",
    "base_url": "https://huangguoac.com",
    # 根域即列表首页；/go-home/ 是「最新地址发布页」，仅作存活探测候选
    "home_url": "https://huangguoac.com/",
    "candidates": [
        "https://huangguoac.com/",
        "https://huangguoac.com/go-home/",
    ],
    # 与 huangguoai 同属一套前端框架（都含 hg-drama-card 的 CSS），
    # 但**列表卡片不同**：本站是 hg-comic-feed-card；短剧站是 hg-drama-card。
    "markers": ["hg-comic-feed-card", "lifan-dongman", "hanman"],
    "strong_markers": ["hg-comic-feed-card"],
    # 必须全部命中。lifan-dongman / hanman 是本站全局导航的分类路径
    # （首页/列表/详情/播放/搜索/标签页均为 7 次），在短剧站恒为 0 次，
    # 因此可稳定区分两站，避免同框架互相误判。
    "required": ["lifan-dongman", "hanman"],
    # 分类名取自站点导航真实文案（不是推测）：
    #   AI漫剧 -> /ai-manju/、里番动漫 -> /lifan-dongman/、
    #   成人日漫 -> /riman/、成人韩漫 -> /hanman/
    # 排名 / 专题 放在末尾，与短剧站保持一致的可读顺序
    # （backends 按 URL 特征取址，不依赖下标）。
    "categories": [
        ("AI漫剧", "/ai-manju/"),
        ("里番动漫", "/lifan-dongman/"),
        ("成人日漫", "/riman/"),
        ("成人韩漫", "/hanman/"),
        ("排行榜", "/ranks/hot/"),
        ("专题", "/topics/"),
    ],
    # 分页：/ai-manju/{page}/（模板来自分页条的 data-jump-tpl）
    "pager": {"style": "path", "segment": "", "total_attr": "data-pages"},
    "download_subdir": "huangguoac",
    "cover_encrypted": True,
    "cover_crypt": "aes",           # 与短剧站同为 AES-128-CBC（已实测）
    "backend": "backend_huangguoac",
    "color": "#8e44ad",
    "note": "黄果漫剧站（漫剧/日漫/韩漫，免费 m3u8 可直接下载）",
}

SITE_AIHUANGDOU = {
    "id": "aihuangdou",
    "name": "黄桃短剧",
    "domain": "aihuangdou.com",
    "base_url": "https://aihuangdou.com",
    # 根域即列表首页；/go-home/ 是「回家的路」发布页（title 明确写「官方最新地址
    # 发布页_回家的路」），只用于读最新域名，不能当 base_url。
    "home_url": "https://aihuangdou.com/",
    "candidates": [
        "https://aihuangdou.com/",
    ],
    "publish_pages": [
        "https://aihuangdou.com/go-home/",
    ],
    # 2026-10-28 实测：与黄果树（hg-drama-card）**不是同一套前端**，
    # 卡片是 article.drama-card + .poster-orbit + .poster-img + .card-score +
    # .eps-flag。所以不能复用 parser_home，必须走 backend_huangdou 自己的解析器。
    "markers": ["poster-orbit", "drama-card"],
    "strong_markers": ["poster-orbit"],
    # 注意 required 里不能写 "drama-card"：黄果树的是 "hg-drama-card"，
    # 子串匹配会把两站混在一起（已踩）。poster-orbit 是本站独有 class。
    "required": ["poster-orbit", "aihuangdou"],
    # 分类取自站点主导航真实文案：首页 / AI短剧 / AI漫剧 / 成人小说 / 短剧社区 /
    # 排行榜 / 热门标签。小说与社区不是视频，不收；标签页当「专题」用。
    "categories": [
        ("AI短剧", "/ai-duanju/"),
        ("AI漫剧", "/ai-manju/"),
        ("排行榜", "/rankings/"),
        ("热门标签", "/tags/"),
    ],
    # 分页：/ai-duanju/{page}/（页面带 data-pages=28，实测第 2 页 24 张卡片）
    "pager": {"style": "path", "segment": "", "total_attr": "data-pages"},
    # 站点路径与黄果不同，URL 模板随站点走（config._rebuild_urls 会读这里）。
    # 第 1 集无后缀：/video/{did}/ ，其余 /video/{did}/{ep}/
    "urls": {
        "detail": "/drama/{did}/",
        "play": "/video/{did}/",
        "play_ep": "/video/{did}/{ep}/",
        "search": "/search/?q={kw}",
        "topic": "/tags/{slug}/",
    },
    "download_subdir": "aihuangdou",
    "cover_encrypted": True,
    "cover_crypt": "aes",           # 实测与 huangguoai 同一套 AES-128-CBC key/iv
    "backend": "backend_huangdou",
    "color": "#f2994a",
    "note": "黄桃短剧（AI短剧/漫剧，免费 m3u8，与黄果有重合内容）",
}

SITE_CRDJ = {
    "id": "crdj",
    "name": "91成人短剧",
    "domain": "91crdjai.com",
    "base_url": "https://91crdjai.com",
    # 列表首页是 /duanju/（首页 / 是导航聚合页，卡片结构一样）
    "home_url": "https://91crdjai.com/duanju/",
    "candidates": [
        "https://91crdjai.com/duanju/",
    ],
    # /dizhi/ 是「官方地址」发布页（title：91成人短剧官网 - 最新地址、永久入口与备用线路）
    "publish_pages": [
        "https://91crdjai.com/dizhi/",
    ],
    # 第四套前端：a.card + img.p-img + .eps-flag + .score
    "markers": ["p-img", "eps-flag", "card-info"],
    "strong_markers": ["p-img"],
    "required": ["p-img", "91crdjai"],
    "categories": [
        ("成人短剧", "/duanju/"),
        ("成人漫剧", "/manju/"),
        ("真人剧", "/zhenrenju/"),
        ("热播榜", "/paihang/"),
        ("标签榜", "/biaoqian/"),
    ],
    # 分页是 /duanju/page/{n}/ —— 注意不是 /duanju/{n}/（后者是「剧 id=n」的详情）
    "pager": {"style": "path", "segment": "page", "total_attr": "pg-input-max"},
    "urls": {
        "detail": "/duanju/{did}/",
        "play": "/duanju/{did}/{ep}/",
        "play_ep": "/duanju/{did}/{ep}/",
        "search": "/search/?q={kw}",
        "topic": "/biaoqian/{slug}/",
    },
    "download_subdir": "crdj",
    "cover_encrypted": True,
    "cover_crypt": "aes",           # 与黄桃同 CDN（pic.drleao.cn），同一套 AES key
    "backend": "backend_crdj",
    "color": "#eb5757",
    "note": "91成人短剧（与黄桃同后端 API，取址走 /videos/{id}/episodes/{ep}/playback）",
}

SITE_YOUZIDJ = {
    "id": "youzidj",
    "name": "柚子短剧",
    "domain": "youzidj.com",
    "base_url": "https://youzidj.com",
    "home_url": "https://youzidj.com/",
    "candidates": ["https://youzidj.com/"],
    # 回家路导航页（title：柚子短剧 · 官方最新地址）
    "publish_pages": ["https://yzdju.online/"],
    "markers": ["home-drama-card", "drama-poster-image"],
    "strong_markers": ["home-drama-card"],
    "required": ["home-drama-card"],
    "categories": [
        ("全部短剧", "/"),
    ],
    "pager": {"style": "path", "segment": "page"},
    "urls": {
        # 柚子的站点 id 是 slug：/dramas/{slug}/season-1/
        "detail": "/dramas/{did}/season-1/",
        "play": "/dramas/{did}/season-1-episode-{ep}/",
        "play_ep": "/dramas/{did}/season-1-episode-{ep}/",
        "search": "/search/?q={kw}",
        "topic": "/tags/{slug}/",
    },
    "download_subdir": "youzidj",
    "cover_encrypted": False,
    "cover_crypt": "",
    "backend": "backend_youzidj",
    "color": "#f2c94c",
    # 已接入浏览/详情。播放直链带凭证（master.m3u8 直取 403，浏览器里走
    # 带 token 的 DASH 分片），下载需要浏览器态放流，故先只做内容补全。
    "enabled": True,
    # 播放直链带凭证，浏览器态才能放流 → 暂时只能浏览
    "downloadable": False,
    "note": "柚子短剧（浏览/详情可用；播放凭证在浏览器端，暂不能直接下载）",
}

SITE_AIWU = {
    "id": "aiwu",
    "name": "AI污短剧",
    "domain": "ai-wu.cc",
    "base_url": "https://aiwu.ai-wu.cc",
    "home_url": "https://aiwu.ai-wu.cc/browse/",
    "candidates": ["https://aiwu.ai-wu.cc/browse/"],
    # 导航页（xn--ai-iw1e 是 punycode 的「ai.污」）
    "publish_pages": ["https://xn--ai-iw1e.ai-wutv.xyz/aiwu/"],
    "markers": ["nf-card", "nf-poster"],
    "strong_markers": ["nf-card"],
    "required": ["nf-card"],
    "categories": [
        ("AI成人短剧", "/aidj/crdj/"),
        ("AI成人漫剧", "/aidj/crmj/"),
        ("AI换脸", "/aidj/aihl/"),
        ("AI魔改", "/aidj/aimg/"),
    ],
    "pager": {"style": "path", "segment": "page"},
    "urls": {
        "detail": "/detail/{did}/",
        "play": "/play/{did}-1-{ep}/",
        "play_ep": "/play/{did}-1-{ep}/",
        "search": "/search/{kw}/",
    },
    "download_subdir": "aiwu",
    "cover_encrypted": False,
    "cover_crypt": "",
    "backend": "backend_aiwu",
    "color": "#6fcf97",
    # 已接入浏览/详情。⚠ 片源与黄果同源：player_data.from="huangguo"，
    # vod id 与封面 /upload/huangguo/{vid}.jpg 一一对应 —— 跨站去重会大面积命中。
    # 播放直链由站点解析器下发，而 /api.php/provide/vod/ 返回 "closed"、
    # /static/player/ 404，所以本站**取不到 m3u8**，只能浏览不能下载。
    "enabled": True,
    # maccms 解析接口返回 closed、静态播放器 404 → 取不到直链，只能浏览
    "downloadable": False,
    "note": "AI污短剧（片源=黄果，浏览可用；解析接口已关闭，取不到直链）",
}

# 候选站点顺序即默认优先级（前者优先）
SITES: List[Dict] = [SITE_HUANGUOAI, SITE_HUANGUOME, SITE_HUANGGUOAC,
                     SITE_AIHUANGDOU, SITE_CRDJ, SITE_YOUZIDJ, SITE_AIWU]
# id -> site 定义
_BY_ID: Dict[str, Dict] = {s["id"]: s for s in SITES}


def get_site(site_id: str) -> Optional[Dict]:
    """按 id 取站点定义。"""
    return _BY_ID.get(site_id)


def all_sites() -> List[Dict]:
    return list(SITES)


def looks_like(html: str, site: Dict) -> bool:
    """页面 HTML 是否匹配该站点。

    规则: required 中的标记必须全部出现；markers 中至少出现一个。
    （required 用于排除「恰好含同名 class 的其它页面」，如宣传落地页）
    """
    if not html:
        return False
    required = site.get("required") or []
    if required and not all(m in html for m in required):
        return False
    markers = site.get("markers") or []
    if markers and not any(m in html for m in markers):
        return False
    return bool(required or markers)


def identify(html: str) -> Optional[Dict]:
    """根据 HTML 内容推断所属站点（按顺序匹配，前者优先）。"""
    for site in SITES:
        if looks_like(html, site):
            return site
    return None


def priority_of(site_id: str) -> int:
    """站点优先级（越小越优先）。"""
    for i, s in enumerate(SITES):
        if s["id"] == site_id:
            return i
    return 99


def page_url(site: Dict, url: str, page: int) -> str:
    """按站点 pager 规则生成第 N 页 URL（page<=1 返回原地址）。

    style="path" 时：
      segment=""     -> {base}/{page}/      （huangguoai: /ai-duanju/2/）
      segment="page" -> {base}/page/{page}/ （huangguome: /category/x/page/2/）
    """
    if page <= 1:
        return url
    pager = site.get("pager") or {}
    seg = (pager.get("segment") or "").strip("/")
    u = url.rstrip("/")
    if seg:
        return f"{u}/{seg}/{page}/"
    return f"{u}/{page}/"


def page_style(site: Dict) -> str:
    return (site.get("pager") or {}).get("style", "path")
