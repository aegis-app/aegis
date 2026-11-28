# ============================================================
# ⌨️ CLI + Web dashboard
# ============================================================
"""Command-line interface and the ``serve`` web dashboard.

Commands: now / watch / ask / history / serve / test /
test-notify. The dashboard is a single auto-refreshing page
(briefing text + weather tables + two-source radar + news) with
JSON APIs at ``/api/briefing`` and ``/api/history``; responses
are cached briefly so browser polling cannot amplify load on
the upstream APIs.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import a2s, radar, weather_tables
from . import assistant as assistant_mod
from . import diagnose as diagnose_mod
from .config import ROOT, load_config, save_default_config
from .engine import Engine
from .models import Event, Location, Severity
from .notify import build_notifiers, dispatch
from .store import Store


def _esc(s: str) -> str:
    # 📝 属性値（href='...' / src='...'）の中でも安全なよう引用符もエスケープする
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#x27;")
    )


def _retention_days(cfg: dict) -> int:
    try:
        return int(cfg.get("retention_days", 90))
    except Exception:
        return 90


def _build_engine(cfg: dict) -> Engine:
    save_default_config()
    locations = [Location.from_dict(d) for d in cfg.get("locations", [])]
    db = ROOT / cfg.get("db_path", "data/aegis.db")
    store = Store(db, retention_days=_retention_days(cfg))
    notifiers = build_notifiers(cfg, ROOT)
    return Engine(cfg, store, notifiers, locations)


# ------------------------------------------------------------
# ⌨️ コマンド実装
# ------------------------------------------------------------
def cmd_now(cfg: dict):
    eng = _build_engine(cfg)
    print(eng.format_briefing("現在の防災ブリーフィング"))


def cmd_watch(cfg: dict):
    eng = _build_engine(cfg)
    eng.watch()


def cmd_ask(cfg: dict, question: str):
    locations = [Location.from_dict(d) for d in cfg.get("locations", [])]
    # 📡 地震・津波の取得（A2S）は Engine を経由しないため、ここでも
    # 接続先を伝える（Engine.__init__ と同じ作法）
    a2s.set_config(*a2s.resolve_settings(cfg))
    print(assistant_mod.answer(question, locations))


def cmd_history(cfg: dict, limit: int = 20):
    db = ROOT / cfg.get("db_path", "data/aegis.db")
    store = Store(db, retention_days=_retention_days(cfg))
    rows = store.recent_history(limit)
    if not rows:
        print("履歴はまだありません。")
        return
    for r in rows:
        print(f"[{r['created_at']}] {r['severity']} {r['title']} — {r['body']}")


def cmd_test(cfg: dict) -> int:
    """自己診断: 各データソースへの実リクエストと設定チェック。
    全て正常なら 0、一部失敗なら 1。"""
    locations = [Location.from_dict(d) for d in cfg.get("locations", [])]
    source_results = diagnose_mod.run_source_checks(locations, cfg)
    config_results = diagnose_mod.run_config_checks(cfg, locations, ROOT)
    print(diagnose_mod.format_diagnosis(source_results, config_results))
    return diagnose_mod.diagnosis_exit_code(source_results, config_results)


def cmd_test_notify(cfg: dict):
    """テスト通知: 設定済みの通知先へ【テスト】明記のサンプルを1件だけ送る。
    監視ループの重複排除ストアには一切触れない。"""
    from datetime import datetime
    notifiers = build_notifiers(cfg, ROOT)
    ev = Event(
        kind="earthquake",
        title="【テスト】地震 最大震度5弱 テスト震源地",
        body="これは AEGIS のテスト通知です。通知経路の確認用に送信しています。実際の地震ではありません。",
        severity=Severity.HIGH,
        source="AEGIS テスト通知",
        event_id="aegis-test-notify",
        occurred_at=datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        action_guide="テストのため対応は不要です",
    )
    dispatch(notifiers, ev)
    names = [type(n).__name__ for n in notifiers]
    print(f"🛡️ テスト通知を送信しました（通知先: {', '.join(names)}）")


# ------------------------------------------------------------
# 🌧️ ダッシュボード部品（レーダー・天気表）
# ------------------------------------------------------------
def radar_marker_pixel(loc: Location, z: int = 8) -> tuple[float, float]:
    """レーダーグリッド（中心タイルを中心とする 3×3、768×768px）内での
    地点のピクセル位置。小数タイル座標から算出する（タイル中心固定だと
    地点がタイル端寄りの場合に最大で半タイル分ずれるため）。"""
    cx, cy = radar.tile_xy(loc.lat, loc.lon, z)
    ox, oy = cx - 1, cy - 1  # グリッド原点（左上のタイル）
    fx, fy = radar.tile_xy_float(loc.lat, loc.lon, z)
    return (fx - ox) * 256, (fy - oy) * 256


def _jma_radar_section(loc: Location, frame: dict, frame_time: str) -> str:
    """気象庁 降水ナウキャストのレーダー（第二ソース）。
    地理院タイル＋ナウキャストタイルをズーム10の 3×3 グリッドで重ねる
    （ナウキャストは z10 まで提供されるため拡大なしで表示できる）。"""
    z = radar.JMA_NOWCAST_MAX_ZOOM
    n = 2 ** z
    cx, cy = radar.tile_xy(loc.lat, loc.lon, z)
    mx, my = radar_marker_pixel(loc, z)
    cells = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            x = (cx + dx) % n
            y = max(0, min(n - 1, cy + dy))
            base = f"https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"
            over = radar.jma_nowcast_tile_url(frame, z, x, y)
            cells.append(
                f"<div class='tile'><img src='{base}' alt=''>"
                f"<img class='ov' src='{over}' alt='' style='left:0;top:0;width:256px;height:256px'></div>"
            )
    return (
        f"<h2>🌧 気象レーダー（気象庁ナウキャスト・{_esc(loc.name)}）</h2>"
        f"<div class='radar'>{''.join(cells)}"
        f"<div class='marker' style='left:{mx:.1f}px;top:{my:.1f}px'></div></div>"
        f"<p class='src'>レーダー: {frame_time} 時点 / 気象庁 降水ナウキャスト（ズーム{z}）・地図: 国土地理院</p>"
    )


def _radar_section(loc: Location, frame: dict, frame_time: str) -> str:
    """地点中心のレーダー画像（地理院タイル z8 の3×3グリッド＋RainViewer）。
    RainViewer のタイルは z7 までしか提供されず、z8 ではバナー画像が
    返ってくるため、雨タイルは z7 で取得し絶対配置で重ねる。
    マーカーは地点の実位置（小数タイル座標）に置く。"""
    z = 8
    n = 2 ** z
    cx, cy = radar.tile_xy(loc.lat, loc.lon, z)
    ox, oy = cx - 1, cy - 1
    mx, my = radar_marker_pixel(loc, z)
    cells = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            x = (cx + dx) % n
            y = max(0, min(n - 1, cy + dy))
            base = f"https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"
            cells.append(f"<div class='tile'><img src='{base}' alt=''></div>")
    # 🖥️ 雨タイル: z7 の1枚は z8 の 2×2 枚分（512px 表示）
    overs = []
    for ty in range(oy // 2, (oy + 2) // 2 + 1):
        for tx in range(ox // 2, (ox + 2) // 2 + 1):
            over = f"https://tilecache.rainviewer.com{frame['path']}/256/7/{tx}/{ty}/2/1_1.png"
            left = (2 * tx - ox) * 256
            top = (2 * ty - oy) * 256
            overs.append(
                f"<img class='ov' src='{over}' alt='' "
                f"style='left:{left}px;top:{top}px;width:512px;height:512px'>"
            )
    return (
        f"<h2>🌧 気象レーダー（{_esc(loc.name)}）</h2>"
        f"<div class='radar'>{''.join(cells)}{''.join(overs)}"
        f"<div class='marker' style='left:{mx:.1f}px;top:{my:.1f}px'></div></div>"
        f"<p class='src'>レーダー: {frame_time} 時点 / RainViewer（気象庁データ）・地図: 国土地理院</p>"
        f"<p class='src'>{_esc(radar.RADAR_RESOLUTION_NOTE)}</p>"
    )


def _pop_cell(v: str) -> str:
    return f"{_esc(v)}%" if v not in ("", "—") else "—"


def _weather_section(loc: Location, tables: dict) -> str:
    """地点の天気を表形式で表示する（時間別・週間・気象庁降水確率）。"""
    parts = [f"<h2>☀ 天気（{_esc(loc.name)}）</h2>"]
    hourly = tables.get("hourly") or []
    if hourly:
        trs = "".join(
            f"<tr><td>{_esc(r['label'])}</td><td class='w'>{_esc(r['icon'])} {_esc(r['desc'])}</td>"
            f"<td>{_esc(r['temp'])}℃</td><td>{_pop_cell(r['pop'])}</td>"
            f"<td>{_pop_cell(r.get('pop_max', '—'))}</td><td>{_esc(r.get('precip', '—'))}mm</td></tr>"
            for r in hourly
        )
        parts.append(
            "<h3>時間別予報</h3><table class='wx'><thead><tr><th>時刻</th><th>天気</th>"
            "<th>気温</th><th>降水確率</th><th>降水確率（3時間区間最大）</th><th>降水量（3時間）</th>"
            f"</tr></thead><tbody>{trs}</tbody></table>"
        )
    weekly = tables.get("weekly") or []
    if weekly:
        trs = "".join(
            f"<tr><td>{_esc(r['date'])}</td><td class='w'>{_esc(r['icon'])} {_esc(r['desc'])}</td>"
            f"<td>{_esc(r['temp'])}℃</td><td>{_pop_cell(r['pop'])}</td><td>{_esc(r.get('precip', '—'))}mm</td></tr>"
            for r in weekly
        )
        parts.append(
            "<h3>週間予報</h3><table class='wx'><thead><tr><th>日付</th><th>天気</th>"
            f"<th>最低〜最高</th><th>降水確率</th><th>降水量</th></tr></thead><tbody>{trs}</tbody></table>"
        )
    pops = tables.get("pops") or []
    if pops:
        trs = "".join(
            f"<tr><td>{_esc(r['date'])}</td><td>{_esc(r['label'])}</td><td>{_pop_cell(r['pop'])}</td></tr>"
            for r in pops
        )
        parts.append(
            "<h3>降水確率（気象庁）</h3><table class='wx'><thead><tr><th>日付</th><th>時間帯</th>"
            f"<th>降水確率</th></tr></thead><tbody>{trs}</tbody></table>"
        )
    return "".join(parts)


def _dashboard_extras(eng: Engine) -> str:
    """ダッシュボード追加表示: 地点ごとの天気表＋レーダー＋ニュース一覧。
    取得に失敗したソースは無言で欄を消さず、失敗を明示する。"""
    from datetime import datetime, timezone, timedelta
    from . import news as news_mod
    parts: list[str] = []
    for loc in eng.locations:
        try:
            tables = weather_tables.fetch_weather_tables(loc)
        except Exception:
            tables = None
        if tables and (tables.get("hourly") or tables.get("weekly") or tables.get("pops")):
            parts.append(_weather_section(loc, tables))
    # 📡 レーダーは RainViewer と気象庁ナウキャストの2ソース。
    # フレーム取得は鮮度連動キャッシュ経由
    frame = None
    try:
        frame = radar.fetch_radar_frame_cached()
    except Exception:
        frame = None
    if frame:
        jst = timezone(timedelta(hours=9))
        ftime = datetime.fromtimestamp(frame["time"], jst).strftime("%m月%d日 %H:%M") if frame["time"] else ""
        # 🕐 フレームの鮮度を相対時刻でも示す（古い場合は注記つき）
        age = radar.radar_age_text(frame.get("time"))
        if ftime and age:
            ftime = f"{ftime}（{age}）"
        elif age:
            ftime = age
        for loc in eng.locations:
            parts.append(_radar_section(loc, frame, ftime))
    else:
        parts.append("<h2>🌧 気象レーダー</h2><p class='src'>⚠ RainViewerレーダーを取得できませんでした（通信状態を確認してください）。気象庁ナウキャストで代替表示します</p>")
    jma_frame = None
    try:
        jma_frame = radar.fetch_jma_nowcast_frame_cached()
    except Exception:
        jma_frame = None
    if jma_frame:
        jtime = radar.jma_nowcast_time_text(jma_frame)
        for loc in eng.locations:
            parts.append(_jma_radar_section(loc, jma_frame, jtime))
    else:
        parts.append("<h2>🌧 気象レーダー（気象庁ナウキャスト）</h2><p class='src'>⚠ 気象庁ナウキャストを取得できませんでした（通信状態を確認してください）</p>")
    items: list[dict] = []
    try:
        items = news_mod.fetch_news(limit=8)
    except Exception:
        items = []
    if items:
        lis = "".join(
            f"<li><a href='{_esc(n['link'])}' target='_blank' rel='noopener'>{_esc(n['title'])}</a>"
            + (f"（{_esc(n['time'])}）" if n["time"] else "")
            + f"<br>{_esc(n['summary'])}</li>"
            for n in items
        )
        parts.append(f"<h2>📰 防災ニュース</h2><ul class='news'>{lis}</ul><p class='src'>出典: NHKニュース</p>")
    return "".join(parts)


# ------------------------------------------------------------
# 🖥️ ダッシュボードサーバー
# ------------------------------------------------------------
def cmd_serve(cfg: dict, port: int = 8765):
    eng = _build_engine(cfg)
    # 💾 /api/briefing は60秒、追加表示（extras）は120秒キャッシュする
    # （クライアントのポーリングで外部APIへ負荷を増幅させない）
    cache: dict = {"at": 0.0, "body": b""}
    extras_cache: dict = {"at": 0.0, "html": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # 標準のアクセスログ出力は抑制する

        def do_GET(self):
            try:
                self._handle()
            except Exception:
                try:
                    body = json.dumps({"error": "internal error"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass

        def _handle(self):
            if self.path.startswith("/api/briefing"):
                import time as _time
                now = _time.time()
                if cache["body"] and now - cache["at"] < 60:
                    body = cache["body"]
                else:
                    events = eng.briefing_events()
                    payload = {"events": [e.to_dict() for e in events]}
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                    cache["at"] = now
                    cache["body"] = body
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/history"):
                rows = eng.store.recent_history(30)
                body = json.dumps({"history": rows}, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            text = eng.format_briefing("AEGIS ダッシュボード")
            import time as _time2
            if _time2.time() - extras_cache["at"] >= 120 or not extras_cache["html"]:
                extras_cache["html"] = _dashboard_extras(eng)
                extras_cache["at"] = _time2.time()
            html = (
                "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='60'>"
                "<title>AEGIS 防災アシスタント</title>"
                "<style>body{font-family:system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px}"
                "pre{white-space:pre-wrap;background:#0b1220;color:#e6edf3;padding:16px;border-radius:12px}"
                ".radar{position:relative;display:grid;grid-template-columns:repeat(3,256px);width:768px;overflow:hidden}"
                ".tile{position:relative;width:256px;height:256px}"
                ".tile img{width:256px;height:256px;display:block}"
                ".radar img.ov{position:absolute;z-index:1}"
                ".marker{position:absolute;left:50%;top:50%;width:12px;height:12px;margin:-6px 0 0 -6px;"
                "border:2px solid #d00;border-radius:50%;background:rgba(255,0,0,.25);z-index:2}"
                ".news li{margin:6px 0}.src{color:#666;font-size:12px}"
                "table.wx{border-collapse:collapse;margin:4px 0 16px}"
                "table.wx th,table.wx td{border:1px solid #b9c6d4;padding:4px 10px;text-align:center;font-size:14px}"
                "table.wx th{background:#e8eef5}table.wx td.w{text-align:left}</style></head>"
                f"<body><h1>🛡️ AEGIS — 地震・津波・気象 自律アシスタント</h1><pre>{_esc(text)}</pre>"
                f"{extras_cache['html']}"
                "<p>JSON: <a href='/api/briefing'>/api/briefing</a> / <a href='/api/history'>/api/history</a>（60秒ごとに自動更新）</p></body></html>"
            )
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    print(f"🛡️ AEGIS dashboard: http://127.0.0.1:{port}/  (Ctrl+Cで停止)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main(argv=None):
    # ⚠️ 文字化け・絵文字での例外防止: 標準出力を UTF-8 に寄せる
    # （Windows の cp932 コンソール対策）
    import sys as _sys
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="aegis", description="AEGIS — 地震・津波・気象 自律アシスタント")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("now", help="今すぐブリーフィングを表示")
    sub.add_parser("watch", help="自律監視を開始（常駐）")
    a = sub.add_parser("ask", help="自然文で質問")
    a.add_argument("question", nargs="+")
    h = sub.add_parser("history", help="通知履歴を表示")
    h.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("serve", help="Webダッシュボードを起動")
    s.add_argument("--port", type=int, default=8765)
    sub.add_parser("test", help="自己診断（各データソースへの接続と設定をチェック）")
    sub.add_parser("test-notify", help="テスト通知を1件送信（通知経路の確認用）")
    args = p.parse_args(argv)
    cfg = load_config()
    if args.cmd == "test":
        raise SystemExit(cmd_test(cfg))
    elif args.cmd == "test-notify":
        cmd_test_notify(cfg)
    elif args.cmd == "watch":
        cmd_watch(cfg)
    elif args.cmd == "ask":
        cmd_ask(cfg, " ".join(args.question))
    elif args.cmd == "history":
        cmd_history(cfg, args.limit)
    elif args.cmd == "serve":
        cmd_serve(cfg, args.port)
    else:
        cmd_now(cfg)


if __name__ == "__main__":
    main()
