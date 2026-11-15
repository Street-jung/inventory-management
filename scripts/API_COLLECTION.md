# 쇼핑몰 API 수집 실패 처리

카페24 주문 API가 401을 반환하면 Refresh Token으로 1회 자동 갱신하고
실패했던 페이지를 다시 조회합니다. 새 Access/Refresh Token은 회전되므로
`scripts/.cafe24_tokens.json`에 원자적으로 저장해 다음 실행에서도 사용합니다.
이 파일은 인증 정보이며 scripts/.gitignore에서 제외합니다. config/.env는 수정하지 않습니다.

필요한 설정은 기존 `CAFE24_MALL_ID`, `CAFE24_ACCESS_TOKEN`과
`CAFE24_REFRESH_TOKEN`, `CAFE24_CLIENT_ID`, `CAFE24_CLIENT_SECRET`입니다.
갱신 규격은 [카페24 공식 문서](https://developers.cafe24.com/en/app/front/app/develop/oauth/retoken)를 따릅니다.
갱신 POST 응답을 잃었을 때는 회전된 토큰이 무효화될 수 있어 무조건 재시도하지 않습니다.
새로 앱 인증을 완료하여 .env 토큰을 교체한 경우 기존 캐시를 별도로 보관하거나
제거해야 새 설정이 적용됩니다.

카페24/스마트스토어 개별 수집 실패는 채널명, 단계, 상태코드 또는 누락 설정을
WARNING으로 기록하고 다른 채널과 후속 결산을 계속합니다. 수집·정규화가 끝나기
전에는 대상 날짜의 기존 CSV/JSON을 교체하지 않습니다. 실패를 0원 매출로 저장하지 않습니다.
결산 자체의 검증 실패나 산출물 쓰기 오류는 기존 정책대로 파이프라인 오류입니다.

실제 실행은 사용 중인 Python의 PYTHONPATH에 scripts/.wing_deps,
.runtime_deps 및 기존 venv site-packages를 포함해야 합니다.
스마트스토어 전자서명에는 requirements.txt에 지정된 bcrypt가 필요합니다.
