# ============================================================
# 📰 News — NHK RSS からの防災ニュース
# ============================================================
"""Disaster-related news from the NHK RSS feeds （主要＋社会）.
Items matching disaster keywords are prioritised; failures in
one feed never take the other down, and news is only ever shown
in briefings / the dashboard — never mixed into alert
notifications.
"""
from __future__ import annotations

from .http_client import fetch_text

NHK_RSS_URLS = [
    "https://www3.nhk.or.jp/rss/news/cat0.xml",  # 主要
    "https://www3.nhk.or.jp/rss/news/cat1.xml",  # 社会
]

# ⌨️ 防災関連キーワード（タイトル・要約に含まれるものを優先抽出する）
NEWS_KEYWORDS = [
    "地震", "津波", "警報", "特別警報", "台風", "大雨", "洪水", "土砂", "避難",
    "震度", "噴火", "熱中症", "暴風", "大雪", "竜巻", "線状降水帯",
]


def summarize_text(text: str, limit: int = 80) -> str:
    """ニュース要約: HTMLタグを除去し、limit 字程度に切り詰める。"""
    import html as _html
    import re
    t = re.sub(r"<[^>]+>", "", text or "")
    t = _html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        return t[:limit].rstrip() + "…"
    return t


def parse_rss(xml_data) -> list[dict]:
    """RSS 2.0 をパースして {title, link, summary, time, pub} のリストを
    返す。失敗時は []。"""
    import xml.etree.ElementTree as ET
    from datetime import timezone, timedelta
    from email.utils import parsedate_to_datetime
    if isinstance(xml_data, str):
        xml_data = xml_data.encode("utf-8")  # encoding宣言つきXMLは bytes で渡す
    try:
        root = ET.fromstring(xml_data)
    except Exception:
        return []
    jst = timezone(timedelta(hours=9))
    items: list[dict] = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub = None
        tstr = ""
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            if dt is not None:
                pub = dt
                tstr = dt.astimezone(jst).strftime("%H:%M")
        except Exception:
            pass
        desc_el = it.find("description")
        desc = "".join(desc_el.itertext()) if desc_el is not None else ""
        items.append({
            "title": title,
            "link": link,
            "summary": summarize_text(desc),
            "time": tstr,
            "pub": pub,
        })
    return items


def fetch_news(limit: int = 8) -> list[dict]:
    """NHK RSS（主要+社会）をマージし、防災関連を優先して最大 limit 件返す。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for url in NHK_RSS_URLS:
        try:
            items = parse_rss(fetch_text(url))
        except Exception:
            continue
        for n in items:
            if n["link"] in seen:
                continue
            seen.add(n["link"])
            merged.append(n)
    merged.sort(key=lambda n: n["pub"].timestamp() if n.get("pub") else 0.0, reverse=True)
    pri = [n for n in merged if any(k in n["title"] or k in n["summary"] for k in NEWS_KEYWORDS)]
    pri_links = {n["link"] for n in pri}
    rest = [n for n in merged if n["link"] not in pri_links]
    return (pri + rest)[:limit]
