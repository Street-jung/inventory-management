@echo off
chcp 65001 > nul
title 이미 로그인된 내 크롬 브라우저 열기 (연동 모드)

echo ================================================================
echo   [내 크롬 브라우저를 연동 모드(포트 9222)로 실행합니다]
echo ================================================================
echo.
echo 💡 안내:
echo 1. 기존에 열려 있는 모든 크롬 브라우저 창을 먼저 닫아주세요.
echo 2. 잠시 후 크롬이 열리면 쿠팡 윙(wing.coupang.com)에 로그인해 두세요.
echo 3. 그 다음 재고 현황 웹 화면에서 [내 크롬으로 즉시 조회]를 누르면 끝!
echo.
pause

taskkill /F /IM chrome.exe > nul 2>&1
timeout /t 1 > nul

start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
if %errorlevel% neq 0 (
    start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
)
if %errorlevel% neq 0 (
    start "" "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
)

echo ✅ 연동용 크롬 브라우저가 실행되었습니다!
echo    이 창은 닫으셔도 되며, 열린 크롬 창은 닫지 마세요.
timeout /t 3

