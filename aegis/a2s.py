# ============================================================
# 📡 A2S client — A.A.S（Aegis Alert System）への唯一の取得経路
# ============================================================
"""Client for the A.A.S hub (A2S), the single intake for
earthquakes, tsunamis, EEW, weather warnings and typhoons.

📌 Contract (the hub's public API):
- REST ``GET /v1/events`` (Bearer token) returns retained
  events, newest-updated first; ``kind=`` filters, ``since=``
  (ISO 8601 or epoch ms) recovers only what changed afterwards.
- SSE ``GET /v1/stream`` replays retained events on connect,
  sends a ``ready`` frame, then pushes live events; comment
  heartbeats arrive every ~25 s. Frames before ``ready`` are
  replay (initial sync) and must never notify.
- Dedupe is by ``key`` + ``updatedAt``.

📌 Enabling is opt-in: both the URL and the token must be set
(config ``a2s_url`` / ``a2s_token``, or the environment variables
``AEGIS_A2S_URL`` / ``AEGIS_A2S_TOKEN``, which win). When unset,
the fetch functions return empty without any network access —
the A2S-covered information simply does not appear, and every
other feature keeps working. There is no direct-source fallback.

📌 Conversion: A2S events are mapped onto the shared item
formats (551/556/552 style) and passed through the same
converters as everything else, so identity keys and rendering
rules live in exactly one place. An event that fails validation
or conversion is dropped on its own — never raised.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from urllib.parse import quote, urlencode

from .convert_eew import eew_events_from_items
from .convert_quake import merge_earthquake_items, quake_events_from_items
from .convert_tsunami import tsunami_events_from_items
from .http_client import UA, fetch_json
from .locations import effective_locations
from .models import Event, Location, Severity
from .weather_codes import WARNING_CODES

A2S_SOURCE_LABEL = "A.A.S（Aegis Alert System）"

# 🔌 再接続までの待機（秒）。尽きたら最後の値を繰り返す
DEFAULT_RECONNECT_DELAYS = (5.0, 10.0, 30.0, 60.0)

# 🕐 SSE の読み取りタイムアウト（秒）。heartbeat は25秒ごとに届くため、
# これを超えて無音なら接続が死んでいるとみなして再接続へ倒す
SSE_READ_TIMEOUT = 90


# ------------------------------------------------------------
# 🎛️ 設定（有効化判定と実行時接続先）
# ------------------------------------------------------------
def resolve_settings(cfg: dict | None) -> tuple[str, str]:
    """(url, token) を返す。環境変数 AEGIS_A2S_URL / AEGIS_A2S_TOKEN が
    config より優先する（トークンを設定ファイルに書かずに渡せるため）。"""
    cfg = cfg or {}
    url = os.environ.get("AEGIS_A2S_URL") or str(cfg.get("a2s_url") or "")
    token = os.environ.get("AEGIS_A2S_TOKEN") or str(cfg.get("a2s_token") or "")
    return url.strip(), token.strip()


def a2s_enabled(cfg: dict | None) -> bool:
    """A2S 経路が有効かどうか。URL とトークンの両方が必要。"""
    url, token = resolve_settings(cfg)
    return bool(url and token)


# fetch_* は引数で cfg を受けない API のため、Engine が起動時にここへ
# 接続先を伝える。未設定の間、取得関数は通信せず空を返す
_CONFIGURED_URL = ""
_CONFIGURED_TOKEN = ""


def set_config(url: str, token: str):
    """Engine が解決した A2S 接続先を伝えるフック。"""
    global _CONFIGURED_URL, _CONFIGURED_TOKEN
    _CONFIGURED_URL = str(url or "").strip()
    _CONFIGURED_TOKEN = str(token or "").strip()


def configured_settings() -> tuple[str, str]:
    """set_config で伝えられた (url, token) を返す。"""
    return _CONFIGURED_URL, _CONFIGURED_TOKEN


# ------------------------------------------------------------
# 🔍 A2S イベントの最小検証
# ------------------------------------------------------------
def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_a2s_event(v) -> bool:
    """受信した JSON が A2S イベントの形をしているかの最小検証。
    形が崩れているものを変換に回すと、変換の失敗なのか上流の差異なのか
    区別できなくなるため、ここで弾く。"""
    if not isinstance(v, dict):
        return False
    key = v.get("key")
    if not isinstance(key, str) or not key:
        return False
    kind = v.get("kind")
    if kind == "quake":
        return (isinstance(v.get("time"), str) and isinstance(v.get("hypocenter"), str)
                and _is_number(v.get("maxScale"))
                and isinstance(v.get("points"), list) and isinstance(v.get("prefScales"), list))
    if kind == "eew":
        return (isinstance(v.get("baseKey"), str) and isinstance(v.get("originTime"), str)
                and _is_number(v.get("estScale"))
                and isinstance(v.get("isWarn"), bool) and isinstance(v.get("cancel"), bool))
    if kind == "tsunami":
        return isinstance(v.get("grade"), str) and isinstance(v.get("areas"), list)
    if kind == "warning":
        # ⚠️ 解除イベント（warnings: [] / maxSeverity: 0 / released: true）も
        # 正規の形式として通す（変換側で「イベント化しない」と扱う）
        return (isinstance(v.get("officeCode"), str) and isinstance(v.get("areaCode"), str)
                and isinstance(v.get("warnings"), list) and _is_number(v.get("maxSeverity")))
    if kind == "typhoon":
        return isinstance(v.get("lines"), list)
    return False


# ------------------------------------------------------------
# 🔁 重複判定（key + updatedAt）
# ------------------------------------------------------------
class DedupTracker:
    """key + updatedAt による重複判定（A2S API の再接続作法）。
    同じ key で updatedAt が変わっていなければ「変化なし」として落とし、
    変わっていれば続報として通す。保持は直近 max_entries 件まで。"""

    def __init__(self, max_entries: int = 500):
        self._seen: dict[str, str] = {}
        self._max_entries = max(1, int(max_entries))

    def is_new_or_updated(self, ev: dict) -> bool:
        key = str((ev or {}).get("key") or "")
        if not key:
            # 📝 キーが無いものは重複判定できないため通す（形式検証は別途）
            return True
        updated = str(ev.get("updatedAt") or "")
        if key in self._seen and self._seen[key] == updated:
            return False
        self._seen[key] = updated
        while len(self._seen) > self._max_entries:
            self._seen.pop(next(iter(self._seen)))
        return True


# ------------------------------------------------------------
# 🔀 A2S イベント → 共通フォーマット item への変換
# ------------------------------------------------------------
def quake_to_item(ev: dict) -> dict | None:
    """A2S の quake イベントを 551 形式 item へ変換する（純粋関数）。
    A2S の points は観測点名を name で持つため addr へ写す。
    深さの null（不明）はセンチネル -1 へ、0（ごく浅い）はそのまま保持する。"""
    if not isinstance(ev, dict) or ev.get("kind") != "quake":
        return None
    points: list[dict] = []
    for p in ev.get("points") or []:
        if not isinstance(p, dict):
            continue
        points.append({
            "pref": str(p.get("pref") or ""),
            "addr": str(p.get("name") or ""),
            "scale": p.get("scale"),
            "isArea": bool(p.get("isArea")),
        })
    mag = ev.get("magnitude")
    depth = ev.get("depthKm")
    item = {
        "id": str(ev.get("telegramId") or ""),
        "time": str(ev.get("announcedTime") or ev.get("time") or ""),
        "code": 551,
        "issue": {
            "time": str(ev.get("announcedTime") or ""),
            "type": str(ev.get("issueType") or ""),
            "correct": str(ev.get("issueCorrect") or ""),
        },
        "earthquake": {
            "time": str(ev.get("time") or ""),
            # 📝 発生時刻の原表記（国外地震のみが持つ）。国内では null のため保持だけする
            "originTime": str(ev.get("originTime") or ""),
            "hypocenter": {
                "name": str(ev.get("hypocenter") or "不明"),
                "magnitude": mag if _is_number(mag) else -1,
                "depth": depth if _is_number(depth) else -1,
                "latitude": ev.get("lat"),
                "longitude": ev.get("lon"),
            },
            "maxScale": ev.get("maxScale"),
            "domesticTsunami": str(ev.get("tsunamiKind") or ""),
        },
        "points": points,
    }
    # 📝 電文コメント原文（国外地震の津波コメント等）は comments へ写す
    if ev.get("tsunamiText"):
        item["comments"] = {"freeFormComment": str(ev["tsunamiText"])}
    return item


def eew_to_item(ev: dict) -> dict | None:
    """A2S の eew イベントを 556 形式 item へ変換する（純粋関数）。
    areaScales の scale は scaleFrom（下限値）に対応する。areaArrivals
    （都道府県ごとの到達予想）は同じ都道府県の先頭の area へ
    arrivalTime として付ける（到達予想が area 単位で探すため）。"""
    if not isinstance(ev, dict) or ev.get("kind") != "eew":
        return None
    arrivals: dict[str, str] = {}
    for a in ev.get("areaArrivals") or []:
        if isinstance(a, dict) and a.get("pref") and a.get("arrivalTime"):
            arrivals.setdefault(str(a["pref"]), str(a["arrivalTime"]))
    areas: list[dict] = []
    arrived_prefs: set[str] = set()
    for a in ev.get("areaScales") or []:
        if not isinstance(a, dict):
            continue
        pref = str(a.get("pref") or "")
        area: dict = {"pref": pref, "name": str(a.get("name") or "")}
        if _is_number(a.get("scale")):
            area["scaleFrom"] = a.get("scale")
        if _is_number(a.get("scaleTo")):
            area["scaleTo"] = a.get("scaleTo")
        if pref and pref in arrivals and pref not in arrived_prefs:
            area["arrivalTime"] = arrivals[pref]
            arrived_prefs.add(pref)
        areas.append(area)
    cancel = ev.get("cancel") is True
    is_assumption = ev.get("isAssumption") is True
    est_mag = ev.get("estMagnitude")
    depth = ev.get("depthKm")
    serial = ev.get("serial")
    try:
        serial = int(serial) if serial is not None else None
    except Exception:
        serial = None
    return {
        "id": str(ev.get("key") or ""),
        "time": str(ev.get("time") or ""),
        "cancelled": cancel,
        "isCancel": cancel,
        "isWarn": ev.get("isWarn") is True,
        # 📝 最終報フラグ。変換器が「最終報」表示に使う
        "isFinal": ev.get("isFinal") is True,
        # 📝 訓練報フラグは A2S の形式には無いが、混入していても保持する
        "isTraining": ev.get("isTraining") is True,
        "isAssumption": is_assumption,
        "issue": {
            "time": str(ev.get("time") or ""),
            "eventId": str(ev.get("eventId")) if ev.get("eventId") else None,
            "serial": serial,
        },
        "earthquake": {
            "originTime": str(ev.get("originTime") or ""),
            "condition": "仮定震源要素" if is_assumption else "",
            "maxScale": ev.get("estScale"),
            "hypocenter": {
                "name": str(ev.get("hypocenter") or "不明"),
                "magnitude": est_mag if _is_number(est_mag) else -1,
                "depth": depth if _is_number(depth) else -1,
                "latitude": ev.get("lat"),
                "longitude": ev.get("lon"),
            },
        },
        "areas": areas,
    }


def tsunami_to_item(ev: dict) -> dict | None:
    """A2S の tsunami イベントを 552 形式 item へ変換する（純粋関数）。
    電文 ID は key（"552:<電文ID>"）から接頭辞を外して取り、従来と同じ
    event_id になるようにする。予想高さはラベル文字列を
    maxHeight.description へ、数値を maxHeight.value へ写す。"""
    if not isinstance(ev, dict) or ev.get("kind") != "tsunami":
        return None
    key = str(ev.get("key") or "")
    telegram_id = key[len("552:"):] if key.startswith("552:") else key
    areas: list[dict] = []
    for a in ev.get("areas") or []:
        if not isinstance(a, dict):
            continue
        area: dict = {"name": str(a.get("name") or ""), "grade": str(a.get("grade") or "")}
        if a.get("maxHeightLabel"):
            max_height: dict = {"description": str(a["maxHeightLabel"])}
            if _is_number(a.get("maxHeightValue")):
                max_height["value"] = a["maxHeightValue"]
            area["maxHeight"] = max_height
        first_height: dict = {}
        if a.get("arrivalLabel"):
            first_height["arrivalTime"] = str(a["arrivalLabel"])
        if a.get("firstHeightCondition"):
            first_height["condition"] = str(a["firstHeightCondition"])
        if first_height:
            area["firstHeight"] = first_height
        areas.append(area)
    return {
        "id": telegram_id,
        "time": str(ev.get("time") or ""),
        "cancelled": ev.get("cancelled") is True,
        "areas": areas,
    }


# ------------------------------------------------------------
# 🌦️ warning（気象警報・注意報）→ Event への変換
# ------------------------------------------------------------
_WARNING_SEVERITY = {4: Severity.CRITICAL, 3: Severity.HIGH, 2: Severity.MEDIUM,
                     1: Severity.LOW, 0: Severity.INFO}


def _warning_action_guide(name: str) -> str:
    """警報・注意報の名称から行動ガイドを出す。"""
    if "大雨" in name or "洪水" in name:
        return "川や崖に近づかない。地下空間を避け、通学ルート・運休情報を確認"
    if "暴風" in name or "強風" in name:
        return "飛来物に注意し、不要な外出を控える。電車の遅延・運休を確認"
    if "大雪" in name:
        return "路面凍結と交通障害に注意。時間に余裕を持って移動"
    return ""


def warning_to_events(ev: dict, loc: Location) -> list[Event]:
    """A2S の warning イベント（一次細分区域ごとに1件）を地点 loc 向けの
    Event リストへ変換する（純粋関数）。
    照合は officeCode（= 地点の jma_pref_code）と areaName（= 地点の
    jma_area_name）。areaName 未設定の地点は官署内全域を対象にする。
    解除イベントは Event 化しない: 発表が無くなることで次回以降の
    取得から自然に消える扱いとする（解除そのものは通知しない）。"""
    if not isinstance(ev, dict) or ev.get("kind") != "warning":
        return []
    if str(ev.get("officeCode") or "") != str(loc.jma_pref_code):
        return []
    if loc.jma_area_name and str(ev.get("areaName") or "") != loc.jma_area_name:
        return []
    if ev.get("released") is True:
        return []
    headline = str(ev.get("headline") or "")
    report_dt = str(ev.get("reportDatetime") or ev.get("time") or "")
    found: dict[str, Severity] = {}
    for w in ev.get("warnings") or []:
        if not isinstance(w, dict):
            continue
        # ⚠️ 除外は明示的な「解除」と「発表警報・注意報はなし」のみ。
        # status が空・欠落のものは継続中として扱う
        if str(w.get("status") or "") in ("解除", "発表警報・注意報はなし"):
            continue
        code = str(w.get("code") or "")
        # ⚠️ 名称が無い・コード表にない警報も落とさずコード付きで扱う
        name = str(w.get("name") or "") or WARNING_CODES.get(code) or f"その他の警報・注意報（コード{code}）"
        sev_num = w.get("severity")
        if _is_number(sev_num) and int(sev_num) in _WARNING_SEVERITY:
            sev = _WARNING_SEVERITY[int(sev_num)]
        elif "特別警報" in name:
            sev = Severity.CRITICAL
        elif name.endswith("警報"):
            sev = Severity.HIGH
        else:
            sev = Severity.MEDIUM
        if name not in found or sev > found[name]:
            found[name] = sev
    events: list[Event] = []
    for name, sev in sorted(found.items(), key=lambda kv: -int(kv[1])):
        ev_out = Event(
            kind="weather_warning",
            title=f"気象 {name}（{loc.name}）",
            body=f"{loc.name}周辺に{name}が発表されています。{headline}".strip(),
            severity=sev,
            source=f"{A2S_SOURCE_LABEL}／気象庁 警報・注意報",
            occurred_at=report_dt,
            action_guide=_warning_action_guide(name),
            raw={"warning": name, "location": loc.name},
        )
        # 🔔 重複排除キーは発表日時（時刻まで）を使う: 同日中の解除→再発表も
        # 拾い、継続中の再通知は防ぐ
        ev_out.event_id = f"warn-{loc.jma_pref_code}-{name}-{report_dt}"
        events.append(ev_out)
    return events


# ------------------------------------------------------------
# 🌀 typhoon（台風）→ Event への変換
# ------------------------------------------------------------
def typhoon_to_event(ev: dict) -> Event | None:
    """A2S の typhoon イベント（台風ごとに1件）を Event へ変換する（純粋関数）。
    表示行は A2S が生成した lines をそのまま使う（データ源に無い項目は
    こちらでも作らない）。lines が空でも名称があれば名称だけは出す。"""
    if not isinstance(ev, dict) or ev.get("kind") != "typhoon":
        return None
    lines = [str(x).strip() for x in (ev.get("lines") or []) if isinstance(x, str) and str(x).strip()]
    if not lines:
        name = str(ev.get("name") or "").strip()
        if not name:
            return None
        lines = [f"・{name}"]
    raw: dict = {}
    for k in ("number", "name", "englishName"):
        if ev.get(k):
            raw[k] = ev[k]
    ev_out = Event(
        kind="typhoon",
        title="台風情報",
        body="\n".join(lines),
        severity=Severity.MEDIUM,
        source=f"{A2S_SOURCE_LABEL}／気象庁 台風情報",
        occurred_at=str(ev.get("time") or ""),
        raw=raw,
    )
    ev_out.ensure_id()
    return ev_out


def event_to_events(ev: dict, locations: list[Location] | None = None) -> list[Event]:
    """A2S イベント1件を内部 Event のリストへ変換する（純粋関数）。
    形式検証を通らないもの・変換できないものは空リストを返し、
    例外にはしない（未知のフィールドで取り込み全体を壊さないため）。"""
    if not is_a2s_event(ev):
        return []
    locs = effective_locations(locations)
    kind = ev.get("kind")
    if kind == "quake":
        item = quake_to_item(ev)
        if item is None:
            return []
        return quake_events_from_items(
            merge_earthquake_items([item]), locs, source_label=A2S_SOURCE_LABEL)
    if kind == "eew":
        item = eew_to_item(ev)
        if item is None:
            return []
        return eew_events_from_items([item], locs, source_label=A2S_SOURCE_LABEL)
    if kind == "tsunami":
        item = tsunami_to_item(ev)
        if item is None:
            return []
        return tsunami_events_from_items([item], source_label=A2S_SOURCE_LABEL)
    if kind == "warning":
        out: list[Event] = []
        for loc in locs:
            out.extend(warning_to_events(ev, loc))
        return out
    if kind == "typhoon":
        tev = typhoon_to_event(ev)
        return [tev] if tev is not None else []
    return []


# ------------------------------------------------------------
# 🌐 REST 取得（GET /v1/events）
# ------------------------------------------------------------
def fetch_events(url: str, token: str | None = None, *, kind: str | None = None,
                 since=None, limit: int = 50) -> list[dict]:
    """A2S の保持イベントを REST で取得し、形式検証を通ったものだけ返す。
    since には ISO 8601 文字列または epoch ミリ秒を指定でき、SSE の
    再生を使わず差分だけを回収したい場合に使う。"""
    params: dict[str, str] = {"limit": str(limit)}
    if kind:
        params["kind"] = str(kind)
    if since is not None:
        params["since"] = str(since)
    headers = {"Authorization": f"Bearer {token}"} if token else None
    data = fetch_json(f"{str(url).rstrip('/')}/v1/events?{urlencode(params)}", headers=headers)
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    return [e for e in events if is_a2s_event(e)]


# ------------------------------------------------------------
# 🌐 REST 取得 → Event 化（fetch 系の実体）
# ------------------------------------------------------------
# 接続先は set_config で伝えられたものを使い、未設定なら通信せず空を返す
# （A2S 対象の情報が出ないだけで、他機能は動き続ける）。
# 到達不能時の例外は呼び出し側（Engine の並列実行）が吸収する。
def fetch_quakes(limit: int = 10, locations: list[Location] | None = None) -> list[Event]:
    """kind=quake を REST で取得し Event 化する。統合で件数が減る場合に
    備え、多めに取得してから limit 件へ切り取る。"""
    url, token = configured_settings()
    if not (url and token):
        return []
    raw_limit = max(limit, min(100, limit * 3))
    raw = fetch_events(url, token, kind="quake", limit=raw_limit)
    items = [it for it in (quake_to_item(e) for e in raw) if it is not None]
    locs = effective_locations(locations)
    return quake_events_from_items(
        merge_earthquake_items(items), locs, source_label=A2S_SOURCE_LABEL)[:limit]


def fetch_tsunamis(limit: int = 5) -> list[Event]:
    """kind=tsunami を REST で取得し Event 化する。"""
    url, token = configured_settings()
    if not (url and token):
        return []
    raw = fetch_events(url, token, kind="tsunami", limit=limit)
    items = [it for it in (tsunami_to_item(e) for e in raw) if it is not None]
    return tsunami_events_from_items(items, source_label=A2S_SOURCE_LABEL)[:limit]


def fetch_eews(limit: int = 10, locations: list[Location] | None = None) -> list[Event]:
    """kind=eew を REST で取得し Event 化する。地点別予想震度は A2S では
    生成されないため、areaScales（scaleTo 込み）から変換器が導出する。"""
    url, token = configured_settings()
    if not (url and token):
        return []
    raw = fetch_events(url, token, kind="eew", limit=limit)
    items = [it for it in (eew_to_item(e) for e in raw) if it is not None]
    locs = effective_locations(locations)
    return eew_events_from_items(items, locs, source_label=A2S_SOURCE_LABEL)[:limit]


def fetch_warnings(loc: Location) -> list[Event]:
    """kind=warning を REST で取得し、地点 loc 向けに Event 化する。
    GET /v1/events?kind=warning は現在発表中の区域だけを返すため、
    解除済みの区域は自然に結果から消える。"""
    url, token = configured_settings()
    if not (url and token):
        return []
    raw = fetch_events(url, token, kind="warning", limit=300)
    out: list[Event] = []
    for e in raw:
        out.extend(warning_to_events(e, loc))
    return out


def fetch_typhoon() -> Event | None:
    """kind=typhoon を REST で取得し、「台風があるときだけ1件の Event」に
    まとめて返す。複数ある場合は表示行を連結する。未設定なら None。"""
    url, token = configured_settings()
    if not (url and token):
        return None
    raw = fetch_events(url, token, kind="typhoon", limit=10)
    events = [e for e in (typhoon_to_event(x) for x in raw) if e is not None]
    if not events:
        return None
    if len(events) == 1:
        return events[0]
    merged = Event(
        kind="typhoon",
        title="台風情報",
        body="\n".join(e.body for e in events),
        severity=Severity.MEDIUM,
        source=f"{A2S_SOURCE_LABEL}／気象庁 台風情報",
        occurred_at=next((e.occurred_at for e in events if e.occurred_at), ""),
        raw={},
    )
    merged.ensure_id()
    return merged


# ------------------------------------------------------------
# 📡 SSE フレーム解析
# ------------------------------------------------------------
class SseFrame:
    """SSE の1フレーム（event: の種別と data: の連結）。"""

    def __init__(self, type: str, data: str):
        self.type = type
        self.data = data

    def __eq__(self, other):
        return isinstance(other, SseFrame) and (self.type, self.data) == (other.type, other.data)

    def __repr__(self):
        return f"SseFrame({self.type!r}, {self.data!r})"


class SseParser:
    """SSE のフレーム解析（状態機械）。テキスト断片を push するか行を
    feed_line するかで取り込み、空行で完成したフレームを返す。
    コメント行（heartbeat）は読み捨てる。"""

    def __init__(self):
        self._buffer = ""
        self._event_type = ""
        self._data_lines: list[str] = []

    def push(self, chunk: str) -> list[SseFrame]:
        self._buffer += chunk
        frames: list[SseFrame] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            frame = self.feed_line(line)
            if frame is not None:
                frames.append(frame)
        return frames

    def feed_line(self, line: str) -> SseFrame | None:
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            if not self._data_lines:
                self._event_type = ""
                return None
            frame = SseFrame(self._event_type or "message", "\n".join(self._data_lines))
            self._event_type = ""
            self._data_lines = []
            return frame
        if line.startswith(":"):
            return None  # heartbeat 等のコメント行
        if line.startswith("event:"):
            self._event_type = line[len("event:"):].strip()
            return None
        if line.startswith("data:"):
            data = line[len("data:"):]
            if data.startswith(" "):
                data = data[1:]
            self._data_lines.append(data)
        return None  # id: / retry: 等は使わない


# ------------------------------------------------------------
# 📡 A2S SSE 常駐クライアント
# ------------------------------------------------------------
def _open_sse_stream(url: str, token: str | None, user_agent: str):
    """SSE ストリームを開き、行イテレーションできる応答オブジェクトを返す。"""
    headers = {"User-Agent": user_agent, "Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT)


class StreamClient:
    """A2S の /v1/stream（SSE）への常駐接続。

    受信ループはデーモンスレッドで回し、切断時は指数バックオフで
    再接続する。再接続時は最後に受信した updatedAt を ``since=`` に
    付けて差分から再生させる（付けられない場合もサーバー側の再生と
    key + updatedAt の重複判定で回収できる）。
    on_event(ev, replay) へ検証済みイベントだけを渡す。replay は
    ready フレームより前（接続時の再生分）かどうかで、呼び出し側は
    再生分を通知に使わないこと。
    """

    # 🌀 台風はブリーフィング表示のみの利用のため SSE では受けない
    ACCEPTED_KINDS = ("quake", "eew", "tsunami", "warning")

    def __init__(self, on_event, *, url: str, token: str | None = None,
                 user_agent: str = UA, delays=DEFAULT_RECONNECT_DELAYS,
                 open_stream=None, log=None, warn=None, wait=None):
        self._on_event = on_event
        self._base_url = str(url or "").rstrip("/")
        self._token = token
        self._user_agent = user_agent
        self._delays = tuple(delays) if delays else DEFAULT_RECONNECT_DELAYS
        self._open_stream = open_stream or _open_sse_stream
        self._log = log or (lambda msg: print(msg, flush=True))
        self._warn = warn or (lambda msg: print(msg, flush=True))
        self._stop = threading.Event()
        # 🧪 再接続待機は停止イベントで割り込めるようにする（テストで差し替え可能）
        self._wait = wait or self._stop.wait
        self._thread: threading.Thread | None = None
        self._resp = None
        self._failures = 0
        self._connected = False
        self._dedup = DedupTracker()
        self._last_updated = ""  # 再接続時の since= に使う最終受信 updatedAt
        self.invalid_count = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def stream_url(self) -> str:
        """接続先 URL。受信済みイベントがあれば since= を付ける。"""
        url = f"{self._base_url}/v1/stream"
        if self._last_updated:
            url += f"?since={quote(self._last_updated, safe='')}"
        return url

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="a2s-sse", daemon=True)
        self._thread.start()

    def stop(self):
        """停止を指示し、接続中なら応答を閉じて受信ループを終わらせる。"""
        self._stop.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _run(self):
        while not self._stop.is_set():
            try:
                resp = self._open_stream(self.stream_url(), self._token, self._user_agent)
                self._resp = resp
                self._connected = True
                # 🔌 接続できたらバックオフをリセットする
                self._failures = 0
                self._log("A2S ストリームに接続しました")
                self._read_stream(resp)
            except Exception as e:
                if self._stop.is_set():
                    break
                self._warn(f"A2S ストリームから切断されました: {e}")
            finally:
                self._connected = False
                if self._resp is not None:
                    try:
                        self._resp.close()
                    except Exception:
                        pass
                    self._resp = None
            if self._stop.is_set():
                break
            delay = self._delays[min(self._failures, len(self._delays) - 1)]
            self._failures += 1
            self._warn(f"A2S ストリームへ {delay:g} 秒後に再接続します")
            if self._wait(delay):
                break

    def _read_stream(self, resp):
        parser = SseParser()
        # 📌 ready が届くまでは接続時の再生分として扱う
        replay = True
        for raw in resp:
            if self._stop.is_set():
                return
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            frame = parser.feed_line(line.rstrip("\r\n"))
            if frame is None:
                continue
            if frame.type == "ready":
                replay = False
                continue
            if frame.type not in self.ACCEPTED_KINDS:
                continue
            try:
                parsed = json.loads(frame.data)
            except Exception:
                self.invalid_count += 1
                continue
            if not is_a2s_event(parsed):
                self.invalid_count += 1
                continue
            updated = str(parsed.get("updatedAt") or "")
            if updated > self._last_updated:
                self._last_updated = updated
            # 🔁 重複判定は key + updatedAt。再生分は記録だけ更新し、
            # 呼び出し側が初期同期としてマージする（通知には使わない）
            changed = self._dedup.is_new_or_updated(parsed)
            if not replay and not changed:
                continue
            try:
                self._on_event(parsed, replay)
            except Exception as e:
                self._warn(f"A2S イベントの取り込みに失敗しました: {e}")
