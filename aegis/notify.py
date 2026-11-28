# ============================================================
# 🔔 Notify — コンソール / ファイル / Webhook / デスクトップ通知
# ============================================================
"""Notification sinks and dispatch.

📌 Console + file are always on. Discord / generic webhooks are
enabled by setting their URLs; the desktop notifier is opt-in
(``desktop_notify``) and silently skips on machines without an
OS notification command. One sink failing never stops the
others (see :func:`dispatch`).
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path

from . import __version__
from .models import Event


# ------------------------------------------------------------
# 🖨️ コンソール / ファイル
# ------------------------------------------------------------
class ConsoleNotifier:
    """Print every notification to stdout."""

    def send(self, ev: Event):
        print(ev.format_message(), flush=True)
        print("-" * 60, flush=True)


LOG_ROTATE_MAX_BYTES = 5 * 1024 * 1024  # alerts.log のローテーション閾値（5MB）


def rotate_log_if_needed(path: Path, max_bytes: int = LOG_ROTATE_MAX_BYTES) -> bool:
    """ログが上限サイズ以上なら path.1 へリネームする（1世代ローテーション）。
    ローテーションしたら True。失敗しても例外を出さず False を返す。"""
    try:
        if max_bytes and max_bytes > 0 and path.exists() and path.stat().st_size >= max_bytes:
            backup = path.with_name(path.name + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
            return True
    except Exception:
        pass
    return False


class FileNotifier:
    """Append every notification to the alert log file."""

    def __init__(self, log_path: str | Path, max_bytes: int = LOG_ROTATE_MAX_BYTES):
        self.path = Path(log_path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, ev: Event):
        rotate_log_if_needed(self.path, self.max_bytes)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(ev.format_message() + "\n" + "-" * 60 + "\n")


# ------------------------------------------------------------
# 🌐 Webhook（Discord / 汎用）
# ------------------------------------------------------------
class WebhookNotifier:
    """Discord Webhook / 汎用 Webhook 共通。url が空なら無効。"""

    def __init__(self, url: str, kind: str = "discord"):
        self.url = url
        self.kind = kind

    def send(self, ev: Event):
        if not self.url:
            return
        if self.kind == "discord":
            payload = {"content": ev.format_message()[:1900]}
        else:
            payload = ev.to_dict()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": f"AEGIS/{__version__}"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[notify] webhook failed: {e}", flush=True)


# ------------------------------------------------------------
# 🔔 デスクトップ通知（OS標準。追加モジュールなし）
# ------------------------------------------------------------
def normalize_system(name: str | None) -> str:
    """OS名を windows / darwin / linux / other に正規化する（純粋関数）。"""
    s = (name or "").strip().lower()
    if s.startswith("win"):
        return "windows"
    if s in ("darwin", "macos", "mac os", "osx"):
        return "darwin"
    if s.startswith("linux"):
        return "linux"
    return "other"


def current_system() -> str:
    return normalize_system(platform.system())


def _flatten(text: str) -> str:
    """通知用に改行を空白へ寄せ、前後の空白を落とす。"""
    return " ".join((text or "").split())


def desktop_text(ev: Event) -> tuple[str, str]:
    """Event からデスクトップ通知の (タイトル, 本文) を作る（純粋関数）。
    本文は本文先頭＋行動ガイド（長すぎる場合は切り詰め）。"""
    title = _flatten(f"{ev.severity.emoji} {ev.title}")
    body = _flatten(ev.body)
    if ev.action_guide:
        guide = _flatten(ev.action_guide)
        body = f"{body} ▶ {guide}" if body else guide
    if len(body) > 240:
        body = body[:240].rstrip() + "…"
    return title, body


def _ps_single_quote(text: str) -> str:
    """PowerShell のシングルクォート文字列用エスケープ (' → '')。"""
    return text.replace("'", "''")


def _applescript_escape(text: str) -> str:
    """AppleScript のダブルクォート文字列用エスケープ。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def desktop_command(system: str, title: str, body: str) -> list[str] | None:
    """OS別のデスクトップ通知コマンドを組み立てる（純粋関数）。
    対応外の OS では None。実行可否（コマンドの有無）は呼び出し側で確認する。"""
    system = normalize_system(system)
    title = _flatten(title)
    body = _flatten(body)
    if system == "linux":
        return ["notify-send", title, body]
    if system == "darwin":
        script = (
            f'display notification "{_applescript_escape(body)}" '
            f'with title "{_applescript_escape(title)}"'
        )
        return ["osascript", "-e", script]
    if system == "windows":
        # ➕ 追加モジュールなし: WinRT ToastNotificationManager を PowerShell から使う
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null; "
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$texts = $t.GetElementsByTagName('text'); "
            f"$texts.Item(0).AppendChild($t.CreateTextNode('{_ps_single_quote(title)}')) > $null; "
            f"$texts.Item(1).AppendChild($t.CreateTextNode('{_ps_single_quote(body)}')) > $null; "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($t); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AEGIS').Show($toast)"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return None


# 📁 OSごとに必要な実行ファイル
_DESKTOP_BINARIES = {"linux": "notify-send", "darwin": "osascript", "windows": "powershell"}


def desktop_available(system: str) -> bool:
    """その OS の通知コマンドが実際に使えるか（対応外OS・コマンド無しは False）。"""
    binary = _DESKTOP_BINARIES.get(normalize_system(system))
    if not binary:
        return False
    if shutil.which(binary):
        return True
    # 📝 Windows では powershell.exe 表記でも探す
    return binary == "powershell" and bool(shutil.which("powershell.exe"))


class DesktopNotifier:
    """OS標準のデスクトップ通知。通知手段が無い環境では静かにスキップする。"""

    def __init__(self, system: str | None = None):
        self.system = normalize_system(system) if system is not None else current_system()

    def command_for(self, ev: Event) -> list[str] | None:
        title, body = desktop_text(ev)
        return desktop_command(self.system, title, body)

    def send(self, ev: Event):
        cmd = self.command_for(ev)
        if cmd is None:
            return  # 対応外OS: 静かにスキップ
        if not desktop_available(self.system):
            return  # notify-send 等が無い環境: 静かにスキップ
        try:
            subprocess.run(cmd, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass  # 通知失敗で他の通知先・監視ループに影響させない


# ------------------------------------------------------------
# 🧩 組み立てと配信
# ------------------------------------------------------------
def build_notifiers(cfg: dict, root) -> list:
    """設定から通知先リストを組み立てる。"""
    notifiers = [ConsoleNotifier(), FileNotifier(root / cfg.get("log_path", "data/alerts.log"))]
    if cfg.get("discord_webhook_url"):
        notifiers.append(WebhookNotifier(cfg["discord_webhook_url"], "discord"))
    if cfg.get("generic_webhook_url"):
        notifiers.append(WebhookNotifier(cfg["generic_webhook_url"], "generic"))
    if cfg.get("desktop_notify"):
        notifiers.append(DesktopNotifier())
    return notifiers


def dispatch(notifiers: list, ev: Event):
    """全通知先へ送る。1つの失敗で他の通知先まで止めない。"""
    for n in notifiers:
        try:
            n.send(ev)
        except Exception as e:
            print(f"[notify] {type(n).__name__} failed: {e}", flush=True)
