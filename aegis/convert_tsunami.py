# ============================================================
# 🌊 Convert tsunami — 津波（552 形式）の Event 化
# ============================================================
"""Tsunami conversion from the shared 552-style item format.

📌 Areas are rendered grouped by grade （大津波警報 / 津波警報 /
津波注意報 / 津波予報）. The title summarises (first 8 areas plus
「ほかN区域」); the body always lists every area — nothing is
silently truncated. First-wave arrival labels and conditions
are preserved.
"""
from __future__ import annotations

from .models import Event, Severity

TSUNAMI_GRADE_LABELS = {
    "MajorWarning": "大津波警報", "Warning": "津波警報",
    "Watch": "津波注意報", "Forecast": "津波予報",
}
TSUNAMI_GRADE_ORDER = ["MajorWarning", "Warning", "Watch", "Forecast"]


def _area_text(a: dict) -> str:
    """対象地域1件の表示テキスト。予想高さ・第1波到達予想・第1波の
    condition（到達確認済み等）も落とさず保持する。"""
    nm = str(a.get("name") or "")
    if not nm:
        return ""
    mh = (a.get("maxHeight") or {}).get("description") if isinstance(a.get("maxHeight"), dict) else None
    if mh:
        nm += f"（予想 {mh}）"
    fh = a.get("firstHeight")
    at = cond = ""
    if isinstance(fh, dict):
        at = str(fh.get("arrivalTime") or "")
        cond = str(fh.get("condition") or "")
    elif isinstance(fh, str):
        at = fh
    if at:
        nm += f"（第1波到達予想 {at[11:16] if len(at) >= 16 else at}）"
    if cond:
        nm += f"（第1波: {cond}）"
    return nm


def tsunami_grouped_text(areas, max_names: int | None = None) -> tuple[str, int]:
    """対象地域をグレード別にグループ化したテキストと総区域数を返す。
    max_names を指定すると表示する地域名をその件数までに抑え、残りは
    「ほかN区域」と付記する（無言の切り捨てはしない）。"""
    groups: dict[str, list[str]] = {}
    others: list[str] = []
    total = 0
    for a in areas or []:
        txt = _area_text(a)
        if not txt:
            continue
        total += 1
        g = str(a.get("grade") or "")
        if g in TSUNAMI_GRADE_LABELS:
            groups.setdefault(g, []).append(txt)
        else:
            others.append(txt)
    parts: list[str] = []
    shown = 0

    def take(names: list[str]) -> list[str]:
        nonlocal shown
        if max_names is None:
            shown += len(names)
            return names
        room = max(0, max_names - shown)
        shown += min(len(names), room)
        return names[:room]

    for g in TSUNAMI_GRADE_ORDER:
        if groups.get(g):
            picked = take(groups[g])
            if picked:
                parts.append(f"【{TSUNAMI_GRADE_LABELS[g]}】" + "、".join(picked))
    if others:
        picked = take(others)
        if picked:
            parts.append("【その他】" + "、".join(picked))
    text = " / ".join(parts)
    if max_names is not None and total > shown:
        text += f" ほか{total - shown}区域"
    return text, total


def tsunami_events_from_items(items, source_label: str = "A.A.S（Aegis Alert System）") -> list[Event]:
    """552 item のリストを Event リストへ変換する（純粋関数）。
    同一視キーは電文 ID。"""
    events: list[Event] = []
    for item in items or []:
        areas = item.get("areas") or []
        cancelled = bool(item.get("cancelled"))
        grades = [str(a.get("grade") or "") for a in areas if a.get("grade")]
        if cancelled:
            sev, label = Severity.MEDIUM, "津波警報・注意報 解除"
            guide = "解除を確認。ただし公式発表で安全が確認できるまで海岸には近づかない"
        # 🌊 大津波警報・津波警報はいずれも即時避難が必要なため CRITICAL
        elif any(g == "MajorWarning" for g in grades):
            sev, label = Severity.CRITICAL, "大津波警報"
            guide = "ただちに高台・津波避難ビルへ避難。海岸・河口に絶対に近づかない"
        elif any(g == "Warning" for g in grades):
            sev, label = Severity.CRITICAL, "津波警報"
            guide = "ただちに高台・津波避難ビルへ避難。海岸・河口に絶対に近づかない"
        elif any(g == "Watch" for g in grades):
            sev, label = Severity.HIGH, "津波注意報"
            guide = "海から上がり海岸から離れる。海水浴・磯釣りは中止"
        else:
            sev, label = Severity.MEDIUM, "津波予報/情報"
            guide = "公式の続報を確認する"
        title_areas, _total = tsunami_grouped_text(areas, max_names=8)
        body_areas, _ = tsunami_grouped_text(areas)
        title_txt = title_areas or "全国"
        ev = Event(
            kind="tsunami",
            title=f"{label}（{title_txt}）",
            body=f"対象地域: {body_areas or '全国'} / 発表: {item.get('time','')}",
            severity=sev,
            source=source_label,
            event_id=str(item.get("id") or ""),
            occurred_at=str(item.get("time") or ""),
            action_guide=guide,
            raw=item,
        )
        ev.ensure_id()
        events.append(ev)
    return events
