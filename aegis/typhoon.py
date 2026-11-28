# ============================================================
# 🌀 Typhoon — 台風情報（取得は A2S）
# ============================================================
"""Typhoon information, fetched through A.A.S only.

The hub generates the display lines; this module is just the
delegation point so callers have a stable import. When no
typhoon exists (or A2S is unconfigured) the result is None and
briefings simply omit the section.
"""
from __future__ import annotations

from .models import Event


def fetch_typhoon() -> Event | None:
    """台風を A2S の REST（/v1/events?kind=typhoon）から取得する。
    実体は a2s.fetch_typhoon で、循環参照を避けるため遅延 import する。"""
    from .a2s import fetch_typhoon as _fetch
    return _fetch()
