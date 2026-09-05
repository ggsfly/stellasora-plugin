@echo off
rem ============================================================
rem  Stella Sora CN-EN dictionary updater
rem  - If local StellaSoraData git clone exists: git pull + local update
rem  - Otherwise: fetch directly from GitHub (remote mode, needs Git)
rem  Requires: python on PATH; git on PATH (remote mode only)
rem ============================================================

chcp 65001 >nul
cd /d %~dp0

echo ==============================================
echo  Stella Sora CN-EN dictionary update
echo ==============================================
echo.

rem Usage: update_dict.bat [path\to\StellaSoraData]
set LOCAL_DATA=%~1
if not defined LOCAL_DATA set LOCAL_DATA=..\StellaSoraData

if exist "%LOCAL_DATA%\EN\language\en_US" (
    if exist "%LOCAL_DATA%\.git" (
        echo [1/3] git pull latest game data ...
        git -C "%LOCAL_DATA%" pull --ff-only
        if errorlevel 1 echo [warn] git pull failed, using local data as-is
        echo.
        echo [2/3] incremental dictionary update from local clone ...
        python tools\update_dict.py --mode local --source "%LOCAL_DATA%"
        goto runtest
    )
)

echo [1/2] local clone not found, fetching from GitHub (remote mode) ...
python tools\update_dict.py --mode remote

:runtest
echo.
echo [3/3] running dictionary consistency test ...
python tests\test_dict.py

echo.
echo Done.
pause
