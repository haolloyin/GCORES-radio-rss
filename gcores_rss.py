#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机核（GCORES）播客 RSS 生成器。

抓取 https://www.gcores.com/radios 的「机核出品」与「入驻播客」两个列表，
分别生成两个带时间轴的 RSS 2.0 文件：

    gcores.xml  -> 机核出品（filter=gcores）
    join.xml    -> 入驻播客（filter=join）

特性：
  * 单集标题固定为 `标题 | 分类`，分类名自身带竖线时只取竖线左侧；
  * 支持在 config.json 中按「分类 / 节目名 / 标题」关键字忽略某些节目；
  * 时间轴同时用两种方式提供给播客客户端：
      1. Shownotes 里的纯文本时间戳（Apple Podcasts / Pocket Casts /
         AntennaPod 等会自动识别成可点击跳转的锚点）；
      2. Podcasting 2.0 的 <podcast:chapters>（chapters/*.json），
         支持章节的客户端可直接跳到对应时间点；
  * Shownotes 内始终带有跳回机核原始节目页的链接；
  * 增量更新：已抓过的单集不会重复抓时间轴，最终按发布时间降序、按 guid 去重。

只依赖 Python 标准库，方便在 GitHub Actions 里直接运行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

API_BASE = "https://www.gcores.com/gapi/v1"
SITE_BASE = "https://www.gcores.com"
AUDIO_BASE = "https://alioss.gcores.com/uploads/audio/"
IMAGE_BASE = "https://image.gcores.com/"
TZ_CN = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CACHE_DIR = ROOT / "cache"
CHAPTER_DIR = ROOT / "chapters"

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}

# 分类/标题里可能出现的竖线：半角 | 、全角 ｜ 、以及机核常用的「丨」(U+4E28)
BAR_RE = re.compile(r"[|｜丨]")

DEFAULT_CONFIG = {
    "feeds": {
        "gcores": {
            "file": "gcores.xml",
            "title": "机核 GCORES · 机核出品",
            "link": "https://www.gcores.com/radios?filter=gcores",
            "description": "机核出品的播客节目，按发布时间降序排列，每集附时间轴。",
            "filter": "gcores",
            "ignore": ["SPEC", "录音笔"],
        },
        "join": {
            "file": "join.xml",
            "title": "机核 GCORES · 入驻播客",
            "link": "https://www.gcores.com/radios?filter=join",
            "description": "机核「入驻播客」节目，按发布时间降序排列，每集附时间轴。",
            "filter": "join_podcast",
            "ignore": ["诗梳风"],
        },
    },
    "max_items": 100,
    "pages": 2,
    "page_size": 100,
    "workers": 8,
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in user.items():
            if key == "feeds" and isinstance(value, dict):
                for name, feed in value.items():
                    cfg["feeds"].setdefault(name, {}).update(feed)
            else:
                cfg[key] = value
    return cfg


def http_get(url: str, *, headers: dict | None = None, retries: int = 3,
             timeout: int = 30, method: str = "GET") -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # 4xx 不重试（除了 429）
            if exc.code < 500 and exc.code != 429:
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url} ({last})")


def api_headers(user_agent: str) -> dict:
    # 机核的 JSON:API 严格要求这个 Accept 头，否则返回 406
    return {
        "User-Agent": user_agent,
        "Accept": "application/vnd.api+json",
        "Referer": SITE_BASE + "/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def api_get(path: str, params: dict | None = None, *, headers: dict) -> dict:
    query = urllib.parse.urlencode(params or {}, safe=",[]")
    url = f"{API_BASE}/{path}" + (f"?{query}" if query else "")
    return json.loads(http_get(url, headers=headers).decode("utf-8"))


# --------------------------------------------------------------------------- #
# 富文本（Draft.js）转 HTML
# --------------------------------------------------------------------------- #

def _render_inline(text: str, entity_ranges: list, entity_map: dict) -> str:
    if not entity_ranges:
        return escape(text)
    parts: list[str] = []
    pos = 0
    for rng in sorted(entity_ranges, key=lambda r: r.get("offset", 0)):
        offset = int(rng.get("offset", 0))
        length = int(rng.get("length", 0))
        if offset > pos:
            parts.append(escape(text[pos:offset]))
        chunk = text[offset:offset + length]
        entity = entity_map.get(str(rng.get("key"))) or {}
        data = entity.get("data") or {}
        if entity.get("type") == "LINK":
            href = data.get("href") or data.get("url") or ""
            if href:
                parts.append(f'<a href="{escape(href, quote=True)}">{escape(chunk)}</a>')
            else:
                parts.append(escape(chunk))
        else:
            parts.append(escape(chunk))
        pos = offset + length
    if pos < len(text):
        parts.append(escape(text[pos:]))
    return "".join(parts)


def draft_to_html(raw: str | None) -> str:
    """把机核 content 字段（Draft.js 序列化 JSON）转成简单的 HTML。"""
    if not raw:
        return ""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return f"<p>{escape(str(raw))}</p>"

    blocks = doc.get("blocks") or []
    entity_map = doc.get("entityMap") or {}
    html: list[str] = []
    list_open: str | None = None

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            html.append(f"</{list_open}>")
            list_open = None

    for block in blocks:
        btype = block.get("type") or "unstyled"
        text = block.get("text") or ""
        inner = _render_inline(text, block.get("entityRanges") or [], entity_map)

        if btype in ("unordered-list-item", "ordered-list-item"):
            tag = "ul" if btype == "unordered-list-item" else "ol"
            if list_open != tag:
                close_list()
                html.append(f"<{tag}>")
                list_open = tag
            html.append(f"<li>{inner}</li>")
            continue

        close_list()

        if btype == "atomic":
            entity = entity_map.get(str((block.get("entityRanges") or [{}])[0].get("key"))) or {}
            data = entity.get("data") or {}
            if entity.get("type") == "IMAGE" and data.get("src"):
                html.append(f'<p><img src="{escape(data["src"], quote=True)}" alt=""/></p>')
            elif data.get("url"):
                title = data.get("title") or data.get("url")
                html.append(
                    f'<p><a href="{escape(data["url"], quote=True)}">{escape(str(title))}</a></p>'
                )
        elif btype in ("header-one", "header-two", "header-three"):
            level = {"header-one": 2, "header-two": 3, "header-three": 4}[btype]
            html.append(f"<h{level}>{inner}</h{level}>")
        elif btype == "blockquote":
            html.append(f"<blockquote>{inner}</blockquote>")
        elif btype == "code-block":
            html.append(f"<pre>{inner}</pre>")
        elif inner.strip():
            html.append(f"<p>{inner}</p>")

    close_list()
    return "\n".join(html)


# --------------------------------------------------------------------------- #
# 数据抓取
# --------------------------------------------------------------------------- #

def plain_text(html_text: str, limit: int = 160) -> str:
    """把 HTML 片段压成纯文本，用于缺简介时的兜底 description。"""
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return text[:limit]


def split_category(name: str) -> str:
    """分类名自身带竖线时，只保留竖线左侧部分。"""
    if not name:
        return ""
    return BAR_RE.split(name)[0].strip() or name.strip()


def clean_title(title: str) -> str:
    """把标题里自带的竖线换成「·」，保证「标题 | 分类」只有一个分隔符。"""
    if not BAR_RE.search(title):
        return title.strip()
    parts = [p.strip() for p in BAR_RE.split(title) if p.strip()]
    return "·".join(parts)


def image_url(asset: str | None) -> str:
    if not asset:
        return ""
    if asset.startswith("http"):
        return asset
    return IMAGE_BASE + asset


def audio_url(media: dict | None) -> str:
    if not media:
        return ""
    src = media.get("audio") or ""
    if not src:
        return ""
    if src.startswith("http"):
        return src
    return AUDIO_BASE + src


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def rfc822(dt: datetime) -> str:
    dt = dt.astimezone(TZ_CN)
    return (f"{WEEKDAYS[dt.weekday()]}, {dt.day:02d} {MONTHS[dt.month - 1]} "
            f"{dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} +0800")


def fetch_list(feed_cfg: dict, cfg: dict, headers: dict) -> list[dict]:
    """按发布时间倒序抓取列表页，返回规范化后的单集列表。"""
    page_size = int(cfg.get("page_size", 100))
    pages = int(cfg.get("pages", 2))
    collected: dict[str, dict] = {}

    for page in range(pages):
        params = {
            "page[limit]": page_size,
            "page[offset]": page * page_size,
            "sort": "-published-at",
            "filter[owner-type]": feed_cfg["filter"],
            "include": "category,media,albums",
        }
        try:
            payload = api_get("radios", params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! 列表第 {page + 1} 页抓取失败：{exc}")
            break

        data = payload.get("data") or []
        if not data:
            break

        cat_map = {i["id"]: (i.get("attributes") or {}).get("name", "")
                   for i in payload.get("included", []) if i.get("type") == "categories"}
        album_map = {i["id"]: (i.get("attributes") or {}).get("title", "")
                     for i in payload.get("included", []) if i.get("type") == "albums"}
        media_map = {i["id"]: (i.get("attributes") or {})
                     for i in payload.get("included", []) if i.get("type") == "medias"}

        for item in data:
            norm = normalize(item, cat_map, album_map, media_map,
                             bool(cfg.get("normalize_title_bar", True)))
            if norm and norm["id"] not in collected:
                collected[norm["id"]] = norm

        if len(data) < page_size:
            break

    return list(collected.values())


def normalize(item: dict, cat_map: dict, album_map: dict, media_map: dict,
              clean_bar: bool = True) -> dict | None:
    attrs = item.get("attributes") or {}
    rid = str(item.get("id") or "")
    if not rid or not attrs.get("is-published", 1):
        return None

    rels = item.get("relationships") or {}

    media_ref = (rels.get("media") or {}).get("data") or {}
    media = media_map.get(media_ref.get("id")) or {}
    if not media:
        return None
    if media.get("playback-type") == "protected_audio" or attrs.get("is-require-privilege"):
        # 会员专享音频需要登录换签名，播客客户端无法直接播放，跳过
        return None
    audio = audio_url(media)
    if not audio:
        return None

    cat_ref = (rels.get("category") or {}).get("data") or {}
    cat_name = cat_map.get(cat_ref.get("id"), "") if cat_ref else ""
    albums = (rels.get("albums") or {}).get("data") or []
    album_name = album_map.get(albums[0].get("id"), "") if albums else ""

    if (attrs.get("owner-type") or "") == "join_podcast":
        raw_cat = album_name or cat_name or "入驻播客"
    else:
        raw_cat = cat_name or album_name

    published = parse_dt(attrs.get("published-at"))
    if published is None:
        return None

    content_html = draft_to_html(attrs.get("content"))
    summary = (attrs.get("summary") or attrs.get("desc") or "").strip()
    if not summary:
        summary = plain_text(content_html)

    return {
        "id": rid,
        "guid": f"gcores-radio-{rid}",
        "title": clean_title(attrs.get("title") or "") if clean_bar
                 else (attrs.get("title") or "").strip(),
        "category_raw": raw_cat,
        "category": split_category(raw_cat),
        "published_at": published.astimezone(TZ_CN).isoformat(),
        "duration": int(attrs.get("duration") or 0),
        "audio": audio,
        "page_url": f"{SITE_BASE}/radios/{rid}",
        "image": image_url(attrs.get("thumb") or attrs.get("cover")),
        "summary": summary,
        "content_html": content_html,
        "media_id": str(media_ref.get("id") or ""),
    }


def fetch_timelines(media_id: str, headers: dict) -> list[dict]:
    """时间轴来自 media 的 timelines 关系（按 at 秒升序）。"""
    if not media_id:
        return []
    payload = api_get(f"medias/{media_id}", {"include": "timelines"}, headers=headers)
    raw = [i for i in payload.get("included", []) if i.get("type") == "timelines"]
    out = []
    for item in raw:
        attrs = item.get("attributes") or {}
        try:
            at = int(attrs.get("at") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "at": max(at, 0),
            "title": (attrs.get("title") or "").strip(),
            "content": (attrs.get("content") or "").strip(),
            "href": (attrs.get("quote-href") or "").strip(),
            "img": image_url(attrs.get("asset")),
        })
    out.sort(key=lambda c: c["at"])
    return out


def probe_audio(url: str, headers: dict) -> tuple[bool, int]:
    """确认音频可直连播放，并尽量拿到文件大小。返回 (可用, 字节数)。"""
    try:
        req = urllib.request.Request(url, headers={**headers, "Accept": "*/*"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, int(resp.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            return False, 0
    except Exception:  # noqa: BLE001
        pass

    # 有些 CDN 不支持 HEAD，用 Range 请求兜底
    try:
        req = urllib.request.Request(
            url, headers={**headers, "Accept": "*/*", "Range": "bytes=0-1"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            total = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
            return True, int(total.group(1)) if total else 0
    except Exception:  # noqa: BLE001
        return False, 0


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #

def fmt_ts(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_duration(seconds: int) -> str:
    return fmt_ts(seconds) if seconds else ""


def cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_shownotes(item: dict) -> str:
    """Shownotes：简介 + 原始页面链接 + 可点击时间轴。"""
    parts: list[str] = []
    summary = item.get("summary") or ""
    if summary:
        parts.append(f"<p>{escape(summary)}</p>")

    page_url = item["page_url"]
    parts.append(
        f'<p><a href="{escape(page_url, quote=True)}">🔗 在机核查看本集原始页面</a></p>'
    )

    content_html = item.get("content_html") or ""
    if content_html:
        parts.append("<hr/>")
        parts.append(content_html)

    timelines = item.get("timelines") or []
    if timelines:
        parts.append("<hr/>")
        parts.append("<h3>时间轴</h3>")
        parts.append("<ul>")
        for chap in timelines:
            stamp = fmt_ts(chap["at"])
            title = escape(chap.get("title") or "")
            line = f"<li><strong>{stamp}</strong> {title}"
            body = chap.get("content") or ""
            if body:
                line += f"<br/>{escape(body)}"
            if chap.get("href"):
                line += (f'<br/><a href="{escape(chap["href"], quote=True)}">'
                         f'相关链接</a>')
            line += "</li>"
            parts.append(line)
        parts.append("</ul>")

    parts.append(
        f'<p><a href="{escape(page_url, quote=True)}">🔗 在机核查看本集原始页面</a></p>'
    )
    return "\n".join(parts)


def item_title(item: dict) -> str:
    cat = item.get("category") or ""
    return f"{item['title']} | {cat}" if cat else item["title"]


# --------------------------------------------------------------------------- #
# 状态（增量）与输出
# --------------------------------------------------------------------------- #

def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {str(k): v for k, v in (data.get("items") or {}).items()}


def load_meta(path: Path) -> dict:
    """读取缓存里的元信息（对外地址等），用于判断是否需要重写 feed。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("meta") or {}
    except (ValueError, OSError):
        return {}


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def signature(items: list[dict]) -> list[tuple]:
    """用于判断「这一轮和上一轮相比内容是否有变化」的指纹。"""
    return sorted(
        (i.get("id"), i.get("title"), i.get("published_at"), i.get("audio"),
         i.get("category"), len(i.get("timelines") or []), i.get("length"),
         i.get("chapters_url") or "")
        for i in items
    )


def is_ignored(item: dict, patterns: list[str]) -> str | None:
    """忽略规则：对「分类原文 / 标题」做不区分大小写的子串匹配。"""
    haystack = f"{item.get('category_raw', '')} {item.get('title', '')}".lower()
    for pattern in patterns or []:
        p = (pattern or "").strip().lower()
        if p and p in haystack:
            return pattern
    return None


def enrich(items: list[dict], state: dict, headers: dict, workers: int,
           chapters_base: str) -> None:
    """为新增单集补全时间轴、简介与音频长度；已抓过的不重复抓。"""
    todo: list[dict] = []
    for item in items:
        old = state.get(item["id"])
        if old and old.get("timelines") is not None and old.get("audio") == item["audio"]:
            item["timelines"] = old.get("timelines") or []
            item["length"] = old.get("length") or 0
            item["tl_checked"] = old.get("tl_checked", 1)
            if old.get("content_html"):
                item["content_html"] = old["content_html"]
        else:
            todo.append(item)

    if todo:
        log(f"  需要抓取时间轴的新单集：{len(todo)}")

    def work(item: dict) -> None:
        try:
            item["timelines"] = fetch_timelines(item.get("media_id", ""), headers)
            item["tl_checked"] = 1
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {item['id']} 时间轴抓取失败：{exc}")
            item["timelines"] = item.get("timelines") or []
            item["tl_checked"] = 0

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(work, todo))

    # 校验音频可直连（会员专享/下架的音频会 404），不可用的单集直接丢弃
    need_probe = [i for i in items if not i.get("length")]
    if need_probe:
        def probe(item: dict) -> None:
            ok, size = probe_audio(item["audio"], headers)
            item["length"] = size
            if not ok:
                item["dead"] = True

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(probe, need_probe))

    for item in items:
        item.setdefault("timelines", [])
        item.setdefault("length", 0)
        item["chapters_url"] = (
            f"{chapters_base}/{item['id']}.json"
            if chapters_base and item["timelines"] else ""
        )


def write_chapters(items: list[dict], chapters_base: str) -> set[str]:
    CHAPTER_DIR.mkdir(parents=True, exist_ok=True)
    keep: set[str] = set()
    for item in items:
        timelines = item.get("timelines") or []
        if not timelines:
            continue
        chapters = []
        for chap in timelines:
            entry = {"startTime": chap["at"], "title": chap.get("title") or fmt_ts(chap["at"])}
            if chap.get("img"):
                entry["img"] = chap["img"]
            if chap.get("href"):
                entry["url"] = chap["href"]
            chapters.append(entry)
        name = f"{item['id']}.json"
        save_json(CHAPTER_DIR / name, {"version": "1.2.0", "chapters": chapters})
        keep.add(name)
    return keep


def prune_chapters(keep: set[str]) -> None:
    if not CHAPTER_DIR.exists():
        return
    for path in CHAPTER_DIR.glob("*.json"):
        if path.name not in keep:
            path.unlink()


def write_feed(path: Path, feed_cfg: dict, items: list[dict], cfg: dict,
               self_url: str) -> None:
    now = datetime.now(TZ_CN)
    title = feed_cfg["title"]
    desc = feed_cfg.get("description", "")
    link = feed_cfg.get("link", SITE_BASE)
    cover = next((i["image"] for i in items if i.get("image")), "")

    out: list[str] = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:podcast="https://podcastindex.org/namespace/1.0" '
        'xmlns:googleplay="http://www.google.com/schemas/play-podcasts/1.0">'
    )
    out.append("<channel>")
    out.append(f"<title>{escape(title)}</title>")
    out.append(f"<link>{escape(link)}</link>")
    out.append(f"<description>{escape(desc)}</description>")
    out.append("<language>zh-cn</language>")
    out.append(f"<lastBuildDate>{rfc822(now)}</lastBuildDate>")
    out.append(f"<pubDate>{rfc822(now)}</pubDate>")
    out.append("<generator>gcores-rss</generator>")
    out.append("<ttl>30</ttl>")
    if self_url:
        out.append(f'<atom:link href="{escape(self_url, quote=True)}" rel="self" '
                   'type="application/rss+xml"/>')
    out.append("<itunes:author>机核 GCORES</itunes:author>")
    out.append(f"<itunes:summary>{escape(desc)}</itunes:summary>")
    out.append("<itunes:explicit>false</itunes:explicit>")
    out.append("<itunes:type>episodic</itunes:type>")
    out.append("<itunes:owner><itunes:name>机核 GCORES</itunes:name>"
               "<itunes:email>support@gcores.com</itunes:email></itunes:owner>")
    out.append('<itunes:category text="Leisure"><itunes:category text="Video Games"/></itunes:category>')
    if cover:
        out.append(f'<itunes:image href="{escape(cover, quote=True)}"/>')
        out.append(f"<image><url>{escape(cover)}</url><title>{escape(title)}</title>"
                   f"<link>{escape(link)}</link></image>")

    for item in items:
        out.extend(build_item_xml(item))

    out.append("</channel>")
    out.append("</rss>")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def build_item_xml(item: dict) -> list[str]:
    lines = ["<item>"]
    lines.append(f"<title>{escape(item_title(item))}</title>")
    lines.append(f"<link>{escape(item['page_url'])}</link>")
    lines.append(f'<guid isPermaLink="false">{escape(item["guid"])}</guid>')
    lines.append(f"<pubDate>{rfc822(datetime.fromisoformat(item['published_at']))}</pubDate>")
    if item.get("summary"):
        lines.append(f"<description>{escape(item['summary'])}</description>")
    lines.append(f"<content:encoded>{cdata(build_shownotes(item))}</content:encoded>")
    lines.append("<author>机核 GCORES</author>")
    lines.append("<itunes:author>机核 GCORES</itunes:author>")
    if item.get("category"):
        lines.append(f"<category>{escape(item['category'])}</category>")
        lines.append(f'<itunes:keywords>{escape(item["category"])}</itunes:keywords>')
    if item.get("summary"):
        lines.append(f"<itunes:summary>{escape(item['summary'])}</itunes:summary>")
    if item.get("duration"):
        lines.append(f"<itunes:duration>{fmt_duration(item['duration'])}</itunes:duration>")
    lines.append("<itunes:explicit>false</itunes:explicit>")
    lines.append("<itunes:episodeType>full</itunes:episodeType>")
    if item.get("image"):
        lines.append(f'<itunes:image href="{escape(item["image"], quote=True)}"/>')
    ext = os.path.splitext(urllib.parse.urlparse(item["audio"]).path)[1].lower()
    mime = AUDIO_MIME.get(ext, "audio/mpeg")
    lines.append(
        f'<enclosure url="{escape(item["audio"], quote=True)}" '
        f'length="{int(item.get("length") or 0)}" type="{mime}"/>'
    )
    if item.get("chapters_url"):
        lines.append(
            f'<podcast:chapters url="{escape(item["chapters_url"], quote=True)}" '
            'type="application/json+chapters"/>'
        )
    lines.append("</item>")
    return lines


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def process_feed(name: str, feed_cfg: dict, cfg: dict, headers: dict,
                 chapters_base: str, feed_base: str, verbose: bool = False) -> set[str]:
    log(f"[{name}] 抓取 {feed_cfg['filter']} …")
    candidates = fetch_list(feed_cfg, cfg, headers)
    log(f"[{name}] 列表拿到 {len(candidates)} 集")

    state_path = CACHE_DIR / f"{name}.json"
    state = load_state(state_path)

    ignore = feed_cfg.get("ignore") or []
    filtered: list[dict] = []
    ignored = 0
    for item in candidates:
        hit = is_ignored(item, ignore)
        if hit:
            ignored += 1
            if verbose:
                log(f"  - 忽略 {item['id']}（命中 {hit}）：{item['title'][:40]}")
            continue
        filtered.append(item)
    if ignored:
        log(f"  按规则忽略 {ignored} 集（{', '.join(ignore)}）")

    max_items = int(cfg.get("max_items", 100))
    filtered.sort(key=lambda i: i["published_at"], reverse=True)

    # 列表页不足时，用缓存里的旧单集补齐，避免 RSS 突然变短
    seen = {i["id"] for i in filtered}
    if len(filtered) < max_items:
        for sid, old in sorted(state.items(),
                               key=lambda kv: kv[1].get("published_at", ""),
                               reverse=True):
            if sid in seen or len(filtered) >= max_items:
                continue
            if is_ignored(old, ignore):
                continue
            old["media_id"] = old.get("media_id", "")
            old["category_raw"] = old.get("category_raw", old.get("category", ""))
            filtered.append(old)

    items = filtered[:max_items]
    enrich(items, state, headers, int(cfg.get("workers", 8)), chapters_base)

    dropped = [i for i in items if i.get("dead")]
    if dropped:
        log(f"  - 跳过 {len(dropped)} 集：音频不可直连（多为会员专享）")
    items = [i for i in items if not i.get("dead")]

    # 去重（按 guid）后按时间降序
    unique: dict[str, dict] = {}
    for item in items:
        unique.setdefault(item["guid"], item)
    items = sorted(unique.values(), key=lambda i: i["published_at"], reverse=True)

    keep = write_chapters(items, chapters_base)

    feed_path = ROOT / feed_cfg["file"]
    meta_now = {"feed_base": feed_base, "chapters_base": chapters_base,
                "max_items": max_items}
    if (feed_path.exists()
            and signature(items) == signature(list(state.values()))
            and load_meta(state_path) == meta_now):
        # 内容没变就不重写，避免 Actions 每小时产生一个几乎相同的提交
        log(f"[{name}] 内容无变化，跳过写入 {feed_cfg['file']}")
        return keep

    self_url = f"{feed_base}/{feed_cfg['file']}" if feed_base else ""
    write_feed(feed_path, feed_cfg, items, cfg, self_url)

    save_json(state_path, {"meta": meta_now,
                           "items": {i["id"]: i for i in items}})
    with_tl = sum(1 for i in items if i.get("timelines"))
    log(f"[{name}] 写出 {feed_cfg['file']}：{len(items)} 集，其中 {with_tl} 集带时间轴")
    return keep


def main() -> int:
    parser = argparse.ArgumentParser(description="生成机核播客 RSS")
    parser.add_argument("--feed", help="只处理指定的 feed（gcores / join）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    headers = api_headers(cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"])

    # chapters / feed 的对外地址：优先用环境变量（Actions 会自动注入），
    # 其次用 config.json 里的同名配置，最后按 GITHUB_REPOSITORY 推导
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    raw_base = f"https://raw.githubusercontent.com/{repo}/main" if repo else ""
    feed_base = (os.environ.get("FEED_BASE_URL")
                 or cfg.get("feed_base_url") or raw_base).rstrip("/")
    chapters_base = (os.environ.get("CHAPTERS_BASE_URL")
                     or cfg.get("chapters_base_url")
                     or (f"{feed_base}/chapters" if feed_base else "")).rstrip("/")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    feeds = cfg["feeds"]
    if args.feed:
        feeds = {k: v for k, v in feeds.items() if k == args.feed}
        if not feeds:
            log(f"未知的 feed：{args.feed}")
            return 2

    failed = False
    keep_all: set[str] = set()
    for name, feed_cfg in feeds.items():
        try:
            keep_all |= process_feed(name, feed_cfg, cfg, headers,
                                     chapters_base, feed_base, args.verbose)
        except Exception as exc:  # noqa: BLE001
            failed = True
            log(f"[{name}] 处理失败：{exc}")

    # 两个 feed 都写完之后再清理过期章节，避免互相删掉对方的文件；
    # 只处理单个 feed 时不能清理，否则会把另一个 feed 的章节删掉
    if not args.feed:
        prune_chapters(keep_all)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
