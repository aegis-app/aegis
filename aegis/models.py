# ============================================================
# 📝 Models — Severity / Event / Location と震度換算
# ============================================================
"""Core value types shared by every module.

📌 :class:`Event` is the one internal representation that all
converters produce and the engine / notifiers consume.
📌 :class:`Location` validates its coordinates at construction
time so a typo in ``config.json`` fails loudly and early.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import IntEnum


# ------------------------------------------------------------
# 📝 Severity（重要度）
# ------------------------------------------------------------
class Severity(IntEnum):
    """Notification severity. The engine compares these against
    the configured ``notify_min_severity`` threshold."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return {0: "INFO", 1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "CRITICAL"}[int(self)]

    @property
    def emoji(self) -> str:
        return {0: "ℹ️", 1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴"}[int(self)]


# ------------------------------------------------------------
# 📝 Event（統一イベント形式）
# ------------------------------------------------------------
@dataclass
class Event:
    """One piece of disaster information, whatever its source.

    ``kind`` is one of: earthquake | tsunami | eew |
    weather_warning | weather_forecast | typhoon.
    ``raw`` keeps the source item for converters/engine helpers
    that need fields the display text does not carry; it is never
    serialised into webhook payloads.
    """

    kind: str
    title: str
    body: str
    severity: Severity = Severity.INFO
    source: str = ""
    event_id: str = ""
    occurred_at: str = ""
    action_guide: str = ""
    raw: dict = field(default_factory=dict)

    def ensure_id(self) -> str:
        """Fill ``event_id`` with a stable content hash if unset."""
        if not self.event_id:
            base = f"{self.kind}|{self.title}|{self.occurred_at}|{self.body[:80]}"
            self.event_id = hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]
        return self.event_id

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.label
        d.pop("raw", None)
        return d

    def format_message(self) -> str:
        lines = [f"{self.severity.emoji} [{self.severity.label}] {self.title}", self.body]
        if self.action_guide:
            lines.append(f"▶ 行動ガイド: {self.action_guide}")
        if self.source:
            lines.append(f"(出典: {self.source} / {self.occurred_at})")
        return "\n".join(lines)


# ------------------------------------------------------------
# 📍 Location（監視地点）と座標チェック
# ------------------------------------------------------------
# 都道府県コード → 座標のおおよその範囲 (min_lat, max_lat, min_lon, max_lon)。
# カスタム地点の座標と都道府県コードの簡易照合用。離島を含むため広めに取る
_PREF_BBOX = {
    "010000": (41.0, 45.6, 139.0, 146.0), "020000": (40.4, 41.6, 139.5, 141.6),
    "030000": (38.7, 40.5, 140.5, 142.2), "040000": (37.7, 39.0, 140.2, 141.7),
    "050000": (38.7, 40.5, 139.6, 140.9), "060000": (37.7, 38.9, 139.3, 140.7),
    "070000": (36.7, 38.0, 139.1, 141.1), "080000": (35.6, 37.0, 139.6, 141.0),
    "090000": (36.1, 37.1, 139.3, 140.4), "100000": (35.9, 37.1, 138.3, 139.6),
    "110000": (35.7, 36.3, 138.7, 139.9), "120000": (34.5, 36.0, 139.6, 140.5),
    "130000": (35.4, 35.9, 138.8, 139.9), "140000": (35.1, 35.7, 138.9, 139.8),
    "150000": (36.7, 38.6, 137.5, 139.9), "160000": (36.3, 37.0, 136.7, 137.8),
    "170000": (36.8, 37.5, 136.2, 137.4), "180000": (35.3, 36.3, 135.4, 136.4),
    "190000": (35.3, 36.0, 138.1, 139.2), "200000": (35.1, 37.0, 137.4, 138.7),
    "210000": (35.1, 36.5, 136.3, 137.7), "220000": (34.5, 35.7, 137.5, 139.2),
    "230000": (34.5, 35.5, 136.5, 137.9), "240000": (33.7, 35.3, 135.7, 137.0),
    "250000": (34.8, 35.7, 135.6, 136.4), "260000": (34.6, 35.8, 134.8, 136.0),
    "270000": (34.3, 35.1, 135.0, 135.9), "280000": (34.2, 35.7, 134.2, 135.9),
    "290000": (33.8, 34.8, 135.5, 136.2), "300000": (33.4, 34.3, 135.0, 135.9),
    "310000": (35.0, 35.7, 132.9, 134.4), "320000": (34.3, 35.6, 131.6, 133.4),
    "330000": (34.3, 35.3, 133.2, 134.5), "340000": (34.0, 34.9, 132.0, 133.4),
    "350000": (33.8, 34.7, 130.8, 132.3), "360000": (33.4, 34.3, 133.9, 134.8),
    "370000": (34.0, 34.5, 133.5, 134.8), "380000": (32.9, 34.1, 132.0, 133.7),
    "390000": (32.7, 33.9, 132.5, 134.3), "400000": (32.9, 34.0, 129.9, 131.2),
    "410000": (32.9, 33.6, 129.6, 130.4), "420000": (31.9, 34.7, 128.6, 130.4),
    "430000": (32.2, 33.2, 129.9, 131.2), "440000": (32.7, 33.7, 130.8, 132.1),
    "450000": (31.3, 32.9, 130.6, 131.9), "460000": (30.3, 32.3, 129.5, 131.2),
    "471000": (24.0, 27.9, 122.9, 131.3),
}

# ⚠️ 日本のおおよその範囲（域外警告用）
JAPAN_LAT_RANGE = (20.0, 46.0)
JAPAN_LON_RANGE = (122.0, 154.0)


def location_warnings(loc: "Location") -> list[str]:
    """地点座標の警告を返す。範囲外は Location 生成時に例外になるため、
    ここでは日本域外と都道府県コードとの不整合（簡易照合）を警告する。"""
    warns: list[str] = []
    if not (JAPAN_LAT_RANGE[0] <= loc.lat <= JAPAN_LAT_RANGE[1]
            and JAPAN_LON_RANGE[0] <= loc.lon <= JAPAN_LON_RANGE[1]):
        warns.append(f"{loc.name}: 座標が日本のおおよその範囲外です（緯度{loc.lat}・経度{loc.lon}）")
        return warns
    bbox = _PREF_BBOX.get(str(loc.jma_pref_code))
    if bbox and not (bbox[0] <= loc.lat <= bbox[1] and bbox[2] <= loc.lon <= bbox[3]):
        warns.append(
            f"{loc.name}: 座標（緯度{loc.lat}・経度{loc.lon}）が都道府県コード "
            f"{loc.jma_pref_code} の範囲と一致しない可能性があります")
    return warns


@dataclass
class Location:
    """A watched place.

    ``jma_pref_code`` is the JMA forecast-area code (e.g. 100000
    for Gunma). ``jma_area_name`` is the primary subdivision name
    used to match weather warnings. ``city`` is the municipality
    name used to match earthquake observation points; when empty
    it is complemented from the place name (see locations.py).
    """

    name: str
    lat: float
    lon: float
    jma_pref_code: str
    jma_area_name: str = ""
    city: str = ""

    def __post_init__(self):
        # ⌨️ 座標の範囲チェック: 緯度・経度の取り違えや誤入力を早期に弾く
        try:
            lat, lon = float(self.lat), float(self.lon)
        except Exception:
            raise ValueError(f"地点「{self.name}」の座標が数値ではありません")
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"地点「{self.name}」の緯度が範囲外です: {lat}")
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"地点「{self.name}」の経度が範囲外です: {lon}")
        self.lat, self.lon = lat, lon

    @staticmethod
    def from_dict(d: dict) -> "Location":
        return Location(
            name=d["name"], lat=float(d["lat"]), lon=float(d["lon"]),
            jma_pref_code=str(d["jma_pref_code"]), jma_area_name=d.get("jma_area_name", ""),
            city=str(d.get("city") or d.get("municipality") or ""),
        )


# ------------------------------------------------------------
# 📝 Seismic scale（震度コード換算）
# ------------------------------------------------------------
# 震度コード: 10=震度1, 20=2, 30=3, 40=4, 45=5弱, 50=5強, 55=6弱, 60=6強, 70=7
SCALE_MAP = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7"}


def scale_to_str(scale) -> str:
    try:
        s = int(scale)
        if s <= 0:
            return "不明"  # 震度コードの -1 等は「震度不明」
        return SCALE_MAP.get(s, str(s))
    except Exception:
        return str(scale)


def scale_to_severity(scale, magnitude: float) -> Severity:
    # 🌏 震度と M のどちらか高い方で判定する（震度が小さくても
    # M が大きければ重要度を上げる）
    try:
        s = int(scale)
    except Exception:
        s = 0
    if s >= 45 or (magnitude or 0) >= 6.0:
        return Severity.CRITICAL
    if s >= 40 or (magnitude or 0) >= 5.0:
        return Severity.HIGH
    if s >= 30 or (magnitude or 0) >= 4.5:
        return Severity.MEDIUM
    if s >= 20:
        return Severity.LOW
    return Severity.INFO
