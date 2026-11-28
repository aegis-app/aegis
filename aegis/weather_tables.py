# ============================================================
# 🌧️ Weather tables — ダッシュボード用天気表（serve 専用）
# ============================================================
"""Row data for the dashboard's weather tables (hourly / weekly /
JMA precipitation probability). Pure builders take already-fetched
JSON so they can be tested without the network.
"""
from __future__ import annotations

from .http_client import fetch_json
from .models import Location
from .weather import OPEN_METEO_MODELS, forecast_area_code, normalize_open_meteo
from .weather_codes import WMO_EMOJI, WMO_WEATHER

_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def wx_icon(code, pop=None, hour=None) -> str:
    """ダッシュボード表用の天気絵文字。アイコンは天気コードに統一する:
    降水確率による上書きは「晴れなのに雨アイコン」等の矛盾を生むため
    行わない（降水確率は数値で表示する）。夜の晴れを月にする変換のみ残す。
    pop 引数は呼び出し互換のために残すがアイコン決定には使わない。"""
    icon = WMO_EMOJI.get(code, "☁")
    if icon == "☀" and hour is not None and (hour >= 21 or hour <= 4):
        icon = "🌙"
    return icon


def _fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def pops_rows(data, area_code: str) -> list[dict]:
    """気象庁降水確率を行データで返す。pops_text と同じ並び・最大6件。"""
    rows: list[dict] = []
    if not data:
        return rows
    for ts in data[0].get("timeSeries") or []:
        tas = ts.get("areas") or []
        if not tas or "pops" not in tas[0]:
            continue
        pa = None
        for a in tas:
            if str((a.get("area") or {}).get("code") or "") == area_code:
                pa = a
                break
        if pa is None:
            pa = tas[0]
        pops = pa.get("pops") or []
        defs = ts.get("timeDefines") or []
        for i, pop in enumerate(pops):
            if len(rows) >= 6:
                break
            d = defs[i] if i < len(defs) else ""
            if len(d) < 13:
                continue
            hour = int(d[11:13])
            rows.append({
                "date": f"{int(d[5:7])}/{int(d[8:10])}",
                "label": f"{hour}〜{(hour + 6) % 24}時",
                "pop": str(pop),
            })
        return rows
    return rows


def hourly_rows(hourly: dict, now=None) -> list[dict]:
    """Open-Meteo hourly から時間別予報の行を作る: 今日の残り＋明日を3時間刻み。
    1時間値は全て保持し、サンプリング行にはその3時間区間（当該時刻と直前
    2時間）の降水確率の最大と降水量の合計を併記する（短時間ピークの
    取りこぼし防止）。"""
    from datetime import datetime, timedelta
    if not hourly:
        return []
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    pops = hourly.get("precipitation_probability") or []
    precs = hourly.get("precipitation") or []
    if now is None:
        now = datetime.now()
    start = now.replace(minute=0, second=0, microsecond=0)
    end_date = (now + timedelta(days=1)).date()
    rows = []
    for i, t in enumerate(times):
        try:
            dt = datetime.strptime(str(t)[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        if dt < start or dt.date() > end_date or dt.hour % 3 != 0:
            continue
        code = codes[i] if i < len(codes) else None
        pop = pops[i] if i < len(pops) else None
        temp = temps[i] if i < len(temps) else None
        win_pops = [pops[j] for j in range(max(0, i - 2), i + 1)
                    if j < len(pops) and pops[j] is not None]
        win_precs = [precs[j] for j in range(max(0, i - 2), i + 1)
                     if j < len(precs) and precs[j] is not None]
        rows.append({
            "label": f"{dt.month}/{dt.day} {dt.hour}時",
            "icon": wx_icon(code, pop, dt.hour),
            "desc": WMO_WEATHER.get(code, "不明"),
            "temp": _fmt_num(temp) if temp is not None else "?",
            "pop": str(int(pop)) if pop is not None else "—",
            "pop_max": str(int(max(win_pops))) if win_pops else "—",
            "precip": _fmt_num(round(sum(win_precs), 1)) if win_precs else "—",
        })
    return rows


def weekly_rows(daily: dict) -> list[dict]:
    """Open-Meteo daily（7日分）から週間予報の行を作る（今日を含む全日）。"""
    from datetime import datetime
    if not daily:
        return []
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    tmaxs = daily.get("temperature_2m_max") or []
    tmins = daily.get("temperature_2m_min") or []
    pops = daily.get("precipitation_probability_max") or []
    precs = daily.get("precipitation_sum") or []
    rows = []
    for i in range(min(7, len(times))):
        d = str(times[i])
        if len(d) < 10:
            continue
        try:
            wd = _WEEKDAY_JA[datetime.strptime(d[:10], "%Y-%m-%d").weekday()]
        except ValueError:
            wd = ""
        code = codes[i] if i < len(codes) else None
        pop = pops[i] if i < len(pops) else None
        lo = _fmt_num(tmins[i]) if i < len(tmins) and tmins[i] is not None else "?"
        hi = _fmt_num(tmaxs[i]) if i < len(tmaxs) and tmaxs[i] is not None else "?"
        prec = precs[i] if i < len(precs) else None
        date = f"{int(d[5:7])}/{int(d[8:10])}"
        if wd:
            date += f"（{wd}）"
        rows.append({
            "date": date,
            "icon": wx_icon(code, pop),
            "desc": WMO_WEATHER.get(code, "不明"),
            "temp": f"{lo}〜{hi}",
            "pop": str(int(pop)) if pop is not None else "—",
            # 🌧️ 日降水量（mm）。「大雨かどうか」の判断材料として併記する
            "precip": _fmt_num(round(float(prec), 1)) if isinstance(prec, (int, float)) else "—",
        })
    return rows


def parse_weather_tables(om: dict, jma, area_code: str, now=None) -> dict:
    """取得済み JSON からダッシュボード表データを組み立てる（純粋関数）。"""
    om = om or {}
    return {
        "hourly": hourly_rows(om.get("hourly") or {}, now),
        "weekly": weekly_rows(om.get("daily") or {}),
        "pops": pops_rows(jma, area_code) if jma else [],
    }


def fetch_weather_tables(loc: Location, now=None) -> dict:
    """ダッシュボード用に Open-Meteo（hourly+daily）と気象庁予報を取得し、
    表データに変換する。"""
    if now is None:
        # 🕐 Open-Meteo の時刻は JST のため、サーバーのタイムゾーンに依らず
        # JST 現在時刻で切り出す
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc.lat}&longitude={loc.lon}"
        "&hourly=temperature_2m,weather_code,precipitation_probability,precipitation"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,precipitation_sum"
        "&forecast_days=7&timezone=Asia%2FTokyo"
        f"&models={OPEN_METEO_MODELS}"
    )
    om = normalize_open_meteo(fetch_json(url))
    jma = None
    area_code = ""
    try:
        jma = fetch_json(f"https://www.jma.go.jp/bosai/forecast/data/forecast/{loc.jma_pref_code}.json")
        area_code = forecast_area_code(jma, loc.jma_area_name)
    except Exception:
        jma = None
    return parse_weather_tables(om, jma, area_code, now)
