# ===== Tests (ネットワーク不要の単体テスト) =====
# 実行: python3 tests/test_aegis.py （pytest でも実行可能）
# 時刻依存の検証は、now を注入できる API では固定時刻を使い、
# それ以外は「常に古い」(2000年) / 「常に新しい」(実行時刻) で決定的にする。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.models import Event, Location, Severity, scale_to_str, scale_to_severity


def _store():
    import tempfile
    from aegis.store import Store
    return Store(Path(tempfile.mkdtemp()) / "t.db")


class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, ev):
        self.sent.append(ev.title)


def _engine(cfg_extra=None, notifiers=None, locations=None):
    from aegis.config import DEFAULT_CONFIG
    from aegis.engine import Engine
    cfg = dict(DEFAULT_CONFIG)
    if cfg_extra:
        cfg.update(cfg_extra)
    return Engine(cfg, _store(), notifiers if notifiers is not None else [], locations or [])


TAKASAKI = Location("高崎", 36.3219, 139.0023, "100000", "南部", "高崎市")
OMIYA = Location("大宮", 35.9064, 139.6239, "110000", "南部", "さいたま市")


# ----------------------------------------
# 📝 基本型・震度換算・設定
# ----------------------------------------
def test_scale_and_severity():
    assert scale_to_str(10) == "1"
    assert scale_to_str(45) == "5弱"
    assert scale_to_str(55) == "6弱"
    assert scale_to_str(70) == "7"
    assert scale_to_str(-1) == "不明"
    assert scale_to_severity(45, 5.0) == Severity.CRITICAL
    assert scale_to_severity(40, 4.0) == Severity.HIGH
    assert scale_to_severity(30, 3.0) == Severity.MEDIUM
    assert scale_to_severity(20, 3.0) == Severity.LOW
    assert scale_to_severity(10, 2.0) == Severity.INFO
    # M が大きければ震度が小さくても重要度を上げる
    assert scale_to_severity(10, 6.5) == Severity.CRITICAL


def test_event_id_and_format():
    ev = Event(kind="earthquake", title="テスト地震", body="本文", severity=Severity.HIGH)
    eid = ev.ensure_id()
    assert eid and len(eid) == 24
    assert ev.ensure_id() == eid  # 安定
    msg = ev.format_message()
    assert "テスト地震" in msg and "HIGH" in msg
    d = ev.to_dict()
    assert d["severity"] == "HIGH" and "raw" not in d


def test_location_validation():
    from aegis.models import location_warnings
    ok = Location("高崎", 36.3219, 139.0023, "100000", "南部")
    assert location_warnings(ok) == []
    # 都道府県コードと座標の不整合は警告になる
    mismatch = Location("高崎", 36.3219, 139.0023, "130000", "東京地方")
    assert location_warnings(mismatch), "pref mismatch warns"
    # 日本域外も警告になる（生成自体は範囲内なので通る）
    outside = Location("海外", 50.0, 10.0, "100000", "")
    assert location_warnings(outside), "outside japan warns"
    # 範囲外の座標は生成時に例外
    for bad in ({"lat": 91.0, "lon": 139.0}, {"lat": 36.0, "lon": 181.0}):
        try:
            Location("x", bad["lat"], bad["lon"], "100000", "")
            raise AssertionError("out-of-range coordinates must raise")
        except ValueError:
            pass


def test_config_and_env_override():
    import os
    from aegis.config import DEFAULT_CONFIG, load_config
    from aegis import a2s
    cfg = load_config(Path("/nonexistent/config.json"))
    assert cfg["poll_seconds_eew"] == 10 and cfg["notify_min_severity"] == "MEDIUM"
    assert cfg["a2s_url"] == "" and cfg["a2s_token"] == ""  # 既定に実値は無い
    # 環境変数が config より優先する
    old = {k: os.environ.get(k) for k in ("AEGIS_A2S_URL", "AEGIS_A2S_TOKEN")}
    try:
        os.environ["AEGIS_A2S_URL"] = "https://env.example"
        os.environ["AEGIS_A2S_TOKEN"] = "env-token"
        url, token = a2s.resolve_settings({"a2s_url": "https://cfg.example", "a2s_token": "cfg-token"})
        assert (url, token) == ("https://env.example", "env-token")
        assert a2s.a2s_enabled({"a2s_url": "https://cfg.example", "a2s_token": "cfg-token"}) is True
        del os.environ["AEGIS_A2S_URL"]
        del os.environ["AEGIS_A2S_TOKEN"]
        assert a2s.a2s_enabled({}) is False
        assert a2s.a2s_enabled({"a2s_url": "https://x"}) is False  # 片方だけでは無効
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ----------------------------------------
# 🗄️ Store
# ----------------------------------------
def test_store_dedup_and_prune():
    store = _store()
    assert not store.is_seen("abc")
    store.mark_seen("abc", "earthquake", "t", "HIGH")
    assert store.is_seen("abc")
    store.add_history("abc", "earthquake", "t", "b", "HIGH")
    rows = store.recent_history(5)
    assert len(rows) == 1 and rows[0]["title"] == "t"
    # EEW の scale 保持: 大きい方へ更新される
    assert store.seen_scale("abc") == -1  # 記録なしは -1
    store.mark_seen("e1", "eew", "t", "HIGH", scale=30)
    assert store.seen_scale("e1") == 30
    store.mark_seen("e1", "eew", "t", "HIGH", scale=25)
    assert store.seen_scale("e1") == 30  # 下方には更新しない
    store.mark_seen("e1", "eew", "t", "HIGH", scale=45)
    assert store.seen_scale("e1") == 45
    assert store.seen_scale("unknown") is None
    # WAL / busy_timeout
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    # prune: 古い行だけ消える
    store.conn.execute(
        "INSERT INTO seen(event_id, kind, title, severity, notified_at) VALUES ('old','earthquake','t','HIGH','2000-01-01 00:00:00')")
    store.conn.execute(
        "INSERT INTO history(event_id, kind, title, body, severity, created_at) VALUES ('old','earthquake','t','b','HIGH','2000-01-01 00:00:00')")
    store.conn.commit()
    res = store.prune()
    assert res == {"seen": 1, "history": 1}
    assert not store.is_seen("old") and store.is_seen("abc")
    store.close()


# ----------------------------------------
# 🔁 Engine
# ----------------------------------------
def test_engine_dedup_and_threshold():
    from aegis.engine import severity_from_name
    assert severity_from_name("HIGH") == Severity.HIGH
    assert severity_from_name("bogus") == Severity.MEDIUM
    fake = _FakeNotifier()
    eng = _engine(notifiers=[fake])
    ev = Event(kind="earthquake", title="震度4テスト", body="b", severity=Severity.HIGH, event_id="fixed-1")
    eng.process([ev])
    eng.process([ev])  # 2回目は重複で送らない
    assert fake.sent == ["震度4テスト"], fake.sent
    eng.store.close()
    # 閾値未満は通知しない（履歴には残る）
    fake2 = _FakeNotifier()
    eng2 = _engine({"notify_min_severity": "HIGH"}, notifiers=[fake2])
    low = Event(kind="earthquake", title="小さい地震", body="b", severity=Severity.LOW, event_id="low-1")
    eng2.process([low])
    assert fake2.sent == []
    assert len(eng2.store.recent_history(5)) == 1
    eng2.store.close()


def test_engine_eew_escalation():
    from datetime import datetime
    fake = _FakeNotifier()
    eng = _engine(notifiers=[fake])
    now_s = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    def eew(scale, eid="eew-x", sev=Severity.HIGH):
        return Event(kind="eew", title=f"EEW{scale}", body="b", severity=sev,
                     event_id=eid, occurred_at=now_s,
                     raw={"earthquake": {"maxScale": scale}})

    eng.process([eew(30)])
    assert fake.sent == ["EEW30"]
    eng.process([eew(30)])  # 同じ推定震度は再通知しない
    assert fake.sent == ["EEW30"]
    eng.process([eew(25)])  # 下方修正も再通知しない
    assert fake.sent == ["EEW30"]
    eng.process([eew(45)])  # 上方修正のみ再通知する
    assert fake.sent == ["EEW30", "EEW45"]
    # 取消は別イベントとして必ず届く
    cancel = Event(kind="eew", title="EEW取消", body="b", severity=Severity.MEDIUM,
                   event_id="eew-x-cancel", occurred_at=now_s,
                   raw={"earthquake": {"maxScale": 45}})
    eng.process([cancel])
    assert fake.sent == ["EEW30", "EEW45", "EEW取消"]
    eng.store.close()


def test_engine_record_only_and_stale():
    from datetime import datetime
    from aegis.engine import is_stale
    fake = _FakeNotifier()
    eng = _engine(notifiers=[fake])
    now_s = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    replay = Event(kind="earthquake", title="再生分", body="b", severity=Severity.CRITICAL,
                   event_id="replay-1", occurred_at=now_s)
    eng.process([replay], record_only=True)
    assert fake.sent == [] and eng.store.is_seen("replay-1")  # 記録のみ
    stale = Event(kind="earthquake", title="古い地震", body="b", severity=Severity.CRITICAL,
                  event_id="stale-1", occurred_at="2000/01/01 00:00:00")
    eng.process([stale])
    assert fake.sent == [] and eng.store.is_seen("stale-1")  # 古いものは記録のみ
    eng.store.close()
    # is_stale は now 注入で決定的に検証できる
    fixed_now = datetime(2026, 1, 1, 12, 0, 0)
    ev = Event(kind="earthquake", title="t", body="b", occurred_at="2026/01/01 10:00:00")
    assert is_stale(ev, now=fixed_now) is True
    ev2 = Event(kind="earthquake", title="t", body="b", occurred_at="2026/01/01 11:30:00")
    assert is_stale(ev2, now=fixed_now) is False
    warn = Event(kind="weather_warning", title="t", body="b", occurred_at="2000/01/01 00:00:00")
    assert is_stale(warn, now=fixed_now) is False  # 警報は stale 判定の対象外


def test_engine_poll_helpers():
    from aegis.engine import poll_due, poll_tick
    assert poll_due(0.0, 1000.0, 10) is True
    assert poll_due(100.0, 109.9, 10) is False
    assert poll_due(100.0, 110.0, 10) is True
    assert poll_due(100.0, 100.0, 0) is True
    assert poll_tick(10, 20) == 10
    assert poll_tick(30, 20) == 20
    assert poll_tick(0, 0) == 1


def test_engine_check_split():
    from aegis import a2s
    eng = _engine()
    eew_ev = Event(kind="eew", title="EEWテスト", body="b", severity=Severity.HIGH, event_id="eew-1")
    q_ev = Event(kind="earthquake", title="地震テスト", body="b", severity=Severity.HIGH, event_id="q-1")
    t_ev = Event(kind="tsunami", title="津波テスト", body="b", severity=Severity.HIGH, event_id="t-1")
    orig = (a2s.fetch_eews, a2s.fetch_quakes, a2s.fetch_tsunamis)
    try:
        a2s.fetch_eews = lambda **kw: [eew_ev]
        a2s.fetch_quakes = lambda **kw: [q_ev]
        a2s.fetch_tsunamis = lambda **kw: [t_ev]
        assert eng.check_eew() == [eew_ev]
        assert eng.check_quakes_only() == [q_ev, t_ev]
        assert len(eng.check_quakes()) == 3
    finally:
        a2s.fetch_eews, a2s.fetch_quakes, a2s.fetch_tsunamis = orig
    eng.store.close()


# ----------------------------------------
# 📡 A2S: 検証・重複判定・REST
# ----------------------------------------
QUAKE_A2S = {
    "kind": "quake", "key": "551:q1", "telegramId": "q1",
    "time": "2026/11/17 09:58:00", "announcedTime": "2026/11/17 10:00:00",
    "hypocenter": "群馬県南部", "magnitude": 4.8, "depthKm": 40,
    "lat": 36.3, "lon": 139.0, "maxScale": 40, "tsunamiKind": "None",
    "issueType": "DetailScale", "issueCorrect": "None",
    "prefScales": [{"pref": "群馬県", "scale": 40}, {"pref": "埼玉県", "scale": 30}],
    "points": [{"pref": "群馬県", "name": "高崎市", "scale": 40, "isArea": False},
               {"pref": "埼玉県", "name": "さいたま市", "scale": 30, "isArea": False}],
    "updatedAt": "2026-11-17T01:00:00Z",
}

EEW_A2S = {
    "kind": "eew", "key": "556:e1", "baseKey": "556:e1",
    "time": "2026/11/17 10:00:10", "originTime": "2026/11/17 10:00:00",
    "hypocenter": "群馬県南部", "estScale": 30, "estMagnitude": 4.5, "depthKm": 40,
    "lat": 36.3, "lon": 139.0, "isWarn": False, "isFinal": False, "cancel": False,
    "serial": 1, "eventId": "20261117100000", "isAssumption": False,
    "areaScales": [{"pref": "群馬県", "name": "群馬県南部", "scale": 30, "scaleTo": 40}],
    "areaArrivals": [{"pref": "群馬県", "arrivalTime": "2026/11/17 10:01:00"}],
    "updatedAt": "2026-11-17T01:00:10Z",
}


def test_a2s_validation():
    from aegis import a2s
    assert a2s.is_a2s_event(QUAKE_A2S)
    assert a2s.is_a2s_event(EEW_A2S)
    assert a2s.is_a2s_event({"kind": "tsunami", "key": "552:t1", "grade": "Warning", "areas": []})
    assert a2s.is_a2s_event({"kind": "warning", "key": "w1", "officeCode": "100000",
                             "areaCode": "100010", "warnings": [], "maxSeverity": 0})
    assert a2s.is_a2s_event({"kind": "typhoon", "key": "ty1", "lines": []})
    # 形が崩れたものは弾く
    assert not a2s.is_a2s_event(None)
    assert not a2s.is_a2s_event({"kind": "quake"})  # key 無し
    assert not a2s.is_a2s_event(dict(QUAKE_A2S, maxScale="40"))  # 数値でない
    assert not a2s.is_a2s_event(dict(EEW_A2S, isWarn="yes"))  # bool でない
    assert not a2s.is_a2s_event({"kind": "unknown", "key": "x"})


def test_a2s_dedup_tracker():
    from aegis import a2s
    tr = a2s.DedupTracker()
    ev = {"key": "k1", "updatedAt": "2026-11-17T00:00:00Z"}
    assert tr.is_new_or_updated(ev) is True
    assert tr.is_new_or_updated(ev) is False  # 同じ key + updatedAt は変化なし
    assert tr.is_new_or_updated({"key": "k1", "updatedAt": "2026-11-17T00:01:00Z"}) is True
    assert tr.is_new_or_updated({"updatedAt": "x"}) is True  # キー無しは通す


def test_a2s_unconfigured_returns_empty():
    from aegis import a2s
    a2s.set_config("", "")
    try:
        assert a2s.fetch_quakes() == []
        assert a2s.fetch_tsunamis() == []
        assert a2s.fetch_eews() == []
        assert a2s.fetch_warnings(TAKASAKI) == []
        assert a2s.fetch_typhoon() is None
    finally:
        a2s.set_config("", "")


def test_a2s_rest_fetch_and_auth():
    from aegis import a2s
    captured = {}

    def fake_fetch_json(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return {"events": [QUAKE_A2S, {"kind": "bogus"}, "junk"]}

    orig = a2s.fetch_json
    a2s.fetch_json = fake_fetch_json
    a2s.set_config("https://a2s.example/", "test-token")
    try:
        events = a2s.fetch_events("https://a2s.example/", "test-token", kind="quake", since="2026-11-17T00:00:00Z", limit=7)
        assert events == [QUAKE_A2S]  # 検証を通ったものだけ
        assert "kind=quake" in captured["url"] and "limit=7" in captured["url"]
        assert "since=2026-11-17T00%3A00%3A00Z" in captured["url"]
        assert captured["headers"] == {"Authorization": "Bearer test-token"}
        # REST 取得 → Event 化まで通る
        evs = a2s.fetch_quakes(1, [TAKASAKI, OMIYA])
        assert len(evs) == 1 and evs[0].kind == "earthquake"
        assert "群馬県内の最大震度 4" in evs[0].body
    finally:
        a2s.fetch_json = orig
        a2s.set_config("", "")


# ----------------------------------------
# 📡 A2S: 変換
# ----------------------------------------
def test_a2s_converters():
    from aegis import a2s
    # quake: 深さ null → センチネル -1、0 はごく浅いとして保持
    item = a2s.quake_to_item(dict(QUAKE_A2S, depthKm=None))
    assert item["earthquake"]["hypocenter"]["depth"] == -1
    item0 = a2s.quake_to_item(dict(QUAKE_A2S, depthKm=0))
    assert item0["earthquake"]["hypocenter"]["depth"] == 0
    assert item["points"][0]["addr"] == "高崎市"  # name → addr
    assert a2s.quake_to_item(EEW_A2S) is None  # 種別違い
    # tsunami: key から電文 ID を取り、予想高さの数値を保持する
    titem = a2s.tsunami_to_item({
        "kind": "tsunami", "key": "552:t9", "time": "2026/11/17 10:05:00", "cancelled": False,
        "grade": "Warning",
        "areas": [{"name": "宮城県沿岸", "grade": "Warning", "maxHeightLabel": "3m",
                   "maxHeightValue": 3.0, "arrivalLabel": "2026/11/17 10:40:00",
                   "firstHeightCondition": "到達確認済み"}]})
    assert titem["id"] == "t9"
    assert titem["areas"][0]["maxHeight"] == {"description": "3m", "value": 3.0}
    assert titem["areas"][0]["firstHeight"]["condition"] == "到達確認済み"
    # eew: 到達予想は同じ都道府県の先頭 area へ付く
    eitem = a2s.eew_to_item(EEW_A2S)
    assert eitem["areas"][0]["arrivalTime"] == "2026/11/17 10:01:00"
    assert eitem["areas"][0]["scaleFrom"] == 30 and eitem["areas"][0]["scaleTo"] == 40
    assert eitem["issue"]["serial"] == 1
    # event_to_events: 不正なイベントは空、typhoon は lines を使う
    assert a2s.event_to_events({"kind": "quake"}) == []
    tev = a2s.event_to_events({"kind": "typhoon", "key": "ty1", "lines": ["・台風第14号"],
                               "time": ""}, [])
    assert len(tev) == 1 and "台風第14号" in tev[0].body
    assert a2s.event_to_events({"kind": "typhoon", "key": "ty2", "lines": []}, []) == []


def test_warning_conversion():
    from aegis import a2s
    wn = {"kind": "warning", "key": "warning:100000:100010", "officeCode": "100000",
          "prefName": "群馬県", "areaCode": "100010", "areaName": "南部",
          "reportDatetime": "2026-11-17T11:00:00+09:00", "headline": "",
          "warnings": [{"code": "14", "name": "雷注意報", "status": "発表", "severity": 2},
                       {"code": "10", "name": "大雨注意報", "status": "", "severity": 2},
                       {"code": "20", "name": "濃霧注意報", "severity": 2},
                       {"code": "03", "name": "大雨警報", "status": "解除", "severity": 3},
                       {"code": "99", "status": "発表", "severity": 2}],
          "maxSeverity": 2, "time": "2026-11-17T11:00:00+09:00", "released": False}
    evs = a2s.warning_to_events(wn, TAKASAKI)
    titles = " ".join(e.title for e in evs)
    assert "雷注意報" in titles, "published kept"
    assert "大雨注意報" in titles, "empty status kept as active"
    assert "濃霧注意報" in titles, "missing status kept as active"
    assert "コード99" in titles, "unknown code kept with code label"
    assert "大雨警報" not in titles, "released excluded"
    assert all(e.severity == Severity.MEDIUM for e in evs), "severity from field"
    assert evs[0].event_id.endswith("2026-11-17T11:00:00+09:00"), "id uses report datetime"
    # 他区域・他官署は照合で落ちる
    other_area = dict(wn, areaCode="100020", areaName="北部")
    assert a2s.warning_to_events(other_area, TAKASAKI) == []
    other_office = dict(wn, officeCode="110000")
    assert a2s.warning_to_events(other_office, TAKASAKI) == []
    # 解除イベント自体は Event 化しない
    released = dict(wn, warnings=[], maxSeverity=0, released=True)
    assert a2s.warning_to_events(released, TAKASAKI) == []
    # 特別警報は severity=4 → CRITICAL
    special = dict(wn, warnings=[{"code": "33", "name": "大雨特別警報", "status": "発表", "severity": 4}],
                   maxSeverity=4)
    assert a2s.warning_to_events(special, TAKASAKI)[0].severity == Severity.CRITICAL


# ----------------------------------------
# 📡 SSE: パーサーとストリームクライアント
# ----------------------------------------
def test_sse_parser():
    from aegis import a2s
    p = a2s.SseParser()
    frames = []
    frames += p.push(": heartbeat\n\n")  # コメント行は読み捨てる
    frames += p.push("event: quake\nda")
    frames += p.push('ta: {"a": 1}\n\n')
    frames += p.push("data: line1\ndata: line2\n\n")  # 複数行 data は連結
    frames += p.push("event: ready\r\ndata: {}\r\n\r\n")  # CRLF
    assert frames[0] == a2s.SseFrame("quake", '{"a": 1}')
    assert frames[1] == a2s.SseFrame("message", "line1\nline2")
    assert frames[2] == a2s.SseFrame("ready", "{}")
    assert len(frames) == 3


def test_stream_client_replay_live_dedupe_since():
    import json as _json
    from aegis import a2s

    def frame(kind, ev):
        return [f"event: {kind}".encode(), f"data: {_json.dumps(ev)}".encode(), b""]

    lines = []
    lines += frame("quake", QUAKE_A2S)                    # 再生分
    lines += [b"event: ready", b"data: {}", b""]          # ready で live へ
    lines += frame("eew", EEW_A2S)                        # live
    lines += frame("eew", EEW_A2S)                        # 重複（key+updatedAt 同じ）は落とす
    lines += [b"event: quake", b"data: {not json", b""]   # 壊れた JSON は数えて落とす
    typhoon_ev = {"kind": "typhoon", "key": "ty1", "lines": ["・台風"], "updatedAt": "2026-11-17T01:00:20Z"}
    lines += frame("typhoon", typhoon_ev)                 # SSE では受けない種別
    received = []
    opened = []

    def fake_open(url, token, ua):
        opened.append(url)
        return iter(lines)

    client = a2s.StreamClient(
        lambda ev, replay: received.append((ev["kind"], replay)),
        url="https://a2s.example", token="test-token",
        open_stream=fake_open, log=lambda m: None, warn=lambda m: None,
        wait=lambda d: True)  # 切断後の待機で即終了させる
    client._run()
    assert received == [("quake", True), ("eew", False)], received
    assert client.invalid_count == 1
    assert opened == ["https://a2s.example/v1/stream"]  # 初回は since 無し
    # 再接続時は最後に受信した updatedAt が since= に付く
    assert "since=2026-11-17T01%3A00%3A10Z" in client.stream_url(), client.stream_url()


# ----------------------------------------
# 🌏 変換器: 地震・EEW・津波
# ----------------------------------------
def test_quake_merge_and_render():
    from aegis.convert_quake import merge_earthquake_items, quake_events_from_items
    prompt = {"id": "p1", "time": "2026/11/17 10:00:05",
              "issue": {"time": "2026/11/17 10:00:05", "type": "ScalePrompt", "correct": "None"},
              "earthquake": {"time": "2026/11/17 09:58:00", "maxScale": 40, "domesticTsunami": "Unknown",
                             "hypocenter": {"name": "群馬県南部", "magnitude": -1, "depth": -1,
                                            "latitude": 36.3, "longitude": 139.0}},
              "points": [{"pref": "群馬県", "addr": "高崎市", "scale": 40}]}
    detail = {"id": "d1", "time": "2026/11/17 10:03:00",
              "issue": {"time": "2026/11/17 10:03:00", "type": "DetailScale", "correct": "None"},
              "earthquake": {"time": "2026/11/17 09:58:00", "maxScale": 45, "domesticTsunami": "None",
                             "hypocenter": {"name": "群馬県南部", "magnitude": 5.2, "depth": 40,
                                            "latitude": 36.3, "longitude": 139.0}},
              "points": [{"pref": "群馬県", "addr": "高崎市", "scale": 45},
                         {"pref": "埼玉県", "addr": "さいたま市", "scale": 30}]}
    merged = merge_earthquake_items([prompt, detail])
    assert len(merged) == 1 and merged[0]["_merged_count"] == 2
    ev = quake_events_from_items(merged, [TAKASAKI, OMIYA])[0]
    assert "最大震度5弱" in ev.title
    assert "※2報を統合" in ev.body and "（確定）" in ev.body
    assert "群馬県内の最大震度 5弱" in ev.body and "各地の震度" in ev.body
    assert "高崎: 高崎市の震度 5弱" in ev.body  # 市区町村照合の実測表示
    assert "深さ約40km" in ev.body  # センチネルの報が判明した深さを上書きしない
    assert ev.event_id == "quake-2026/11/17 09:58:00-群馬県南部"  # 発生時刻＋震源名で安定
    # センチネル値（不明）の扱い
    unknown = {"id": "q2", "time": "2026/11/17 10:00:00",
               "earthquake": {"time": "2026/11/17 09:58:00", "maxScale": -1, "domesticTsunami": "Unknown",
                              "hypocenter": {"name": "不明", "magnitude": -1, "depth": -1}},
               "points": []}
    ev2 = quake_events_from_items(merge_earthquake_items([unknown]), [])[0]
    assert "M不明" in ev2.body and "深さ不明" in ev2.body and "震度不明" in ev2.title
    assert "ごく浅い" not in ev2.body
    # 深さ 0 は「ごく浅い」（判明した値）
    shallow = dict(unknown, earthquake=dict(unknown["earthquake"],
                 hypocenter={"name": "浅い震源", "magnitude": 3.0, "depth": 0}, maxScale=20))
    ev3 = quake_events_from_items(merge_earthquake_items([shallow]), [])[0]
    assert "深さごく浅い" in ev3.body


def test_eew_conversion():
    from aegis.convert_eew import (eew_action_guide, eew_arrival_estimates,
                                    eew_events_from_items)
    base_eq = {"originTime": "2026/11/17 10:00:00", "maxScale": 30,
               "hypocenter": {"name": "群馬県南部", "magnitude": 4.5, "latitude": 36.3, "longitude": 139.0, "depth": 40}}
    items = [
        {"id": "e1", "time": "2026/11/17 10:00:10", "isWarn": True, "isCancel": False,
         "earthquake": base_eq, "areas": [{"pref": "群馬県", "name": "群馬県南部", "scaleFrom": 30}]},
        {"id": "e2", "time": "2026/11/17 10:00:25", "isWarn": True, "isCancel": False,
         "earthquake": dict(base_eq, maxScale=45), "areas": [{"pref": "群馬県", "name": "群馬県南部", "scaleFrom": 45}]},
    ]
    evs = eew_events_from_items(items, [TAKASAKI])
    assert len(evs) == 2 and evs[0].event_id == evs[1].event_id  # 続報は同一 ID
    assert "緊急地震速報（警報）" in evs[0].title
    assert "高崎: 予想震度3" in evs[0].body  # 地点別予想震度は areaScales から導出
    # 取消は -cancel 付きの別イベントで MEDIUM
    cancel = eew_events_from_items([dict(items[0], isCancel=True, cancelled=True)], [])[0]
    assert cancel.event_id == evs[0].event_id + "-cancel"
    assert cancel.severity == Severity.MEDIUM and "（取消）" in cancel.title
    # 訓練報は INFO で通知対象外
    training = eew_events_from_items([dict(items[0], isTraining=True)], [])[0]
    assert training.severity == Severity.INFO and "【訓練】" in training.title
    assert training.event_id.endswith("-training")
    # scaleTo=99 は「程度以上」
    over = eew_events_from_items([{
        "id": "e9", "time": "2026/11/17 10:00:10", "isWarn": False, "isCancel": False,
        "earthquake": base_eq,
        "areas": [{"pref": "群馬県", "name": "群馬県南部", "scaleFrom": 45, "scaleTo": 99}]}], [TAKASAKI])[0]
    assert "予想震度5弱程度以上" in over.body
    # 行動文言の出し分け
    assert "強い揺れに備え" in eew_action_guide(45, True)
    assert "やや強い揺れに備え" in eew_action_guide(40, True)
    assert eew_action_guide(30, False) == "揺れに注意する"
    assert "強い揺れに備え" in eew_action_guide(-1, True)   # 不明×警報は強い文言
    assert eew_action_guide(-1, False) == "揺れに注意する"  # 不明×予報は注意喚起のみ


def test_eew_arrival_estimates():
    from datetime import datetime, timedelta
    from aegis.convert_eew import eew_arrival_estimates
    from aegis.geo import haversine_km
    assert abs(haversine_km(35.0, 139.0, 36.0, 139.0) - 111.19) < 0.5
    item_a = {"time": "2026/11/17 10:00:10", "isCancel": False,
              "earthquake": {"originTime": "2026/11/17 10:00:00",
                             "hypocenter": {"name": "群馬県南部", "latitude": 36.3, "longitude": 139.0, "depth": 40}},
              "areas": [{"pref": "群馬県", "name": "群馬県南部", "arrivalTime": "2026/11/17 10:01:00"}]}
    lines = eew_arrival_estimates(item_a, [TAKASAKI], now=datetime(2026, 11, 17, 10, 0, 30))
    assert len(lines) == 1 and "高崎の主要動到達予想 10:01" in lines[0]
    assert "あと約30秒" in lines[0] and "推定" not in lines[0]
    # 到達直後（1秒未満）は「あと約0秒」ではなく到達扱い（floor 丸め）
    just_after = datetime(2026, 11, 17, 10, 1, 0) + timedelta(milliseconds=500)
    assert "到達した可能性が高いです" in eew_arrival_estimates(item_a, [TAKASAKI], now=just_after)[0]
    just_before = datetime(2026, 11, 17, 10, 0, 59) + timedelta(milliseconds=500)
    assert "あと約0秒" in eew_arrival_estimates(item_a, [TAKASAKI], now=just_before)[0]
    # arrivalTime 無し → S波推定（地点直下・深さ35km → 35/3.5=10秒）
    item_b = {"time": "2026/11/17 10:00:10", "isCancel": False,
              "earthquake": {"originTime": "2026/11/17 10:00:00",
                             "hypocenter": {"name": "直下", "latitude": 36.3219, "longitude": 139.0023, "depth": 35}},
              "areas": []}
    lines = eew_arrival_estimates(item_b, [TAKASAKI], now=datetime(2026, 11, 17, 10, 0, 0))
    assert len(lines) == 1 and "推定" in lines[0] and "あと約10秒" in lines[0]
    # 取消・空は空
    assert eew_arrival_estimates(dict(item_a, isCancel=True), [TAKASAKI], now=datetime(2026, 11, 17, 10, 0, 0)) == []
    assert eew_arrival_estimates({}, [TAKASAKI], now=datetime(2026, 11, 17, 10, 0, 0)) == []


def test_tsunami_conversion():
    from aegis.convert_tsunami import tsunami_events_from_items, tsunami_grouped_text
    areas = [{"name": "宮城県沿岸", "grade": "Warning",
              "maxHeight": {"description": "3m"},
              "firstHeight": {"arrivalTime": "2026/11/17 10:40:00", "condition": "到達確認済み"}},
             {"name": "岩手県沿岸", "grade": "Watch"}]
    text, total = tsunami_grouped_text(areas)
    assert total == 2 and "【津波警報】" in text and "【津波注意報】" in text
    assert "予想 3m" in text and "到達予想 10:40" in text and "到達確認済み" in text
    # タイトル用は最大件数で抑え「ほかN区域」を付ける。本文は全件
    many = [{"name": f"区域{i}", "grade": "Watch"} for i in range(10)]
    t2, total2 = tsunami_grouped_text(many, max_names=8)
    assert total2 == 10 and "ほか2区域" in t2
    t3, _ = tsunami_grouped_text(many)
    assert "ほか" not in t3 and "区域9" in t3
    ev = tsunami_events_from_items([{"id": "t1", "time": "2026/11/17 10:05:00",
                                     "cancelled": False, "areas": areas}])[0]
    assert ev.severity == Severity.CRITICAL and "津波警報" in ev.title
    cancel = tsunami_events_from_items([{"id": "t2", "time": "2026/11/17 11:00:00",
                                         "cancelled": True, "areas": []}])[0]
    assert cancel.severity == Severity.MEDIUM and "解除" in cancel.title


# ----------------------------------------
# 🌦️ 天気・ニュース・レーダー
# ----------------------------------------
def test_weather_parsers():
    from aegis import weather
    fc = [{"timeSeries": [
        {"areas": [{"area": {"name": "南部", "code": "100010"}, "weathers": ["晴れ"]}]},
        {"timeDefines": ["2026-11-17T12:00:00+09:00"], "areas": [{"area": {"name": "南部", "code": "100010"}, "pops": ["60"]}]},
        {"areas": [{"area": {"name": "前橋", "code": "42251"}, "temps": ["13", "21", "9", "20"]}]}]}]
    assert "60%" in weather.pops_text(fc, "100010")
    assert "今日 13〜21℃" in weather.temps_text(fc)
    # 気温は区域コード一致のエリアを選ぶ（先頭固定ではない）
    fc2 = [{"timeSeries": [
        {"areas": [{"area": {"name": "南部", "code": "100010"}, "weathers": ["晴れ"]}]},
        {"areas": [
            {"area": {"name": "北部代表", "code": "100020"}, "temps": ["1", "10", "2", "11"]},
            {"area": {"name": "南部代表", "code": "100010"}, "temps": ["13", "21", "9", "20"]}]}]}]
    assert "気温（南部代表）" in weather.temps_text(fc2, "100010")
    assert "今日 1〜10℃" in weather.temps_text(fc2)  # コード無しは先頭にフォールバック
    assert weather.forecast_area_code(
        [{"timeSeries": [{"areas": [{"area": {"name": "南部", "code": "100010"}}]}]}], "南部") == "100010"
    # 週間予報は今日を含む
    daily = {"time": ["2026-11-17", "2026-11-18"], "weather_code": [0, 61],
             "temperature_2m_max": [13.0, 15.0], "temperature_2m_min": [9.0, 8.0]}
    wk = weather.weekly_text(daily)
    assert "11/17 ☀ 9〜13℃" in wk and "11/18 🌧 8〜15℃" in wk
    assert weather.weekly_text({}) == "" and weather.weekly_text(None) == ""
    # Open-Meteo 正規化: 接尾辞キーだけを読む
    raw = {"current": {"temperature_2m": 12.0},
           "hourly": {"time": ["t"], "temperature_2m_jma_seamless": [12.0],
                      "temperature_2m": [99.0],
                      "precipitation_probability_ecmwf_ifs025": [80]},
           "daily": {"time": ["2026-11-17"], "temperature_2m_max_jma_seamless": [15.0]}}
    norm = weather.normalize_open_meteo(raw)
    assert norm["hourly"]["temperature_2m"] == [12.0]
    assert norm["hourly"]["precipitation_probability"] == [80]
    # 欠測は「—」表示（None℃ / None% にしない）
    orig = weather.fetch_json
    try:
        weather.fetch_json = lambda url, headers=None: {
            "current": {"time": "2026-11-17T12:00", "temperature_2m": None,
                        "weather_code": None, "wind_speed_10m": None},
            "daily": {"time": ["2026-11-17"], "weather_code": [None],
                      "temperature_2m_max": [None], "temperature_2m_min": [None],
                      "precipitation_probability_max": [None]}}
        ev = weather.fetch_open_meteo(TAKASAKI)
        assert "None" not in ev.body and "—℃" in ev.body and "最大—%" in ev.body
    finally:
        weather.fetch_json = orig


def test_weather_tables():
    from datetime import datetime
    from aegis import weather_tables as wt
    # アイコンは天気コードに統一（降水確率で上書きしない）。夜の晴れは月
    assert wt.wx_icon(0, 86, 3) == "🌙"
    assert wt.wx_icon(0, 60, 12) == "☀"
    assert wt.wx_icon(61, 10, 12) == "🌧"
    assert wt.wx_icon(3, 90, 12) == "☁"
    fc = [{"timeSeries": [
        {"areas": [{"area": {"name": "南部", "code": "100010"}, "weathers": ["晴れ"]}]},
        {"timeDefines": ["2026-11-17T12:00:00+09:00", "2026-11-17T18:00:00+09:00"],
         "areas": [{"area": {"name": "南部", "code": "100010"}, "pops": ["60", "90"]}]}]}]
    rows = wt.pops_rows(fc, "100010")
    assert rows[0] == {"date": "11/17", "label": "12〜18時", "pop": "60"}
    assert rows[1]["label"] == "18〜0時"
    assert wt.pops_rows(None, "100010") == []
    om = {
        "hourly": {
            "time": ["2026-11-17T16:00", "2026-11-17T18:00", "2026-11-17T21:00",
                     "2026-11-18T00:00", "2026-11-19T00:00"],
            "temperature_2m": [11.0, 9.5, 8.0, 7.0, 6.0],
            "weather_code": [0, 0, 0, 61, 0],
            "precipitation_probability": [10, 86, 20, 40, 0],
            "precipitation": [0.0, 1.0, 0.5, 0.0, 0.0],
        },
        "daily": {
            "time": ["2026-11-17", "2026-11-18"],
            "weather_code": [0, 61],
            "temperature_2m_max": [13.0, 15.0],
            "temperature_2m_min": [9.0, 8.0],
            "precipitation_probability_max": [86, 40],
            "precipitation_sum": [3.0, 0.0],
        },
    }
    tables = wt.parse_weather_tables(om, fc, "100010", now=datetime(2026, 11, 17, 17, 30))
    assert [r["label"] for r in tables["hourly"]] == ["11/17 18時", "11/17 21時", "11/18 0時"]
    h0 = tables["hourly"][0]
    assert h0["icon"] == "☀" and h0["temp"] == "9.5" and h0["pop"] == "86"
    # 3時間区間の最大降水確率と降水量合計が併記される
    assert h0["pop_max"] == "86" and h0["precip"] == "1"
    assert tables["weekly"][0]["date"] == "11/17（火）"
    assert tables["weekly"][0]["precip"] == "3"
    assert wt.parse_weather_tables({}, None, "", now=datetime(2026, 11, 17)) == {"hourly": [], "weekly": [], "pops": []}


def test_news_rss():
    from aegis import news
    rss = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>NHK</title>
<item><title>群馬県で震度4の地震</title><link>http://example.jp/1.html</link>
<pubDate>Sat, 08 Aug 2026 21:54:30 +0900</pubDate>
<description><p>群馬県南部を震源とする地震がありました。津波の心配はありません。</p></description></item>
<item><title>通常のニュース</title><link>http://example.jp/2.html</link>
<pubDate>Sat, 08 Aug 2026 22:00:00 +0900</pubDate>
<description>説明文です</description></item>
</channel></rss>"""
    items = news.parse_rss(rss)
    assert len(items) == 2 and items[0]["time"] == "21:54"
    assert "<p>" not in items[0]["summary"] and "津波の心配はありません" in items[0]["summary"]
    assert news.parse_rss("not xml") == []
    s = news.summarize_text("あ" * 120)
    assert len(s) == 81 and s.endswith("…")
    orig = news.fetch_text
    try:
        news.fetch_text = lambda url: rss
        merged = news.fetch_news(limit=8)
        assert len(merged) == 2  # リンクで重複排除
        assert merged[0]["title"] == "群馬県で震度4の地震"  # 防災キーワード優先
    finally:
        news.fetch_text = orig


def test_radar():
    from aegis import radar
    frame = radar.radar_latest_frame({"radar": {"past": [{"time": 1, "path": "/a"}, {"time": 2, "path": "/b"}]}})
    assert frame == {"path": "/b", "time": 2}
    assert radar.radar_latest_frame({"radar": {"past": []}}) is None
    assert radar.radar_latest_frame(None) is None
    base = 1_700_000_000
    assert radar.radar_age_text(base, now=base + 5 * 60) == "約5分前の観測"
    assert radar.radar_age_text(base, now=base + 20 * 60) == "約20分前の観測"
    stale = radar.radar_age_text(base, now=base + 21 * 60)
    assert "古い情報の可能性があります" in stale
    assert radar.radar_age_text(0, now=base) == ""
    assert radar.radar_age_text(base, now=base - 60) == "約0分前の観測"
    assert radar.tile_xy(0.0, 0.0, 8) == (128, 128)
    assert radar.tile_xy_float(0.0, 0.0, 8) == (128.0, 128.0)
    # ナウキャスト: 実況（basetime == validtime）の最新を選ぶ
    latest = radar.parse_jma_nowcast_latest([
        {"basetime": "202611170100", "validtime": "202611170200"},  # 予報は対象外
        {"basetime": "202611170100", "validtime": "202611170100"},
        {"basetime": "202611170050", "validtime": "202611170050"}])
    assert latest == {"basetime": "202611170100", "validtime": "202611170100"}
    assert radar.jma_nowcast_time_text({"validtime": "20261117010000"}) == "11月17日 10:00"
    # FrameCache: fresh → cached → extended（同じ観測）→ stale（失敗時）
    cache = radar.FrameCache(ttl=120, stale_ttl=900)
    f1 = {"path": "/a", "time": 100}
    assert cache.get(lambda: f1, now=1000) == (f1, "fresh")
    assert cache.get(lambda: {"path": "/b", "time": 200}, now=1100) == (f1, "cached")
    assert cache.get(lambda: dict(f1), now=1200) == (f1, "extended")
    def boom():
        raise RuntimeError("down")
    assert cache.get(boom, now=1400) == (f1, "stale")


# ----------------------------------------
# 🔔 通知・診断・CLI・Assistant・HTTP
# ----------------------------------------
def test_desktop_notify():
    import tempfile
    from pathlib import Path as P
    from aegis import notify
    from aegis.config import DEFAULT_CONFIG
    from aegis.notify import DesktopNotifier, build_notifiers, desktop_command, desktop_text
    assert notify.normalize_system("Windows") == "windows"
    assert notify.normalize_system("Darwin") == "darwin"
    assert notify.normalize_system("FreeBSD") == "other"
    ev = Event(kind="earthquake", title="地震 最大震度5弱 テスト", body="本文です\n次の行",
               severity=Severity.HIGH, action_guide="頭を守る")
    title, body = desktop_text(ev)
    assert "🟠" in title and title.endswith("地震 最大震度5弱 テスト")
    assert "\n" not in body and "本文です 次の行" in body and "頭を守る" in body
    _, long_body = desktop_text(Event(kind="earthquake", title="t", body="あ" * 300, severity=Severity.HIGH))
    assert len(long_body) == 241 and long_body.endswith("…")
    assert desktop_command("linux", "タイトル", "本文") == ["notify-send", "タイトル", "本文"]
    cmd = desktop_command("darwin", 'タイ"トル', "本\\文")
    assert cmd[0] == "osascript" and 'display notification "本\\\\文" with title "タイ\\"トル"' in cmd[2]
    cmd = desktop_command("windows", "タイ'トル", "本文")
    assert cmd[0] == "powershell" and "ToastNotificationManager" in cmd[-1] and "タイ''トル" in cmd[-1]
    assert desktop_command("other", "t", "b") is None
    # 通知手段が無い環境では静かにスキップする
    orig_which, orig_run = notify.shutil.which, notify.subprocess.run
    calls = []
    try:
        notify.shutil.which = lambda name: None
        assert notify.desktop_available("linux") is False
        DesktopNotifier(system="linux").send(ev)
        DesktopNotifier(system="other").send(ev)
        notify.subprocess.run = lambda *a, **k: calls.append(a)
        notify.shutil.which = lambda name: "/usr/bin/notify-send"
        assert notify.desktop_available("linux") is True
        DesktopNotifier(system="linux").send(ev)
        assert len(calls) == 1 and calls[0][0][:1] == ["notify-send"]
    finally:
        notify.shutil.which, notify.subprocess.run = orig_which, orig_run
    root = P(tempfile.mkdtemp())
    off = build_notifiers(dict(DEFAULT_CONFIG), root)
    assert not any(isinstance(n, DesktopNotifier) for n in off)
    cfg = dict(DEFAULT_CONFIG)
    cfg["desktop_notify"] = True
    assert any(isinstance(n, DesktopNotifier) for n in build_notifiers(cfg, root))


def test_notify_file_and_webhook():
    import tempfile
    from pathlib import Path as P
    from aegis import __version__, notify
    from aegis.notify import FileNotifier, WebhookNotifier, rotate_log_if_needed
    d = P(tempfile.mkdtemp())
    p = d / "alerts.log"
    p.write_text("x" * 100, encoding="utf-8")
    assert rotate_log_if_needed(p, 100) is True
    assert (d / "alerts.log.1").read_text(encoding="utf-8") == "x" * 100
    assert rotate_log_if_needed(p, 100) is False
    p.write_text("y" * 50, encoding="utf-8")
    FileNotifier(p, max_bytes=10).send(
        Event(kind="earthquake", title="回転テスト", body="b", severity=Severity.HIGH))
    assert "回転テスト" in p.read_text(encoding="utf-8")
    # Webhook の UA は現在のバージョン表記
    captured = {}
    orig = notify.urllib.request.urlopen
    try:
        notify.urllib.request.urlopen = lambda req, timeout=None: captured.setdefault("ua", req.get_header("User-agent"))
        WebhookNotifier("http://example.invalid/hook", "generic").send(
            Event(kind="earthquake", title="t", body="b", severity=Severity.HIGH))
    finally:
        notify.urllib.request.urlopen = orig
    assert captured.get("ua") == f"AEGIS/{__version__}"


def test_diagnose_format():
    from aegis import diagnose
    src_ok = [{"name": "A2S", "ok": True, "ms": 123, "detail": ""}]
    cfg_ok = [{"name": "監視地点", "ok": True, "ms": None, "detail": "2件登録"}]
    text = diagnose.format_diagnosis(src_ok, cfg_ok)
    assert "○ A2S: 接続OK（123ms）" in text and "全て正常です（2/2）" in text
    assert diagnose.diagnosis_exit_code(src_ok, cfg_ok) == 0
    src_ng = [{"name": "A2S", "ok": False, "ms": 5000, "detail": "URLError: timeout"}]
    text2 = diagnose.format_diagnosis(src_ng, cfg_ok)
    assert "× A2S: 失敗（URLError: timeout）" in text2 and "1件に問題があります" in text2
    assert diagnose.diagnosis_exit_code(src_ng, cfg_ok) == 1


def test_cli_esc_and_radar_marker():
    from aegis import cli
    assert cli._esc("a'b\"c<d>&") == "a&#x27;b&quot;c&lt;d&gt;&amp;"
    assert cli._esc("通常テキスト") == "通常テキスト"
    mx, my = cli.radar_marker_pixel(TAKASAKI, 8)
    # 高崎はタイル中心ではない位置に出る（小数タイル座標からの算出）
    assert abs(mx - 472.6) < 1.0 and abs(my - 318.5) < 1.0
    assert (mx, my) != (384.0, 384.0)
    frame = {"path": "/v2/radar/1700000000", "time": 1700000000}
    html = cli._radar_section(TAKASAKI, frame, "11月17日 20:30")
    assert html.count("cyberjapandata.gsi.go.jp/xyz/std/8/") == 9  # ベース地図 3×3
    assert "/256/8/" not in html  # 雨タイルに z8 は使わない
    assert html.count("/256/7/") == 4  # 雨タイルは z7 で 4 枚
    assert f"left:{mx:.1f}px;top:{my:.1f}px" in html


def test_assistant_routing():
    from aegis import a2s, assistant, weather
    orig_t, orig_q, orig_w = a2s.fetch_tsunamis, a2s.fetch_quakes, weather.fetch_warnings
    try:
        a2s.fetch_tsunamis = lambda **kw: []
        assert "ありません" in assistant.answer("津波は？", [TAKASAKI])
        ev = Event(kind="earthquake", title="地震 最大震度4 群馬県南部", body="b",
                   severity=Severity.HIGH, event_id="q1")
        a2s.fetch_quakes = lambda **kw: [ev]
        assert "群馬県南部" in assistant.answer("最近の地震は？", [TAKASAKI])
        weather.fetch_warnings = lambda loc: []
        assert "ありません" in assistant.answer("高崎に警報は？", [TAKASAKI])
    finally:
        a2s.fetch_tsunamis, a2s.fetch_quakes, weather.fetch_warnings = orig_t, orig_q, orig_w


def test_http_retry_policy():
    import io
    import urllib.error
    from aegis import http_client

    class Resp:
        def __init__(self, body):
            self._body = body
            self.headers = {}

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = {"n": 0}
    orig_open, orig_delays = http_client.urllib.request.urlopen, http_client.FETCH_RETRY_DELAYS
    try:
        http_client.FETCH_RETRY_DELAYS = (0.0, 0.0)  # テストでは待たない
        def not_found(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        http_client.urllib.request.urlopen = not_found
        try:
            http_client.fetch_json("https://example.invalid/x")
            raise AssertionError("404 must raise")
        except urllib.error.HTTPError:
            pass
        assert calls["n"] == 1  # 4xx はリトライしない
        calls["n"] = 0
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
            return Resp(b'{"ok": true}')
        http_client.urllib.request.urlopen = flaky
        assert http_client.fetch_json("https://example.invalid/x") == {"ok": True}
        assert calls["n"] == 3  # 5xx はリトライして復旧する
    finally:
        http_client.urllib.request.urlopen = orig_open
        http_client.FETCH_RETRY_DELAYS = orig_delays


if __name__ == "__main__":
    _tests = [(name, fn) for name, fn in sorted(globals().items())
              if name.startswith("test_") and callable(fn)]
    for _name, _fn in _tests:
        _fn()
        print(f"ok {_name}")
    print(f"ALL TESTS PASSED ({len(_tests)} tests)")
