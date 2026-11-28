# ============================================================
# 🌦️ Weather — 気象庁予報 + Open-Meteo（警報の取得は A2S）
# ============================================================
"""Weather forecasts, fetched directly (A.A.S does not cover them).

📌 Source roles are fixed: overview and precipitation probability
come from the JMA forecast; current values and hourly data come
from Open-Meteo. Warnings come from A.A.S (see a2s.fetch_warnings).

📌 Open-Meteo uses an explicit composite model
``jma_seamless,ecmwf_ifs025`` (order matters: the first model
decides the response grid). Temperature/weather/wind/precipitation
amounts are read from the JMA-model suffixed keys only, and
precipitation probability from the ECMWF keys only (JMA models do
not return it). Missing keys render as "—"; there is no silent
fallback to an unspecified best-match model.
"""
from __future__ import annotations

from .http_client import fetch_json
from .models import Event, Location, Severity
from .weather_codes import WMO_WEATHER

OPEN_METEO_MODELS = "jma_seamless,ecmwf_ifs025"
OPEN_METEO_SOURCE = "Open-Meteo（現在値・時間別の正／気温・天気: 気象庁モデル MSM/GSM、降水確率: ECMWF IFS）"
JMA_FORECAST_SOURCE_ROLE = "概況・降水確率の正"


def forecast_area_code(data, area_name: str) -> str:
    """予報 JSON から一次細分区域名に対応する区域コードを得る。"""
    if not data:
        return ""
    series = (data[0].get("timeSeries") or [])
    if not series:
        return ""
    for a in series[0].get("areas") or []:
        area = a.get("area") or {}
        if area.get("name") == area_name:
            return str(area.get("code") or "")
    return ""


def fetch_warnings(loc: Location) -> list[Event]:
    """気象警報・注意報を A2S 経由で取得する（実体は a2s.fetch_warnings。
    循環参照を避けるため遅延 import する）。"""
    from .a2s import fetch_warnings as _fetch
    return _fetch(loc)


# ------------------------------------------------------------
# 🌤️ 気象庁予報
# ------------------------------------------------------------
def fetch_jma_forecast(loc: Location) -> Event | None:
    """気象庁の天気予報（概況・降水確率・気温）を1件の Event にする。"""
    url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{loc.jma_pref_code}.json"
    data = fetch_json(url)
    if not data:
        return None
    first = data[0]
    ts = (first.get("timeSeries") or [{}])[0]
    areas = ts.get("areas") or []
    chosen = None
    for a in areas:
        if loc.jma_area_name and a.get("area", {}).get("name") == loc.jma_area_name:
            chosen = a
            break
    if chosen is None and areas:
        chosen = areas[0]
    if not chosen:
        return None
    weathers = chosen.get("weathers") or []
    winds = chosen.get("winds") or []
    today = weathers[0] if weathers else "不明"
    tomorrow = weathers[1] if len(weathers) > 1 else ""
    body = f"{loc.name}: 今日 {today}"
    if winds:
        body += f" / 風: {winds[0]}"
    if tomorrow:
        body += f" / 明日 {tomorrow}"
    chosen_code = str((chosen.get("area") or {}).get("code") or "")
    pops = pops_text(data, chosen_code)
    if pops:
        body += f" / {pops}"
    temps = temps_text(data, chosen_code)
    if temps:
        body += f" / {temps}"
    ev = Event(
        kind="weather_forecast",
        title=f"天気予報（{loc.name}）",
        body=body,
        severity=Severity.INFO,
        source=f"気象庁（{first.get('publishingOffice','')}／{JMA_FORECAST_SOURCE_ROLE}）",
        occurred_at=str(first.get("reportDatetime", "")),
        raw={"location": loc.name},
    )
    ev.ensure_id()
    return ev


def pops_text(data, area_code: str) -> str:
    """気象庁予報の降水確率（6時間ごと）を整形する。無ければ ""。"""
    if not data:
        return ""
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
        parts = ["降水確率:"]
        prev_date = ""
        shown = 0
        for i, pop in enumerate(pops[:6]):
            d = defs[i] if i < len(defs) else ""
            if len(d) < 13:
                continue
            hour = int(d[11:13])
            date = d[5:10].replace("-", "/")
            if date != prev_date:
                parts.append(date)
                prev_date = date
            parts.append(f"{hour}〜{(hour + 6) % 24}時{pop}%")
            shown += 1
        return " ".join(parts) if shown else ""
    return ""


def _temps_by_date(temps, defs):
    """temps を timeDefines と組で解釈し {日付: (最低, 最高)} を返す。
    気象庁の気温の timeDefine は 0時がその日の最低、それ以外（9時）が
    その日の最高を指す。発表回で先頭の日が変わるため日付で振り分ける。
    組として解釈できなければ None。"""
    if not defs or len(defs) != len(temps):
        return None
    by_date: dict[str, dict] = {}
    for v, d in zip(temps, defs):
        d = str(d or "")
        if len(d) < 13:
            return None
        date, hour = d[:10], d[11:13]
        kind = "min" if hour == "00" else "max"
        by_date.setdefault(date, {})[kind] = v
    return by_date or None


def temps_text(data, area_code: str = "", today=None) -> str:
    """気象庁予報の気温を整形する。無ければ ""。
    temps 系列の中から区域コード一致のエリアを選ぶ（一致しない場合のみ
    先頭エリアにフォールバック）。日付対応は timeDefines と組で解釈し、
    組が無い場合は配列順 [今日の最低, 今日の最高, 明日の最低, 明日の最高]
    にフォールバックする。today はテスト用に注入可能。"""
    if not data:
        return ""
    for ts in data[0].get("timeSeries") or []:
        tas = ts.get("areas") or []
        if not tas or "temps" not in tas[0]:
            continue
        ta = None
        if area_code:
            for a in tas:
                if str((a.get("area") or {}).get("code") or "") == area_code:
                    ta = a
                    break
        if ta is None:
            ta = tas[0]
        temps = ta.get("temps") or []
        city = (ta.get("area") or {}).get("name", "")
        by_date = _temps_by_date(temps, ts.get("timeDefines") or [])
        if by_date is not None:
            from datetime import datetime, timedelta, timezone
            if today is None:
                today = datetime.now(timezone(timedelta(hours=9))).date()
            parts = []
            for offset, day_label in ((0, "今日"), (1, "明日"), (2, "明後日")):
                key = (today + timedelta(days=offset)).isoformat()
                entry = by_date.get(key)
                if not entry:
                    continue
                lo, hi = entry.get("min"), entry.get("max")
                if lo is not None and hi is not None:
                    parts.append(f"{day_label} {lo}〜{hi}℃")
                elif hi is not None:
                    parts.append(f"{day_label} 最高{hi}℃")
                elif lo is not None:
                    parts.append(f"{day_label} 最低{lo}℃")
            if not parts:
                for key in sorted(by_date):
                    entry = by_date[key]
                    if entry.get("min") is not None and entry.get("max") is not None:
                        parts.append(f"{int(key[5:7])}/{int(key[8:10])} {entry['min']}〜{entry['max']}℃")
            return f"気温（{city}）: " + " / ".join(parts) if parts else ""
        if len(temps) < 4:
            return ""
        return f"気温（{city}）: 今日 {temps[0]}〜{temps[1]}℃ / 明日 {temps[2]}〜{temps[3]}℃"
    return ""


# ------------------------------------------------------------
# 🌤️ Open-Meteo
# ------------------------------------------------------------
def fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def num_or_dash(v) -> str:
    """数値なら整形して返し、None 等の欠測は「—」にする。"""
    if isinstance(v, (int, float)):
        return fmt_num(v)
    return "—"


def weekly_text(daily: dict) -> str:
    """Open-Meteo daily（7日分）から週間予報1行を作る（今日を含む）。"""
    from .weather_codes import WMO_EMOJI
    if not daily:
        return ""
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    tmaxs = daily.get("temperature_2m_max") or []
    tmins = daily.get("temperature_2m_min") or []
    parts = []
    for i in range(min(7, len(times))):
        d = str(times[i])
        if len(d) < 10:
            continue
        date = f"{int(d[5:7])}/{int(d[8:10])}"
        icon = WMO_EMOJI.get(codes[i] if i < len(codes) else None, "☁")
        lo = fmt_num(tmins[i]) if i < len(tmins) and tmins[i] is not None else "?"
        hi = fmt_num(tmaxs[i]) if i < len(tmaxs) and tmaxs[i] is not None else "?"
        parts.append(f"{date} {icon} {lo}〜{hi}℃")
    return "週間: " + " / ".join(parts) if parts else ""


def normalize_open_meteo(data) -> dict:
    """複合指定の応答（接尾辞付きキー）を従来形式のキーに正規化する（純粋関数）。
    current は接尾辞なし（先頭モデル＝JMA の値）。hourly/daily は接尾辞付き
    キーだけを読む。"""
    data = data or {}
    hourly_in = data.get("hourly") or {}
    daily_in = data.get("daily") or {}
    return {
        "current": data.get("current") or {},
        "hourly": {
            "time": hourly_in.get("time") or [],
            "temperature_2m": hourly_in.get("temperature_2m_jma_seamless") or [],
            "weather_code": hourly_in.get("weather_code_jma_seamless") or [],
            "precipitation_probability": hourly_in.get("precipitation_probability_ecmwf_ifs025") or [],
            # 🌧️ 降水量（mm）は気温・天気と同じく JMA 系モデルから読む
            "precipitation": hourly_in.get("precipitation_jma_seamless") or [],
        },
        "daily": {
            "time": daily_in.get("time") or [],
            "weather_code": daily_in.get("weather_code_jma_seamless") or [],
            "temperature_2m_max": daily_in.get("temperature_2m_max_jma_seamless") or [],
            "temperature_2m_min": daily_in.get("temperature_2m_min_jma_seamless") or [],
            "precipitation_probability_max": daily_in.get("precipitation_probability_max_ecmwf_ifs025") or [],
            "precipitation_sum": daily_in.get("precipitation_sum_jma_seamless") or [],
        },
    }


def fetch_open_meteo(loc: Location) -> Event | None:
    """Open-Meteo の現在値＋今日の予報を1件の Event にする。"""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc.lat}&longitude={loc.lon}"
        "&current=temperature_2m,weather_code,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,precipitation_sum"
        "&forecast_days=7&timezone=Asia%2FTokyo"
        f"&models={OPEN_METEO_MODELS}"
    )
    data = normalize_open_meteo(fetch_json(url))
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    code = cur.get("weather_code")
    desc = WMO_WEATHER.get(code, "不明")
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    pop = (daily.get("precipitation_probability_max") or [None])[0]
    precip = (daily.get("precipitation_sum") or [None])[0]
    sev = Severity.INFO
    guide = ""
    if pop is not None and pop >= 80:
        sev = Severity.MEDIUM
        guide = "降水確率が高め。折りたたみ傘ではなく大きめの傘が安心"
    if (cur.get("wind_speed_10m") or 0) >= 54:  # 15m/s = 54km/h
        sev = Severity.MEDIUM
        guide = "風が強い。飛来物と電車の遅延に注意"
    body = (
        f"{loc.name}: 現在 {desc} {num_or_dash(cur.get('temperature_2m'))}℃ / 風 {num_or_dash(cur.get('wind_speed_10m'))}km/h"
        f" / 今日 {num_or_dash(tmin)}〜{num_or_dash(tmax)}℃ / 降水確率 最大{num_or_dash(pop)}%"
    )
    # 🌧️ 降水量（mm）を併記し「大雨かどうか」の判断材料にする
    if isinstance(precip, (int, float)):
        body += f" / 降水量 今日 約{fmt_num(round(float(precip), 1))}mm"
    weekly = weekly_text(daily)
    if weekly:
        body += f" / {weekly}"
    ev = Event(
        kind="weather_forecast",
        title=f"現在の気象（{loc.name}）",
        body=body,
        severity=sev,
        source=OPEN_METEO_SOURCE,
        occurred_at=str(cur.get("time", "")),
        action_guide=guide,
        raw={"location": loc.name, "pop": pop},
    )
    ev.ensure_id()
    return ev
