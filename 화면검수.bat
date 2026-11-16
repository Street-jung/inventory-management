@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0scripts\.wing_deps;%PYTHONPATH%"
set "XRAY_PYTHON=%UI_XRAY_PYTHON%"
if defined XRAY_PYTHON goto run
set "XRAY_PYTHON=C:\Program Files\LM Studio\resources\app\.webpack\bin\extensions\backends\vendor\_amphibian\cpython3.11-win-x86@6\python.exe"
if exist "%XRAY_PYTHON%" goto run
set "XRAY_PYTHON=python"
:run
"%XRAY_PYTHON%" -c "import playwright.sync_api" >nul 2>&1
if errorlevel 1 (
    "%XRAY_PYTHON%" -m pip install --target "%~dp0scripts\.wing_deps" "playwright>=1.49,<2"
    if errorlevel 1 goto fail
)
"%XRAY_PYTHON%" -m playwright install chromium
if errorlevel 1 goto fail
"%XRAY_PYTHON%" "%~dp0scripts\ui_xray.py" --all-tabs %*
set "XRAY_RESULT=%ERRORLEVEL%"
start "" explorer.exe "%~dp0ui_preview"
if not "%XRAY_RESULT%"=="0" goto fail
echo UI snapshots and report are ready in ui_preview.
exit /b 0
:fail
echo UI X-Ray failed. Check the message above and ui_preview\ui_report_tabs.md.
exit /b 1
