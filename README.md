# AEGIS — Disaster Alert Client (Python)

AEGIS is a standard-library-only Python client that receives Japanese
disaster information — earthquakes, earthquake early warnings (EEW),
tsunamis, weather warnings, typhoons, weather and news — primarily from the
A.A.S (A2S) hub, and notifies you on the console, in a log file, via webhooks
(Discord / generic JSON) or as a desktop notification. It also provides a
local dashboard, a keyword Q&A mode and a self-diagnosis command.

[日本語版 README](README.ja.md) · [Design notes](DESIGN.md) · [Changelog](CHANGELOG.md)

| Item | Value |
| --- | --- |
| Version | 1.0.0 |
| Status | Rebuilt from scratch (2026-11-28); functionally equivalent to the previous implementation |
| Runs on | The user's own devices (Windows / Linux / macOS) |
| Implementation | Python 3.11+, standard library only (no third-party dependencies) |

## ✨ Features

- **Hub-first intake** — when an A2S hub URL and token are configured, quake /
  EEW / tsunami / warning events are received from the hub over REST
  (`/v1/events`) and SSE (`/v1/stream`), with `since=` recovery on reconnect
  and key + `updatedAt` deduplication. The EEW stream is push-driven, so
  alerts arrive without polling delay.
- **Staged telegram merging** — prompt and detailed quake telegrams are merged
  into one event (type priority, 30 km proximity fallback, prefecture maxima
  and per-city scales), with sentinel values (unknown magnitude / depth)
  rendered honestly instead of as fake numbers.
- **EEW handling** — follow-up reports share one event ID and re-notify only
  when the estimated scale grows; cancellations are separate events; training
  reports are recorded but never notified; tiered action guidance and
  per-location arrival estimates (JMA area times, S-wave fallback at 3.5 km/s).
- **Warnings per location** — JMA warnings are matched to each configured
  location by office and area codes; released warnings are dropped, unknown
  codes are kept with their code label, and severity follows the hub's
  numeric severity.
- **Weather** — JMA forecast plus Open-Meteo with an explicitly pinned
  composite model (`jma_seamless,ecmwf_ifs025`); missing values render as `—`,
  never as `None`.
- **Radar** — RainViewer rain tiles over GSI base tiles plus JMA nowcast,
  with freshness-linked frame caching (fresh / cached / extended / stale).
- **Notifications** — console and rotating log file always; Discord webhook,
  generic JSON webhook and OS desktop notifications (Windows toast /
  `notify-send` / `osascript`) optionally.
- **Dashboard** — `serve` starts a loopback-only web dashboard (briefing and
  history APIs) on `127.0.0.1:8765`.
- **Diagnosis** — `diagnose` probes every source and the configuration and
  exits 0/1, so scripts can gate on it.

## 🚀 Quick start

```bash
cp config.example.json config.json   # edit locations / notifications as needed
python3 -m aegis now                 # one-shot briefing
python3 -m aegis watch               # resident monitoring
```

Windows: use `run.bat`-style invocation `python -m aegis ...`, or the
one-command build below to produce a distributable zip.

## 🧭 Commands

| Command | Description |
| --- | --- |
| `now` | Print the current briefing (weather, quakes, warnings, news) once |
| `watch` | Resident monitoring: polls EEW 10 s / quake+tsunami 20 s / weather 600 s, receives the A2S SSE stream, briefs at 07:00 and 21:00 |
| `ask "<question>"` | Keyword Q&A （津波は？ / 最近の地震は？ / 高崎の天気は？ …) |
| `history [n]` | Show the last n recorded events (default 10) |
| `serve [--port N]` | Local dashboard on 127.0.0.1 (default port 8765) |
| `test` | Self-check: fetches every source once and prints a summary |
| `test-notify` | Send a test notification through the configured notifiers |
| `diagnose` | Probe sources and configuration; exit code 0 = all healthy |

## ⚙️ Configuration

Configuration lives in `config.json` (see `config.example.json`). The A2S hub
is **opt-in**: it is used only when both `a2s_url` and `a2s_token` are set.
The environment variables `AEGIS_A2S_URL` / `AEGIS_A2S_TOKEN` override the
file. Tokens are read from configuration or the environment only — never
hard-code a real token into the source tree.

Key settings: `locations[]` (name, lat, lon, `jma_pref_code`, `jma_area_name`,
optional `jma_city_name`), `notify_min_severity` (default `MEDIUM`),
`quake_min_scale` (default 30), `discord_webhook_url`, `webhook_url`,
`desktop_notify`, `log_file`, `data_dir`.

## 📡 A2S integration

| Aspect | Behaviour |
| --- | --- |
| REST | `GET /v1/events?kind=&since=&limit=`, `Authorization: Bearer <token>` |
| SSE | `GET /v1/stream`; frames before the `ready` frame are replay (recorded, never notified); heartbeats are comments; read timeout 90 s |
| Reconnect | Backoff 5 → 10 → 30 → 60 s; the last received `updatedAt` is sent as `since=` |
| Dedupe | By `key` + `updatedAt` (500 entries); EEW re-notifies only on scale growth |
| Kinds over SSE | quake, eew, tsunami, warning (typhoon is REST-only, briefing-only) |

## 🔨 Build

```bash
./build.sh        # runs the tests, then builds dist/aegis-<version>.zip
build.bat         # same on Windows
```

Tests are network-free and run with a single command:

```bash
python3 tests/test_aegis.py    # or: pytest
```

## 📄 License

MIT — see [LICENSE](LICENSE).
