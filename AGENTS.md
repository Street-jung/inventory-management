# Repository Rules — 일쩜오도(1.5°) 재고현황 도구

## 1. 세션 시작 및 개발 일지(`DEVELOPMENT_LOG.md`) 유지 규칙
- **새 대화가 시작되면 작업을 시작하기 전에 먼저 루트의 `DEVELOPMENT_LOG.md`와 관련 소스(`app.py`, 테스트 파일 등)를 읽어 현재 프로젝트 상태와 주의사항을 파악한다.**
- 개발 작업(기능 추가, 버그 수정, 리팩토링, 설정 변경 등)이 완료될 때마다 루트의 `DEVELOPMENT_LOG.md` 하단에 아래 5개 항목을 간결하게 **추가(append)**한다.
  1. 작업 날짜와 변경한 기능
  2. 수정한 파일과 핵심 변경 사항
  3. 변경 이유 및 중요한 설계 결정
  4. 실행한 테스트와 그 결과
  5. 남은 문제 및 다음 작업 시 주의사항
- **기존 기록은 절대 삭제하거나 덮어쓰지 말고 누적한다.**
- **실제로 직접 실행해 확인하지 않은 테스트·검증 결과는 절대 성공으로 기록하지 않는다.** 미실행 시 `미실행`으로, 실패 시 실제 실패 원인을 사실대로 적는다.

## 2. 프로젝트 핵심 불변 조건 및 작업 주의사항
- **보안·컬럼 노출 금지**: 대시보드 HTML(`GET /`) 및 CSV 다운로드(`/api/export-master-csv`)에 기본 관리자 PIN(`1500`)이나 `비고` / `biko` 컬럼을 노출하지 않는다.
- **운영 데이터 파일 보호**: 테스트나 스크립트 실행 시 `inventory_master_store.json` 원본을 직접 덮어쓰지 않는다(테스트에서는 반드시 임시 `DATA_FILE` 복사본 사용).
- **단일 파일 UI/백엔드 정합성**: `app.py`는 Flask 백엔드와 `HTML_TEMPLATE`(Tailwind CDN, FontAwesome, SheetJS `xlsx`)을 함께 포함한다. DOM ID, 인라인 이벤트 핸들러, JS 전역 상태를 수정할 때는 HTML/JS/테스트(`test_admin_mode.py`) 간 깨진 참조가 없는지 반드시 확인한다.
- **클라우드 영구 저장 구조**: Render 배포(`render.yaml`, gunicorn 1 worker / 4 threads)는 파일시스템이 임시적이므로, 관리자 변경·업로드 데이터의 영구 보존은 `google_apps_script_bridge.gs` 기반 Google Drive 동기화에 의존한다.
- **필수 검증 명령**: 코드 수정 후 완료 전 아래 명령으로 문법 및 전체 테스트를 확인한다.
  - `python -W error::SyntaxWarning -m py_compile app.py test_app.py test_business_logic.py test_admin_mode.py`
  - `python -m unittest discover -p "test_*.py" -v`
- **Git 푸시**: 사용자의 명시적 요청 또는 확인 없이 원격 저장소(`origin/main`)에 `git push`하지 않는다.
