# ============================================================
# 📡 Radar — RainViewer + 気象庁 降水ナウキャスト
# ============================================================
"""Precipitation radar frames for the dashboard, from two sources.

📌 RainViewer is easy but its rain tiles stop at zoom 7; the JMA
nowcast tiles go to zoom 10. The dashboard shows both, and a
source that fails is reported as failed instead of silently
disappearing.

📌 Frames are cached with freshness linkage: the cache only
extends when the re-fetched frame is the same observation, so
caching never adds delay on top of the source's own latency.
"""
from __future__ import annotations

from .http_client import fetch_json

RAINVIEWER_MAPS_URL = "https://api.rainviewer.com/public/weather-maps.json"


def radar_latest_frame(data) -> dict | None:
    """RainViewer の weather-maps から最新フレーム {path, time} を返す。"""
    try:
        past = ((data or {}).get("radar") or {}).get("past") or []
        if not past:
            return None
        last = past[-1]
        if not last.get("path"):
            return None
        return {"path": str(last["path"]), "time": int(last.get("time") or 0)}
    except Exception:
        return None


def fetch_radar_frame() -> dict | None:
    return radar_latest_frame(fetch_json(RAINVIEWER_MAPS_URL))


# 📡 フレームが古いとみなす閾値（分）。これを超えたら古い旨の注記を出す
RADAR_STALE_MINUTES = 20


def radar_age_text(frame_time, now=None) -> str:
    """フレーム時刻の相対表示（例:「約5分前の観測」）。閾値を超えた場合は
    古い旨の注記を付ける。frame_time が 0/None の場合は ""。
    now は UNIX 時刻（テスト用に注入可能）。"""
    if not frame_time:
        return ""
    import time as _time
    if now is None:
        now = _time.time()
    try:
        age_sec = float(now) - float(frame_time)
    except Exception:
        return ""
    if age_sec < 0:
        age_sec = 0.0  # フレーム時刻が未来（時計ずれ等）の場合は 0 分前に丸める
    minutes = int(age_sec // 60)
    text = f"約{minutes}分前の観測"
    if minutes > RADAR_STALE_MINUTES:
        text += f"（{RADAR_STALE_MINUTES}分以上前のため、古い情報の可能性があります）"
    return text


def tile_xy_float(lat: float, lon: float, z: int) -> tuple[float, float]:
    """緯度経度 → XYZ タイル座標（小数。slippy map 式）。
    地点のタイル内位置を求めるために使う。"""
    import math
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    """緯度経度 → XYZ タイル座標（整数）。"""
    n = 2 ** z
    fx, fy = tile_xy_float(lat, lon, z)
    x, y = int(fx), int(fy)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


# ------------------------------------------------------------
# 🌧️ 気象庁 降水ナウキャスト（第二ソース）
# ------------------------------------------------------------
# 対象時刻一覧: .../nowc/targetTimes_N1.json（実況の配列、新しい順）
# タイル: .../nowc/{basetime}/none/{validtime}/surf/hrpns/{z}/{x}/{y}.png
JMA_NOWCAST_TARGET_URL = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"
JMA_NOWCAST_TILE_BASE = "https://www.jma.go.jp/bosai/jmatile/data/nowc"
JMA_NOWCAST_MAX_ZOOM = 10

# 📝 RainViewer の z7 拡大に関する利用者向け注記
RADAR_RESOLUTION_NOTE = (
    "※ RainViewer の雨タイルはズーム7までしか提供されないため、表示は拡大表示で"
    "実効解像度が粗くなります。高解像度で見たい場合は気象庁ナウキャスト（ズーム10まで）を併用してください"
)


def parse_jma_nowcast_latest(data) -> dict | None:
    """targetTimes_N1.json から最新の実況フレーム {basetime, validtime} を
    返す。実況（basetime == validtime）のうち validtime が最大のものを選ぶ。"""
    best = None
    for el in data or []:
        if not isinstance(el, dict):
            continue
        bt, vt = str(el.get("basetime") or ""), str(el.get("validtime") or "")
        if not bt or not vt or bt != vt:
            continue
        if best is None or vt > best["validtime"]:
            best = {"basetime": bt, "validtime": vt}
    return best


def fetch_jma_nowcast_frame() -> dict | None:
    return parse_jma_nowcast_latest(fetch_json(JMA_NOWCAST_TARGET_URL))


def jma_nowcast_tile_url(frame: dict, z: int, x: int, y: int) -> str:
    return (f"{JMA_NOWCAST_TILE_BASE}/{frame['basetime']}/none/{frame['validtime']}"
            f"/surf/hrpns/{z}/{x}/{y}.png")


def jma_nowcast_time_text(frame: dict | None) -> str:
    """validtime（YYYYMMDDHHMMSS・UTC）を JST の「M月D日 HH:MM」にする。"""
    if not frame:
        return ""
    vt = str(frame.get("validtime") or "")
    if len(vt) < 12:
        return ""
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(vt[:14], "%Y%m%d%H%M%S") + timedelta(hours=9)  # UTC→JST
    except ValueError:
        return ""
    return dt.strftime("%m月%d日 %H:%M")


# ------------------------------------------------------------
# 💾 フレームキャッシュ（鮮度連動）
# ------------------------------------------------------------
class FrameCache:
    """レーダーフレームのキャッシュ。再取得したフレームの時刻が前回と
    同じ（＝新しい観測がまだ配信されていない）場合のみキャッシュを延長する。
    ttl 秒以内は再取得しない。取得失敗時は stale_ttl 秒以内なら前回を返す。"""

    def __init__(self, ttl: float = 120, stale_ttl: float = 900):
        self.ttl = ttl
        self.stale_ttl = stale_ttl
        self._frame: dict | None = None
        self._fetched_at: float = 0.0

    def get(self, fetcher, now: float | None = None):
        """fetcher() でフレームを取得する。
        戻り値: (frame, status) status は "fresh"|"cached"|"extended"|"stale"。"""
        import time as _time
        if now is None:
            now = _time.time()
        if self._frame is not None and now - self._fetched_at < self.ttl:
            return self._frame, "cached"
        try:
            new = fetcher()
        except Exception:
            if self._frame is not None and now - self._fetched_at < self.stale_ttl:
                return self._frame, "stale"
            raise
        if new is None:
            if self._frame is not None and now - self._fetched_at < self.stale_ttl:
                return self._frame, "stale"
            return None, "fresh"
        old_key = self._frame_key(self._frame)
        if self._frame is not None and old_key is not None and old_key == self._frame_key(new):
            # 🕐 フレーム時刻が変わっていない: キャッシュを延長する
            self._fetched_at = now
            return self._frame, "extended"
        self._frame = new
        self._fetched_at = now
        return new, "fresh"

    @staticmethod
    def _frame_key(frame):
        if not frame:
            return None
        return frame.get("validtime") or frame.get("time") or frame.get("path")


# 🌦️ ダッシュボード用の共有キャッシュ（ソースごとに別々に持つ）
RADAR_CACHE = FrameCache()
JMA_RADAR_CACHE = FrameCache()


def fetch_radar_frame_cached(now: float | None = None):
    """鮮度連動キャッシュ経由で RainViewer の最新フレームを返す。"""
    frame, _status = RADAR_CACHE.get(fetch_radar_frame, now=now)
    return frame


def fetch_jma_nowcast_frame_cached(now: float | None = None):
    """鮮度連動キャッシュ経由で気象庁ナウキャストの最新フレームを返す。"""
    frame, _status = JMA_RADAR_CACHE.get(fetch_jma_nowcast_frame, now=now)
    return frame
