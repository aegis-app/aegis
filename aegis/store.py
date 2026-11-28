# ============================================================
# 🗄️ Store — SQLite による重複排除と履歴
# ============================================================
"""SQLite-backed seen/history storage.

📌 ``seen`` remembers which events were already processed (with
the highest notified EEW scale, for the escalation re-notify
rule); ``history`` keeps the notification log for ``aegis
history`` and the dashboard. Rows older than the retention
period are pruned at startup and then at most once a day.

🌐 The connection is thread-safe (the dashboard server reads
history from its own threads) and uses WAL + busy_timeout.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

RETENTION_DAYS_DEFAULT = 90  # seen / history の保持期間（日）
PRUNE_INTERVAL_SECONDS = 24 * 3600  # 書き込み時の prune チェック間隔（1日）


class Store:
    """Dedupe + history store over a single SQLite database file."""

    def __init__(self, db_path: str | Path, retention_days: int = RETENTION_DAYS_DEFAULT):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.retention_days = max(1, int(retention_days))
        except Exception:
            self.retention_days = RETENTION_DAYS_DEFAULT
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        # 📝 WAL で読み書きの競合を減らし、ロック待ちは busy_timeout で吸収する
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS seen (
                event_id TEXT PRIMARY KEY,
                kind TEXT, title TEXT, severity TEXT, notified_at TEXT DEFAULT (datetime('now','localtime'))
            )"""
        )
        # 🚨 EEW の上方修正再通知用に、通知済みの推定最大震度（震度コード）を
        # seen に保持する。既存 DB に列が無ければ追加する
        seen_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(seen)").fetchall()]
        if "scale" not in seen_cols:
            self.conn.execute("ALTER TABLE seen ADD COLUMN scale INTEGER")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT, kind TEXT, title TEXT, body TEXT, severity TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )"""
        )
        self.conn.commit()
        self._last_prune = 0.0
        # 🚀 起動時に1回、保持期間を過ぎた行を削除する（肥大化対策）
        self.prune()

    def prune(self, retention_days: int | None = None) -> dict:
        """保持期間を過ぎた seen / history の行を削除し、削除件数を返す。"""
        days = self.retention_days if retention_days is None else max(1, int(retention_days))
        modifier = f"-{days} days"
        with self._lock:
            c1 = self.conn.execute(
                "DELETE FROM seen WHERE notified_at < datetime('now','localtime',?)", (modifier,)
            )
            c2 = self.conn.execute(
                "DELETE FROM history WHERE created_at < datetime('now','localtime',?)", (modifier,)
            )
            self.conn.commit()
            self._last_prune = time.time()
            return {"seen": c1.rowcount, "history": c2.rowcount}

    def _maybe_prune(self):
        """前回 prune から1日以上経っていれば prune する（書き込み経路用）。"""
        if time.time() - self._last_prune >= PRUNE_INTERVAL_SECONDS:
            self.prune()

    def is_seen(self, event_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("SELECT 1 FROM seen WHERE event_id=?", (event_id,))
            return cur.fetchone() is not None

    def seen_scale(self, event_id: str) -> int | None:
        """その event_id で処理済みの最大の推定震度（震度コード）を返す。
        未処理なら None、処理済みだが scale 未記録なら -1。"""
        with self._lock:
            cur = self.conn.execute("SELECT scale FROM seen WHERE event_id=?", (event_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return int(row[0]) if row[0] is not None else -1

    def mark_seen(self, event_id: str, kind: str, title: str, severity: str, scale: int | None = None):
        self._maybe_prune()
        with self._lock:
            # 🚨 再通知（EEW 上方修正など）のときは scale を大きい方へ更新する
            self.conn.execute(
                """INSERT INTO seen(event_id, kind, title, severity, scale) VALUES (?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET
                     title=excluded.title, severity=excluded.severity,
                     scale=MAX(COALESCE(seen.scale, -1), COALESCE(excluded.scale, -1))""",
                (event_id, kind, title, severity, scale),
            )
            self.conn.commit()

    def add_history(self, event_id: str, kind: str, title: str, body: str, severity: str):
        self._maybe_prune()
        with self._lock:
            self.conn.execute(
                "INSERT INTO history(event_id, kind, title, body, severity) VALUES (?,?,?,?,?)",
                (event_id, kind, title, body, severity),
            )
            self.conn.commit()

    def recent_history(self, limit: int = 20) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT event_id, kind, title, body, severity, created_at FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            cols = ["event_id", "kind", "title", "body", "severity", "created_at"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self):
        self.conn.close()
