# AEGIS — 防災情報クライアント（Python）

AEGIS は、標準ライブラリのみで動く Python 製の防災情報クライアントです。
地震・緊急地震速報（EEW）・津波・気象警報・台風・天気・ニュースを、主に
A.A.S（A2S）ハブから受け取り、コンソール／ログファイル／Webhook
（Discord・汎用 JSON）／デスクトップ通知で知らせます。ローカル
ダッシュボード、キーワード Q&A、自己診断コマンドも備えます。

[English README](README.md) · [設計メモ](DESIGN.md) · [変更履歴](CHANGELOG.md)

| 項目 | 内容 |
| --- | --- |
| バージョン | 1.0.0 |
| 状態 | 2026-11-28 にゼロから再実装（旧実装と機能同等） |
| 稼働先 | ユーザーの端末（Windows / Linux / macOS） |
| 実装 | Python 3.11+、標準ライブラリのみ（外部依存なし） |

## ✨ 機能

- **ハブ優先の取り込み** — A2S の URL とトークンを設定すると、地震・EEW・
  津波・警報をハブから REST（`/v1/events`）と SSE（`/v1/stream`）で受信。
  再接続時は `since=` で復旧し、`key` + `updatedAt` で重複を排除。EEW は
  プッシュ駆動でポーリング遅延なく届きます。
- **段階電文の統合** — 震度速報と震源・震度情報を 1 件に統合（種別優先度、
  震央 30km 近接フォールバック、都道府県最大震度・市区町村別震度）。
  不明な M・深さはセンチネル値として正直に表示します。
- **EEW の扱い** — 続報は同一 ID で、推定震度が上がったときだけ再通知。
  取消は別イベント、訓練報は記録のみで通知しません。段階別の行動ガイドと
  地点別の到達予想（区域別到達時刻／S 波 3.5km/s の推定）付き。
- **地点別の警報** — 気象警報・注意報を地点ごとに官署・区域コードで照合。
  解除分は落とし、未知コードはコード表記で残し、重要度はハブの数値に従います。
- **天気** — 気象庁予報と Open-Meteo（複合モデル
  `jma_seamless,ecmwf_ifs025` を明示固定）。欠測は `None` ではなく `—` 表示。
- **レーダー** — RainViewer の雨タイル＋地理院タイル＋気象庁ナウキャスト。
  鮮度連動のフレームキャッシュ（fresh / cached / extended / stale）。
- **通知** — コンソールとローテーション付きログは常時。Discord Webhook、
  汎用 JSON Webhook、OS デスクトップ通知（Windows トースト / notify-send /
  osascript）は任意。
- **ダッシュボード** — `serve` で 127.0.0.1:8765 にローカル専用
  ダッシュボード（ブリーフィング／履歴 API）。
- **診断** — `diagnose` で全ソースと設定を点検し、終了コード 0/1 を返します。

## 🚀 クイックスタート

```bash
cp config.example.json config.json   # 地点・通知先を編集
python3 -m aegis now                 # 1 回だけブリーフィング表示
python3 -m aegis watch               # 常駐監視
```

## 🧭 コマンド

| コマンド | 説明 |
| --- | --- |
| `now` | 現在のブリーフィング（天気・地震・警報・ニュース）を 1 回表示 |
| `watch` | 常駐監視: EEW 10 秒／地震+津波 20 秒／天気 600 秒ポーリング、A2S の SSE 受信、07:00 / 21:00 に定期ブリーフィング |
| `ask "<質問>"` | キーワード Q&A（津波は？ / 最近の地震は？ / 高崎の天気は？ など） |
| `history [n]` | 記録済みイベントの直近 n 件（既定 10） |
| `serve [--port N]` | 127.0.0.1 のローカルダッシュボード（既定ポート 8765） |
| `test` | セルフチェック: 全ソースを 1 回取得して要約を表示 |
| `test-notify` | 設定済み通知先へテスト通知を送信 |
| `diagnose` | ソースと設定の点検（終了コード 0 = 全て正常） |

## ⚙️ 設定

設定は `config.json`（雛形は `config.example.json`）。A2S ハブは
**オプトイン** で、`a2s_url` と `a2s_token` の両方が設定されたときだけ
使われます。環境変数 `AEGIS_A2S_URL` / `AEGIS_A2S_TOKEN` がファイルより
優先します。トークンは設定ファイルか環境変数からのみ読み、ソースツリーに
実値を書かないでください。

主な設定: `locations[]`（name、lat、lon、`jma_pref_code`、`jma_area_name`、
任意の `jma_city_name`）、`notify_min_severity`（既定 `MEDIUM`）、
`quake_min_scale`（既定 30）、`discord_webhook_url`、`webhook_url`、
`desktop_notify`、`log_file`、`data_dir`。

## 📡 A2S 連携

| 項目 | 振る舞い |
| --- | --- |
| REST | `GET /v1/events?kind=&since=&limit=`、`Authorization: Bearer <token>` |
| SSE | `GET /v1/stream`。`ready` フレームより前は再生分（記録のみ・通知しない）。ハートビートはコメント行。読み取りタイムアウト 90 秒 |
| 再接続 | バックオフ 5 → 10 → 30 → 60 秒。最後に受信した `updatedAt` を `since=` で送る |
| 重複排除 | `key` + `updatedAt`（500 件保持）。EEW は震度が上がったときだけ再通知 |
| SSE の対象種別 | quake / eew / tsunami / warning（台風は REST のみ・ブリーフィング専用） |

## 🔨 ビルド

```bash
./build.sh        # テスト実行後、dist/aegis-<version>.zip を作成
build.bat         # Windows 版
```

テストはネットワーク不要で、コマンド一発で実行できます:

```bash
python3 tests/test_aegis.py    # または pytest
```

## 📄 ライセンス

MIT — [LICENSE](LICENSE) を参照。
