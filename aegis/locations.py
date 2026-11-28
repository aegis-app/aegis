# ============================================================
# 📍 Locations — 地点・観測点の照合ヘルパー
# ============================================================
"""Matching watched locations against earthquake observation points.

📌 地点震度は「同一市区町村内の観測点実測」がある場合のみ実測として
表示し、無い地点は「実測なし」と明示する。最寄り観測点の実測値を
根拠に地点の震度を推定表示する仕組みは持たない（推定値を作らない）。
EEW の地域別予想震度（公式の推定情報）は対象外で別経路に残す。
"""
from __future__ import annotations

from .models import Location, scale_to_str

# 地点名から市区町村名が一意に決まる既知の対応（city 未設定地点の補完用）
_KNOWN_CITIES = {
    "高崎": "高崎市", "大宮": "さいたま市", "仙台": "仙台市", "前橋": "前橋市",
}

# fetch 系に locations が渡されない場合の既定地点（初期設定の高崎・大宮）。
# Engine は生成時に set_locations で自前の地点を登録するため通常は使われない
_DEFAULT_LOCATIONS = [
    Location("高崎", 36.3219, 139.0023, "100000", "南部", "高崎市"),
    Location("大宮", 35.9064, 139.6239, "110000", "南部", "さいたま市"),
]
_ACTIVE_LOCATIONS: list[Location] | None = None


def set_locations(locations: list[Location] | None):
    """Engine が登録地点を伝えるためのフック（fetch 系の既定の照合対象）。"""
    global _ACTIVE_LOCATIONS
    _ACTIVE_LOCATIONS = list(locations) if locations else None


def effective_locations(locations: list[Location] | None) -> list[Location]:
    """引数 → 登録済み地点 → 既定地点 の順で照合対象を決める。"""
    if locations is not None:
        return locations
    if _ACTIVE_LOCATIONS is not None:
        return _ACTIVE_LOCATIONS
    return _DEFAULT_LOCATIONS


def location_city_names(loc: Location) -> list[str]:
    """地点に対応する市区町村名の候補を優先順に返す。
    明示設定（city）→ 既知の対応 → 地点名からの推定 の順。
    観測点 addr は市区町村名から始まるため前方一致で照合できる。"""
    names: list[str] = []

    def add(v: str):
        v = (v or "").strip()
        if v and v not in names:
            names.append(v)

    add(loc.city)
    raw = (loc.name or "").strip()
    inner = ""
    if "（" in raw and raw.endswith("）"):
        inner = raw[raw.index("（") + 1:-1]
    for base in (inner, raw):
        if not base:
            continue
        if base in _KNOWN_CITIES:
            add(_KNOWN_CITIES[base])
        if base.endswith(("市", "区", "町", "村")):
            add(base)
        else:
            add(base + "市")
            add(base + "区")
    return names


def city_match(addr: str, cities: list[str]) -> str:
    """addr が市区町村候補のいずれかで始まればその市区町村名を返す（無ければ ""）。"""
    addr = addr or ""
    for c in cities:
        if addr == c or addr.startswith(c):
            return c
    return ""


# ------------------------------------------------------------
# 🌏 観測点情報と地点ごとの実測表示
# ------------------------------------------------------------
def observed_points(points) -> list[dict]:
    """points のうち震度観測点の実測データ（地域単位の isArea を除き、
    実測震度が正の値）のものだけを返す。"""
    out: list[dict] = []
    for p in points or []:
        if not isinstance(p, dict) or p.get("isArea"):
            continue
        try:
            sc = int(p.get("scale") or 0)
        except Exception:
            continue
        if sc > 0:
            out.append(p)
    return out


def observation_points_note(points, top_n: int = 5) -> str:
    """観測点情報の表示テキスト（純粋関数）。
    最大震度観測地点・観測点総数・主要観測点（震度上位 top_n 件）を出す。
    実測データが無い報では無理に出さず「観測点情報なし」と明示する。"""
    obs = observed_points(points)
    if not obs:
        return "観測点情報なし（この報には震度観測点の実測データが含まれていません）"

    def sc_of(p):
        try:
            return int(p.get("scale") or 0)
        except Exception:
            return 0

    max_sc = max(sc_of(p) for p in obs)
    top_addr = ""
    for p in obs:
        if sc_of(p) == max_sc:
            top_addr = str(p.get("addr") or p.get("pref") or "")
            break
    major = sorted(obs, key=lambda p: (-sc_of(p), str(p.get("addr") or "")))[:top_n]
    major_txt = "、".join(
        f"{p.get('addr') or p.get('pref') or ''}: 実測震度 {scale_to_str(sc_of(p))}" for p in major)
    return (f"観測点情報: 最大震度観測 {top_addr}: 実測震度 {scale_to_str(max_sc)}"
            f" / 観測点総数 {len(obs)}地点 / 主要観測点: {major_txt}")


def location_intensity_notes(points, locations) -> list[str]:
    """地点ごとの震度表示テキスト（純粋関数）。
    同一市区町村内の観測点実測がある場合のみ実測ベースとして根拠つきで示し、
    無い地点は「実測なし（市区町村内の観測点実測なし）」と明示する。"""
    obs = observed_points(points)
    notes: list[str] = []
    for loc in locations or []:
        cities = location_city_names(loc)
        best = None
        for p in obs:
            city = city_match(str(p.get("addr") or ""), cities)
            if not city:
                continue
            sc = int(p.get("scale") or 0)
            if best is None or sc > best[1]:
                best = (city, sc, str(p.get("addr") or ""))
        if best is not None:
            notes.append(
                f"{loc.name}: {best[0]}の震度 {scale_to_str(best[1])}"
                f"（実測ベース・観測点{best[2]}の実測震度 {scale_to_str(best[1])}）")
            continue
        notes.append(f"{loc.name}: 実測なし（市区町村内の観測点実測なし）")
    return notes


def quake_estimate_note(item: dict, *, magnitude: float = -1.0) -> str:
    """速報段階の地震報について推定値であることを明示するテキスト（純粋関数）。
    確定報（詳細情報等）では "" を返し、確定値と推定値を混同させない。"""
    typ = str(((item or {}).get("issue") or {}).get("type") or "")
    if typ == "ScalePrompt":
        return ("推定情報: この報は震度速報段階で、震源・マグニチュードは未確定です"
                "（確定報で更新されます）")
    if typ == "Destination":
        mag_txt = f"推定M{magnitude}" if magnitude is not None and magnitude >= 0 else "M不明"
        return (f"推定情報: この報は震源情報段階で、震源・{mag_txt}は速報段階の推定値です"
                "（震度の実測は詳細情報で更新されます）")
    return ""
