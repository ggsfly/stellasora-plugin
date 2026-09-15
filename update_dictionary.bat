@echo off
rem ============================================================
rem  Stella Sora dictionary updater (CN-EN)
rem  - If a local ss-data git clone exists: git pull + local mode
rem  - Otherwise: sparse-clone language dirs from GitHub (remote mode)
rem  - Python: uses MaiBot root .venv (../../.venv, where maibot_sdk lives)
rem  - Data: written to MaiBot host-granted dirs
rem      dict/names  -> <MaiBot>\data\plugins\ggsfly.stellasora-plugin\
rem      offline     -> <MaiBot>\data\plugins\ggsfly.stellasora-plugin\offline\
rem      cache       -> <MaiBot>\temp\plugins\ggsfly.stellasora-plugin\cache\
rem    Plugin source dir stays clean; no runtime writes land there.
rem  Requires: MaiBot root .venv; git on PATH (remote mode only)
rem
rem  Usage: update_dictionary.bat [path\to\ss-data]
rem         update_dictionary.bat --direct     (force direct connection)
rem
rem  Proxy: uses http://127.0.0.1:7890 by default; override with the
rem  HTTPS_PROXY environment variable, or pass --direct to bypass proxy.
rem ============================================================

chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d %~dp0

echo ==============================================
echo  Stella Sora CN-EN dictionary update
echo ==============================================
echo.

rem ---- resolve MaiBot root & host data/cache dirs ---------------------
rem bat 位于 plugins\<plugin>\ 下，MaiBot 根目录即上两级；
rem 插件必须用宿主 .venv 运行（maibot_sdk 等依赖只在其中）。
set "MAIBOT_ROOT=%~dp0..\.."
if not exist "%MAIBOT_ROOT%\.venv\Scripts\python.exe" (
    echo [error] MaiBot root not found: %MAIBOT_ROOT%
    echo         请确认插件位于 MaiBot\plugins\ 目录下
    pause
    exit /b 1
)
set "PY=%MAIBOT_ROOT%\.venv\Scripts\python.exe"
set "DATA_PLUGIN=%MAIBOT_ROOT%\data\plugins\ggsfly.stellasora-plugin"
set "CACHE_PLUGIN=%MAIBOT_ROOT%\temp\plugins\ggsfly.stellasora-plugin\cache"
if not exist "%DATA_PLUGIN%" mkdir "%DATA_PLUGIN%"
if not exist "%CACHE_PLUGIN%" mkdir "%CACHE_PLUGIN%"
echo [info] using venv python: %PY%
echo [info] data dir: %DATA_PLUGIN%
echo [info] cache dir: %CACHE_PLUGIN%
echo.

rem ---- resolve proxy argument --------------------------------
rem PROXY_ARGS is empty by default: update_dict.py auto-detects
rem HTTPS_PROXY env var, else falls back to 127.0.0.1:7890.
set "PROXY_ARGS="
if "%~1"=="--direct" (
    set "PROXY_ARGS=--proxy="
    echo [info] direct connection mode (proxy disabled)
    echo.
    shift
)

rem ---- locate local ss-data clone ---------------------
rem first positional arg after optional --direct
set "LOCAL_DATA=%~1"
if not defined LOCAL_DATA set LOCAL_DATA=..\ss-data

if exist "%LOCAL_DATA%\EN\language\en_US" (
    if exist "%LOCAL_DATA%\.git" (
        echo [1/3] git pull latest game data ...
        rem local pull honors system proxy env vars; add HTTPS_PROXY hint if unset
        if not defined HTTPS_PROXY if not defined HTTP_PROXY (
            set "HTTPS_PROXY=http://127.0.0.1:7890"
            set "HTTP_PROXY=http://127.0.0.1:7890"
        )
        git -C "%LOCAL_DATA%" pull --ff-only
        if errorlevel 1 echo [warn] git pull failed, using local data as-is
        echo.
        echo [2/3] incremental dictionary update from local clone ...
        "%PY%" tools\update_dict.py --mode local --source "%LOCAL_DATA%" --output "%DATA_PLUGIN%"
        goto sync_offline
    )
)

echo [1/3] local clone not found, fetching from GitHub (remote mode) ...
"%PY%" tools\update_dict.py --mode remote --output "%DATA_PLUGIN%" %PROXY_ARGS%

:sync_offline
echo.
echo [3/3] syncing offline guides / presets / ss-data / leaderboard ...
"%PY%" tools\sync_data.py --all --offline-dir "%DATA_PLUGIN%\offline" --cache-dir "%CACHE_PLUGIN%" %PROXY_ARGS%

:runtest
echo.
echo running dictionary and offline data consistency tests ...
"%PY%" tests\test_all.py A B C D M N

echo.
echo Done.
pause