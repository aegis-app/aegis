# ============================================================
# 🧩 Geo — 都道府県名・距離・時刻などの共通ヘルパー
# ============================================================
"""Small pure helpers shared by the converters."""
from __future__ import annotations

# 📝 気象庁予報区コード → 都道府県名（EEW の area.pref との照合用）
PREF_NAMES = {
    "010000": "北海道", "020000": "青森県", "030000": "岩手県", "040000": "宮城県",
    "050000": "秋田県", "060000": "山形県", "070000": "福島県", "080000": "茨城県",
    "090000": "栃木県", "100000": "群馬県", "110000": "埼玉県", "120000": "千葉県",
    "130000": "東京都", "140000": "神奈川県", "150000": "新潟県", "160000": "富山県",
    "170000": "石川県", "180000": "福井県", "190000": "山梨県", "200000": "長野県",
    "210000": "岐阜県", "220000": "静岡県", "230000": "愛知県", "240000": "三重県",
    "250000": "滋賀県", "260000": "京都府", "270000": "大阪府", "280000": "兵庫県",
    "290000": "奈良県", "300000": "和歌山県", "310000": "鳥取県", "320000": "島根県",
    "330000": "岡山県", "340000": "広島県", "350000": "山口県", "360000": "徳島県",
    "370000": "香川県", "380000": "愛媛県", "390000": "高知県", "400000": "福岡県",
    "410000": "佐賀県", "420000": "長崎県", "430000": "熊本県", "440000": "大分県",
    "450000": "宮崎県", "460000": "鹿児島県", "471000": "沖縄県",
}

S_WAVE_KM_S = 3.5  # S波の伝播速度（EEW 到達推定用）


def short_pref_name(name: str) -> str:
    """都道府県名の末尾の 県/府/都 を1文字だけ落とす（京都府→京都）。
    rstrip だと「京都府」が「京」まで削れてしまうため末尾1文字だけを見る。"""
    return name[:-1] if name.endswith(("県", "府", "都")) else name


def known_depth(v):
    """震源の深さとして「判明している」値を返す（判明していなければ None）。

    気象庁の表記では 0 が「ごく浅い」を表す正当な値であり、不明・未設定は
    -1 等の負値センチネル・null・欠落で表される。そのため 0 は判明した値
    として保持し、負値・非数値は不明（None）とする。整数値は int で返し、
    表示が「40.0km」のようにならないようにする。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return int(f) if f == int(f) else f


def parse_jst_time(s: str):
    """JST の時刻文字列を datetime にする。対応形式に合わなければ None。"""
    from datetime import datetime
    s = (s or "")[:19]
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の大円距離（km）。"""
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
