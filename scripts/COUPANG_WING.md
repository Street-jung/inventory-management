# 쿠팡 윙 자동 수집

프로젝트 루트의 PowerShell에서 최초 한 번 실행:

```powershell
.\아침자동실행.bat --login-init
```

이 명령은 필요한 Playwright를 `scripts/.wing_deps`에 설치한 뒤 설치된
Google Chrome을 엽니다. 로그인과 2차 인증을 끝내면 로그인된 화면의
`로그아웃` 또는 `판매자 정보` 표시를 감지하고 창을 닫습니다(최대 15분).
이 경로에서는 결산·클라우드·대시보드 배포를 실행하지 않습니다.
프로필은 실행 시 `config/coupang_chrome_profile`에 생성됩니다.
프로필에는 인증 정보가 있으므로 공유하거나 Git에 올리지 마세요.

이후 평소처럼 `아침자동실행.bat`를 실행하면 1단계에서 쿠팡 수집이 실행됩니다.
`--skip-fetch`는 쿠팡 수집도 생략합니다. Chrome 프로필은 동시 실행하지 마세요.
Chrome이 없는 PC에서는 같은 Python으로 `-m playwright install chromium`을
실행한 후 `COUPANG_BROWSER_CHANNEL=chromium` 환경변수를 설정할 수 있습니다.

매출은 파이프라인 `--date`(기본 어제)가 속한 달 1일부터 해당 일까지입니다.
단독 실행 시 기본 날짜는 한국 시간 오늘입니다. 재고는 실시간이므로 항상
실행일 파일명으로 저장됩니다. 과거 재고를 재현하지 않습니다.

다운로드는 임시 파일에 저장하고 엑셀 구조를 검증한 뒤 원자적으로 교체합니다.
세션 만료, 브라우저 실행 실패, 버튼 변경, 다운로드 실패는 경고와 기존 파일
경로를 남기고 반환합니다. 기존 파일을 오늘 날짜로 복사하지 않습니다.
기존 파일조차 없으면 데이터 누락 경고를 남깁니다. 다른 수집기나 결산기의
오류는 기존 정책대로 처리됩니다.

Wing 계정별 메뉴나 화면 변경에 대응하는 환경변수:

- `COUPANG_SALES_URL`: 판매분석 URL (start_date/end_date를 자동 추가)
- `COUPANG_INVENTORY_URL`: 재고현황의 실제 URL (없으면 로켓그로스 메뉴로 이동)
- `COUPANG_BROWSER_CHANNEL`: 기본 `chrome`
- `COUPANG_SALES_GROWTH_SELECTOR`: 로켓그로스 판매방식 선택
- `COUPANG_SALES_DOWNLOAD_SELECTOR`: 엑셀 다운로드 버튼
- `COUPANG_SALES_DETAIL_SELECTOR`: 상품별 엑셀 메뉴
- `COUPANG_GROWTH_MENU_SELECTOR`: 로켓그로스 메뉴
- `COUPANG_INVENTORY_MENU_SELECTOR`: 재고관리/현황 메뉴
- `COUPANG_INVENTORY_STATUS_SELECTOR`: 재고현황 하위 탭
- `COUPANG_INVENTORY_DOWNLOAD_SELECTOR`: 가용/반품 재고를 함께 포함하는 엑셀 버튼

변수는 실행할 PowerShell의 `$env:변수명='값'` 또는 작업 스케줄러 환경에
설정하세요. `.env` 파일을 자동으로 읽지 않습니다.

진단은 같은 Python으로 `scripts/fetch_coupang_wing.py --headed --date YYYY-MM-DD`를
실행합니다. 계정의 실제 화면에서 수집이 성공하는지 최초 한 번 확인해야 합니다.
인증 우회나 CAPTCHA 자동 처리는 하지 않습니다.

제한된 작업 영역에서 결산을 검증하려면
`scripts/run_daily_pipeline.py --skip-fetch --skip-cloud --output-dir raw_data/pipeline_verification`
을 사용하세요. 대조표, 결산서, dashboard_data.json을 모두 지정한 경로에 생성합니다.
`COUPANG_PROFILE_DIR` 환경변수는 테스트용 프로필 경로를 지정합니다.
`COUPANG_DIAGNOSTICS=1`은 실패 시 메뉴/버튼 DOM을 `scripts/wing_diagnostics`에 기록합니다.

실제 다운로드 성공 여부를 확인할 때는 `--require-downloads`를 추가하세요.
기존 파일로 대체된 경우 일반 실행은 계속 진행하지만 이 검증 옵션은 종료 코드 2를 반환합니다.
최초 로그인 성공 시 `wing_session_state.json`도 프로필 안에 저장하여 세션 전용
쿠키를 명시적으로 복원합니다. 이 파일 역시 로그인 인증 정보이므로 외부 공유하지 마세요.
이전 버전으로 저장한 프로필이 재시작 후 로그인 화면으로 돌아간다면 수정된
`--login-init`을 한 번 실행해야 이 보조 상태 파일이 만들어집니다.
재고의 `고객반품 지난 30일`은 반품 이력으로 현재 반품 재고를 대체하지 않습니다.
현재 Wing 재고 양식에서는 `상품등급`의 `반품-미개봉/최상/상/중` 행에 표시된
실시간 `판매가능재고`가 반품 상품의 현재 재고입니다.

실제 확인한 화면 흐름은 매출 `엑셀 다운로드 → 상품별 판매`, 재고
`/tenants/rfm-inventory/management/list → 엑셀 다운로드 → 엑셀 다운로드 요청`입니다.
매출 원본은 `raw_data/sales/coupang/source_exports/`에 보존하고, 지정된 매출 파일은
`판매방식=로켓그로스` 행만 남깁니다. 매출은 당월 누적 집계이며 일별 거래 원장은 아닙니다.
`--kind sales` 또는 `--kind inventory`로 한 종류만 재수집할 수 있습니다.

다른 쇼핑몰 API가 401을 반환하는 상태에서 쿠팡부터 결산까지 검증하려면
`scripts/run_daily_pipeline.py --date YYYY-MM-DD --skip-cloud --skip-api-fetch --output-dir raw_data/pipeline_verification`
를 사용하세요. API 인증 오류를 숨기지 않도록 기본 실행에서는 기존처럼 오류를 반환합니다.
