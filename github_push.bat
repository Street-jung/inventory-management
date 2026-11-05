@echo off
chcp 65001 > nul
title 일쩜오도 재고현황 - GitHub 원클릭 동기화
setlocal enabledelayedexpansion

echo =================================================================
echo   [🌱 일쩜오도 재고현황] GitHub 자동 연동 및 푸시 도구
echo   저장소: https://github.com/Street-jung/inventory-management.git
echo =================================================================
echo.

set GIT_EXE="C:\Users\ethan\AppData\Local\Microsoft\WinGet\Packages\Git.MinGit_Microsoft.Winget.Source_8wekyb3d8bbwe\cmd\git.exe"
set GH_EXE="C:\Program Files\GitHub CLI\gh.exe"

echo 1단계: GitHub 로그인 상태를 확인합니다...
%GH_EXE% auth status >nul 2>&1
if errorlevel 1 (
    echo.
    echo 🔑 GitHub 로그인이 필요합니다.
    echo 웹 브라우저가 열리면 화면에 표시되는 일회용 코드를 확인하고 [Authorize]를 눌러주세요.
    echo.
    %GH_EXE% auth login -h github.com -p https --web
    if errorlevel 1 (
        echo [오류] 로그인에 실패했습니다. 다시 시도해주세요.
        pause
        exit /b
    )
) else (
    echo ✅ 이미 GitHub에 로그인되어 있습니다.
)

echo.
echo 2단계: Git 자격 증명을 설정합니다...
%GH_EXE% auth setup-git

echo.
echo 3단계: 최신 코드를 Street-jung/inventory-management 저장소로 푸시합니다...
%GIT_EXE% remote remove origin >nul 2>&1
%GIT_EXE% remote add origin https://github.com/Street-jung/inventory-management.git
%GIT_EXE% branch -M main
%GIT_EXE% push -u origin main

echo.
echo =================================================================
echo 🎉 축하합니다! 깃허브(GitHub) 저장이 성공적으로 완료되었습니다!
echo 👉 https://github.com/Street-jung/inventory-management
echo =================================================================
echo.
pause
