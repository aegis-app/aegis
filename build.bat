@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  AEGIS (Python) — ワンコマンドビルド
rem  使い方: build.bat を実行するだけ
rem    1) 単体テスト (tests\test_aegis.py) を実行
rem    2) 配布 zip を dist\aegis-<version>.zip に作成
rem  必要なもの: Python 3 (標準ライブラリのみ)
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [エラー] python が見つかりません。Python 3 をインストールしてください。
    pause
    exit /b 1
)

echo === テストを実行します ===
python tests\test_aegis.py
if errorlevel 1 (
    echo [エラー] テストが失敗しました。上の出力を確認してください。
    pause
    exit /b 1
)

echo.
echo === 配布 zip を作成します ===
python tools\make_dist_zip.py
if errorlevel 1 (
    echo [エラー] zip の作成に失敗しました。上の出力を確認してください。
    pause
    exit /b 1
)

echo.
echo ビルド完了。成果物は dist フォルダ内の aegis-*.zip です。
pause
endlocal
