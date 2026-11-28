# ============================================================
# ⚙️ Config — config.json の読み込みと既定値
# ============================================================
"""Configuration loading.

📌 The config file is ``config.json`` next to the package's
parent directory. Missing keys fall back to
:data:`DEFAULT_CONFIG`; a broken file never stops the watcher
(the defaults are used instead).

🔐 Secrets: the A2S token and webhook URLs live in the config
file or — preferably — in the ``AEGIS_A2S_URL`` /
``AEGIS_A2S_TOKEN`` environment variables, which take priority.
No real value is ever baked into the defaults.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG = {
    # 📍 監視地点（初期設定は通学動線の両端: 高崎と大宮）
    "locations": [
        {"name": "高崎", "lat": 36.3219, "lon": 139.0023, "jma_pref_code": "100000", "jma_area_name": "南部", "city": "高崎市"},
        {"name": "大宮", "lat": 35.9064, "lon": 139.6239, "jma_pref_code": "110000", "jma_area_name": "南部", "city": "さいたま市"},
    ],
    "poll_seconds_quake": 20,
    "poll_seconds_eew": 10,  # EEW だけ高頻度にチェックする間隔（秒）
    "poll_seconds_weather": 600,
    "notify_min_severity": "MEDIUM",  # INFO|LOW|MEDIUM|HIGH|CRITICAL
    "quake_min_scale": 30,  # 震度コード: 30 = 震度3
    # 📡 A.A.S (A2S) のベース URL と認証トークン。地震・津波・EEW・
    # 気象警報・台風の唯一の取得経路で、両方を指定したときだけ有効。
    # 環境変数 AEGIS_A2S_URL / AEGIS_A2S_TOKEN が優先する
    "a2s_url": "",
    "a2s_token": "",
    "discord_webhook_url": "",
    "generic_webhook_url": "",
    "desktop_notify": False,  # デスクトップ通知（OS標準）を使うか。既定 OFF
    "morning_briefing": "07:00",
    "evening_briefing": "21:00",
    "db_path": "data/aegis.db",
    "log_path": "data/alerts.log",
    "retention_days": 90,  # 重複排除（seen）と履歴（history）の保持期間（日）
}


def load_config(path: Path | None = None) -> dict:
    """Read the config file over the defaults and return the merge."""
    p = path or DEFAULT_CONFIG_PATH
    cfg = dict(DEFAULT_CONFIG)
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            # 👁️ 壊れた config.json で監視を止めない: 読めなければ既定値で起動する
            pass
    return cfg


def save_default_config(path: Path | None = None) -> Path:
    """Write the defaults as ``config.json`` if it does not exist yet."""
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        p.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
