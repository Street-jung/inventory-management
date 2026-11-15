"""Auto-sync web_dashboard/dashboard_data.json to GitHub repository for Render deployment."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def log(msg: str, log_file: Path | None = None) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now_str}] {msg}"
    print(formatted)
    if log_file and log_file.parent.exists():
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except Exception:
            pass


def find_git_executable() -> str:
    # Check default PATH first
    git_cmd = shutil.which("git")
    if git_cmd:
        return git_cmd

    # Known Winget MinGit path on this system
    fallback = Path(r"C:\Users\ethan\AppData\Local\Microsoft\WinGet\Packages\Git.MinGit_Microsoft.Winget.Source_8wekyb3d8bbwe\cmd\git.exe")
    if fallback.exists():
        return str(fallback)

    return "git"


def sync_dashboard(
    project_root: Path | None = None,
    repo_dir: Path | None = None,
    branch: str = "main",
) -> int:
    project_root = project_root or Path(__file__).resolve().parent.parent
    repo_dir = repo_dir or Path(r"C:\AI\재고현황도구")
    log_file = project_root / "logs" / "morning_pipeline.log"

    src_json = project_root / "web_dashboard" / "dashboard_data.json"
    dst_json = repo_dir / "dashboard_data.json"

    if not src_json.exists():
        log(f"[경고] 원본 dashboard_data.json 파일 없음: {src_json}", log_file)
        return 1

    if not (repo_dir / ".git").exists():
        log(f"[경고] Git 저장소 디렉터리 없음: {repo_dir}", log_file)
        return 1

    git_bin = find_git_executable()

    try:
        # Copy to git repo directory
        shutil.copy2(src_json, dst_json)

        # Stage ONLY dashboard_data.json
        subprocess.run(
            [git_bin, "add", "dashboard_data.json"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        # Check if there are staged differences
        diff_proc = subprocess.run(
            [git_bin, "diff", "--cached", "--quiet", "dashboard_data.json"],
            cwd=repo_dir,
        )

        # Exit code 0 means no differences; exit code 1 means staged differences
        if diff_proc.returncode == 0:
            log("[정보] dashboard_data.json 변경 사항 없음 (Git 푸시 생략)", log_file)
            return 0

        # Commit
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        commit_msg = f"chore(data): auto-update dashboard_data.json [{timestamp}]"
        commit_proc = subprocess.run(
            [git_bin, "commit", "-m", commit_msg],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if commit_proc.returncode != 0:
            log(f"[경고] Git 커밋 실패: {commit_proc.stderr.strip()}", log_file)
            return commit_proc.returncode

        # Push to remote
        push_proc = subprocess.run(
            [git_bin, "push", "origin", branch],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if push_proc.returncode != 0:
            log(f"[경고] Git 푸시 실패: {push_proc.stderr.strip()}", log_file)
            return push_proc.returncode

        log(f"[성공] dashboard_data.json GitHub 푸시 완료 ({commit_msg}) -> Render 자동 배포 시작", log_file)
        return 0

    except Exception as exc:
        log(f"[에러] Git 동기화 중 예외 발생: {exc}", log_file)
        return 1


if __name__ == "__main__":
    sys.exit(sync_dashboard())
