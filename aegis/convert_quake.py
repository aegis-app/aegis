# ============================================================
# 🌏 Convert quake — 地震（551 形式）の統合と Event 化
# ============================================================
"""Earthquake conversion from the shared 551-style item format.

📌 A.A.S delivers one event per telegram, so the same earthquake
can arrive as several staged reports （震度速報 → 震源情報 →
詳細情報）. :func:`merge_earthquake_items` groups them by origin
time + hypocenter name (with a 30 km coordinate-proximity
fallback for name variations) and merges them into one item
before :func:`quake_events_from_items` renders the Event.

📌 The merged event_id is "origin time + hypocenter name", so a
follow-up report never double-notifies even when the primary
telegram changes.
"""
from __future__ import annotations

from .geo import haversine_km, known_depth, short_pref_name
from .locations import city_match, location_city_names, location_intensity_notes, observation_points_note, quake_estimate_note
from .models import Event, Location, Severity, scale_to_str, scale_to_severity

# 報種別のランク（詳細情報が最も完全）
QUAKE_TYPE_RANK = {"DetailScale": 4, "ScaleAndDestination": 3, "Foreign": 3, "Destination": 2, "ScalePrompt": 1}
QUAKE_MERGE_COORD_KM = 30.0


def quake_issue_label(item: dict) -> str:
    """issue の種別・訂正情報から報種別ラベル（速報／確定／訂正）を返す。"""
    issue = item.get("issue") or {}
    correct = str(issue.get("correct") or "")
    typ = str(issue.get("type") or "")
    if correct and correct != "None":
        return "訂正"
    if typ == "ScalePrompt":
        return "速報"
    if QUAKE_TYPE_RANK.get(typ, 0) >= 2:
        return "確定"
    return "情報"


def _issue_time(item: dict) -> str:
    return str((item.get("issue") or {}).get("time") or item.get("time") or "")


def _coords(eq: dict):
    hypo = eq.get("hypocenter") or {}
    lat, lon = hypo.get("latitude"), hypo.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180:
        return float(lat), float(lon)
    return None


def _same_quake(a: dict, b: dict) -> bool:
    """同一地震か: 発生時刻が一致し、震源名が一致するか、どちらかが
    未確定か、震央座標が近いこと。"""
    ea, eb = a.get("earthquake") or {}, b.get("earthquake") or {}
    ta, tb = str(ea.get("time") or ""), str(eb.get("time") or "")
    if not ta or ta != tb:
        return False
    na = str((ea.get("hypocenter") or {}).get("name") or "").replace(" ", "").replace("　", "")
    nb = str((eb.get("hypocenter") or {}).get("name") or "").replace(" ", "").replace("　", "")
    if not na or not nb or na == nb:
        return True
    ca, cb = _coords(ea), _coords(eb)
    if ca and cb:
        return haversine_km(ca[0], ca[1], cb[0], cb[1]) <= QUAKE_MERGE_COORD_KM
    return False


def merge_earthquake_items(data) -> list[dict]:
    """551 item のリストを同一地震ごとに統合する（純粋関数）。
    各グループでは種別ランク → 発表時刻の順で主報を選び、欠けた項目は
    他の報から補う。points は全報の和集合（同一観測点は最大震度）。"""
    groups: list[list[dict]] = []
    for item in data or []:
        if not isinstance(item, dict) or not item.get("earthquake"):
            continue
        for g in groups:
            if _same_quake(item, g[0]):
                g.append(item)
                break
        else:
            groups.append([item])
    merged_items: list[dict] = []
    for g in groups:
        ordered = sorted(
            g,
            key=lambda it: (QUAKE_TYPE_RANK.get(str((it.get("issue") or {}).get("type") or ""), 0), _issue_time(it)),
            reverse=True,
        )
        primary = ordered[0]
        peq = primary.get("earthquake") or {}
        phypo = peq.get("hypocenter") or {}

        def pick(fn):
            for it in ordered:
                v = fn(it)
                if v is not None:
                    return v
            return None

        name = pick(lambda it: ((it.get("earthquake") or {}).get("hypocenter") or {}).get("name") or None) or "不明"
        mag = pick(lambda it: (lambda m: m if isinstance(m, (int, float)) and m >= 0 else None)(
            ((it.get("earthquake") or {}).get("hypocenter") or {}).get("magnitude")))
        # 📝 深さは判明している値だけを候補にする（センチネル -1 の報が
        # 判明している報の深さを上書きしないように。正当な 0=ごく浅い は判明値）
        depth = pick(lambda it: known_depth(((it.get("earthquake") or {}).get("hypocenter") or {}).get("depth")))
        lat = pick(lambda it: (lambda c: c[0] if c else None)(_coords(it.get("earthquake") or {})))
        lon = pick(lambda it: (lambda c: c[1] if c else None)(_coords(it.get("earthquake") or {})))
        max_scale = pick(lambda it: (lambda s: s if isinstance(s, (int, float)) and s >= 0 else None)(
            (it.get("earthquake") or {}).get("maxScale")))
        tsunami = pick(lambda it: (lambda t: t if t and t not in ("Unknown", "Checking") else None)(
            (it.get("earthquake") or {}).get("domesticTsunami"))) or peq.get("domesticTsunami")
        # 📍 points は全報の和集合（続報ほど観測点が増えるため）
        points_by_key: dict[tuple, dict] = {}
        for it in ordered:
            for pt in it.get("points") or []:
                key = (pt.get("pref") or "", pt.get("addr") or "")
                prev = points_by_key.get(key)
                if prev is None:
                    points_by_key[key] = dict(pt)
                else:
                    try:
                        if int(pt.get("scale") or 0) > int(prev.get("scale") or 0):
                            points_by_key[key] = dict(pt)
                    except Exception:
                        pass
        merged = dict(primary)
        merged_eq = dict(peq)
        merged_hypo = dict(phypo)
        merged_hypo.update({"name": name, "magnitude": mag if mag is not None else phypo.get("magnitude", -1),
                            "depth": depth, "latitude": lat, "longitude": lon})
        merged_eq.update({"maxScale": max_scale if max_scale is not None else peq.get("maxScale", -1),
                          "domesticTsunami": tsunami, "hypocenter": merged_hypo})
        merged["earthquake"] = merged_eq
        merged["points"] = list(points_by_key.values())
        merged["_merged_count"] = len(g)
        merged_items.append(merged)
    return merged_items


def quake_events_from_items(items, locs: list[Location],
                            source_label: str = "A.A.S（Aegis Alert System）") -> list[Event]:
    """統合済み 551 item のリストを Event リストへ変換する（純粋関数）。
    同一視キー・本文・重要度の規則はここで一元的に決める。"""
    # 🌏 地点の市区町村候補と、最大震度地点の優先判定に使う
    loc_cities = [(loc, location_city_names(loc)) for loc in locs]
    events: list[Event] = []
    for item in items or []:
        eq = item.get("earthquake") or {}
        hypo = eq.get("hypocenter") or {}
        max_scale = eq.get("maxScale", -1)
        mag = hypo.get("magnitude", -1)
        try:
            mag_f = float(mag)
        except Exception:
            mag_f = -1.0
        place = hypo.get("name") or "不明"
        # ⚠️ センチネル値（-1）は「不明」。深さ 0 は「ごく浅い」という正当な値
        mag_str = "M不明" if mag_f < 0 else f"M{mag_f}"
        depth_v = known_depth(hypo.get("depth"))
        if depth_v is None:
            depth_str = "深さ不明"
        elif depth_v == 0:
            depth_str = "深さごく浅い"
        else:
            depth_str = f"深さ約{depth_v}km"
        # 🌊 津波の見通しを日本語で明示する
        tsunami_jp = {
            "None": "津波の心配なし",
            "Forecast": "若干の海面変動の可能性あり",
            "Advisory": "津波注意報レベル",
            "Warning": "津波警報レベル",
            "Unknown": "津波の有無は確認中",
        }.get(str(eq.get("domesticTsunami") or "None"), "津波の有無は確認中")
        # 🌊 電文コメント原文（国外地震の津波コメント等）があれば本文へ併記する
        _comments = item.get("comments") or {}
        comment_text = str(_comments.get("freeFormComment") or _comments.get("text") or "")
        comment_note = f" / 津波コメント: {comment_text}" if comment_text else ""
        points = item.get("points") or []
        # 🌏 最大震度を観測した地点。登録地点の市区町村と一致する観測点を優先する
        top_pts = [p for p in points if p.get("scale") == max_scale]
        top_note = ""
        if top_pts:
            chosen_top = top_pts[0]
            for p in top_pts:
                if p.get("isArea"):
                    continue
                if any(city_match(str(p.get("addr") or ""), cities) for _, cities in loc_cities):
                    chosen_top = p
                    break
            top_note = f" / 最大震度観測: {chosen_top.get('addr') or chosen_top.get('pref') or ''}"
            if len(top_pts) > 1:
                top_note += f" ほか{len(top_pts) - 1}地点"
        # 🌏 震源マップへのリンク
        lat, lon = hypo.get("latitude"), hypo.get("longitude")
        map_note = ""
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
            map_note = f" / 地図: https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=8/{lat}/{lon}"
        sev = scale_to_severity(max_scale, mag_f)
        guide = ""
        if sev >= Severity.CRITICAL:
            guide = "頭を守り安全な場所へ。火の元を確認し、津波情報に注意。エレベーターは使わない"
        elif sev >= Severity.HIGH:
            guide = "落下物に注意し、続報と津波の有無を確認する"
        # 🌏 地点ごとの震度（市区町村内の実測がある地点のみ実測表示）
        loc_note = ""
        try:
            loc_parts = location_intensity_notes(points, locs)
        except Exception:
            loc_parts = []
        if loc_parts:
            loc_note = " / 地点別: " + " / ".join(loc_parts)
        # 🌏 観測点情報と実測値（実測が無い報では「観測点情報なし」と明示）
        obs_note = " / " + observation_points_note(points)
        # 🌏 県内最大震度と各地の震度（都道府県別トップ）。
        # 地点自体の震度と誤読されないよう「○○県内の最大震度」と明記する
        local_max: dict[str, int] = {}
        pref_max: dict[str, int] = {}
        for p in points:
            pref = p.get("pref") or ""
            try:
                sc = int(p.get("scale") or 0)
            except Exception:
                sc = 0
            if pref:
                pref_max[pref] = max(pref_max.get(pref, 0), sc)
            if pref in ("群馬県", "埼玉県"):
                local_max[pref] = max(local_max.get(pref, 0), sc)
        local_note = ""
        if local_max:
            local_note = " / " + " / ".join(
                f"{k}内の最大震度 {scale_to_str(v)}" for k, v in local_max.items()
            )
        pref_note = ""
        if len(pref_max) > 1:
            top_prefs = sorted(pref_max.items(), key=lambda kv: -kv[1])[:6]
            pref_note = " / 各地の震度:" + "".join(
                f" {short_pref_name(k)} {scale_to_str(v)}" for k, v in top_prefs
            )
        # 🕐 報種別ラベルと発表時刻（どの報の値か判別できるように）
        issue_time = _issue_time(item)
        issue_label = quake_issue_label(item)
        merged_count = int(item.get("_merged_count") or 1)
        issue_note = f" / 発表: {issue_time}（{issue_label}）" if issue_time else f" / 報種別: {issue_label}"
        if merged_count > 1:
            issue_note += f" ※{merged_count}報を統合"
        # 🌏 速報段階では推定であることを明示する
        est_note = quake_estimate_note(item, magnitude=mag_f)
        est_note = f" / {est_note}" if est_note else ""
        body = (
            f"{eq.get('time','')}発生 / 震源: {place} / {mag_str} / {depth_str} / "
            f"最大震度 {scale_to_str(max_scale)} / {tsunami_jp}{comment_note}{top_note}{obs_note}{loc_note}{local_note}{pref_note}{issue_note}{est_note}{map_note}"
        )
        ev = Event(
            kind="earthquake",
            title=f"地震 最大震度{scale_to_str(max_scale)} {place}",
            body=body,
            severity=sev,
            source=source_label,
            # 🌏 統合後の同一地震は発生時刻＋震源名で同一視する
            event_id=f"quake-{eq.get('time') or item.get('time','')}-{place}",
            occurred_at=str(item.get("time") or eq.get("time") or ""),
            action_guide=guide,
            raw=item,
        )
        ev.ensure_id()
        events.append(ev)
    return events
