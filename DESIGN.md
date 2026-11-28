# AEGIS 設計メモ

AEGIS（Python クライアント）の設計方針と、2026-11-28 の再実装における
旧実装との対応・判断記録。

## 1. 概要

AEGIS は防災情報の**クライアント**である。情報の集約は A.A.S（A2S）ハブが
担い、本アプリはハブから受け取ったイベントを、利用者の地点に合わせて
整形し、通知・表示・記録する。ハブ未設定時は直接取得（気象庁・Open-Meteo・
NHK・RainViewer）にフォールバックする構成は旧実装から維持した。

- 実行形態: CLI（`python -m aegis <command>`）。常駐は `watch`。
- 依存: Python 3.11+ 標準ライブラリのみ。サードパーティ依存を持たない
  ことで、配布 zip をそのまま展開して動かせる状態を保つ。
- 永続化: SQLite（既読管理＋履歴、WAL、90 日保持）。

## 2. モジュール構成

| モジュール | 役割 |
| --- | --- |
| `models.py` | Severity / Event / Location、震度換算、地点の整合チェック |
| `config.py` | 設定の既定値・読み込み・検証（地点・通知先） |
| `http_client.py` | 取得基盤（リトライ方針つき fetch、gzip 対応） |
| `geo.py` | 座標計算（距離・タイル・震央近接判定） |
| `locations.py` | 設定 dict ⇄ Location の変換、地点名の照合 |
| `convert_quake.py` | 地震電文（551 相当）の統合と Event 化 |
| `convert_eew.py` | EEW（556 相当）の同一性・行動文言・到達予想 |
| `convert_tsunami.py` | 津波電文（552 相当）の種別グルーピング |
| `a2s.py` | A2S クライアント（検証・重複判定・REST・SSE・ハブイベント変換） |
| `weather.py` | 気象庁予報・Open-Meteo の取得と整形 |
| `weather_codes.py` | 警報コード表・WMO 天気コード表（データのみ） |
| `weather_tables.py` | ダッシュボード用天気表（3 時間・週間・降水確率） |
| `news.py` | NHK RSS の取得・要約・優先順位付け |
| `radar.py` | 雨レーダーのフレーム解決と鮮度連動キャッシュ |
| `typhoon.py` | 台風ブリーフィング（A2S 経由の委譲点） |
| `engine.py` | 常駐監視（ポーリング・SSE 受信・重複排除・通知判定） |
| `notify.py` | 通知チャネル（コンソール・ファイル・Webhook・デスクトップ） |
| `store.py` | SQLite ストア（既読・履歴・保持期間の剪定） |
| `assistant.py` | キーワード Q&A |
| `diagnose.py` | ソース・設定の自己診断 |
| `cli.py` | コマンド実装とダッシュボード（serve） |

データの流れ:

```
A2S ハブ ──REST/SSE──▶ a2s.py ──変換──▶ Event ──▶ engine ──▶ notify / store
気象庁・Open-Meteo ──▶ weather.py ─┘        └─▶ assistant / cli（表示・Q&A）
NHK RSS ──▶ news.py ─┘
RainViewer・気象庁ナウキャスト ──▶ radar.py ──▶ cli ダッシュボード
```

## 3. A2S 連携の契約

- 有効化条件: `a2s_url` と `a2s_token` の両方が設定されたときだけ。
  未設定時はネットワークアクセス自体を行わず空を返す。環境変数
  `AEGIS_A2S_URL` / `AEGIS_A2S_TOKEN` が設定ファイルより優先する。
- REST: `GET /v1/events?kind=&since=&limit=`。`since=` は ISO 8601 でも
  エポック ms でもよい。応答は更新時刻の新しい順。認証は Bearer トークン。
- SSE: `GET /v1/stream`。`ready` フレームより前のフレームは再生分であり、
  記録のみで通知しない。ハートビートはコメント行（約 25 秒間隔）。
  読み取りタイムアウト 90 秒。切断時は 5→10→30→60 秒のバックオフで
  再接続し、最後に受信した `updatedAt` を `since=` として送る。
- 重複排除: `key` + `updatedAt`（DedupTracker、500 件）。REST ポーリング側は
  ストアの既読で排除する。
- 検証: 種別ごとに最小限の形を検証し（`is_a2s_event`）、崩れたイベントは
  静かに落とす。SSE の対象は quake / eew / tsunami / warning の 4 種別で、
  台風は REST のみ（ブリーフィング専用）。

## 4. イベント処理の原則

- **記録してから絞る**: 通知条件に合わないイベントも履歴には残す。
  古い地震・津波・EEW（発生から 1 時間超）は記録のみ。
- **EEW の再通知は格上げ時のみ**: 続報は同一イベント ID で、推定震度が
  保存済みの値を上回ったときだけ再通知する。取消は `-cancel` の別イベント
  として必ず届ける。訓練報は INFO で通知対象外。
- **不明は不明と言う**: 震度・M・深さのセンチネル値（負数・null）は
  「不明」と表示し、実数に見せかけない。深さ 0 は「ごく浅い」という
  判明した値として扱う。天気の欠測は `—` で表示する。
- **警報は地点照合が正**: 官署コードと区域名で照合し、解除・発表なしの
  状態はイベント化しない。未知のコードは落とさずコード表記で残す。

## 5. 旧実装 → 新実装の対応表

| 旧（v1.11.1） | 新（v1.0.0） | 備考 |
| --- | --- | --- |
| `sources_http.py` | `http_client.py` | リトライ方針は同一（5xx・通信エラーは 1s→3s、4xx は即失敗） |
| `sources_common.py` | `geo.py` | 座標・タイル計算のみに純化 |
| `sources_location.py` | `locations.py` | |
| `sources_quake.py` | `convert_quake.py` | 統合ロジックを再実装（種別優先度・30km 近接・地点和集合） |
| `sources_eew.py` | `convert_eew.py` | 同一性（発生時刻＋丸め座標）・段階文言・到達予想を再現 |
| `sources_tsunami.py` | `convert_tsunami.py` | 種別グルーピングを再現 |
| `sources_a2s.py` | `a2s.py` | 検証・重複判定・REST・SSE・変換を 1 モジュールに集約 |
| `sources_weather.py` | `weather.py` | 複合モデル固定・接尾辞キー正規化を再現 |
| （weather 内の定数表） | `weather_codes.py` | データ表を分離（循環 import 回避） |
| `sources_weather_tables.py` | `weather_tables.py` | |
| `sources_news.py` | `news.py` | |
| `sources_radar.py` | `radar.py` | フレームキャッシュの状態遷移を再現 |
| `sources_typhoon.py` | `typhoon.py` | A2S への委譲点として維持 |
| `sources.py`（互換シム） | （廃止） | 内部 API はモジュール直接参照に統一 |
| `engine.py` / `notify.py` / `store.py` / `assistant.py` / `diagnose.py` / `cli.py` / `config.py` / `models.py` | 同名 | 役割は同一、実装は書き直し |

## 6. 判断記録

- **バージョンは 1.0.0 から再開する**（2026-11-28）: 再実装はコードの
  継続ではなく作り直しであり、旧版との混同を避けるため体系をリセット
  した。機能同等であることはテストで担保する。
- **互換シム（旧 sources.py）は持たない**: 旧実装のシムは移行期の都合で
  あり、新規実装ではモジュール直接参照に統一した方が構成が読みやすい。
  外部に公開する互換面は CLI と設定ファイル形式のみとする。
- **SSE 再接続で since= を送る**: 旧実装は再接続のたびに全件再生を受けて
  重複排除に頼っていた。新実装は最後に受信した `updatedAt` を `since=`
  として送り、再生量を抑える。再生分の「記録のみ」規則と重複排除は
  そのまま維持し、取りこぼし時はハブ側の保持分で復旧できる。
- **天気コード表を weather_codes.py に分離する**: 警報コード表は a2s.py
  （警報変換）と weather.py（警報取得）の双方から使うため、データのみの
  モジュールに切り出して循環 import を避けた。
- **標準ライブラリのみを維持する**: 旧実装の方針を踏襲。HTTP・SSE・
  SQLite・XML すべて標準機能で足りており、配布の容易さを優先した。
- **テストはモック前提・時計固定**: ネットワークに触れない単体テストで
  A2S クライアントの振る舞い（SSE 受信・since= 復旧・イベント処理）を
  検証する。時刻依存の判定は now を注入できる形にし、固定時刻で検証
  する（実時刻依存のテストは将来壊れるため）。
