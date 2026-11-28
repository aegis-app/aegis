# ============================================================
# 🚨 Convert EEW — 緊急地震速報（556 形式）の Event 化と到達予想
# ============================================================
"""EEW conversion from the shared 556-style item format.

📌 Identity: follow-ups of the same EEW share an event_id built
from the origin time plus the rounded hypocenter coordinates
(0.1° grid), so a renamed hypocenter cannot break dedupe.
Cancellations get a ``-cancel`` suffix (a separate event that
always notifies), training/test reports a ``-training`` suffix
and are kept as INFO, outside the notification targets.

📌 Guidance text is tiered by the estimated maximum scale:
45+ strong, 40 intermediate, otherwise a plain caution.
📌 Location forecasts are derived client-side from the area
scales (A.A.S does not know the watched locations).
"""
from __future__ import annotations

from .geo import PREF_NAMES, S_WAVE_KM_S, haversine_km, known_depth, parse_jst_time
from .models import Event, Location, Severity, scale_to_str, scale_to_severity


def _area_scale_str(area: dict) -> str:
    """area の予想震度を文字列にする。scale があればそれを、無ければ
    scaleFrom〜scaleTo の範囲を使う（scaleTo=99 は「〜程度以上」）。"""
    sc = area.get("scale")
    if isinstance(sc, (int, float)) and sc > 0:
        return scale_to_str(sc)
    try:
        sf = int(area.get("scaleFrom") or 0)
    except Exception:
        sf = 0
    try:
        st = int(area.get("scaleTo") or 0)
    except Exception:
        st = 0
    if sf > 0 and st == 99:
        return f"{scale_to_str(sf)}程度以上"
    if sf > 0 and st > 0 and st != sf:
        return f"{scale_to_str(sf)}〜{scale_to_str(st)}"
    if sf > 0:
        return scale_to_str(sf)
    if st > 0:
        return scale_to_str(st)
    return ""


def eew_location_scales(item: dict, locations: list[Location]) -> list[str]:
    """areas を地点の都道府県・一次細分区域に照合し、
    「高崎: 予想震度5弱」形式の地点別予想震度を返す。"""
    areas = item.get("areas") or []
    lines: list[str] = []
    for loc in locations:
        pref = PREF_NAMES.get(str(loc.jma_pref_code), "")
        if not pref:
            continue
        matched = [a for a in areas if a.get("pref") == pref]
        if not matched:
            continue
        chosen = matched[0]
        # 📍 同じ都道府県に複数の細分区域がある場合は地点の区域名を含むものを優先
        if loc.jma_area_name:
            for a in matched:
                if loc.jma_area_name in str(a.get("name") or ""):
                    chosen = a
                    break
        s = _area_scale_str(chosen)
        if s:
            lines.append(f"{loc.name}: 予想震度{s}")
    return lines


def eew_is_training(item: dict) -> bool:
    """訓練報・テスト報かどうか。フラグ・issue.type・見出し・震源名を
    横断して判定する（訓練報は候補フィールドのどれかに現れるため）。"""
    if not isinstance(item, dict):
        return False
    for key in ("isTraining", "training", "isTest", "test"):
        if item.get(key) is True:
            return True
    issue = item.get("issue") or {}
    texts = [
        str(issue.get("type") or ""), str(item.get("headline") or ""),
        str(item.get("title") or ""),
        str(((item.get("earthquake") or {}).get("hypocenter") or {}).get("name") or ""),
    ]
    joined = " ".join(texts)
    return ("訓練" in joined or "テスト" in joined
            or "Training" in joined or "TestReport" in joined)


def eew_event_id(item: dict, eq: dict, hypo_name: str, cancel: bool, training: bool = False) -> str:
    """EEW の同一視キー。発生時刻＋震央座標の丸め値（小数1桁≒11km格子）。
    座標が無い場合のみ震源名を使う。取消・訓練は接尾辞で別イベントにする。"""
    base = f"eew-{eq.get('originTime') or item.get('time','')}"
    hypo = eq.get("hypocenter") or {}
    lat, lon = hypo.get("latitude"), hypo.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
        base += f"-{round(float(lat), 1)},{round(float(lon), 1)}"
    else:
        base += f"-{hypo_name}"
    if cancel:
        base += "-cancel"
    if training:
        base += "-training"
    return base


def eew_area_forecasts(item: dict) -> list[str]:
    """areas から地域別予想震度を全件返す（すべて推定値）。"""
    out: list[str] = []
    for a in (item or {}).get("areas") or []:
        s = _area_scale_str(a)
        if not s:
            continue
        name = str(a.get("name") or a.get("pref") or "")
        if not name:
            continue
        txt = f"{name}: 予想震度{s}"
        if txt not in out:
            out.append(txt)
    return out


def eew_is_cancel(item: dict) -> bool:
    """取消かどうか。実形式の cancelled と旧形式の isCancel の両方を見る。"""
    if not isinstance(item, dict):
        return False
    if "cancelled" in item:
        return bool(item.get("cancelled"))
    return bool(item.get("isCancel"))


def eew_is_warn(item: dict, default: bool = False) -> bool:
    """警報かどうか。明示フラグ isWarn を優先し（A2S 経由は必ず持つ）、
    556 実形式（code=556 / cancelled / issue の eventId・serial）は警報とする。"""
    if not isinstance(item, dict):
        return default
    if "isWarn" in item and item.get("isWarn") is not None:
        return bool(item.get("isWarn"))
    if item.get("code") in (556, "556"):
        return True
    issue = item.get("issue") or {}
    if "cancelled" in item or issue.get("eventId") or issue.get("serial") is not None:
        return True
    return default


def eew_is_assumption(item: dict, eq: dict | None = None) -> bool:
    """仮定震源要素かどうか（フラグ、または earthquake.condition の表記）。"""
    if not isinstance(item, dict):
        return False
    if item.get("isAssumption") is True:
        return True
    eq = eq if isinstance(eq, dict) else (item.get("earthquake") or {})
    return "仮定震源要素" in str(eq.get("condition") or "")


def eew_serial(item: dict):
    """(serial, eventId) を issue から取る。"""
    issue = (item or {}).get("issue") or {}
    serial = issue.get("serial")
    try:
        serial = int(serial) if serial is not None else None
    except Exception:
        serial = None
    event_id = issue.get("eventId") or (item or {}).get("eventId")
    return serial, (str(event_id) if event_id else None)


def eew_estimated_max_scale(item: dict, eq: dict) -> int:
    """推定最大震度。earthquake.maxScale があればそれを、無ければ
    areas の scaleFrom（または scale）の最大値を使う。"""
    ms = (eq or {}).get("maxScale")
    if isinstance(ms, (int, float)) and ms >= 0:
        return int(ms)
    best = -1
    for a in (item or {}).get("areas") or []:
        if not isinstance(a, dict):
            continue
        for key in ("scaleFrom", "scale"):
            v = a.get(key)
            if isinstance(v, (int, float)) and v > best:
                best = int(v)
    return best


def eew_action_guide(est_scale, is_warn: bool) -> str:
    """行動文言を推定最大震度で出し分ける。
    45以上（5弱〜）は強い文言、40（震度4）は中間、30以下は注意喚起のみ。
    震度不明の場合は警報なら強い文言、予報なら注意喚起のみ。
    取消・訓練報では呼び出し側が空にする。"""
    try:
        scale = int(est_scale)
    except Exception:
        scale = -1
    if scale >= 45:
        return "強い揺れに備え、頭を守り安全な場所へ移動する"
    if scale == 40:
        return "やや強い揺れに備え、落下物に注意して頭を守る"
    if scale > 0:
        return "揺れに注意する"
    return "強い揺れに備え、頭を守り安全な場所へ移動する" if is_warn else "揺れに注意する"


def eew_events_from_items(data, locs: list[Location],
                          source_label: str = "A.A.S（Aegis Alert System）",
                          default_is_warn: bool = False) -> list[Event]:
    """556 item のリストを Event リストへ変換する（純粋関数）。
    予報/警報の区別はタイトルに（予報）/（警報）と出し、報番号も表示する。"""
    events: list[Event] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        eq = item.get("earthquake")
        if not eq:
            continue
        hypo = eq.get("hypocenter") or {}
        hypo_name = hypo.get("name") or "不明"
        cancel = eew_is_cancel(item)
        training = eew_is_training(item)
        is_warn = eew_is_warn(item, default=default_is_warn)
        is_assumption = eew_is_assumption(item, eq)
        serial, _src_event_id = eew_serial(item)
        est_scale = eew_estimated_max_scale(item, eq)
        try:
            est_mag = float(hypo.get("magnitude"))
        except Exception:
            est_mag = -1.0
        # 📝 対象都道府県は打ち切らず全件列挙する
        prefs: list[str] = []
        for a in item.get("areas") or []:
            p = a.get("pref")
            if p and p not in prefs:
                prefs.append(p)
        est_depth_v = known_depth(hypo.get("depth"))
        if est_depth_v is None:
            est_depth_str = "推定深さ不明"
        elif est_depth_v == 0:
            est_depth_str = "推定深さごく浅い"
        else:
            est_depth_str = f"推定深さ約{est_depth_v}km"
        kind_label = "警報" if is_warn else "予報"
        body = (
            f"【推定情報】推定震源: {hypo_name} / 推定最大震度 {scale_to_str(est_scale)} / "
            f"{'推定M不明' if est_mag < 0 else f'推定M{est_mag}'} / {est_depth_str} / "
            f"発表: {item.get('time','')} / ※EEWの値は確定値ではなく推定値です（確定報で更新されます）"
        )
        body += f" / 区分: 緊急地震速報（{kind_label}）"
        if serial is not None:
            body += f" / 第{serial}報"
        if item.get("isFinal") is True and not cancel:
            body += " / 最終報"
        if is_assumption:
            body += " / ※仮定震源要素（震源要素が仮定に基づく推定です）"
        area_forecasts = eew_area_forecasts(item)
        if area_forecasts and not cancel:
            body += " / 地域別予想震度（推定）: " + "、".join(area_forecasts)
        if training:
            body += " / ※訓練報・テスト報のため通知対象外"
        if prefs:
            body += " / 対象: " + "、".join(prefs)
        if not cancel and not training:
            loc_scales = eew_location_scales(item, locs)
            if loc_scales:
                body += " / " + " / ".join(loc_scales)
        if training:
            sev = Severity.INFO
        elif cancel:
            sev = Severity.MEDIUM
        else:
            sev = scale_to_severity(est_scale, est_mag)
            if sev < Severity.HIGH and (est_scale in (-1, None)):
                sev = Severity.HIGH
        # 📝 正規化した raw: Engine の上方修正判定が raw の maxScale を読むため、
        # 推定最大震度を補い、区分等の判定結果も残す
        raw_item = dict(item)
        raw_eq = dict(eq)
        raw_eq["maxScale"] = est_scale
        raw_item["earthquake"] = raw_eq
        raw_item["isWarn"] = is_warn
        raw_item["isAssumption"] = is_assumption
        raw_item["cancelled"] = cancel
        raw_item["isCancel"] = cancel
        ev = Event(
            kind="eew",
            title=f"{'【訓練】' if training else ''}緊急地震速報（{kind_label}）{'（取消）' if cancel else ''} {hypo_name}",
            body=body,
            severity=sev,
            source=source_label,
            event_id=eew_event_id(item, eq, hypo_name, cancel, training),
            occurred_at=str(item.get("time") or ""),
            action_guide="" if (cancel or training) else eew_action_guide(est_scale, is_warn),
            raw=raw_item,
        )
        ev.ensure_id()
        events.append(ev)
    return events


# ------------------------------------------------------------
# 🚨 EEW 到達予想（主要動の到達時刻を地点ごとに推定）
# ------------------------------------------------------------
def eew_arrival_estimates(item: dict, locations: list[Location], now=None) -> list[str]:
    """地点ごとの主要動到達予想テキストを作る（静的テキスト用）。
    area に arrivalTime があればそれを使い、無ければ震源距離と
    S波 3.5km/s で推定し「推定」と明記する。``now`` はテスト用に注入可能。"""
    from datetime import datetime, timedelta
    import math
    if not item or item.get("isCancel") or item.get("cancelled"):
        return []
    eq = item.get("earthquake") or {}
    origin = parse_jst_time(str(eq.get("originTime") or item.get("time") or ""))
    if origin is None:
        return []
    now = now or datetime.now()
    hypo = eq.get("hypocenter") or {}
    hlat, hlon = hypo.get("latitude"), hypo.get("longitude")
    try:
        depth = float(hypo.get("depth"))
    except Exception:
        depth = 0.0
    if depth < 0:
        depth = 0.0  # 深さ不明（負値センチネル）・ごく浅い（0）は推定では 0km とみなす
    areas = item.get("areas") or []
    lines: list[str] = []
    for loc in locations:
        arrival = None
        estimated = False
        pref = PREF_NAMES.get(str(loc.jma_pref_code), "")
        if pref:
            for a in areas:
                if a.get("pref") == pref and a.get("arrivalTime"):
                    arrival = parse_jst_time(str(a["arrivalTime"]))
                    if arrival is not None:
                        break
        if arrival is None and isinstance(hlat, (int, float)) and isinstance(hlon, (int, float)):
            dist = math.sqrt(haversine_km(loc.lat, loc.lon, float(hlat), float(hlon)) ** 2 + depth ** 2)
            arrival = origin + timedelta(seconds=dist / S_WAVE_KM_S)
            estimated = True
        if arrival is None:
            continue
        # 📝 floor で切り下げる（到達直後1秒未満を「あと約0秒」と出さないため）
        remain = int(math.floor((arrival - now).total_seconds()))
        note = f"あと約{remain}秒" if remain >= 0 else "到達した可能性が高いです"
        est = "推定・" if estimated else ""
        lines.append(f"{loc.name}の主要動到達予想 {arrival.strftime('%H:%M')}（{est}{note}）")
    return lines
