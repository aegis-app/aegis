#!/usr/bin/env bash
# AEGIS 起動スクリプト
# 使い方: ./run.sh [now|watch|serve|ask ...]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m aegis "${@:-now}"
