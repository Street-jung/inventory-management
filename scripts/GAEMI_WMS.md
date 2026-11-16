# 개미창고 WMS 자동 수집

기존 원본은 `raw_data/inventory/2026-11-14_3pl_inventory.xlsx`이며 실제 파일 형식은
CSV입니다(가상 시트명 CSV, 헤더 포함 80행). 헤더는 번호, 상품코드, 상품명, 옵션명,
상태, 입고량, 출고량, 재고량, 등록일입니다. 상품코드와 옵션/등록일별 행을 보존하며,
화이트리스트는 기존 대시보드의 `오키드상품정보.xlsx → 채널코드맵핑`에서 적용됩니다.

1. `config/gaemi_wms.env.example`의 세 항목을 기존 `config/.env`에 추가하세요.
   기존 쿠팡/쇼핑몰 설정은 유지합니다. 비밀번호는 로컬 파일에만 입력하세요.
2. 프로젝트 루트 PowerShell에서 실행합니다.

   ```powershell
   .\아침자동실행.bat --gaemi-login-init
   ```

3. 설정된 ID/PW로 자동 로그인한 뒤 **상품 관리** 메뉴를 자동 탐색합니다.
   2차 인증/CAPTCHA가 있다면 사용자가 완료해야 합니다.
4. 상품관리 화면과 엑셀 버튼이 확인되면 자동으로
   `scripts/wing_diagnostics/gaemi_dom.json`에 URL·조회/엑셀 선택자·iframe 정보를
   기록하고 프로필을 저장한 뒤 종료합니다. 최대 15분 기다립니다.

로그인 초기화 명령은 결산, 클라우드 백업, 대시보드 배포를 실행하지 않습니다.
프로필은 `config/gaemi_chrome_profile`이며 세션 쿠키 보조 스냅샷도 이 안에 저장됩니다.
인증 프로필은 외부 공유하지 마세요. 허용된 디렉터리의 scripts/.gitignore와
config/.gitignore에 `gaemi_*profile*/`을 추가했습니다. 루트 .gitignore의 기존
config/ 제외 규칙도 유지합니다.

정상 다운로드는 먼저 필수 열(상품코드·상품명·옵션명·재고량·등록일), 수량,
빈 데이터 여부를 검증합니다. CSV/구형 XLS/정상 XLSX를 실제 XLSX로 변환하여
`raw_data/inventory/YYYY-MM-DD_gaemi_inventory.xlsx`에 저장합니다.
기존 프론트엔드가 파일명의 `3pl`을 찾으므로 동일 자료의
`YYYY-MM-DD_3pl_inventory.xlsx`도 함께 만듭니다. 프론트엔드 코드는 변경하지 않습니다.
입고량·출고량과 로트 정보를 삭제하거나 상품코드 단위로 합치지 않습니다.
실시간 스냅샷이므로 날짜는 실행일(한국 시간)을 사용합니다.

파이프라인 1단계는 쿠팡, 개미창고, 쇼핑몰 API 순서입니다.
`--skip-fetch`는 세 수집기 모두 생략하고 `--skip-api-fetch`는 쇼핑몰 API만 생략합니다.
미설정·로그인 만료·버튼 변경·검증 실패는 경고와 기존 최신 파일 경로를 반환합니다.
오늘 날짜로 기존 파일을 복사하거나 0원/0개 자료로 덮어쓰지 않습니다.

계정별 로그인 폼이나 버튼이 다르면 config/.env의 선택자 설정을 사용하세요:
`GAEMI_WMS_ID_SELECTOR`, `GAEMI_WMS_PASSWORD_SELECTOR`, `GAEMI_WMS_LOGIN_SELECTOR`,
`GAEMI_WMS_QUERY_SELECTOR`, `GAEMI_WMS_EXPORT_SELECTOR`.
선택자가 여러 개에 일치하면 추측해서 다운로드하지 않고 경고를 반환합니다.
재고 화면에 날짜 필터가 있다면 최초 캡처 시 과거 고정일 대신 현재 재고 조회로
설정하세요. 수집기는 저장된 화면의 의미를 임의로 변경하지 않습니다.

이 PC에서 직접 실행하려면 배치와 동일한 Python 및 PYTHONPATH를 사용해
`scripts/fetch_gaemi_wms.py --headed --require-download`를 실행합니다.
`--require-download`는 기존 파일로 대체되었을 때 종료 코드 2를 반환하는 실검증용 옵션입니다.

2026-11-15 실제 headless 수집 검증 완료:
`https://www.gemichango.com/mypage/product_list.html?code=imv`의 `검색` 및
`엑셀다운로드` 버튼을 사용합니다. 숨겨진 팝업 검색 버튼과 구분하도록
버튼 type과 `:visible`을 선택자에 포함합니다. 검색어를 비우고 전체 상품을 조회하며,
정확히 확인된 전체 데이터 다운로드 confirm만 자동 승인합니다.
`--require-downloads`는 기존 `--require-download`의 별칭입니다.
전체/활성/비활성 탭 중 기본 전체 경로를 사용하며 페이지 번호로 내보내기 범위를 제한하지 않습니다.
일반 무인 실행에서도 메뉴를 탐색하고 DOM 스냅샷을 갱신하므로 수동 캡처가 필수는 아닙니다.
비밀번호/입력 값/쿠키는 DOM 진단에 기록하지 않습니다.

정식 JSON에는 `inventory_snapshots.gaemi`에 원본 날짜, 전체 상품코드/옵션행/재고,
화이트리스트 상품코드/재고 합계를 기록하고 metadata.sources에도 원본을 추가합니다.
화면의 기존 재고 파서는 호환용 `_3pl_inventory.xlsx`를 읽습니다. 프론트엔드 변경은 없습니다.
