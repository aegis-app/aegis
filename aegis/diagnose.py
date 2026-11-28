# ============================================================
# 🩺 Diagnose — 自己診断（aegis test）
# ============================================================
"""Self-diagnosis for ``aegis test``.

Probes every data source with one short-timeout request each and
checks the configuration (locations, coordinate sanity, notifier
setup, data-directory writability). The exit code is 0 only when
everything passes, so the command is usable from scripts.

⚠️ The A2S probe never prints the token; an unconfigured A2S is
reported as a failure with the reason spelled out, not silently
treated as healthy.
"""
from __future__ import annotations

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import a2s, news, radar, weather
from .http_client import UA
from .models import Location, location_warnings

PROBE_TIMEOUT = 10


def _probe(url: str, kind: str = "json", headers: dict | None = None) -> dict:
    """1回だけ実リクエストし {ok, ms, detail} を返す。kind: json | xml | any。
    headers は追加ヘッダー（A2S の Authorization 等）。値は出力しない。"""
    started = time.time()
    try:
        req_headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip as _gzip
                raw = _gzip.decompress(raw)
        ms = int((time.time() - started) * 1000)
        if kind == "json":
            json.loads(raw.decode("utf-8"))
        elif kind == "xml":
            ET.fromstring(raw)
        return {"ok": True, "ms": ms, "detail": ""}
    except Exception as e:
        ms = int((time.time() - started) * 1000)
        return {"ok": False, "ms": ms, "detail": f"{type(e).__name__}: {e}"}


def run_source_checks(locations: list[Location], cfg: dict | None = None) -> list[dict]:
    """各データソースへ実際にリクエストし、結果リストを返す。"""
    checks: list[dict] = []

    def add(name: str, url: str, kind: str = "json", headers: dict | None = None):
        r = _probe(url, kind, headers=headers)
        checks.append({"name": name, "ok": r["ok"], "ms": r["ms"], "detail": r["detail"]})

    # 📡 地震・津波・EEW・警報・台風は A2S のみ。稼働状態は GET /v1/status で
    # まとめて確認する。未設定なら取得されない旨を失敗として明示する
    a2s_url, a2s_token = a2s.resolve_settings(cfg)
    if a2s_url and a2s_token:
        add("A.A.S（地震・津波・EEW・警報・台風）", f"{a2s_url.rstrip('/')}/v1/status",
            headers={"Authorization": f"Bearer {a2s_token}"})
    else:
        checks.append({"name": "A.A.S（地震・津波・EEW・警報・台風）", "ok": False, "ms": None,
                       "detail": "未設定（a2s_url / a2s_token）: 地震・津波・EEW・警報・台風は取得されません"})
    if locations:
        loc = locations[0]
        add(f"気象庁 予報（{loc.name}）",
            f"https://www.jma.go.jp/bosai/forecast/data/forecast/{loc.jma_pref_code}.json")
        add(f"Open-Meteo（{loc.name}）",
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc.lat}&longitude={loc.lon}&current=temperature_2m&forecast_days=1"
            f"&models={weather.OPEN_METEO_MODELS}")
    else:
        for name in ("気象庁 予報", "Open-Meteo"):
            checks.append({"name": name, "ok": False, "ms": None, "detail": "地点が未登録のため確認できません"})
    add("RainViewer レーダー", radar.RAINVIEWER_MAPS_URL)
    add("気象庁 降水ナウキャスト", radar.JMA_NOWCAST_TARGET_URL)
    add("NHK RSS ニュース", news.NHK_RSS_URLS[0], kind="xml")
    return checks


def run_config_checks(cfg: dict, locations: list[Location], root: Path) -> list[dict]:
    """設定の妥当性チェック（地点・座標・通知先・保存先）。"""
    checks: list[dict] = []
    n = len(locations)
    checks.append({
        "name": "監視地点",
        "ok": n > 0,
        "ms": None,
        "detail": f"{n}件登録" if n > 0 else "地点が1件も登録されていません",
    })
    # 📍 地点座標の検証: 日本域外・都道府県コードとの不整合を警告として出す
    coord_warns: list[str] = []
    for loc in locations:
        coord_warns.extend(location_warnings(loc))
    checks.append({
        "name": "地点座標の検証",
        "ok": True,  # 警告は detail に出す（致命的ではないため診断自体は継続）
        "ms": None,
        "detail": "／".join(coord_warns) if coord_warns else "範囲・区域整合に問題なし",
    })
    hooks = []
    if cfg.get("discord_webhook_url"):
        hooks.append("Discord Webhook")
    if cfg.get("generic_webhook_url"):
        hooks.append("汎用 Webhook")
    if cfg.get("desktop_notify"):
        hooks.append("デスクトップ通知")
    checks.append({
        "name": "通知先",
        "ok": True,  # コンソール＋ファイルは常時有効
        "ms": None,
        "detail": ("コンソール＋ファイル" + ("＋" + "・".join(hooks) if hooks else "（追加の通知先は未設定）")),
    })
    db = root / cfg.get("db_path", "data/aegis.db")
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        probe = db.parent / ".aegis_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append({"name": "データ保存先", "ok": True, "ms": None, "detail": f"書込OK: {db.parent}"})
    except Exception as e:
        checks.append({"name": "データ保存先", "ok": False, "ms": None,
                       "detail": f"書込不可: {db.parent}（{type(e).__name__}: {e}）"})
    return checks


def format_diagnosis(source_results: list[dict], config_results: list[dict]) -> str:
    """診断結果を人が読む形に整形する（純粋関数）。"""
    lines = ["🛡️ AEGIS 自己診断", "=" * 60, "■ データソース"]
    for r in source_results:
        if r["ok"]:
            ms = f"（{r['ms']}ms）" if r.get("ms") is not None else ""
            lines.append(f"○ {r['name']}: 接続OK{ms}")
        else:
            lines.append(f"× {r['name']}: 失敗（{r.get('detail') or '理由不明'}）")
    lines.append("■ 設定")
    for r in config_results:
        mark = "○" if r["ok"] else "×"
        detail = f"（{r['detail']}）" if r.get("detail") else ""
        lines.append(f"{mark} {r['name']}{detail}")
    total = len(source_results) + len(config_results)
    ok_count = sum(1 for r in source_results + config_results if r["ok"])
    lines.append("-" * 60)
    if ok_count == total:
        lines.append(f"結果: 全て正常です（{ok_count}/{total}）")
    else:
        lines.append(f"結果: {total - ok_count}件に問題があります（正常 {ok_count}/{total}）")
    return "\n".join(lines)


def diagnosis_exit_code(source_results: list[dict], config_results: list[dict]) -> int:
    """全て成功なら 0、一部でも失敗があれば 1。"""
    return 0 if all(r["ok"] for r in source_results + config_results) else 1
