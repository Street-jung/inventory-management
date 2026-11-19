# 일쩜오도(1.5°) 재고현황 도구 — DEVELOPMENT LOG

> **기록 원칙**
> - 새 작업이 완료될 때마다 **파일 하단에 순서대로 추가(append)**하며, **기존 기록은 절대 삭제하거나 덮어쓰지 않는다.**
> - 각 항목은 ① 작업 날짜와 변경한 기능 ② 수정한 파일과 핵심 변경 사항 ③ 변경 이유 및 중요한 설계 결정 ④ 실행한 테스트와 그 결과 ⑤ 남은 문제 및 다음 작업 시 주의사항을 간결하게 기록한다.
> - **실제로 직접 실행·관찰하지 않은 테스트 결과는 성공으로 기록하지 않는다.**

---

## 프로젝트 기준 구조 및 공통 주의사항 (Baseline Overview)
- **구성**: Flask 단일 앱(`app.py`, `render_template_string` 내 Tailwind CDN / FontAwesome 6.4.0 / SheetJS `xlsx@0.18.5` 포함), 상태 저장소(`inventory_master_store.json`), Google Drive 영구 저장 브리지(`google_apps_script_bridge.gs`), 배포 설정(`render.yaml`, `Procfile`).
- **테스트**: `test_app.py`(엔드포인트·HTML/CSV 금지 항목), `test_business_logic.py`(3채널 CSV 파싱·WMA 런웨이·B2B S코드 배수·FIFO 소비기한), `test_admin_mode.py`(PIN 인증·수동 매핑·로트·숨김·탐색기 일괄 숨김/복원).
- **선행 커밋 이력 (`0e0a385` ~ `190196c`)**: 본 개발 일지 도입 이전의 `git log` 선행 이력으로, 당시 개별 커밋 시점의 테스트 실행 로그는 본 문서에 사후 추정 기재하지 않음(현재 코드 기준 검증은 아래 최근 이력 참조).
- **핵심 주의사항**:
  - 대시보드 HTML(`GET /`)과 CSV 내보내기(`/api/export-master-csv`)에 기본 관리자 PIN(`1500`)이나 `비고`/`biko` 컬럼을 절대 노출하지 않는다.
  - 테스트 실행 중 `inventory_master_store.json` 원본이 변형되지 않도록 임시 `DATA_FILE` 복사본 격리를 유지한다.
  - Render 무료 플랜의 임시 파일시스템 특성상, 운영 데이터 보존은 `GOOGLE_APPS_SCRIPT_URL` / `GOOGLE_APPS_SCRIPT_TOKEN` 연동에 의존한다.

---

## [2026-11-11] 불필요한 파일·코드 정리 및 모노톤 통합형 UI/UX 개편 (`6d1722d`)
1. **작업 날짜와 변경한 기능**: `2026-11-11` / 레거시 파일·미사용 코드 정리 및 대시보드 UI/UX 모노톤 개편
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`, `test_admin_mode.py` 수정
   - 미사용 파일 6개 삭제: `README.md`, `dashboard_action_board_mockup.jpg`, `dashboard_action_board_mockup_v2.jpg`, `github_push.bat`, `run.bat`, `일쩜오도상품_샘플.csv`
   - 최소 헤더 + `관리자 메뉴` 드롭다운, 1행 8개 상태 필터 카드(`#statusFilterPills`), 카테고리 `<select id="categorySelect">`, 모노톤(white/slate) 업로드 스트립·모달·접이식 로그 적용, `animate-pulse` 제거
3. **변경 이유 및 중요한 설계 결정**: 컬러 배지와 상단 액션 버튼이 과다해 시인성이 떨어지던 문제를 해결하기 위해 차분한 모노톤(white/slate) 1열 스캔 구조로 단순화함. 기존 API 스펙과 주요 DOM ID(`#statusFilterPills`, `categorySelect` 등) 계약은 유지해 기존 기능·테스트 호환성을 보존함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (DOM ID 144개, 인라인 핸들러 130개 이상 없음)
   - `python -m unittest discover -p "test_*.py" -v` — 32개 테스트 전부 통과 (`OK`)
5. **남은 문제 및 다음 작업 시 주의사항**: `app.py`가 단일 파일(약 8,600줄)에 백엔드와 `HTML_TEMPLATE`을 함께 담고 있으므로, DOM ID나 `on*` 핸들러 이름 변경 시 JS 참조 및 `test_admin_mode.py` 문자열 검증 영향을 함께 확인해야 함.

---

## [2026-11-11] 쿠팡 반품 재고 컬러 톤 완화 및 자동감지 배지 제거 (`1bf4577`)
1. **작업 날짜와 변경한 기능**: `2026-11-11` / 쿠팡 반품 재고 시각 강조도 완화 및 불필요한 배지 삭제
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`: KPI 요약, 마스터 테이블 본문·소계(`tfoot`), 상세행(번들별 옵션), 관리자 모달 내 반품(`🔄 반품`) 표시 색상을 강한 `rose-*`에서 옅은 `slate-*`(`slate-50/70`, `slate-100`, `slate-200`, `slate-500`, `slate-600`) 톤으로 완화
   - `app.py` (`renderMasterTable()`): `'CSV G열 반품 자동감지'` 배지 문구 삭제
3. **변경 이유 및 중요한 설계 결정**: 쿠팡 반품 상품은 폐기 대상이 아니라 재판매 가능 재고이므로 품절·긴급 발주 경보와 비슷한 고채도 적색 강조를 피하고 보조 정보 톤으로 위계를 낮춤. `parse_coupang_rows`와 `coupang_return_inventory` 분리 집계 로직은 그대로 유지함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과
   - `python -m unittest discover -p "test_*.py" -v` — 32개 테스트 전부 통과 (`OK`)
5. **남은 문제 및 다음 작업 시 주의사항**: 정상 재고와 반품 재고의 구분 표시 자체는 유지되어야 하므로 이후 테이블/상세행 리디자인 시 반품 분리 데이터 바인딩이 누락되지 않도록 주의 필요.

---

## [2026-11-11] 코드 매핑 탐색기 다중 선택(Shift+클릭) 및 일괄 숨김·복원 추가 (`f6cf311`)
1. **작업 날짜와 변경한 기능**: `2026-11-11` / 관리자 모드 미매핑 코드 탐색기 다중 선택 및 일괄 숨김·복원 버튼 추가
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`: 미매핑 코드 탐색기 테이블에 체크박스 열(`#acMapCsvSelectAll`, `.ac-map-exp-select`), `Shift+클릭` 범위 선택, 상단 일괄 작업 바(`#acMapCsvBulkBar`: `선택 일괄 숨김`, `선택 일괄 복원`, `필터 전체 숨김`) 추가
   - `test_admin_mode.py`: `test_15_unmapped_explorer_hide_restore_persistence`에 배치 `action: "unhide", keys: [...]` 및 일괄 숨김 UI/JS 검증 추가
3. **변경 이유 및 중요한 설계 결정**: 미매핑 항목이 많을 때 건별 숨김 클릭의 번거로움을 줄이기 위해 다중/범위/필터 일괄 처리를 도입하되, 신규 백엔드 API를 추가하지 않고 기존 `/api/admin/unmapped-explorer/hidden`의 `keys: [...]` 배치 인터페이스를 재사용함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (DOM ID 151개, 인라인 핸들러 136개 이상 없음)
   - `python -m unittest discover -p "test_*.py" -v` — 32개 테스트 전부 통과 (`OK`)
5. **남은 문제 및 다음 작업 시 주의사항**: 탐색기 테이블 열(`<th>`/`<td>`) 변경 시 빈 상태 행 `colspan` 값 및 `syncAcMapBulkSelectionUI()` 선택 동기화 로직을 함께 점검해야 함.

---

## [2026-11-11] DEVELOPMENT_LOG.md 및 프로젝트 워크스페이스 규칙(AGENTS.md) 구축
1. **작업 날짜와 변경한 기능**: `2026-11-11` / 개발 일지(`DEVELOPMENT_LOG.md`) 생성 및 새 세션 자동 인식용 루트 규칙(`AGENTS.md`) 추가
2. **수정한 파일과 핵심 변경 사항**:
   - `DEVELOPMENT_LOG.md` (신규 생성): 프로젝트 기준 구조, 최근 검증된 변경 이력(`6d1722d`, `1bf4577`, `f6cf311`), 누적 작성·테스트 사실 기록 원칙 정리
   - `AGENTS.md` (신규 생성): Antigravity 디렉터리 규칙 파일로 새 대화 시작 시 `DEVELOPMENT_LOG.md`와 코드 우선 확인, 작업 종료 시 5개 항목 누적 기록, 미확인 테스트 결과 기록 금지 및 프로젝트 불변 조건 명시
3. **변경 이유 및 중요한 설계 결정**: 대화 컨텍스트가 새로 시작되더라도 개발 이력과 설계 의도·주의사항을 일관되게 계승하기 위해 Antigravity가 자동 로드하는 루트 규칙 파일(`AGENTS.md`)과 누적형 개발 일지(`DEVELOPMENT_LOG.md`)를 분리·연계함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - `python -m unittest discover -p "test_*.py" -v` — 32개 테스트 전부 통과 (`Ran 32 tests in 1.047s`, `OK`)
   - 문서/규칙 파일 추가 작업이며 `app.py` 및 테스트 코드 로직은 변경하지 않음
5. **남은 문제 및 다음 작업 시 주의사항**: `AGENTS.md`와 `DEVELOPMENT_LOG.md`는 현재 워킹 트리에만 생성된 상태이므로 필요 시 커밋·푸시 여부를 사용자에게 확인해야 함. 이후 모든 작업 완료 시 본 파일 하단에만 새 섹션을 추가할 것.

---

## [2026-11-11] 소비기한·코드매핑 데이터 보존 강화 (Drive 덮어쓰기 차단 · 로트키/Excel 시리얼 정규화)
1. **작업 날짜와 변경한 기능**: `2026-11-11` / Google Drive 덮어쓰기 보호 잠금·자동 복구 및 Excel 시리얼·CSV 양방향 로트키/코드매핑 정규화
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`:
     - `_call_gas_bridge` 타임아웃 상향(20s)·일시 오류 재시도, 부팅 시 `pull_from_drive` 실패하면 `drive_sync_state["sync_blocked"] = True`, `status = "blocked"` 설정해 로컬 구버전 스냅샷이 Drive를 덮어쓰지 못하도록 차단하고 백그라운드 복구(`_attempt_boot_drive_recovery_once`, 차단 중 업로드한 CSV는 병합 보존) 및 `POST /api/admin/drive/sync-now` `409 require_force` 가드 추가
     - `/api/upload-file-data` 및 `/api/load-sample`의 `save_state(..., wait_drive=True)` 동기 실행 전환과 상단 보호 배너(`#driveProtectionBanner`), 배지/저장 알림 동기화 상태 표시 추가
     - `excel_serial_to_datetime`, `normalize_gaemi_reg_date`, `canonicalize_lot_key`, `build_lot_override_index`, `resolve_lot_expiry_override`, `_apply_lot_expiry_overrides_locked` 및 JS 동등 함수(`excelSerialToJsIso`, `canonicalizeJsLotKey`, `resolveJsLotOverride`) 추가; `parse_gaemi_rows`·`parse_sales_rows`·`parse_order_datetime`·`get_matching_gaemi_items`에 소수점 수치(`_clean_int_cell`), Excel 날짜 시리얼, NFC/대소문자 무관 매칭 적용
     - `openEditProductModal` / `toggleCoupangOptionReturn` 캐시 최신화(`ensureAdminConfig`), `saveProductModal` 빈 입력 `__CLEAR__`/`__RESTORE__` 구분, `renderAdminExpiryTable` 기본 소비기한 힌트 추가
   - `google_apps_script_bridge.gs`: `save` 액션 시 빈/유효하지 않은 `master_products` 페이로드 거부 가드 + 기존 KST 일일 1회 스냅샷 백업(`iljeomodo_backup_YYYY-MM-DD.json`, 최근 30일 보관) 유지
   - `test_admin_mode.py`: `test_18_drive_boot_failure_overwrite_protection_and_recovery`, `test_19_excel_serial_lot_key_normalization_case_nfc_and_clear` 추가
3. **변경 이유 및 중요한 설계 결정**: 실운영 진단 결과 ① Render 재시작 시 GAS 복원 실패에도 자동 Drive 덮어쓰기가 허용되던 구조적 위험과 ② Excel `.xlsx` 시리얼 `reg_date`(`46280.68877314815`)와 CSV 문자열(`2026-09-15 16:31:50`) 간 로트키 불일치·레거시 `gaemi_codes` 대소문자·NFD 불일치가 공존함을 확인함. 기존 실운영 Drive에 이미 저장된 Excel 시리얼 로트키를 무손실 호환(`resolve_lot_expiry_override` 4단계 매칭)하면서 신규/수정 저장 시에만 중복 별칭 키를 정리하도록 설계함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 153 DOM ID·138 인라인 핸들러 이상 없음, `GET /` 374,600 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 34개 테스트(기존 32개 + 신규 2개) 전부 통과 (`Ran 34 tests in 2.030s`, `OK`)
   - `git diff inventory_master_store.json` — 출력 없음 (원본 운영 파일 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 수정된 `google_apps_script_bridge.gs` 코드는 Google Apps Script 웹앱 편집기에 사용자가 직접 붙여넣어 **새 버전으로 재배포**해야 클라우드 백업 가드·일일 백업이 반영됨.
   - `origin/main` 원격 푸시는 규칙상 미실행 상태이며, 배포 전 관리자 센터 `[JSON 전체 백업 다운로드]` 후 사용자 승인 하에 커밋·푸시해야 함.

---

## [2026-11-11] 상품 행 확장 UI 단순화(쿠팡 번들 재고 전용) · 세부정보 팝업 모달 분리 · 매출 주문일 시리얼 정규화
1. **작업 날짜와 변경한 기능**: `2026-11-11` / 상품 행 클릭 시 쿠팡 번들 재고만 심플하게 노출하고 전체 상세 내역은 `세부정보보기` 팝업 모달로 분리, `recent_orders` Excel 시리얼 날짜 표시 정규화
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`:
     - `renderMasterTable()` 내 인라인 확장 행(`#subrow_${m.id}`)에서 입고 배너·4칸 KPI·개미 FIFO·소진 시뮬레이션·B2B·쿠팡외 최근 주문 카드를 제거하고, 상단 요약 Strip(`세부정보보기` / `접기`) + 정상·반품 요약 바 + 번들 그룹별(`N개입 번들`) 카드 UI만 가독성 높게 배치(기존 `.subrow-sticky-shell` 및 반품 전환·등급미확인 마커 유지). 툴바 전체 토글 버튼 텍스트를 `번들 재고 펼치기`/`번들 재고 접기`로 변경.
     - 신규 팝업 모달 `#productDetailModal` (`z-[95]`) 및 `openProductDetailModal()`, `closeProductDetailModal()`, `renderProductDetailModal()`, `Esc` 키/배경 클릭 닫기, `isAnyModalOpen()`/`updateAdminUI()`/`fetchSummary()` 연동 추가. 팝업 내부에 입고예정·판매정체 알람·4칸 KPI·리드타임/메모·개미창고 소비기한 FIFO·소진 시뮬레이션·쿠팡 번들 요약·B2B/쿠팡외 최근 주문을 배치.
     - `get_summary()`에서 기존 저장 데이터(`sales_inventory`)를 읽어 `recent_orders`를 생성할 때도 `normalize_gaemi_reg_date()`로 Excel 시리얼 날짜(`46316.69378472222` 등)를 `YYYY-MM-DD HH:MM:SS` 형식으로 정규화하고, B2B(S코드) 합산 시 날짜 내림차순 정렬 및 최근 5건 제한 추가(프론트 `renderProductDetailModal`에서도 `normalizeJsGaemiRegDate()` 이중 적용).
   - `test_admin_mode.py`: `test_20_compact_bundle_subrow_detail_modal_and_sales_date_norm` 추가(구 Excel 시리얼 매출 주문일 정규화, 인라인 확장행 vs 팝업 모달 구조 분리, PIN/금지 컬럼 미노출 검증)
3. **변경 이유 및 중요한 설계 결정**: 모바일/좁은 화면에서 행을 클릭했을 때 9개 섹션이 세로로 길게 나열되어 핵심인 쿠팡 번들 재고 파악이 어렵다는 피드백을 반영해, 인라인 확장은 쿠팡 번들별 정상·반품 실물 재고 확인에만 집중하고 전체 운영 지표·FIFO·시뮬레이션·주문 내역은 독립 팝업 모달(`z-[95]`, 수정/매핑 모달 아래 레이어)로 분리함. 또한 화면 하단 최근 주문에 노출되던 구 매출 데이터의 Excel 시리얼 날짜를 재업로드 전에도 즉시 사람이 읽을 수 있도록 조회 시점에 정규화함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 158개 정적 `document.getElementById()` 타겟 확인, 143개 인라인 이벤트 핸들러 확인, `GET /` 381,461 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 35개 테스트 전부 통과 (`Ran 35 tests in 2.082s`, `OK`)
   - `git diff --stat inventory_master_store.json` — 출력 없음 (운영 원본 데이터 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 현재 변경사항은 로컬 워킹 트리(`app.py`, `test_admin_mode.py`, `DEVELOPMENT_LOG.md`)에만 반영된 미커밋 상태이며, `origin/main` 푸시 및 Render 배포 전 사용자 확인이 필요함.
   - `test_13`, `test_16`, `test_17`, `test_20`은 인라인 확장행의 `.subrow-sticky-shell` / `쿠팡 윙 옵션별 번들 · 실물 재고` / `반품` 버튼 마커와 팝업 모달의 `판매 정체(소진무) 알람:` 마커를 검증하므로, 추후 UI 수정 시 해당 마커 유실에 유의할 것.

---

## [2026-11-12] 번들 0개 반품 옵션 숨김 및 상세 팝업 내 쿠팡 출고량 표시 추가
1. **작업 날짜와 변경한 기능**: `2026-11-12` / 행 확장 시 재고 0개 반품 옵션 비노출 처리 및 `#productDetailModal` 내 쿠팡 출고량(통합·번들별) 표시 추가
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`:
     - `renderMasterTable()` 내 `subrowCoupangBody`에서 반품 옵션(`return_options`)을 `physical_stock > 0 || coupang_stock > 0` 조건으로 필터링해 반품 실물 재고가 있을 때만 행 및 그룹/요약 반품 배지를 노출(`hasAnyReturn = retPhysTot > 0 || retPkgTot > 0`, `visibleOptCount` 기준으로 헤더 옵션 수 표시).
     - `renderProductDetailModal()` 내 KPI `출고 속도 · 통합` 카드에 채널별 요약(`쿠팡 7일/30일 · 타 7일/30일`) 추가, `쿠팡 재고 · 출고 요약` 카드에 `쿠팡 출고: 7일 / 30일 · 일평균` 요약 바 및 번들 칩별 출고량(`출고 7일 / 30일`) 표시 추가.
   - `test_admin_mode.py`: `test_20_compact_bundle_subrow_detail_modal_and_sales_date_norm`에 `subrowCoupangBody` 재고>0 반품 필터 마커 및 `renderProductDetailModal`의 `쿠팡 출고:` / `출고 7일` 마커 검증 추가.
3. **변경 이유 및 중요한 설계 결정**: 재고 0인 반품 옵션까지 행 확장에 함께 노출되면 번들 재고 파악 시 시선이 분산된다는 사용자 피드백을 반영하되, 백엔드 API(`coupang_bundle_groups[*]["return_options"]`, `test_17` 계약)와 관리자 `[🔗 코드 매핑]` 목록은 유지하고 대시보드 인라인 표시만 0개 반품을 숨김. 상세 팝업에서는 `쿠팡외 출고`와 동일한 비중으로 `쿠팡 출고` 및 번들별 출고량을 함께 노출해 총 출고량 대비 쿠팡 기여도를 즉시 파악하도록 구성함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 158개 정적 `document.getElementById()` 타겟 확인, 143개 인라인 이벤트 핸들러 확인, `GET /` 383,967 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 35개 테스트 전부 통과 (`Ran 35 tests in 2.055s`, `OK`)
   - `git diff --stat inventory_master_store.json` — 출력 없음 (운영 원본 데이터 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 현재 변경사항은 로컬 워킹 트리(`app.py`, `test_admin_mode.py`, `DEVELOPMENT_LOG.md`)에만 반영되어 있으며, `origin/main` 원격 푸시는 사용자 승인 후 진행해야 함.
   - 관리자 모드에서 재고 0개 수동 반품 옵션 플래그를 해제/조정해야 할 때는 팝업 하단 또는 테이블 우측 `[🔗 코드 매핑]` 모달에서 전체 옵션 목록을 확인할 수 있음.

---

## [2026-11-12] Google Sites iframe 환경 알림 차단 대응 — 인앱 토스트·확인 모달·저장 중 로딩 UI 구현
1. **작업 날짜와 변경한 기능**: `2026-11-12` / Google Sites 임베드 iframe(`allow-modals` 미허용)에서 `window.alert()`·`confirm()`이 무음 차단되어 일괄저장 완료 메시지가 뜨지 않던 문제 해결 (인앱 토스트·확인 모달·일괄저장 배너·버튼 스피너 적용)
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`:
     - 전역 플로팅 토스트 컨테이너(`#appToastContainer`, `z-[220]`), 인앱 확인 모달(`#appConfirmModal`, `z-[210]`, Promise 기반 `showConfirmModal`/`resolveConfirmModal`, Esc 닫기 우선 처리), 관리자 센터 공용 고정 배너(`#adminCenterToast`) 및 일괄편집 전용 배너(`#acBulkToast`) 추가
     - `showAppToast`, `showAdminCenterToast`, `showAcBulkToast`, `hideAdminCenterToasts`, `getSaveFeedbackInfo`, `notifyUser`, `setButtonBusy` 헬퍼 구현 및 기존 `showAcMapToast`가 전역 플로팅 토스트도 함께 표시하도록 확장
     - `saveAdminBulkProducts(this)` 등 관리자 센터·수정/매핑 모달 주요 저장/Drive/샘플 버튼에 `type="button"`, 고유 ID(`#acBulkSaveBtn`, `#acExpirySaveBtn`, `#acB2bSaveBtn`, `#acSettingsSaveBtn`, `#epSaveBtn`, `#cmSaveBtn`, `#acDriveSyncBtn`, `#acDrivePullBtn`, `#acSampleLoadBtn`) 및 저장 중 스피너/중복클릭 방지 적용, 저장 완료 시 `HH:MM:SS` 시각 및 Google Drive 동기화 상태 표시
     - 인라인 `<script>` 내 모든 브라우저 기본 `alert()`/`confirm()` 호출을 `notifyUser()` / `await showConfirmModal()`로 전면 교체하고 `copyPublicUrl`·`copyGasScript` 클립보드 폴백 강화
   - `test_admin_mode.py`: `test_21_in_app_toast_confirm_and_bulk_save_feedback` 추가(`/api/admin/products/bulk-save` 부분 일괄저장 무손실·요약 반영 검증, 인앱 토스트/확인 모달/버튼 ID/함수 마커 및 `<script>` 내 `alert()`/`confirm()` 미존재 검증, PIN/금지 컬럼 미노출 검증)
3. **변경 이유 및 중요한 설계 결정**: 운영 Render API(`/api/summary`) 대조 결과 관리자 일괄저장(`20TZ01`=996, `20TZ03`=768)은 실제로 정상 저장 및 Drive 동기화까지 완료되었으나, 사용자가 대시보드를 Google Sites(`sites.google.com`) iframe 내부에서 사용 중이어서 Chrome의 sandbox 정책(`allow-modals` 부재)에 의해 `window.alert()`와 `window.confirm()`이 예외 없이 무음 무시되고 있었음. 또한 `표 변경사항 일괄 저장` 버튼에 로딩 표시가 없어 완료 여부를 체감할 수 없었으므로, 모든 `alert()`/`confirm()`을 iframe 정책 영향을 받지 않는 순수 DOM 토스트(`#appToastContainer`)·컨텍스트 배너(`#acBulkToast`, `#adminCenterToast`)·비동기 확인 모달(`#appConfirmModal`) 및 버튼 스피너로 대체함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 174개 정적 `document.getElementById()` 타겟 확인, 147개 인라인 이벤트 핸들러 확인, `GET /` 408,679 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 36개 테스트 전부 통과 (`Ran 36 tests in 2.168s`, `OK`)
   - `git diff --stat inventory_master_store.json` — 출력 없음 (운영 원본 데이터 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 현재 변경사항은 로컬 워킹 트리(`app.py`, `test_admin_mode.py`, `DEVELOPMENT_LOG.md`)에만 반영되어 있으며, `origin/main` 원격 푸시 및 Render 재배포는 사용자 확인 후 진행해야 함.
   - 추후 신규 UI 기능을 추가할 때도 Google Sites iframe 호환성을 위해 `window.alert()` / `window.confirm()`을 직접 호출하지 말고 반드시 `notifyUser()` / `await showConfirmModal()`을 사용할 것 (`test_21`에서 회귀 방지).

---

## [2026-11-12] 일괄편집 제조일 기준 소비기한(개월수) 전환·상세정보 표시 및 단품발주보드 열 단순화
1. **작업 날짜와 변경한 기능**: `2026-11-12` / 관리자 `전체 품목 일괄 편집` 기본 소비기한을 제조일 기준 개월수(`개월`)로 전환·상품 상세정보(`#productDetailModal`) 표시 및 `단품 발주 보드` 관리코드 열 삭제·발주 권고/추천 조치 열 통합
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`:
     - 백엔드(`_parse_optional_shelf_life_months`, `get_summary`, `/api/admin/product/save`, `/api/admin/products/bulk-save`): 제조일 기준 소비기한 개월수 필드 `shelf_life_months` 추가(양의 정수 및 `"N개월"`/`"N월"` 파싱, `0`/빈값 입력 시 `None` 초기화). CSV 기한 미기재 로트 대체용 `default_expiry_date`와 분리해 기존 FIFO·대체기한 동작을 보존함.
     - 관리자 UI(`#acTab_bulk`, `#editProductModal`, `#acTab_expiry`): 전체 품목 일괄 편집의 `기본소비기한` 열을 `제조일 기준(개월)` 숫자+`개월` 입력(`.ac-b-shelflife`)으로 교체하고, 단품 등록/수정 모달 3-C 영역에 `#ep_shelf_life_months`와 `#ep_default_expiry_date`(`대체 소비기한 날짜`)를 분리 배치했으며 `소비기한 일괄 입력` 힌트 문구를 `대체 소비기한`으로 명확화함.
     - 상세정보 팝업(`renderProductDetailModal`): 기준 정보 바(`기본 소비기한 제조일로부터 N개월` 또는 `미등록`) 및 `개미창고 소비기한 · FIFO` 헤더(`제조일 기준 N개월` 배지)에 개월수 표시 추가.
     - `단품 발주 보드` 테이블(`COLUMN_DEFS`, `DEFAULT_COLUMN_ORDER`, `renderMasterTable`, `tfoot`): 13열에서 `'code'`(관리코드)와 `'action_cta'`(추천 조치)를 제거해 11열로 정리하고, `'status'` 열을 `'발주 권고 · 조치'` 단일 열로 통합(상태 배지 + 툴팁 `action_cta` 병기 + 관리자 모드 빠른 버튼 `🔗`/`✏️` 유지, `B2B연동` 배지는 `'name'` 열로 이동). 열 순서 저장 키를 `iljeomodo_col_order_v7`로 전환.
   - `test_admin_mode.py`: `test_22_shelf_life_months_and_board_column_simplification` 추가(`shelf_life_months` 저장/`"24개월"` 파싱/`0`·빈값 초기화 및 `default_expiry_date` 무손실 보존, HTML/JS 11열·개월수 UI 마커, CSV 내보내기 계약 및 PIN/금지 컬럼 미노출 검증).
3. **변경 이유 및 중요한 설계 결정**: 일괄 편집의 `기본소비기한`을 제조일로부터의 소비기한 개월수 확인용으로 전환하되, 기존 개미창고 CSV 로트 기한 미입력 시 적용되던 `default_expiry_date`를 손상하지 않도록 별도 필드(`shelf_life_months`)로 분리 설계함. 단품 발주 보드에서는 매핑 전용인 관리코드 열과 내용이 중복되던 추천 조치 열을 정리해 11열로 시인성을 높이면서도 코드 매핑 모달·검색(`m.seller_code`)·CSV 내보내기·관리자 빠른 버튼 동선을 보존하고, 구 `v6` 열 순서 캐시 충돌을 피하기 위해 `iljeomodo_col_order_v7` 키로 마이그레이션함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 175개 정적 `document.getElementById()` 타겟 확인, 148개 인라인 이벤트 핸들러 확인, `GET /` 410,983 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 37개 테스트 전부 통과 (`Ran 37 tests in 2.218s`, `OK`)
   - `git diff --stat inventory_master_store.json` — 출력 없음 (운영 원본 데이터 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 현재 변경사항은 로컬 워킹 트리(`app.py`, `test_admin_mode.py`, `DEVELOPMENT_LOG.md`)에만 반영되어 있으며, `origin/main` 원격 푸시 및 Render 재배포는 사용자 확인 후 진행해야 함.
   - 제조일 기준 개월수(`shelf_life_months`)는 신규 필드이므로 품목별 값을 일괄 편집에서 입력·저장하면 상세정보 모달에 `제조일로부터 N개월`이 표시됨(기존 `default_expiry_date`는 그대로 유지됨). 또한 보드 11열 전환에 따라 브라우저의 기존 `v6` 열 순서 캐시는 `v7` 기본 11열 순서로 초기화됨.

---

## [2026-11-12] 쿠팡 윙 미연동 상품 클릭·번들창 비활성화 및 쿠팡창고·쿠팡런웨이·0출고량 마스킹
1. **작업 날짜와 변경한 기능**: `2026-11-12` / 쿠팡 윙 미연동(`options` 미등록) 상품의 행 클릭·인라인 번들 재고 창 비활성화 및 보드 내 쿠팡창고·쿠팡 런웨이·출고량(쿠팡+쿠팡외 합산 0건 시) 배경색 동일 마스킹 적용
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py`:
     - `get_summary()` 각 마스터 항목에 `"has_coupang_link"`(유효한 쿠팡 옵션 ID 매핑 유무) 불리언 필드 추가 및 기존 `total_sales_7d = coupang_sales_7d + other_sales_7d`·`total_sales_30d = coupang_sales_30d + other_sales_30d`·`daily_sales = coupang_daily + other_daily` 합산 유지 재확인.
     - CSS `.masked-cell-value` 추가(`color: transparent !important; background-color: transparent !important; border-color: transparent !important; opacity: 0 !important; user-select: none !important; pointer-events: none !important;`): 일반 흰 배경뿐 아니라 행 hover(`hover:bg-slate-50/80`) 시에도 값·하위 배지·서브텍스트가 노출되지 않고 `<td>` 테두리만 유지되도록 내부 래퍼에 적용.
     - JS `isCoupangLinked(m)`, `shouldMaskSalesCell(m)` 추가 및 `toggleSingleOption(prodId)` 가드 추가.
     - `renderMasterTable()`에서 `!hasCoupangLink` 행의 `onclick`·`cursor-pointer`·확장 화살표(`<i id="chev_...">`) 및 `#subrow_${m.id}` DOM 렌더링을 제거해 행 클릭 및 `[번들 재고 펼치기]` 시 어떤 창도 열리지 않도록 처리(`🔗`/`✏️` 관리자 빠른 버튼만 동작). 쿠팡 미연동 행의 `coupang_stock`·`coupang_runway`는 전체 마스킹하고, `total_sales`는 `other_sales` 포함 총 출고량이 0일 때만 마스킹(스마트스토어·카페24 등 쿠팡외 출고 존재 시 정상 표시).
   - `test_admin_mode.py`: `test_23_unlinked_coupang_no_click_and_masked_cells` 추가(`has_coupang_link`·동적 매핑 해제/재연결·`total_sales == coupang_sales + other_sales` 합산 검증, CSS/JS 마스킹 및 클릭 비활성화 마커, PIN/금지 컬럼 미노출 검증).
3. **변경 이유 및 중요한 설계 결정**: 쿠팡 윙과 매핑되지 않은 상품은 쿠팡에서 판매하지 않으므로 행 클릭 시 `연결된 쿠팡 윙 번들 옵션이 없습니다` 확장창이나 팝업이 열리지 않도록 차단해 혼선을 방지함(관리자는 `🔗`/`✏️` 버튼으로만 접근). 또한 `쿠팡창고(실물)`/`쿠팡 런웨이`는 미연동 전체에서 숨기되, `출고량 (7일/30일)`은 쿠팡+쿠팡외 통합 출고 합산 열이므로 스마트스토어·카페24 등 쿠팡외 출고가 있는 5개 품목(`80XP03`·`20XO02` 등)은 정상 표시하고 출고가 0인 품목만 마스킹하도록 사용자 확인을 반영함. 마스킹은 `<td>`가 아닌 내부 `div.masked-cell-value`에 적용해 표 구분선 유실 및 hover 대비 노출을 방지함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 175개 정적 `document.getElementById()` 타겟 확인, 148개 인라인 이벤트 핸들러 확인, `GET /` 414,417 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 38개 테스트 전부 통과 (`Ran 38 tests in 2.223s`, `OK`)
   - `git diff --stat inventory_master_store.json` — 출력 없음 (운영 원본 데이터 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 쿠팡 옵션이 매핑되어 있으나 현재 재고가 0인 품목(예: `10GL06`)은 `has_coupang_link = True`로 유지되어 쿠팡 `0`·`재고없음` 및 행 클릭 번들 확인이 계속 가능함.
   - 추후 쿠팡외 출고가 0인 쿠팡 미연동 상품에 스마트스토어 주문이 새로 유입되면 `출고량` 열이 자동으로 보이기 시작함(`shouldMaskSalesCell`).

---

## [2026-11-12] 리포트 CSV 내보내기 매핑 코드 3종(개미창고 상품코드·개미창고 옵션명·쿠팡 옵션 ID) 추가
1. **작업 날짜와 변경한 기능**: `2026-11-12` / 통합 리포트 CSV 다운로드(`/api/export-master-csv`)에 `판매자관리코드` 외 `개미창고 상품코드`, `개미창고 옵션명`, `쿠팡 옵션 ID` 매핑 컬럼 추가
2. **수정한 파일과 핵심 변경 사항**:
   - `app.py` (`export_master_csv`):
     - CSV 헤더 `판매자관리코드` 바로 뒤에 `개미창고 상품코드`, `개미창고 옵션명`, `쿠팡 옵션 ID` 3개 컬럼을 추가해 총 19컬럼으로 확장.
     - `개미창고 상품코드`: 관리자 명시 매핑(`gaemi_mappings`)이 있으면 해당 코드 목록, 없으면 구방식 `gaemi_codes`(또는 일치 `csv_gaemi_lots` 코드)를 중복 제거·순서 보존하여 `", "`로 결합.
     - `개미창고 옵션명`: `gaemi_mappings`의 구체 옵션명은 그대로 표기하고 와일드카드(`""`/`"*"`)는 일치하는 `csv_gaemi_lots` 옵션명(CSV 미존재 시 `"전체(*)"`)을 사용. 구방식(`gaemi_codes` + `gaemi_filter`)에서는 실제 일치한 `csv_gaemi_lots` 옵션명을 우선 표기하고 CSV 미매칭 시 `gaemi_filter` 키워드로 폴백.
     - `쿠팡 옵션 ID`: `m["options"]` 내 정상·반품 구분 없이 등록된 모든 유효 `id`를 중복 제거·순서 보존하여 `", "`로 결합(미연동 품목은 빈 문자열).
   - `test_admin_mode.py`: `test_24_export_master_csv_mapped_codes` 추가(19개 헤더 및 기존 컬럼·보안 불변 조건(`비고`/`biko`/`1500` 미포함), CSV 로트 매칭 상품·미매칭 폴백(`10GL06`)·쿠팡 미연동(`80FC04`)·관리자 `gaemi_mappings` 구체/와일드카드/반품 옵션 ID 동적 반영 검증).
3. **변경 이유 및 중요한 설계 결정**: 리포트 CSV만으로는 각 마스터 단품이 개미창고(3PL)의 어떤 상품코드·옵션명과 쿠팡 윙의 어떤 옵션 ID들에 매핑되어 있는지 역추적하기 어려웠음. `판매자관리코드` 바로 우측에 매핑 코드 3종을 배치하고, 동일 옵션명을 가진 다중 로트(예: 동일 옵션 다른 등록일/재고)는 중복 제거하며 구방식 매핑 품목 중 CSV 미매칭 항목도 `gaemi_filter` 키워드가 빈칸으로 누락되지 않도록 폴백 처리함.
4. **실행한 테스트와 그 결과**:
   - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py` — 통과 (exit code 0)
   - 정적 HTML/JS 검증(`verify_html_js.py`, 스크래치) — 통과 (`JS delimiter & string/template balance OK`, 175개 정적 `document.getElementById()` 타겟 확인, 148개 인라인 이벤트 핸들러 확인, `GET /` 414,417 chars, `GET /api/summary` 38 masters)
   - `python -m unittest discover -p "test_*.py" -v` — 39개 테스트 전부 통과 (`Ran 39 tests in 2.373s`, `OK`)
   - `/api/export-master-csv` 전수 38개 마스터 CSV 스팟체크 — 19컬럼·38행 정상 파싱 및 `비고`/`biko`/`1500` 미노출 확인
   - `git diff --stat inventory_master_store.json` — 출력 없음 (운영 원본 데이터 무수정 확인)
5. **남은 문제 및 다음 작업 시 주의사항**:
   - 현재 변경사항은 로컬 워킹 트리(`app.py`, `test_admin_mode.py`, `DEVELOPMENT_LOG.md`)에만 반영되어 있으며, `origin/main` 원격 푸시 및 Render 재배포는 사용자 확인 후 진행해야 함.
   - Excel에서 단일 `쿠팡 옵션 ID`(11자리 숫자 1개만 매핑된 신규 품목 등) 열을 바로 열 경우 Excel 자체 숫자 서식으로 표기될 수 있으므로 필요 시 향후 서식 접두 검토 가능(현재 쉼표 구분 다중 ID 및 표준 UTF-8-BOM CSV 호환 우선).


## [2026-11-15] 경영관리 통합 JSON 및 조회 API 연결
1. 작업 날짜와 변경 기능: 2026-11-15. 일일 파이프라인 마지막 단계에서 dashboard_data.json 생성, 웹 조회 경로 추가.
2. 수정 파일: app.py에 /dashboard_data.json 및 /api/dashboard-data 추가. test_dashboard_data.py에 임시 파일 기반 404/JSON/캐시 검증 추가. 상위 scripts/export_dashboard_json.py 및 run_daily_pipeline.py에서 현금흐름·정산·손익·미분류를 통합 생성.
3. 설계 결정: UTF-8 JSON에 금액은 숫자, 원가 누락은 null로 기록. 플랜디에이 9월 수금과 10월 상계채권 분리. 쿠팡 파일명 기준 임시반영 여부와 기준일·누락자료 경고 포함. 기존 재고 UI와 inventory_master_store.json은 수정하지 않음. 현재 설정에서 쿠팡 임시반영을 기본 사용하며 --no-coupang-filename-month로 해제 가능.
4. 실제 검증: 웹 필수 py_compile 명령 및 unittest discover 전체 통과(exit 0). 새 JSON 라우트 응답/404/no-store 검증 통과. 백엔드 테스트 16개 통과. --date 2026-11-14 --skip-fetch로 파이프라인 전체 로컬 처리 성공, 기준일 2026-11-15 JSON 생성 확인. 최종 exporter 수정 후 관련 2개 테스트와 파일 재생성 통과.
5. 남은 주의사항: 기존 화면에 경영관리 카드 렌더링은 추가하지 않았으며 fetch('/dashboard_data.json')로 읽을 수 있는 데이터/API 준비 완료. Render 원격 배포나 데이터 동기화는 수행하지 않음. 로컬 생성 파일은 배포 서버에 자동 전송되지 않음. 원가 미매칭 및 채널별 수집 누락은 JSON metadata.warnings 및 sales_profit에 표시됨.
## [2026-11-15] MD 지표·최신 재고·인증 CSV 다운로드
1. 작업 날짜와 기능: 2026-11-15. JSON 스키마 1.1, 추이/성장률/월말 예상/상품 순위 및 CSV API 추가.
2. 수정 파일: scripts/export_dashboard_json.py, scripts/test_dashboard_export.py, web_dashboard/app.py, test_dashboard_data.py, requirements.txt, dashboard_data.json. 재고는 원본 파일명 날짜로 채널별 최신 선택, CSV/XLS/XLSX 형식 판별, 스냅샷 전체 교체. 운영 inventory_master_store.json은 쓰지 않음.
3. 설계 결정: 쿠팡 누적 스냅샷 중복 합산 방지 및 basis 표시. 비교자료 부족/원가 누락은 null. CSV는 전체 transactions 사용, UTF-8 BOM과 수식 주입 방어. JSON 별칭·CSV·손익 엑셀·수기거래 API에 기존 X-Admin-Token 검증 연결. 다운로드는 인증 헤더가 붙은 fetch/blob 사용. 토큰 서명에 프로세스 난수 키 추가(서버 재시작 시 재인증, 현재 gunicorn 1 worker 기준).
4. 실제 검증: 대체 Python 3.11.9 + 로컬 site-packages로 exporter unittest 3개 통과. 필수 py_compile 및 추가 파일 문법 검사 통과. 실제 JSON 재생성 성공(매출 960190). 함수 AST 추출 기반 재고 검증 통과(실파일 3PL 54코드, 쿠팡 145옵션, 채널별 최신일 선택 및 이전 옵션 제거). Python HTML 문자열 평가 후 Node vm으로 인라인 JS 문법 검사 통과. 웹 전체 unittest discover는 Flask 미설치로 4개 모듈 import 실패. pipeline 테스트는 requests 미설치로 import 실패. 패키지 설치 시도는 네트워크 제한으로 실패. 새 Flask 통합 테스트는 추가했으나 실행 검증 미완료.
5. 남은 주의사항: Flask/requests가 있는 정상 개발 환경에서 웹 전체 테스트와 pipeline 회귀 테스트 재실행 필요. Render 배포/원격 푸시 미수행. 최신 파일 파싱 실패 시 이전 재고 유지 및 서버 경고 로그, 다음 요청에서 재시도. 정상 월말 예상 기여이익은 원가/수수료 완전 매칭 시에만 숫자 제공. 재고 파일명은 YYYY-MM-DD_3pl_inventory 또는 YYYY-MM-DD_coupang_inventory 규칙 사용.
## [2026-11-15] 채널코드맵핑 단일 화이트리스트로 타사 데이터 제외
1. 작업 날짜와 기능: 2026-11-15. 오키드상품정보.xlsx의 채널코드맵핑 시트를 자사 상품 유일 기준으로 적용.
2. 수정 파일: scripts/channel_whitelist.py(공통 엄격 허용 목록), generate_monthly_pnl.py(쿠팡 옵션ID 필터, 이름 대체 매칭 제거, 공란 원가 null, 최신 누적파일 선택), export_dashboard_json.py(보고일 이전 최신 스냅샷), web_dashboard/app.py(최신 재고 필터 및 Drive/업로드 재고 조회 필터, 기준표 변경 시 캐시 갱신), 관련 테스트 4개 파일. JSON 및 2026-11 손익 엑셀 재생성.
3. 설계 결정: 단일 지정 파일만 소유권 판정에 사용. 컬럼은 판매자관리코드/개미창고 상품코드/개미창고 옵션명/쿠팡 옵션 ID의 4개. 38상품, 쿠팡39옵션, 3PL28코드. 쿠팡은 등록 옵션ID 정확일치, 3PL은 등록 창고 상품코드 정확일치. 타사는 미매칭 목록에서도 제외. 허용 목록 로드 실패 시 재고는 비워서 노출 방지. 허용 목록 변경 시 원본 재고가 같아도 다시 필터. 전후 비교를 위해 기존 11월14일 보고 기준/11월15일 은행 기준 유지(15일 매출 스냅샷은 기존 보고일 이후이므로 미반영).
4. 실제 검증: 화이트리스트/Exporter unittest 9개 통과. 백엔드 및 웹 필수 py_compile 통과. 함수 AST 추출로 실제 refresh_raw_inventory 실행 검증. 매출 원본90행→자사28행(기존 식품33행), 매출960190/원가247561/수수료111959/기여이익600670, 원가누락5→0. 쿠팡 재고145→39옵션, 3PL54→28코드(51행). 저장된 Excel 상세의 ID·금액·원가/수수료/기여이익 수식과 JSON의 금액 전부 일치. Excel 원가미매칭 시트는 헤더만 존재. 네이티브 Excel 계산 엔진은 미실행, 파일은 열 때 자동 재계산 설정 유지. 웹 전체 unittest는 Flask 미설치로 4개 모듈 import 실패(실행 검증 미완료).
5. 남은 주의사항: 11월15일 최신 매출 파일을 사용하면 별도 스냅샷 수치(26행/매출827850/원가217259/기여이익514063)로 바뀜. 원격 배포/푸시 미수행. 정상 의존성 환경에서 웹 전체 테스트와 requests 의존 파이프라인 테스트 재실행 필요. 운영 inventory_master_store.json 및 기준 엑셀은 수정하지 않음. 콘솔 검증 결과는 scratch/whitelist_verification.json 및 재대조 스크립트에 보관.

## [2026-11-15] URL별 조회 권한 분리 및 9월 이후 월별 집계
1. 기능: 내부 조회 JSON/CSV/손익 Excel 및 조회 탭 PIN 해제. 기존 관리자 변경/삭제/초기화 인증 유지. /share/inventory는 is_share_mode=True와 재고 필드 allowlist만 전달하는 독립 읽기 전용 템플릿(버튼/관리자/탭/경영자료 스크립트 없음). Excel은 ?month=YYYY-MM 선택 지원.
2. 파일: app.py, test_dashboard_data.py, scripts/generate_monthly_pnl.py, export_dashboard_json.py, run_daily_pipeline.py, fetch_api_sales.py, filter_bank_and_hometax.py, test_dashboard_export.py 및 생성 JSON/월별 Excel. 월별 루프, monthly_series/mom_growth_pct, 귀속월별 정산 연결. requests는 실제 수집 시, dotenv는 실제 알림 시 로드하여 오프라인 파이프라인 지원.
3. 설계: 9/10월 쿠팡 파일은 옵션ID 없는 일별 점포 총매출이므로 자사 소유권/원가 확인 불가. unverified_store_totals에 참고 매출 54,648,110 / 54,946,590 저장하고 확정 손익은 null 유지. 11/15 최신 자사 스냅샷 26행: 매출827850/원가217259/수수료96528/기여이익514063/이익률62.1%. 9월 플랜디에이 확인입금6103956 연결, 계산서 미조회로 매출/순액 추정 금지. MoM은 당월 보고일까지 누계 대 전월 누계, 이전 월 자사 손익 자료 부족으로 현 데이터 null.
4. 검증: --date 2026-11-15 --skip-fetch로 실제 파이프라인 성공 및 9/10/11월 Excel/JSON 생성. scripts unittest 24개 통과(월별 계산/MoM 합성자료 검증 포함). 웹 필수 py_compile 및 변경 스크립트 문법검사 통과. 웹 전체 unittest는 Flask 미설치로 4모듈 import 실패; pip 설치는 네트워크 WinError10013으로 실패. 웹 통합 테스트 실행 완료로 간주하지 않음.
5. 주의: 실행 중 9~11월 범위 은행 원본이 추가되어 최종 재실행은 은행298건/미분류40건 반영(원래 114건 fixture 회귀 테스트는 파일을 명시해 격리). 9/10월 자사 옵션별 매출/수량과 9월 세금계산서 원본 필요. 운영 inventory_master_store.json 직접 쓰기 및 원격 배포/푸시 미수행. Antigravity는 별도 공유 템플릿을 디자인할 때 재고 allowlist 및 경영 API 미호출을 유지해야 함.

## [2026-11-15] 플랜디에이 9·10월 정산서 우선 손익 집계
1. 기능: 새 대행 정산서 2개의 2P 옵션집계/매출RAW/견적(을지)/10월3P 분석. RAW 귀속월 기준 확정 정산 우선 로더 적용. 월별 Excel/JSON 및 콘솔 요약표 재생성.
2. 파일: scripts/generate_monthly_pnl.py(정산서 검증/화이트리스트/묶음수량/실비/비용근거 시트), export_dashboard_json.py(실비 합산·monthly_series 감사근거·MoM·콘솔표), test_agency_settlement.py(5개 새 테스트), test_dashboard_export.py(정산서 기준 회귀). web_dashboard/app.py와 운영 inventory_master_store.json 변경 없음.
3. 계산: 순매출=정산서 판매액-판매자쿠폰. 매출원가=현재 마스터 단가×옵션 묶음수량×정산 판매수량, 행별 원단위 반올림. 실제 비용은 판매수수료(VAT 포함), CFS 물류비(VAT별도→포함), 대행료5%, 광고비 포함, 재고손실보상은 비용 차감. 광고비는 2P 소유매출 비율 배분하고 소수점2자리/마지막행 잔액으로 총액 보존(9월9883542.9). Excel 저장 반복소수 정밀도로 생기는 1원 차이는 곱셈결과 소수6자리 안정화 후 원단위 반올림으로 JSON/Excel 통일. 점포 총매출/RAW/중복 쿠팡CSV/임시스냅샷은 정산서와 합산하지 않음.
4. 실제 검증: --date 2026-11-15 --skip-fetch 최종 파이프라인 성공. 백엔드 unittest29개 통과, 변경 스크립트 py_compile 통과. scratch/verify_agency_outputs.py로 저장 Excel 매출상세 및 비용근거 전행을 독립 재합산하여 세 월 JSON과 일치 확인. 최종 9월:21324660/8031224/12827484.4/465951.6/2.19%; 10월:24281610/9320619/16402812/-1441821/-5.94%/MoM13.87%; 11월15일:827850/217259/96528/514063/62.10%/MoM-96.59%. 네이티브 Excel 계산/시각적 렌더 검증은 미실행(수식 보존 및 열 때 재계산 설정).
5. 주의: 9월 RAW AC 판매수수료합계763행 공란은 원본 SUMIF처럼 합산 제외하며 AA 할인전 수수료로 추정 대체하지 않음(2P 수수료463528와 RAW AC합계 일치). 갑지2025년 오기는 RAW2026 귀속월로 검증. 10월3P 17행/매출546900은 현재 옵션ID 화이트리스트 미등록으로 제외. 월별 settlement_audit에 명시. 11월은 정산서 없는 임시 자사스냅샷이며 실제 광고/물류/대행비 및 묶음수량 검증 미완료, 확정 손익으로 해석하지 말 것. MoM은 10월 전체와 11월15일까지 부분 집계 비교. 누락 채널 수집 및 9월 세금계산서 원본 부재는 계속 별도 표시. 원격 배포/푸시 미수행.

## [2026-11-15] 누적 파일 백업 및 Render 최신 데이터 조회
1. 기능: 일일 파이프라인 마지막 5단계에 GAS/로컬 Drive 백업 연결. 아침 배치는 --require-cloud, UTF-8, 경로 자동결정, 종료코드/로그 지원. Render 부팅·조회 시 기본30초 주기로 dashboard/기준표/최신 원본재고/손익 Excel·대조 CSV 복원. Git 없이 Drive 직접 읽기 선택(현재 및 상위 경로 .git 없음).
2. 파일: scripts/sync_to_cloud.py, run_daily_pipeline.py, web_dashboard/cloud_storage.py, app.py(데이터 로더), google_apps_script_bridge.gs(기존 save/load 유지 + backup_*), .gitignore, 아침자동실행.bat, config/cloud.env.example, CLOUD_SYNC_SETUP.md, scripts/test_cloud_sync.py, web_dashboard/test_cloud_refresh.py. 기존 프론트엔드 디자인 보존.
3. 설계: raw_data 전체, output_excel 누적, master_data, dashboard 및 기존 로컬 재고/수기거래 JSON을 SHA-256 객체로 누적 보관. 이전 파일/manifest 자동 삭제 없음. 전송 완료 후 최신 manifest 게시. 원본경로 복구 --restore-to/--revision 지원(빈 폴더만). .env 제외, 토큰 서버측 POST/로그 미출력. 해시/길이/경로/보고일·생성시각 롤백/동시게시 검증. Render 캐시는 관리자 live DB와 분리. web_dashboard 단독 배포용 재고파싱 fallback. GAS 개별32MiB 초과는 오류, 큰 파일은 로컬 Drive 모드 안내.
4. 검증: 신규 동기화 unittest 최종9개 통과. Node에서 실제 GAS 핸들러+mock Drive 업로드/게시/재시도/권한/롤백/기존 save-load 분리 검증 통과(실제 Google 호출 아님). 실제 데이터 임시 로컬 미러 E2E: --date 2026-11-15 --skip-fetch --require-cloud 성공,39파일/약24.8MB,2차 추가업로드0,Render 캐시15파일 복원,운영 inventory_master_store.json SHA불변(scratch/cloud_pipeline_verification.json). 웹 필수 py_compile 및 변경 Python 문법검사 통과. 웹 unittest는 Flask 미설치로5모듈 import 실패. 백엔드 전체 discover 당시37개 중6개 실패: 추가 국세청/쿠팡 원본으로 기존 고정 기대 행수/금액/9월 계산서 미조회 기대값 불일치. 전체 테스트 통과로 기록하지 않음.
5. 미완료/설정: 현재 config/.env에 GOOGLE_APPS_SCRIPT_URL/TOKEN 없음. 실제 Google 업로드·Apps Script 재배포·Render 코드/환경변수 배포·모바일 실서비스 검증 미실행. --required 단독은 미설정으로 오류종료 확인. 최초 GS/Render 코드 배포 및 PC/Render 동일 URL·토큰 필요. Windows 예약 작업 신규 등록 안 함, 기존 작업 배치 연결/시간 설정 필요. GS Script Properties API_TOKEN 권장. 모의 백업 성공을 실제 Drive 연결 성공으로 표현하지 않음.

## [2026-11-15] 쿠팡 입력 일관화·미분류 출력·배치 경로 고정 및 전체38개 테스트 복구
1. 기능: 해당 월 *정산*.xlsx 최우선(기준일과 무관한 확정 귀속월 결산). 미정산 쿠팡은 월별/일자별 옵션ID 상세 중 기준일+원본 생성시각으로 최신1개 선택. 기본 cutoff=today(KST), 명시15일과 기본현재15일 동일 입력. 원본 생성시각UTC→KST, 월말 상한, 생성정보 없을 때만 mtime fallback. 11월 선택=2026-11_coupang_sales.xlsx/기준일2026-11-15/36행/매출17890260.
2. 수정: scripts/generate_monthly_pnl.py, export_dashboard_json.py(구형 파일명 날짜 재필터 제거 및 상세 파일을 점포총액 참고로 오인하지 않음), filter_bank_and_hometax.py(거래처별 미분류 집계/--unclassified-only), 기존4개 테스트 파일, 아침자동실행.bat, CLOUD_SYNC_SETUP.md, config/cloud.env.example. app.py/HTML/JS/CSS 등 프론트엔드 소스 수정 없음. JSON 및 월별 Excel은 백엔드 산출물로 재생성.
3. 설계: 구형 고정16건/미조회 상태/정산순서 가정 대신 승인번호 유일성·현재 원본 개수·입금배분 보존·9월 계산서 매칭·동일cutoff입력선택·임시폴더 통제자료로 검증. 테스트 삭제/skip/개수 조정 없이 기존38개 유지. API패키지없는 현재실행환경을 위해 배치는 실제 실행가능한 LM Studio Python3.11 절대경로 및 기존 pure-Python 패키지경로 고정, 최초온라인실행 requests/python-dotenv 프로젝트 내부 자동설치 지원.
4. 실제 결과: unittest discover -s scripts -t . -p test_*.py -v 38개 전부OK(35.560초). 수정 Python py_compile 통과. --date2026-11-15 --skip-fetch --skip-cloud 파이프라인 성공. 세 월 저장Excel 상세/비용근거와 JSON 독립재합산 오차0. 11월 매출17890260/원가4638867/수수료2086003/기여11165390/이익률62.41%/MoM-26.32%. PYTHONPATH/PYTHON_EXE를 비운 임시복사본 배치 검증: 도움말0,로컬파이프라인0,잘못된인자2. 운영재고원본에 직접 쓰기 없음.
5. 검토 대기: 미분류40건/35거래처, 입금987226422/출금506831932. 콘솔에 거래처별건수·입출금 출력하고 scratch/unclassified_counterparties_2026-11-15.txt 보관. 포함/제외 키워드는 사용자 판단 전 유지. 11월은 실제 정산서 없는 임시 손익이며 원본 생성시각 기반 기준일의 정확한 집계기간은 별도 확인 대상. API패키지 최초온라인설치/실제API수집/실제Drive백업은 이번 테스트에서 미실행(네트워크·기존인증설정 필요). LM Studio가 이동하면 배치 고정경로도 변경 필요.

## [2026-11-15] 20년 차 식품 MD 기준 4개 탭 UI/UX 고도화 및 Render 모바일 웹 배포
1. 기능:
   - 탭 1 (일일 상품 매출): 총 판매수량 합계(875개) 및 판매 품목(36종) 정상 분리 표기. 11개 MD 실무 컬럼(ASP, 원가, 수수료, 기여이익률, 현재 가용재고 및 런웨이, 품절임박 배지, 미판매 품목 토글, 요약 footer) 구현.
   - 탭 2 (일일 재고현황): 총 재고자산 금액(약 1.43억 원) 및 판매 정체(21종) 묶인 원가(약 6,705만 원) 가치 평가 배지 반영.
   - 탭 3 (일일 현금흐름): 정산 월 선택 스트립(9월/10월/11월/전체), 6대 자금 용도 칩(건수+금액 동시 표시), 30건 제한 해제 및 102건 전체 내역 토글 구현.
   - 탭 4 (월 손익결산): 11월 흑자 착시 방지 경고 배너 및 현실 예상 기여이익(99만 원, 5.5%), B2B 현금매출(+594만 원) 및 식품 외주·판관비 상세 반영.
   - 외부 협력사 전용 보안 모드 (/share/inventory): 매출/마진/스크립트/버튼을 완전 차단한 순수 읽기 전용 실물 재고 테이블(6열) 서빙.
2. 파일: app.py, dashboard_data.json, cloud_storage.py, requirements.txt, test_dashboard_data.py, .gitignore
3. 검증:
   - test_dashboard_data.py 6개 통과 (100%)
   - test_app.py 6개 통과 (100%)
   - test_business_logic.py 9개 통과 (100%)
   - Playwright 브라우저 콘솔 에러 0건 확인
   - .gitignore로 raw_data/ 및 config/.env 파일 완전 제외 확인


## [2026-11-16] 매출·현금흐름·손익 모바일 카드 및 3탭 X-Ray
1. 기능: 모바일 상단 탭 2×2 그리드, 3개 탭 상세 카드 기본 표시/전체 표 토글, 매출 TOP10 마스터명 우선, 차트 범례·버튼 줄바꿈 개선, 정산 알람 내부 파일 경로 문구 정제.
2. 파일: app.py(HTML/CSS/JS), scripts/ui_xray.py(PIN 사전 인증·세션 주입, 루트 자동선택/--url, 3탭 PC·모바일 6장 및 ui_report_tabs.md), 화면검수.bat. PIN/토큰은 소스와 리포트에 저장하지 않음.
3. 설계: 최종 필터·계산이 적용된 표의 값을 모바일 카드에도 재사용하여 월별 정산·추정치·입출금 부호와 표의 정합성 유지. PC 표와 기존 집계 로직 유지. 로컬 검수는 임시 DATA_FILE 복사본으로 실행, 운영 재고 JSON SHA-256 불변 확인.
4. 실제 검증: 필수 py_compile 통과. unittest 46개 중44개 통과,2개 실패(test_13 col_widths_v1 기대/현재v2, test_24 CSV 옵션명 라노500ml 기대/현재500). 배포 전 코드로도 두 기대값 실패 재현. 실제 화면검수.bat 로컬 실행 exit0, PNG6장/리포트 생성·탐색기 열기. 6화면 모두 뷰포트 밖 요소[], 가로Overflow 없음, 겹침0, 콘솔오류0, API200. 모바일 카드/표 행수·보기토글·매출필터/미판매·입출금필터/전체보기·손익9/10/11월/누적·360/390/640/641/1920px 전환 통과. 모바일 PNG3장 육안 확인.
5. 남은 사항: 기존 테스트 기대값2건은 이번 UI 범위에서 변경하지 않음. 11px 글자는 X-Ray의12px미만 경고에 계속 기록됨. 모바일 전체표 모드는 의도적으로 가로스크롤 허용. 원격 배포 결과는 후속 기록.

## [2026-11-16] 프로젝트 Slimming 및 검증 데이터 격리
1. 기능: config/업무 데이터/선택자/배치/파이프라인/의존성/회귀 테스트를 보존하고 미사용 테스트 브라우저 프로필4개, 캐시·백업, 과거 X-Ray·QA 이미지, scratch 임시파일, 미사용 검사도구7개 및 구형 UI 패치 파일 정리.
2. 파일: scripts/fetch_gaemi_wms.py(미사용 Path import 1줄 삭제), 루트·web_dashboard/.gitignore(pytest/tmp/ui_preview/cloud cache 제외), test_dashboard_data.py(임시 DATA_FILE 격리), SLIMMING_REPORT.md(삭제 경로 전체·용량·코드 줄 수·최종 트리). app.py 및 UI/라우트 함수는 참조가 있어 변경하지 않음.
3. 설계: 보호대상716개 SHA-256 기록·최종불변 확인. 현재 로그인은 config 프로필이므로 scripts 테스트 프로필만 삭제. 재사용 unittest는 README에서 실행하는 회귀검증이므로 일회성 테스트와 구분하여 보존. cloud cache 폴더는 기존에 없었음. manual_transactions.json도 원래 없었으며 검증에서는 TEMP에만 생성.
4. 실제 검증: app.py+scripts/*.py23개 및 웹검증4파일 py_compile/SyntaxWarning 검사 성공. 파이프라인62개 전부통과, 오프라인 실제 파이프라인 TEMP 출력 성공. 웹46개 중44개통과/기존2개 실패(v1/v2 저장키 기대값, CSV옵션명 기대값). 주요5GET API200, 수기거래 TEMP등록·무인증차단 확인. 기존 웹검증의 스냅샷시각 쓰기 부작용을 감지해 원래 SHA-256과 정확히 일치하도록 복구한 뒤 테스트를 격리하고 재검증, 보호716개 모두 원본일치. 최종 ui_preview 비움 및 검증 생성 캐시도 삭제.
5. 결과/주의: 고유2518개 파일경로 삭제, 최초정리197.64MiB(207240552 bytes) 회수. 미사용소스/패치1867줄 및 scratch코드2706줄 제거, 테스트격리11줄 추가 반영 순4562줄감소(백업소스 제외). 운영 데이터/로그인 선택자/핵심 실행 도구는 유지. 기존 테스트 기대값2건은 그대로 남음. Git 추적 캐시/임시파일은 없어 git rm --cached 적용대상 없음.


## 2026-11-18 Multichannel reviewed release
1. Change: channel-specific provisional sales UI; explicit unavailable combined total and B2B.
2. Files: app.py, test_multichannel_phase1_ui.py. Python routes/auth/generation readers unchanged.
3. Basis: 45 Naver rows 1,452,680 KRW; Cafe24 goods payments 126,000 KRW with refunds separate; do not mix with Coupang basis.
4. Validation: identical workspace code py_compile passed and full web 93 tests passed (16.211s); candidate payload hashes and frontend evidence verified.
5. Next: deploy code before generation 66c09a0f; preserve source/cost/refund limitations. Manual sales remains separate future work.


## 2026-11-19 Mapping recovery diagnostics
1. Change: server-only classified mapping restore diagnostics.
2. File: cloud_storage.py; no additional requests or retry changes.
3. Purpose: identify production stale stage without secrets or raw errors.
4. Validation: isolated web109 and related18 passed; syntax passed; original master hash unchanged.
5. Pending: observe Render diagnostic before publishing fixed approved generation.
