"""Cafe24 OAuth2 Access Token Issuer & Refresher.
Pure Python standard library implementation (no external pip dependencies).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env_file():
    env_path = ROOT / "config" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def get_auth_url(
    mall_id: str,
    client_id: str,
    redirect_uri: str = "https://example.com",
    scope: str = "mall.read_order,mall.read_product",
    state: str = "orchidind_auth",
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "state": state,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    query = urllib.parse.urlencode(params)
    return f"https://{mall_id}.cafe24api.com/api/v2/oauth/authorize?{query}"


def extract_code(code_or_url: str) -> str:
    code_or_url = code_or_url.strip()
    if "code=" in code_or_url:
        parsed = urllib.parse.urlparse(code_or_url)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params and params["code"]:
            return params["code"][0]
    return code_or_url


def _post_token_endpoint(mall_id: str, client_id: str, client_secret: str, form_data: dict) -> dict:
    url = f"https://{mall_id}.cafe24api.com/api/v2/oauth/token"
    auth_str = f"{client_id}:{client_secret}"
    auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    body = urllib.parse.urlencode(form_data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            err_msg = err_json.get("error_description") or err_json.get("message") or err_body
        except Exception:
            err_msg = err_body
        raise RuntimeError(f"HTTP {e.code} 오류: {err_msg}") from e
    except Exception as e:
        raise RuntimeError(f"네트워크 오류: {e}") from e


def issue_token(
    mall_id: str, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict:
    form_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    return _post_token_endpoint(mall_id, client_id, client_secret, form_data)


def refresh_token(mall_id: str, client_id: str, client_secret: str, r_token: str) -> dict:
    form_data = {
        "grant_type": "refresh_token",
        "refresh_token": r_token,
    }
    return _post_token_endpoint(mall_id, client_id, client_secret, form_data)


def update_env_file(access_token: str, r_token: str = ""):
    env_path = ROOT / "config" / ".env"
    if not env_path.exists():
        print(f"[!] {env_path} 파일이 존재하지 않습니다.")
        return

    content = env_path.read_text(encoding="utf-8")
    if "CAFE24_ACCESS_TOKEN=" in content:
        content = re.sub(r"CAFE24_ACCESS_TOKEN=.*", f"CAFE24_ACCESS_TOKEN={access_token}", content)
    else:
        content += f"\nCAFE24_ACCESS_TOKEN={access_token}"

    if r_token:
        if "CAFE24_REFRESH_TOKEN=" in content:
            content = re.sub(r"CAFE24_REFRESH_TOKEN=.*", f"CAFE24_REFRESH_TOKEN={r_token}", content)
        else:
            content += f"\nCAFE24_REFRESH_TOKEN={r_token}"

    env_path.write_text(content, encoding="utf-8")
    print(f"[*] config/.env 파일에 토큰 정보가 성공적으로 업데이트되었습니다.")


def main():
    load_env_file()
    mall_id = os.getenv("CAFE24_MALL_ID", "").strip()
    client_id = os.getenv("CAFE24_CLIENT_ID", "").strip()
    client_secret = os.getenv("CAFE24_CLIENT_SECRET", "").strip()

    if not mall_id or not client_id or not client_secret:
        print("[!] config/.env 에 CAFE24_MALL_ID, CAFE24_CLIENT_ID, CAFE24_CLIENT_SECRET 가 설정되어 있어야 합니다.")
        sys.exit(1)

    redirect_uri = "https://example.com"

    parser = argparse.ArgumentParser(description="Cafe24 OAuth2 Token Issuer & Refresher")
    parser.add_argument("--code", help="브라우저에서 획득한 authorization code 또는 리다이렉트 전체 URL")
    parser.add_argument("--refresh", action="store_true", help="저장된 refresh_token을 이용해 토큰 갱신")
    parser.add_argument("--url-only", action="store_true", help="인증 링크만 출력")
    args = parser.parse_args()

    if args.url_only:
        print(get_auth_url(mall_id, client_id, redirect_uri))
        return

    # Handle refresh flow
    if args.refresh:
        current_refresh_token = os.getenv("CAFE24_REFRESH_TOKEN", "").strip()
        if not current_refresh_token:
            print("[!] config/.env 에 CAFE24_REFRESH_TOKEN 이 존재하지 않습니다. 먼저 최초 인증을 진행하세요.")
            sys.exit(1)

        print("[*] 기존 refresh_token을 사용하여 토큰 갱신 중...")
        try:
            res = refresh_token(mall_id, client_id, client_secret, current_refresh_token)
            new_access_token = res.get("access_token", "")
            new_refresh_token = res.get("refresh_token", "")
            expires_at = res.get("expires_at", "")

            print("\n" + "=" * 70)
            print("토큰 갱신 성공!")
            print(f"- New Access Token: {new_access_token}")
            if new_refresh_token:
                print(f"- New Refresh Token: {new_refresh_token}")
            if expires_at:
                print(f"- 만료 일시: {expires_at}")
            print("=" * 70)

            update_env_file(new_access_token, new_refresh_token)
            return
        except Exception as e:
            print(f"\n[토큰 갱신 오류] {e}")
            sys.exit(1)

    # Handle code authorization flow
    auth_url = get_auth_url(mall_id, client_id, redirect_uri)
    print("=" * 70)
    print("[1단계] 아래 인증 링크를 브라우저에 접속하여 권한을 승인하세요:")
    print(auth_url)
    print("=" * 70)
    print("[2단계] 로그인 및 앱 승인 완료 후 https://example.com/?code=... 으로 리다이렉트됩니다.")
    print("        브라우저 주소창의 code= 뒤의 값(또는 URL 전체)을 복사해 입력하세요 (유효기간 1분).")
    print("=" * 70)

    raw_code = args.code
    if not raw_code:
        try:
            raw_code = input(">> 발급받은 code(또는 리다이렉트된 전체 URL) 입력: ").strip()
        except EOFError:
            print("[!] 입력이 없어 종료합니다. --code 인자를 사용해 실행할 수 있습니다.")
            sys.exit(1)

    code = extract_code(raw_code)
    if not code:
        print("[!] 인증 코드가 비어 있습니다.")
        sys.exit(1)

    print(f"[*] 인증 코드 확인: {code[:10]}...")
    print("[*] 카페24 토큰 발급 API 호출 중...")
    try:
        res = issue_token(mall_id, client_id, client_secret, redirect_uri, code)
        access_token = res.get("access_token", "")
        r_token = res.get("refresh_token", "")
        expires_at = res.get("expires_at", "")

        print("\n" + "=" * 70)
        print("토큰 발급 성공!")
        print(f"- Access Token: {access_token}")
        if r_token:
            print(f"- Refresh Token: {r_token}")
        if expires_at:
            print(f"- 만료 일시: {expires_at}")
        print("=" * 70)

        update_env_file(access_token, r_token)
    except Exception as e:
        print(f"\n[토큰 발급 오류] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
