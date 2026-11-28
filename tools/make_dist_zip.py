#!/usr/bin/env python3
"""📦 AEGIS (Python) の配布 zip を dist/ に作る。

build.sh / build.bat から呼ばれる。含めるもの:
  - aegis/ パッケージ (.py のみ)
  - run.sh, config.example.json, README.md, DESIGN.md
含めないもの:
  - config.json (個人設定), data/ (実行時データ), __pycache__, tests/
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aegis import __version__  # noqa: E402


def main() -> int:
    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"aegis-{__version__}.zip")

    files = []
    pkg_dir = os.path.join(ROOT, "aegis")
    for name in sorted(os.listdir(pkg_dir)):
        if name.endswith(".py"):
            files.append((os.path.join(pkg_dir, name), f"aegis/{name}"))
    for name in ("run.sh", "config.example.json", "README.md", "DESIGN.md"):
        files.append((os.path.join(ROOT, name), name))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in files:
            z.write(src, arc)

    print(f"作成しました: {zip_path}")
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
