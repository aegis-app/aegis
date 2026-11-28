#!/usr/bin/env bash
# ============================================================
#  AEGIS (Python) — ワンコマンドビルド
#  使い方: ./build.sh
#    1) 単体テスト (tests/test_aegis.py) を実行
#    2) 配布 zip を dist/aegis-<version>.zip に作成
#  必要なもの: Python 3 (標準ライブラリのみ)
#  Windows では build.bat を使ってください。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "=== テストを実行します ==="
python3 tests/test_aegis.py

echo
echo "=== 配布 zip を作成します ==="
python3 tools/make_dist_zip.py

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' aegis/__init__.py)"
echo
echo "ビルド完了。成果物: $(pwd)/dist/aegis-${VERSION}.zip"
