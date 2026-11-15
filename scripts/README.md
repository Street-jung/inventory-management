# 식품부문 경영관리 자동화

Python 3.10 이상을 사용합니다. 경로는 실행 위치와 관계없이 이 프로젝트 루트를 기준으로 처리합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest scripts.test_pipeline -v
.\.venv\Scripts\python.exe scripts\run_daily_pipeline.py
```

## 분석 결과

`master_data/오키드상품정보.xlsx`:

| 시트 | 헤더 및 구조 | 사용 방법 |
|---|---|---|
| CODE분류 | 1행. A:C 단품 카테고리/코드, E:F 브랜드/코드, H:K 구분/브랜드코드/단품코드/제품명, M:O 세트 카테고리/코드, Q:U 세트코드/제품명/단품코드/제품명/수량 | 상품 및 세트 구성 참조. 90 카테고리는 비식품 |
| 상품data | 2행. A:K 단품코드/제품명/상시판매가/매입가/부자재비/원가/마진액/마진율/할인가/할인마진/비고. M:W는 같은 구조의 세트표 | 3행부터 A/F 및 M/R을 상품코드/기준원가로 사용 |
| 비용data | 3행. A:D 플랫폼/판매·결제 수수료 합계/수수료 구성/판매대금 정산주기 | B열은 소수 비율. 스마트스토어 6.63%, N배송 8.13%, 쿠팡 Wing·로켓그로스 11.66%, 카페24 KG이니시스 3.85% |

원본은 수동 계산 모드이며 일부 세트 마진율에 `#DIV/0!`가 있습니다. 마진율은 읽지 않으며, 원가 셀의 저장된 계산값이 없거나 오류이면 확인 대상으로 남깁니다. 기준표를 수정했다면 Excel에서 재계산 후 저장해야 합니다. 기존 원본은 수정하지 않습니다.

`config/food_filter_rules.json`은 `include_keywords`(포함), `exclude_keywords`(제외), `b2b_payment_terms_days`(거래처별 일수) 구조입니다. 현재 기본거래처 기한은 작성일 + 15일(달력일)입니다. 포함·제외 동시 일치는 확인필요입니다. 거래처별 약정일이 입력 파일에 있으면 그 날짜를 우선합니다.

선택적으로 `customer_aliases` 객체를 추가할 수 있습니다. 예: `{"입금표기명":"세금계산서상호"}`. 공백을 제거한 정확한 이름으로 대조하며 부분 문자열로 입금을 확정하지 않습니다. 거래처명에 포함 키워드가 없으면 자동 미수 알림 대상이 되지 않으므로 B2B 식품 거래처 키워드를 등록하세요.

`config/.env`에 있는 키는 다음과 같으며 분석 시 전부 빈 값이었습니다. 값은 로그에 출력하지 않습니다.

```dotenv
CAFE24_MALL_ID=
CAFE24_CLIENT_ID=
CAFE24_CLIENT_SECRET=
CAFE24_ACCESS_TOKEN=
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

카페24는 주문 읽기 권한(`mall.read_order`)이 있는 유효한 액세스 토큰이 필요합니다. 클라이언트 ID/Secret만으로 최초 토큰을 발급할 수 없으며 OAuth 승인이 선행되어야 합니다. 현재 모듈은 발급된 토큰을 사용하며 만료 시 HTTP 오류로 중단합니다. 선택 설정: `CAFE24_API_VERSION`, `CAFE24_SHOP_NO`(기본 1). 네이버는 bcrypt 서명으로 토큰을 발급하며 `NAVER_AUTH_TYPE` 기본값은 SELF입니다. SELLER이면 `NAVER_ACCOUNT_ID`도 필요합니다. 네이버 API 허용 IP와 주문 조회 권한도 설정해야 합니다.

## 실행 예시

```powershell
python scripts/fetch_api_sales.py --date 2026-11-13
python scripts/fetch_api_sales.py --channel smartstore --date 2026-11-13
python scripts/filter_bank_and_hometax.py --as-of 2026-11-14
python scripts/filter_bank_and_hometax.py --as-of 2026-11-14 --notify
python scripts/generate_monthly_pnl.py --month 2026-11
python scripts/run_daily_pipeline.py --date 2026-11-13
python scripts/run_daily_pipeline.py --date 2026-11-13 --skip-fetch
python scripts/run_daily_pipeline.py --date 2026-11-13 --notify
```

전일은 한국 시간 기준입니다. 월초에는 전월 손익을 생성합니다. API 오류 시 파이프라인은 즉시 중단합니다. 인증값이 없는 상태에서 로컬 처리만 하려면 `--skip-fetch`를 사용합니다. 텔레그램은 사용자가 실행 시 `--notify`를 지정해야 실제 전송됩니다.

## 입력·출력 계약

- API 원본 페이지: `raw_data/sales/{cafe24|smartstore}/YYYY-MM-DD.json`.
- 같은 위치의 CSV: `channel,order_id,item_id,payment_date,product_code,product_name,quantity,sales_amount,status,currency,source_json`. 비어 있는 날도 헤더가 있는 CSV를 저장합니다. 수집 실패는 빈 매출로 덮어쓰지 않습니다.
- 기타 채널도 이 CSV 구조를 맞춰 `raw_data/sales/<채널>/`에 저장하면 월간 집계에 포함됩니다. 기준표 플랫폼명 또는 `coupang`, `coupang_growth`, `naver_ndelivery`를 channel 값으로 사용할 수 있습니다. 미등록 채널 수수료는 확인필요로 남깁니다.
- 은행/홈택스: 각 폴더의 `.xlsx`, `.xlsm`, `.xls`, `.csv`를 읽습니다. 처음 40행에서 헤더를 찾습니다. 암호화된 파일은 미리 해제해야 합니다. 실제 파일이 없어 현재는 아래 별칭으로 구현했으며, 실제 하나은행·홈택스 내보내기 양식과 검증이 필요합니다.
- 은행 필수 열: 거래일시/거래일자/거래일, 입금액/입금금액/맡기신금액/입금, 거래처명/의뢰인/보낸분/보내신분/적요/거래내용. 거래번호가 있으면 중복 식별에 사용합니다. 출금도 원본 행과 분류는 보존하되 입금 대조에서는 양수 입금만 사용합니다.
- 홈택스 필수 열: 승인번호, 작성일자/작성일/발급일자, 공급받는자상호/공급받는자 상호/공급받는자 상호(법인명)/상호/거래처명, 합계금액/합계/총금액. 약정결제일/결제예정일/지급기일은 선택 열입니다. **매출 세금계산서 파일만** 넣으세요. 매입 자료를 혼합하지 마세요.
- 은행분류·전체 대조·연체미수 CSV: `output_excel/reconciliation/`.
- 월간 결산: `output_excel/YYYY-MM_식품부문_손익결산서.xlsx`. 월간손익/매출상세/확인사항/입력출처 시트로 구성하며 Excel에서 열 때 수식을 재계산합니다.

## 금액 및 대조 정책

네이버는 결제일 조회(`PAYED_DATETIME`)로 현재 배송·구매확정 상태에 관계없이 최초 결제액과 최초 수량을 가져옵니다. 카페24는 `date_type=pay_date`로 조회하고, 주문의 실제 결제액에서 배송비를 뺀 금액을 품목 판매금액 비율로 배분합니다. 포인트·쿠폰은 실제 결제액에 반영되며 원 단위 반올림 차이는 마지막 품목에서 조정합니다. 카페24 API가 필요한 결제·배송비 상세를 반환하지 않으면 추측하지 않고 중단합니다. 다중통화는 지원하지 않습니다.

보고서는 **상품 결제액 − 현재 기준원가 − 추정 플랫폼 수수료**의 기여이익입니다. 부가세를 자동 분리하지 않습니다. 인건비·광고비·고정비·배송수익/비용·환불은 별도 계정 자료가 없어 반영하지 않습니다. 기준표 수수료는 추정치이며 실제 정산서와 차이가 날 수 있습니다. 환불 및 취소 후 변동을 수집하는 별도 원장은 향후 연결해야 합니다. 회계상 영업이익 확정값으로 사용하지 마세요.

90/S9 코드 상품은 비식품으로 제외합니다. 상품코드를 정확히 매핑하며 이름 유사도로 원가를 확정하지 않습니다. 플랫폼의 자체상품코드/옵션관리코드에 내부 코드가 등록되어 있어야 합니다. 세트는 상품data의 세트 원가를 사용합니다. 매핑/원가/수수료 누락은 0원으로 메우지 않고 확인필요로 표시합니다. 월간 요약도 영향을 받는 원가·이익을 확인필요로 표시합니다. 동일 채널/주문/품목은 1회 집계하고 값이 충돌하면 중단합니다. 일별 파일 누락은 확인사항에 기록합니다.

입금은 같은 거래처의 작성일 이후 입금부터 오래된 계산서 순으로 소진합니다(FIFO). 부분입금·일괄입금을 지원하며 재사용하지 않습니다. 약정일 당일은 연체가 아닙니다. 분류 불명·비식품·음수 수정계산서는 자동 확정하지 않습니다. 선수금, 상계, 수정계산서, 기초미수는 별도 확인이 필요합니다. 전체 미수 발생기간을 포함하는 은행·홈택스 자료를 유지하세요. 과거 계산서를 빼면 입금 배분이 바뀝니다. 거래번호 없는 동일 내용 거래는 중복 제거되므로 동일 일자·동일액의 복수 입금은 원본 거래번호가 필요합니다.

알림은 확정 채권 통지가 아닌 **대조상 미수 확인 요청**입니다. 동일 기준일·승인번호·잔액은 성공 전송 후 재발송하지 않습니다. 텔레그램 타임아웃은 전달 여부가 불명확하므로 자동 재시도하지 않습니다. 병렬 실행은 지원하지 않으므로 스케줄러에서 이전 실행이 끝나기 전 중복 실행을 금지하세요.

## 공식 API 참고

- 카페24 주문 조회: https://developers.cafe24.com/docs/api/admin/?version=2025-12-01
- 네이버 결제일 기준 주문 조회: https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-product-orders-with-conditions-pay-order-seller
- 네이버 인증: https://apicenter.commerce.naver.com/docs/auth

## 이 환경에서의 검증 상태

원본 XLSX 내부 XML로 시트명·헤더·저장 원가·수수료를 확인했습니다. 작업 당시 Python 명령이 없으며 다른 프로젝트의 가상환경도 원본 Python이 삭제되어 실행할 수 없었습니다. 설치 파일 다운로드도 연결 차단으로 실패했습니다. 따라서 Python 테스트 실행, 생성 Excel의 수식 재계산·화면 검증, 실 API 및 텔레그램 전송은 수행되지 않았습니다. 운영 전 위 회귀 테스트와 인증 후 샘플 일자 대조를 실행하세요.

## 경영관리 대시보드 JSON

일일 파이프라인 마지막 단계에서 `web_dashboard/dashboard_data.json`을 원자적으로 갱신합니다.

```powershell
python scripts/run_daily_pipeline.py --date 2026-11-14 --skip-fetch
python scripts/export_dashboard_json.py --date 2026-11-14 --as-of 2026-11-15
```

현재 config/food_filter_rules.json의 dashboard_export.coupang_filename_month는 true입니다. 필요하면 `--no-coupang-filename-month`로 해제합니다. API 수집을 포함하려면 `--skip-fetch`를 생략합니다.

Flask 웹페이지에서는 `fetch('/dashboard_data.json').then(r => r.json())`으로 읽습니다. `/api/dashboard-data`도 같은 JSON을 제공합니다. 기존 화면 카드 추가나 원격 서버 배포는 별도 작업입니다.

- `metadata`: 생성시각(KST), report_date(손익 기준일), as_of(은행·정산 기준일), 월, 통화, 입력 출처, 임시반영 여부, 수집 누락 경고.
- `cash_flow`: daily/monthly, 일별·월별 history, 카테고리 집계, 최근 식품 거래 30건, 전체 은행 분류 집계. 현금 합계는 제공된 거래 기준이며 거래 없는 날이 수집 완료를 뜻하지는 않습니다.
- `settlements`: 식품 정산 items, 미수·연체 합계, 원본 미조회 이전 기간 수금. 플랜디에이 9월 수금은 별도이며 10월 순액에서 차감하지 않습니다.
- `sales_profit`: daily/monthly 및 채널별·일별 매출/원가/수수료/기여이익, 매핑 문제, 원가 미매칭 목록. 데이터가 없거나 원가가 누락된 전체 지표는 null이며 매칭분 지표는 별도 제공합니다. 일별 쿠팡 수치는 파일명 기준 임시 귀속입니다.
- `unclassified`: 미분류 count, 중복 제거 customers, 거래 transactions. 현재 0건이어도 새 거래처는 자동 노출됩니다.

### MD 지표 (schema 1.3)

`sales_profit.monthly`와 각 요약의 `total_quantity`는 묶음 수량을 반영한 판매수량 합계입니다.
`active_sku_count`는 채널과 옵션 ID 조합의 고유 개수이며, `row_count`는 원본 집계 행 수입니다.
`sales_profit.transactions`에는 상품별 `asp`, `cost`, `fees`, `profit`, `margin_pct`를 제공합니다.
ASP는 순매출액/판매수량이며, 수량 0 또는 원가·수수료 미확인 시 해당 지표는 null입니다.

미결산 임시월의 `estimated_real_profit`은 기존 `profit`에서 최근 확정 2개월의 매출가중
비판매수수료 비용률에 따른 추가 예상비용을 차감한 시나리오입니다. 물류·대행·광고 및
정산조정(보상 차감 포함)을 반영하며, 당월에 이미 반영된 비판매수수료는 중복 차감하지 않습니다.
확정월 또는 비교 가능한 확정 이력이 없으면 추정 필드는 null입니다. `estimation_basis`에
기준월·비율·산식을 남깁니다. 채널 구성·판매조건이 달라지면 실제 이익과 차이가 날 수 있습니다.

`monthly_series`와 `cash_flow.monthly_summary`의 `b2b_deposit_total`은 고정 B2B 및
일회성 선물세트 매출 입금입니다. `food_sga_withdrawal_total`은 디자인·인쇄·패키징·용차·
포워딩·통관 출금이며, OEM 제조비 및 수입상품 대금은 제외합니다. 이 두 지표는 은행 거래일
기준 현금 흐름으로, 발생 기준 매출/기여이익에 더하거나 재차 차감하지 않습니다.

숫자는 KRW 숫자형, 날짜는 ISO 문자열입니다. 파일 생성만으로 Render 등 원격 호스팅에 전송되지는 않습니다.
