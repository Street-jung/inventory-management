"""Wing persistent-profile collector. Failures preserve the last valid raw files.

Install dependencies with scripts/requirements_coupang_wing.txt. UI overrides can
be supplied through environment variables (see COUPANG_WING.md in this folder).
"""
from __future__ import annotations

import argparse
import logging
import json
import shutil
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Path(os.getenv('COUPANG_PROFILE_DIR', str(ROOT / 'config' / 'coupang_chrome_profile')))
HOME = 'https://wing.coupang.com/'
KST = timezone(timedelta(hours=9))
LOG = logging.getLogger('coupang_wing')
LOGIN_COMMAND = '아침자동실행.bat --login-init'


class LoginRequired(RuntimeError):
    pass


def today():
    return datetime.now(KST).date()


def check_login(page):
    url = urlparse(page.url)
    if ('wing.coupang.com' != url.hostname or
            re.search(r'login|signin|auth/|verify|mfa', url.path, re.I) or
            page.locator('input[type="password"]:visible').count()):
        raise LoginRequired('로그인 또는 2차 인증이 필요합니다')


def authenticated(page):
    try:
        check_login(page)
        # A Wing URL alone is insufficient: wait for an authenticated shell.
        for pattern in [r'로그아웃|판매자정보|판매자 정보', r'^로켓그로스$']:
            matches = page.get_by_text(re.compile(pattern))
            if any(matches.nth(i).is_visible() for i in range(matches.count())):
                return True
        return False
    except Exception:
        return False


def click(page, key, labels, optional=False):
    """Prefer a configured selector; otherwise use exact visible Korean labels."""
    selector = os.getenv('COUPANG_' + key + '_SELECTOR')
    deadline = time.monotonic() + (2 if optional else 30)
    while time.monotonic() < deadline:
        check_login(page)
        locators = [page.locator(selector)] if selector else [
            page.get_by_text(label, exact=True) for label in labels]
        for locator in locators:
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    item.click(timeout=10000)
                    LOG.info('%s 클릭 완료 (%s)', key, labels)
                    return True
        page.wait_for_timeout(250)
    if optional:
        return False
    raise RuntimeError(f'{key}: 메뉴/버튼을 찾지 못했습니다. COUPANG_{key}_SELECTOR 확인 필요')


def validate_excel(path, kind):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in book:
            for row in sheet.iter_rows(max_row=30, values_only=True):
                headers = {re.sub(r'\s+', '', str(v)) for v in row if v is not None}
                if kind == 'sales' and {'옵션ID', '매출(원)', '판매량'} <= headers:
                    return
                if kind == 'inventory' and any('옵션ID' in h for h in headers):
                    available = any('가용' in h or '판매가능' in h for h in headers)
                    returns = any('반품' in h and '재고' in h and
                                  not any(term in h for term in ('지난', '최근', '30일', '이력'))
                                  for h in headers)
                    # Wing inventory exports encode return stock as separate
                    # option rows with 상품등급=반품-미개봉/최상/상/중.
                    graded_stock = '상품등급' in headers
                    if available and (returns or graded_stock):
                        return
        raise ValueError(f'{kind}: 필수 열 누락 (매출: 옵션 ID/매출(원)/판매량, 재고: 옵션 ID/가용/반품)')
    finally:
        book.close()


def growth_only(path, destination):
    """Keep the exact downloaded workbook as evidence, then retain Growth rows."""
    from openpyxl import load_workbook
    book = load_workbook(path)
    try:
        sheet = book.active
        headers = [str(c.value or '').strip() for c in sheet[1]]
        if '판매방식' not in headers:
            raise ValueError('매출 상세에 판매방식 열이 없어 로켓그로스를 구분할 수 없습니다')
        column = headers.index('판매방식') + 1
        source_dir = destination.parent / 'source_exports'
        source_dir.mkdir(parents=True, exist_ok=True)
        original = source_dir / (destination.stem + '_all_methods.xlsx')
        original_temp = original.with_suffix('.partial.xlsx')
        shutil.copyfile(path, original_temp)
        original_temp.replace(original)
        removed = 0
        for row in range(sheet.max_row, 1, -1):
            if str(sheet.cell(row, column).value or '').strip() != '로켓그로스':
                sheet.delete_rows(row)
                removed += 1
        book.save(path)
        LOG.info('로켓그로스 매출 %s행 유지, 다른 판매방식 %s행 제외. 원본: %s', sheet.max_row - 1, removed, original)
    finally:
        book.close()


def download(page, kind, destination):
    # Listen on every page as some Wing exports use a popup.
    context = page.context
    downloads = []
    def on_download(item):
        downloads.append(item)
    def on_page(new_page):
        new_page.on('download', on_download)
    for existing in context.pages:
        existing.on('download', on_download)
    context.on('page', on_page)
    temporary = destination.with_suffix('.partial.xlsx')
    try:
        page.evaluate('''() => {
            window.__wingExportUrls = [];
            window.__wingOriginalOpen = window.open;
            window.open = function(url, ...args) {
                if (url) window.__wingExportUrls.push(String(url));
                return window.__wingOriginalOpen.call(this, url, ...args);
            };
        }''')
        if kind == 'sales':
            click(page, 'SALES_DOWNLOAD', ['엑셀 다운로드'])
            if os.getenv('COUPANG_DIAGNOSTICS'):
                (ROOT / 'scripts/wing_diagnostics/sales_download_menu.txt').write_text(page.locator('body').inner_text(), encoding='utf-8')
            click(page, 'SALES_DETAIL', ['상품별 판매', '상품별 엑셀', '상품별', '상품별 다운로드'])
        else:
            click(page, 'INVENTORY_DOWNLOAD', ['엑셀 다운로드', '다운로드', '엑셀다운로드'])
            click(page, 'INVENTORY_REQUEST', ['엑셀 다운로드 요청'])
            page.wait_for_timeout(1000)
            if os.getenv('COUPANG_DIAGNOSTICS'):
                (ROOT / 'scripts/wing_diagnostics/inventory_download_menu.txt').write_text(page.locator('body').inner_text(), encoding='utf-8')
        deadline = time.monotonic() + int(os.getenv('COUPANG_DOWNLOAD_TIMEOUT', '180'))
        export_url = None
        while not downloads and time.monotonic() < deadline:
            check_login(page)
            opened = page.evaluate('() => window.__wingExportUrls || []')
            if opened:
                from urllib.parse import urljoin
                candidate = urljoin(page.url, opened[-1])
                if urlparse(candidate).hostname != 'wing.coupang.com':
                    raise RuntimeError('외부 도메인의 내보내기 URL은 사용하지 않습니다')
                export_url = candidate
                break
            page.wait_for_timeout(500)
        if not downloads and not export_url:
            raise RuntimeError(f'{kind}: 엑셀 다운로드 대기 시간 초과')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if downloads:
            downloads[0].save_as(str(temporary))
        else:
            response = context.request.get(export_url, timeout=180000)
            if not response.ok:
                raise RuntimeError(f'엑셀 응답 HTTP {response.status}')
            temporary.write_bytes(response.body())
        validate_excel(temporary, kind)
        if kind == 'sales':
            growth_only(temporary, destination)
        temporary.replace(destination)
        LOG.info('%s 저장: %s', kind, destination)
        return str(destination)
    finally:
        if not page.is_closed():
            page.evaluate('''() => {
                if (window.__wingOriginalOpen) window.open = window.__wingOriginalOpen;
            }''')
        temporary.unlink(missing_ok=True)
        context.remove_listener('page', on_page)
        for existing in context.pages:
            existing.remove_listener('download', on_download)


def collect_sales(page, target):
    start = target.replace(day=1)
    base = os.getenv('COUPANG_SALES_URL', HOME + 'tenants/business-insight/sales-analysis')
    separator = '&' if '?' in base else '?'
    page.goto(f'{base}{separator}start_date={start}&end_date={target}', wait_until='domcontentloaded')
    # The established Wing sales-analysis route uses these date query parameters.
    check_login(page)
    LOG.info('판매분석 조회: %s ~ %s', start, target)
    # Do not click the identically named left-navigation menu: it is not a filter.
    return download(page, 'sales', ROOT / 'raw_data/sales/coupang' / f'{target}_coupang_sales.xlsx')


def collect_inventory(page):
    url = os.getenv('COUPANG_INVENTORY_URL', HOME + 'tenants/rfm-inventory/management/list')
    if url:
        page.goto(url, wait_until='domcontentloaded')
    else:
        page.goto(HOME, wait_until='domcontentloaded')
        click(page, 'GROWTH_MENU', ['로켓그로스'])
        click(page, 'INVENTORY_MENU', ['재고현황', '재고 현황', '재고관리', '재고 관리'])
        click(page, 'INVENTORY_STATUS', ['재고현황', '재고 현황'], optional=True)
    check_login(page)
    # First visit shows a multi-slide inventory tutorial over the export button.
    page.get_by_text('엑셀 다운로드', exact=True).first.wait_for(timeout=30000)
    for _ in range(8):
        guide = page.locator('[data-wuic-props*="name:modal-view"]:visible').filter(
            has=page.locator('img[src*="onboarding"]'))
        if not guide.count():
            break
        advanced = False
        for label in ('다음', '시작하기', '확인', '완료', '닫기'):
            control = guide.get_by_role('button', name=label, exact=True)
            if control.count() and control.first.is_visible():
                control.first.click()
                LOG.info('재고현황 가이드: %s', label)
                page.wait_for_timeout(400)
                advanced = True
                break
        if not advanced:
            raise RuntimeError('재고현황 가이드 종료 버튼 확인 필요: ' + guide.inner_text())
    return download(page, 'inventory', ROOT / 'raw_data/inventory' / f'{today()}_coupang_inventory.xlsx')


def fallback(kind):
    folder = ROOT / ('raw_data/sales/coupang' if kind == 'sales' else 'raw_data/inventory')
    pattern = '*_coupang_sales.xlsx' if kind == 'sales' else '*_coupang_inventory.xlsx'
    for path in sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            validate_excel(path, kind)
        except Exception:
            continue
        LOG.warning('%s 수집 생략. 기존 파일 유지: %s (수정시각 %s)', kind, path,
                    datetime.fromtimestamp(path.stat().st_mtime, KST).isoformat())
        return str(path)
    LOG.warning('%s: 사용할 기존 상세 엑셀이 없습니다. 후속 집계의 데이터 누락에 유의하세요.', kind)
    return None


def run(target=None, login_init=False, headed=False, login_timeout=900, kinds=('sales', 'inventory')):
    target = target or today()
    result = {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            PROFILE.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE), channel=os.getenv('COUPANG_BROWSER_CHANNEL', 'chrome'),
                headless=not (login_init or headed), accept_downloads=True,
                args=['--restore-last-session'],
                locale='ko-KR', timezone_id='Asia/Seoul', timeout=60000)
            try:
                state_file = PROFILE / 'wing_session_state.json'
                if not login_init and state_file.exists():
                    state = json.loads(state_file.read_text(encoding='utf-8'))
                    context.add_cookies(state.get('cookies', []))
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(30000)
                page.set_default_navigation_timeout(60000)
                if login_init:
                    page.goto(HOME, wait_until='domcontentloaded')
                    LOG.info('브라우저에서 로그인과 2차 인증을 완료하세요. 최대 %s초 대기합니다.', login_timeout)
                    deadline = time.monotonic() + login_timeout
                    while time.monotonic() < deadline:
                        for candidate in context.pages:
                            if authenticated(candidate):
                                # Explicitly restore session cookies on the next run too;
                                # Chrome may discard session-only cookies after shutdown.
                                context.storage_state(path=str(state_file))
                                LOG.info('로그인 확인 완료. 프로필을 저장하고 종료합니다: %s', PROFILE)
                                return {'login': 'saved'}
                        page.wait_for_timeout(1000)
                    raise LoginRequired('로그인 확인 시간 초과 (로그아웃/판매자 정보 표시 확인 필요)')
                for kind, action in [('sales', lambda: collect_sales(page, target)),
                                     ('inventory', lambda: collect_inventory(page))]:
                    if kind not in kinds:
                        continue
                    try:
                        result[kind] = {'status': 'downloaded', 'path': action()}
                        context.storage_state(path=str(state_file))
                    except Exception as exc:
                        if os.getenv('COUPANG_DIAGNOSTICS'):
                            diagnostic = ROOT / 'scripts' / 'wing_diagnostics'
                            diagnostic.mkdir(exist_ok=True)
                            details = {'url': page.url, 'body': page.locator('body').inner_text(), 'frames': [f.url for f in page.frames],
                                       'elements': page.locator('a,button,input,[role="button"],[role="tab"]').evaluate_all(
                                           '(els) => els.map(e => ({tag:e.tagName,text:(e.innerText||e.getAttribute("aria-label")||"").trim().slice(0,150),href:e.getAttribute("href"),id:e.id,type:e.type})).filter(e=>e.type!=="password")')}
                            (diagnostic / f'{kind}.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
                        if isinstance(exc, LoginRequired):
                            LOG.warning('쿠팡 세션 만료: %s. 재로그인: %s', exc, LOGIN_COMMAND)
                        else:
                            LOG.warning('쿠팡 %s 수집 실패 (%s): %s', kind, type(exc).__name__, exc)
                        result[kind] = {'status': 'fallback', 'path': fallback(kind)}
            finally:
                # Closing the persistent context flushes cookies/local storage.
                context.close()
    except Exception as exc:
        if login_init:
            LOG.error('초기 로그인 실패: %s', exc)
            raise
        LOG.warning('쿠팡 브라우저 실행 실패: %s. 설치/프로필 잠금 확인. 재로그인: %s', exc, LOGIN_COMMAND)
        for kind in kinds:
            if kind not in result:
                result[kind] = {'status': 'fallback', 'path': fallback(kind)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=today(), help='매출 조회 종료일 (당월 1일부터)')
    parser.add_argument('--login-init', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--kind', choices=['all', 'sales', 'inventory'], default='all')
    parser.add_argument('--login-timeout', type=int, default=900)
    parser.add_argument('--require-downloads', action='store_true', help='대체 파일을 사용하면 종료 코드 2 (실다운로드 검증용)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    try:
        kinds = ('sales', 'inventory') if args.kind == 'all' else (args.kind,)
        result = run(args.date, args.login_init, args.headed, args.login_timeout, kinds)
        print(result)
        if args.require_downloads and not args.login_init and any(
                result.get(kind, {}).get('status') != 'downloaded' for kind in kinds):
            return 2
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
