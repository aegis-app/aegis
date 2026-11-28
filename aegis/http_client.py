# ============================================================
# 🌐 HTTP client — リトライ付き取得基盤
# ============================================================
"""The one HTTP fetch path used by every source.

🔁 Retry policy: network errors / timeouts and HTTP 5xx are
retried up to twice (waits in :data:`FETCH_RETRY_DELAYS`); HTTP
4xx fails immediately because retrying cannot change the result.
Responses are requested gzip-compressed.

🧪 ``FETCH_RETRY_DELAYS`` is a module constant so tests can
replace it (and ``fetch_json`` / ``fetch_text``) without touching
the network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import __version__

UA = f"AEGIS/{__version__} (autonomous disaster assistant; +local)"
TIMEOUT = 15

# 🔁 リトライの待機間隔（秒）。試行回数は 初回 + len(FETCH_RETRY_DELAYS) 回
FETCH_RETRY_DELAYS = (1.0, 3.0)


def fetch_raw(url: str, accept: str = "application/json", headers: dict | None = None) -> bytes:
    """GET ``url`` and return the raw body, with the retry policy above."""
    import time as _time
    last: Exception | None = None
    for attempt in range(len(FETCH_RETRY_DELAYS) + 1):
        try:
            req_headers = {"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip"}
            if headers:
                # 📝 追加ヘッダー（A2S の Authorization 等）。既定より優先する
                req_headers.update(headers)
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip as _gzip
                    raw = _gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < len(FETCH_RETRY_DELAYS):
            _time.sleep(FETCH_RETRY_DELAYS[attempt])
    raise last if last is not None else RuntimeError("fetch failed")


def fetch_json(url: str, headers: dict | None = None):
    """GET ``url`` and parse the body as JSON."""
    return json.loads(fetch_raw(url, headers=headers).decode("utf-8"))


def fetch_text(url: str) -> str:
    """GET ``url`` and decode the body as text (RSS/XML feeds)."""
    return fetch_raw(url, accept="application/rss+xml, application/xml, text/xml, */*").decode("utf-8", errors="replace")
