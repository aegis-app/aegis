# ============================================================
# 🔁 Engine — 重要度判定・重複排除・自律監視ループ
# ============================================================
"""The resident watch engine.

📌 Intake: when A2S is configured, an SSE subscription pushes
events (replay frames are merged record-only, never notified)
while REST polling keeps running per kind — EEW every
``poll_seconds_eew``, quakes/tsunamis every
``poll_seconds_quake``, warnings every ``poll_seconds_weather``.

📌 Notification rules (process):
- dedupe via the store; EEW follow-ups re-notify only when the
  estimated scale exceeds the previously notified one
  (cancellations are separate events and always pass);
- everything is recorded first, then filtered: stale events
  (>1 h for quake/tsunami/EEW), the quake min-scale, and the
  severity threshold only gate the notification, not the record;
- one source failing never stops the others (parallel checks
  absorb exceptions), and the loop backs off exponentially
  (max 5 min) on failure.

🕐 Scheduled briefings print at ``morning_briefing`` /
``evening_briefing``.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import a2s, news, typhoon, weather
from .convert_eew import eew_arrival_estimates
from .locations import set_locations
from .models import Event, Location, Severity
from .notify import dispatch
from .store import Store


def severity_from_name(name: str) -> Severity:
    return {"INFO": Severity.INFO, "LOW": Severity.LOW, "MEDIUM": Severity.MEDIUM,
            "HIGH": Severity.HIGH, "CRITICAL": Severity.CRITICAL}.get(str(name).upper(), Severity.MEDIUM)


def poll_due(last: float, now: float, interval: float) -> bool:
    """前回実行時刻 last から interval 秒以上経っていれば True（純粋関数）。
    last=0（未実行）の場合は常に True。"""
    if interval <= 0:
        return True
    return (now - last) >= interval


def poll_tick(eew_interval: int, quake_interval: int) -> int:
    """watch ループの基本スリープ秒数（純粋関数）。どちらの間隔も守れるよう
    小さい方を返す（最低1秒）。"""
    vals = [v for v in (eew_interval, quake_interval) if v and v > 0]
    return max(1, min(vals)) if vals else 1


def is_stale(ev: Event, now: datetime | None = None) -> bool:
    """地震・津波・EEWについて、発生時刻が1時間を超えていれば True
    （パースできない場合は False）。now はテスト用に注入可能。"""
    if ev.kind not in ("earthquake", "tsunami", "eew"):
        return False
    from .geo import parse_jst_time
    t = parse_jst_time(ev.occurred_at or "")
    if t is None:
        return False
    return ((now or datetime.now()) - t).total_seconds() > 3600


class Engine:
    """Owns the config, store, notifiers and locations; runs checks
    and the watch loop."""

    def __init__(self, cfg: dict, store: Store, notifiers: list, locations: list[Location]):
        self.cfg = cfg
        self.store = store
        self.notifiers = notifiers
        self.locations = locations
        # 🚨 登録地点を照合ヘルパーへ伝える（地震の市区町村照合・EEW の地点別予想）
        if locations:
            set_locations(locations)
        # 📡 A2S: 地震・津波・EEW・気象警報・台風の唯一の取得経路。
        # URL とトークンの両方が指定されたときだけ有効（既定は無効）。
        # 未設定・到達不能でも監視ループや他機能は動き続ける
        self.a2s_url, self.a2s_token = a2s.resolve_settings(cfg)
        self.a2s_enabled = bool(self.a2s_url and self.a2s_token)
        a2s.set_config(self.a2s_url, self.a2s_token)
        self._a2s_client = None
        # 📡 process は watch ループと SSE 受信スレッドの両方から呼ばれるため直列化する
        self._process_lock = threading.Lock()
        self.min_sev = severity_from_name(cfg.get("notify_min_severity", "MEDIUM"))
        self.quake_min_scale = int(cfg.get("quake_min_scale", 30))
        self._failures = 0
        self._last_briefing_key = ""

    # ------------------------------------------------------------
    # 📝 1回分のチェック
    # ------------------------------------------------------------
    @staticmethod
    def _parallel(tasks) -> list[Event]:
        """(表示名, 引数なし関数) のリストを並列実行し、順序どおりに結果を
        結合する。1つの取得の失敗はそのタスクだけ空にして吸収する。"""
        def run(task):
            name, fn = task
            try:
                r = fn()
                if r is None:
                    return []
                return r if isinstance(r, list) else [r]
            except Exception as e:
                print(f"[engine] {name} failed: {e}", flush=True)
                return []
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(run, tasks))
        out: list[Event] = []
        for r in results:
            out.extend(r)
        return out

    def check_quakes(self) -> list[Event]:
        return self._parallel([
            ("fetch_earthquakes", lambda: a2s.fetch_quakes()),
            ("fetch_tsunami", lambda: a2s.fetch_tsunamis()),
            ("fetch_eew", lambda: a2s.fetch_eews()),
        ])

    def check_quakes_only(self) -> list[Event]:
        """地震・津波のみ（EEW は check_eew で高頻度に別チェックする）。"""
        return self._parallel([
            ("fetch_earthquakes", lambda: a2s.fetch_quakes()),
            ("fetch_tsunami", lambda: a2s.fetch_tsunamis()),
        ])

    def check_eew(self) -> list[Event]:
        """EEW のみ（poll_seconds_eew 間隔で高頻度チェックする）。"""
        return self._parallel([
            ("fetch_eew", lambda: a2s.fetch_eews()),
        ])

    def check_weather(self) -> list[Event]:
        return self._parallel([
            (f"fetch_warnings({loc.name})", (lambda loc=loc: weather.fetch_warnings(loc)))
            for loc in self.locations
        ])

    def briefing_events(self) -> list[Event]:
        """ブリーフィング用: 天気予報＋現在の気象＋警報＋最新の地震・津波＋
        EEW＋台風を集める（通知閾値とは無関係に返す）。
        2ソースの役割は固定: 概況・降水確率は気象庁、現在値・時間別は
        Open-Meteo、警報は A2S を正とする。"""
        tasks = []
        for loc in self.locations:
            tasks.append((f"fetch_jma_forecast({loc.name})", (lambda loc=loc: weather.fetch_jma_forecast(loc))))
            tasks.append((f"fetch_open_meteo({loc.name})", (lambda loc=loc: weather.fetch_open_meteo(loc))))
            tasks.append((f"fetch_warnings({loc.name})", (lambda loc=loc: weather.fetch_warnings(loc))))
        tasks.append(("fetch_earthquakes", lambda: a2s.fetch_quakes(limit=3)[:3]))
        tasks.append(("fetch_tsunami", lambda: a2s.fetch_tsunamis(limit=1)))
        # 🚨 EEW は実データのある最新報が1時間以内のときだけ含める
        tasks.append(("fetch_eew", lambda: [ev for ev in a2s.fetch_eews(limit=3) if not is_stale(ev)][:1]))
        tasks.append(("fetch_typhoon", typhoon.fetch_typhoon))
        events = self._parallel(tasks)
        for ev in events:
            if ev.kind == "eew":
                self._attach_eew_arrival(ev)
        return events

    def _attach_eew_arrival(self, ev: Event):
        """EEW イベントの本文に、登録地点ごとの主要動到達予想を付ける。"""
        if not isinstance(ev.raw, dict) or not ev.raw.get("earthquake"):
            return
        if "主要動到達予想" in ev.body:
            return
        try:
            lines = eew_arrival_estimates(ev.raw, self.locations)
        except Exception:
            lines = []
        if lines:
            ev.body += " / " + " / ".join(lines)

    @staticmethod
    def summarize(events: list[Event]) -> list[str]:
        """一括で把握するための冒頭まとめ（警報・津波・最新地震・天気）。"""
        lines = ["📌 まとめ"]
        warns = [ev for ev in events if ev.kind == "weather_warning"]
        if warns:
            lines.append("⚠ 警報・注意報: " + " / ".join(ev.title for ev in warns[:4]))
        else:
            lines.append("⚠ 警報・注意報: 発表なし")
        tsunamis = [ev for ev in events if ev.kind == "tsunami"]
        lines.append("🌊 津波: " + (tsunamis[0].title if tsunamis else "警報・注意報なし"))
        quakes = [ev for ev in events if ev.kind == "earthquake"]
        if quakes:
            lines.append(f"🌏 最新の地震: {quakes[0].title}")
        forecasts = [ev for ev in events if ev.kind == "weather_forecast"]
        for ev in forecasts[:4]:
            lines.append(f"☀ {ev.title}: {ev.body.splitlines()[0] if ev.body else ''}")
        return lines

    def format_briefing(self, title: str = "AEGIS ブリーフィング") -> str:
        events = self.briefing_events()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"🛡️ {title} — {now}", "=" * 60]
        if not events:
            lines.append("現在、取得できた情報がありません（通信状態を確認してください）。")
        else:
            lines.extend(self.summarize(events))
            lines.append("-" * 60)
        for ev in events:
            lines.append(ev.format_message())
            lines.append("-" * 60)
        # 📰 防災ニュース（取得失敗時はセクションごと落とす。監視通知には混ぜない）
        try:
            items = news.fetch_news(limit=8)
        except Exception:
            items = []
        if items:
            lines.append("📰 防災ニュース")
            for n in items[:3]:
                lines.append(f"・{n['title']}（{n['time']}）" if n["time"] else f"・{n['title']}")
                if n["summary"]:
                    lines.append(f"  {n['summary']}")
            lines.append("-" * 60)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # 🔔 通知判定
    # ------------------------------------------------------------
    def process(self, events: list[Event], *, force: bool = False,
                record_only: bool = False) -> list[Event]:
        # 📡 SSE 受信スレッドからも呼ばれるため、重複排除（確認→記録→通知）を直列化する
        with self._process_lock:
            return self._process(events, force=force, record_only=record_only)

    def _process(self, events: list[Event], *, force: bool = False,
                 record_only: bool = False) -> list[Event]:
        notified: list[Event] = []
        for ev in events:
            ev.ensure_id()
            # 🚨 EEW（取消を除く）の重複排除は「event_id＋推定最大震度」で判定する。
            # 通知済みの推定震度以下の続報は再通知せず、上がった続報のみ再通知する。
            # 取消報は従来どおり event_id で判定する
            eew_scale: int | None = None
            if ev.kind == "eew" and not ev.event_id.endswith("-cancel"):
                raw_scale = (ev.raw or {}).get("earthquake", {}).get("maxScale") if isinstance(ev.raw, dict) else None
                try:
                    eew_scale = int(raw_scale) if raw_scale is not None else -1
                except Exception:
                    eew_scale = -1
                prev_scale = self.store.seen_scale(ev.event_id)
                if prev_scale is not None and eew_scale <= prev_scale:
                    self.store.mark_seen(ev.event_id, ev.kind, ev.title, ev.severity.label, scale=eew_scale)
                    continue
            elif self.store.is_seen(ev.event_id):
                continue
            if ev.kind == "eew":
                self._attach_eew_arrival(ev)
            # 🔔 通知判定より先に記録する: 閾値未満・古いイベントも「処理済み」に
            # して再通知しない
            self.store.mark_seen(ev.event_id, ev.kind, ev.title, ev.severity.label, scale=eew_scale)
            self.store.add_history(ev.event_id, ev.kind, ev.title, ev.body, ev.severity.label)
            # 📡 record_only（A2S の再生分＝初期同期）は状態へのマージのみ
            if record_only:
                continue
            # 🚨 監視開始直後の通知フラッド防止: 発生から1時間を超えた
            # 地震・津波・EEWは記録のみ
            if not force and is_stale(ev):
                continue
            # 🌏 設定 quake_min_scale 未満の地震は通知しない（記録はする）
            if not force and ev.kind == "earthquake":
                ms = (ev.raw or {}).get("earthquake", {}).get("maxScale") if isinstance(ev.raw, dict) else None
                if isinstance(ms, (int, float)) and ms >= 0 and ms < self.quake_min_scale:
                    continue
            if not force and ev.severity < self.min_sev:
                continue
            dispatch(self.notifiers, ev)
            notified.append(ev)
        return notified

    def _on_a2s_event(self, data: dict, replay: bool):
        """A2S SSE で受信したイベントを REST ポーリングと同じ変換・通知経路へ
        流す（StreamClient の受信スレッドから呼ばれる）。同一視キーは REST
        取得分と一致し、重複排除は process 側の seen が担う。
        再生分（replay=True）は record_only で状態へマージするだけで通知しない。"""
        try:
            events = a2s.event_to_events(data, self.locations)
            if events:
                self.process(events, record_only=replay)
        except Exception as e:
            print(f"[engine] A2S イベントの取り込みに失敗しました: {e}", flush=True)

    # ------------------------------------------------------------
    # 🔁 自律ループ
    # ------------------------------------------------------------
    def watch(self):
        q_interval = int(self.cfg.get("poll_seconds_quake", 20))
        e_interval = int(self.cfg.get("poll_seconds_eew", 10))
        if e_interval <= 0:
            e_interval = 10
        w_interval = int(self.cfg.get("poll_seconds_weather", 600))
        morning = self.cfg.get("morning_briefing", "07:00")
        evening = self.cfg.get("evening_briefing", "21:00")
        tick = poll_tick(e_interval, q_interval)
        print(f"🛡️ AEGIS watch 開始: EEW {e_interval}秒間隔, 地震/津波 {q_interval}秒間隔, 気象 {w_interval}秒間隔, 閾値 {self.min_sev.label}以上")
        print(f"   監視地点: {', '.join(l.name for l in self.locations)} / 朝 {morning}・夜 {evening} に自動ブリーフィング")
        # 📡 A2S が有効なときだけ SSE 常時接続する。接続できない場合も受信
        # スレッド側で再接続を続けるだけで、監視ループ自体は止まらない
        if self.a2s_enabled:
            self._a2s_client = a2s.StreamClient(
                self._on_a2s_event, url=self.a2s_url, token=self.a2s_token)
            self._a2s_client.start()
            print("   A.A.S: 地震・津波・EEW・気象警報は A.A.S からのみ取得します（SSE プッシュ受信＋REST ポーリング。台風は REST のみ）")
        else:
            print("   A.A.S: 未設定のため地震・津波・EEW・気象警報・台風は取得されません（a2s_url / a2s_token を設定してください）")
        last_eew = 0.0
        last_quake = 0.0
        last_weather = 0.0
        while True:
            try:
                now_t = time.time()
                # 🚨 EEW だけ高頻度にチェックして速報性を上げる
                if poll_due(last_eew, now_t, e_interval):
                    self.process(self.check_eew())
                    last_eew = now_t
                if poll_due(last_quake, now_t, q_interval):
                    self.process(self.check_quakes_only())
                    last_quake = now_t
                if poll_due(last_weather, now_t, w_interval):
                    self.process(self.check_weather())
                    last_weather = now_t
                # 📰 定時ブリーフィング（分単位で1回だけ）
                hm = datetime.now().strftime("%H:%M")
                key = datetime.now().strftime("%Y-%m-%d ") + hm
                if hm in (morning, evening) and self._last_briefing_key != key:
                    self._last_briefing_key = key
                    title = "朝の防災ブリーフィング" if hm == morning else "夜の防災ブリーフィング"
                    print(self.format_briefing(title), flush=True)
                self._failures = 0
                time.sleep(tick)
            except KeyboardInterrupt:
                if self._a2s_client is not None:
                    self._a2s_client.stop()
                    self._a2s_client = None
                print("\n🛡️ AEGIS watch を停止しました")
                return
            except Exception as e:
                self._failures += 1
                # 📝 指数バックオフ（失敗が続くほど間隔を延ばし、上限は5分）
                backoff = min(300, q_interval * (2 ** min(self._failures, 4)))
                print(f"[engine] loop error ({self._failures}): {e} — {backoff}秒後に再試行", flush=True)
                time.sleep(backoff)
