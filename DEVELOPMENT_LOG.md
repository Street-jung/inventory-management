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
