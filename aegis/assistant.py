# ============================================================
# 💬 Assistant — 自然文 Q&A
# ============================================================
"""Natural-language question answering for ``aegis ask``.

Routing is keyword-based and deliberately simple: tsunami →
earthquake → warnings → weather (with the umbrella guide), and
anything else falls back to a short briefing plus how to ask.
Fetch failures are answered with an explicit "could not fetch"
message, never a traceback.
"""
from __future__ import annotations

from . import a2s, weather
from .models import Location


def _pick_location(text: str, locations: list[Location]) -> Location:
    for loc in locations:
        if loc.name in text:
            return loc
    # 📍 地名が無い質問は先頭の地点で答える。地点が無い場合は既定の高崎を使う
    return locations[0] if locations else Location("高崎", 36.3219, 139.0023, "100000", "南部")


def answer(question: str, locations: list[Location]) -> str:
    """Answer one natural-language question as display text."""
    q = question.strip()
    loc = _pick_location(q, locations)
    # 🌊 津波
    if "津波" in q:
        try:
            evs = a2s.fetch_tsunamis(limit=5)
        except Exception as e:
            return f"津波情報を取得できませんでした（{e}）。通信状態を確認してください。"
        if not evs:
            return "現在、発表されている津波警報・注意報はありません（A.A.Sで確認）。海岸付近でも通常通りで問題ありませんが、強い地震を感じたらすぐ高台へ。"
        return "\n".join(e.format_message() for e in evs)
    # 🌏 地震
    if "地震" in q or "揺れ" in q or "震度" in q:
        try:
            evs = a2s.fetch_quakes(limit=5)
        except Exception:
            return "最近の地震情報は取得できませんでした。"
        if not evs:
            return "最近の地震情報は取得できませんでした。"
        head = "最近の地震（新しい順・最大5件）:\n"
        return head + "\n---\n".join(e.format_message() for e in evs)
    # ⚠️ 警報
    if "警報" in q or "注意報" in q:
        try:
            evs = weather.fetch_warnings(loc)
        except Exception as e:
            return f"警報・注意報を取得できませんでした（{e}）。通信状態を確認してください。"
        if not evs:
            return f"{loc.name}周辺に、現在発表されている警報・注意報はありません（A.A.Sで確認）。"
        return "\n".join(e.format_message() for e in evs)
    # 🌏 天気（傘の目安も含む）
    if "天気" in q or "雨" in q or "気温" in q or "傘" in q or "明日" in q or "今日" in q:
        parts = []
        for fetcher in (weather.fetch_jma_forecast, weather.fetch_open_meteo):
            try:
                ev = fetcher(loc)
                if ev:
                    parts.append(ev.format_message())
            except Exception as e:
                parts.append(f"（{fetcher.__name__} 取得失敗: {e}）")
        try:
            warns = weather.fetch_warnings(loc)
        except Exception:
            warns = []
        if warns:
            parts.append("⚠ 警報・注意報:\n" + "\n".join(w.format_message() for w in warns))
        else:
            parts.append(f"{loc.name}周辺に警報・注意報は出ていません。")
        if "傘" in q:
            parts.append("▶ 傘の目安: 降水確率 最大60%以上なら傘推奨、80%以上なら大きめの傘が安心です（上の数値を参照）。")
        return "\n---\n".join(parts)
    # 📰 フォールバック: 総合ブリーフィング
    parts = []
    try:
        ev = weather.fetch_open_meteo(loc)
        if ev:
            parts.append(ev.format_message())
    except Exception:
        pass
    try:
        quakes = a2s.fetch_quakes(limit=1)
        if quakes:
            parts.append("最新の地震:\n" + quakes[0].format_message())
    except Exception:
        pass
    parts.append("聞き方: 「高崎の天気は？」「大宮に警報は？」「最近の地震は？」「津波は？」のように聞いてください。")
    return "\n---\n".join(parts)
