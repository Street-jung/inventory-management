@echo off
chcp 65001 > nul
title 일쩜오도 재고현황 - GitHub 저장 도구
setlocal enabledelayedexpansion

echo =================================================================
echo   [🌱 일쩜오도 재고현황] GitHub 원클릭 업로드 도구
echo =================================================================
echo.

set GIT_EXE="C:\Users\ethan\AppData\Local\Microsoft\WinGet\Packages\Git.MinGit_Microsoft.Winget.Source_8wekyb3d8bbwe\cmd\git.exe"
set GH_EXE="C:\Program Files\GitHub CLI\gh.exe"

echo 1. 기존에 생성해둔 GitHub 저장소 URL로 바로 올리기
echo 2. GitHub CLI로 새 저장소 자동 생성해서 올리기 (추천)
echo.
set /p CHOICE="원하시는 방법의 번호를 입력하세요 (1 또는 2): "

if "%CHOICE%"=="1" (
    echo.
    set /p REPO_URL="GitHub 저장소 주소를 붙여넣어주세요 (예: https://github.com/내아이디/저장소이름.git): "
    if "!REPO_URL!"=="" (
        echo [오류] URL이 입력되지 않았습니다.
        pause
        exit /b
    )
    echo.
    echo 🚀 GitHub 원격 저장소를 연결하고 푸시합니다...
    %GIT_EXE% remote remove origin >nul 2>&1
    %GIT_EXE% remote add origin !REPO_URL!
    %GIT_EXE% branch -M main
    %GIT_EXE% push -u origin main
    echo.
    echo 🎉 업로드가 완료되었습니다!
    pause
    exit /b
)

if "%CHOICE%"=="2" (
    echo.
    echo 🌐 GitHub 로그인을 진행합니다... (웹 브라우저가 열리면 승인 버튼을 눌러주세요)
    %GH_EXE% auth login --web -h github.com -p https
    echo.
    echo 🚀 GitHub에 'iljeomodo-inventory' 저장소를 자동 생성하고 푸시합니다...
    %GH_EXE% repo create iljeomodo-inventory --public --source=. --remote=origin --push
    echo.
    echo 🎉 성공적으로 GitHub에 저장되었습니다!
    pause
    exit /b
)

echo 잘못된 입력입니다.
pause
