"""Persistent-session Gaemi WMS inventory collector; failures keep prior files."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin

try:
    from .common import ROOT, load_env, read_excel_sheets, money, write_json
except ImportError:
    from common import ROOT, load_env, read_excel_sheets, money, write_json

PROFILE = ROOT / 'config/gaemi_chrome_profile'
SNAPSHOT = ROOT / 'scripts/wing_diagnostics/gaemi_dom.json'
LOG = logging.getLogger('gaemi_wms')
KST = timezone(timedelta(hours=9))
REQUIRED = {'상품코드', '상품명', '옵션명', '재고량', '등록일'}
EXPORT_HEADERS = ['번호','상품코드','상품명','옵션명','상태','입고량','출고량','재고량','등록일']
PRODUCT_PATH = '/mypage/product_list.html?code=imv'
CONTROL_SCRIPT = r'''() => [...document.querySelectorAll('button,a,input,[role="button"]')]
 .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
 .filter(e => !['password','hidden'].includes(e.type))
 .map(e => {
   const text = (e.innerText || e.getAttribute('aria-label') || (['submit','button'].includes(e.type) ? e.value : '') || '').trim();
   const tag = e.tagName.toLowerCase();
   let selector = e.id ? tag + '[id=' + JSON.stringify(e.id) + ']' : '';
   if (!selector && e.name) selector = tag + '[name=' + JSON.stringify(e.name) + ']';
   if (!selector && text && text.length < 80) selector = tag + (e.type ? '[type=' + JSON.stringify(e.type) + ']' : '') + ':text-is(' + JSON.stringify(text) + ')';
   if (selector) selector += ':visible';
   return {tag, text:text.slice(0,100), selector, type:e.type || '', name:e.name || ''};
 }).filter(e=>e.selector)'''


def today():
    return datetime.now(KST).date()


def safe_reason(exc):
    reason = str(exc)
    for key, value in os.environ.items():
        if any(s in key.upper() for s in ('PASSWORD', 'TOKEN', 'SECRET', '_PW')) and len(value) > 3:
            reason = reason.replace(value, '[REDACTED]')
    # Browser exceptions may contain request URLs; don't print query credentials.
    return re.sub(r'https?://\S+', '[WMS URL]', reason)[:400]


def validate_rows(rows):
    for index, row in enumerate(rows[:15]):
        headers = [re.sub(r'\s+', '', str(v or '')) for v in row]
        if not REQUIRED <= set(headers):
            continue
        codes, stocks = headers.index('상품코드'), headers.index('재고량')
        count = 0
        for values in rows[index+1:]:
            if not any(v is not None and str(v).strip() for v in values):
                continue
            if len(values) <= max(codes, stocks) or not str(values[codes] or '').strip():
                raise ValueError('상품코드/재고량이 없는 데이터 행')
            quantity = money(values[stocks])
            if quantity < 0 or quantity != quantity.to_integral_value():
                raise ValueError('음수 또는 소수 재고량: 내보내기 화면/재고 정의 확인 필요')
            count += 1
        if not count:
            raise ValueError('재고 데이터가 비어 있어 기존 파일을 교체하지 않습니다')
        return index, headers, count
    raise ValueError('필수 재고 열 누락: ' + ', '.join(sorted(REQUIRED)))


def inventory_sheet(path):
    errors = []
    for name, rows in read_excel_sheets(path):
        try:
            index, headers, count = validate_rows(rows)
            return name, rows[index:], headers, count
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError('; '.join(errors) or '인식할 재고 시트가 없습니다')


def publish(source, target=None):
    """Normalize CSV/legacy XLS exports into a real XLSX for both filenames."""
    from openpyxl import Workbook
    target = target or today()
    _, rows, headers, count = inventory_sheet(source)
    if not set(EXPORT_HEADERS) <= set(headers):
        raise ValueError('상품관리 엑셀의 기존 9개 재고 헤더가 모두 포함되어야 합니다')
    folder = ROOT / 'raw_data/inventory'
    folder.mkdir(parents=True, exist_ok=True)
    canonical = folder / f'{target}_gaemi_inventory.xlsx'
    compatible = folder / f'{target}_3pl_inventory.xlsx'
    originals = {path: path.read_bytes() if path.exists() else None for path in (canonical, compatible)}
    committed = []
    staged = []
    book = Workbook()
    sheet = book.active
    sheet.title = '재고현황'
    sheet.append(headers)
    for row in rows[1:]:
        sheet.append(list(row))
    sheet.freeze_panes = 'A2'
    try:
        for output in (canonical, compatible):
            temp = output.with_suffix('.partial.xlsx')
            staged.append(temp)
            book.save(temp)
            inventory_sheet(temp)
        for temp, output in zip(staged, (canonical, compatible)):
            temp.replace(output)
            committed.append(output)
    except Exception:
        for output in reversed(committed):
            if originals[output] is None:
                output.unlink(missing_ok=True)
            else:
                restore = output.with_suffix('.partial.xlsx')
                restore.write_bytes(originals[output])
                restore.replace(output)
        raise
    finally:
        book.close()
        for temp in staged:
            temp.unlink(missing_ok=True)
    code_col, stock_col = headers.index('상품코드'), headers.index('재고량')
    data = [r for r in rows[1:] if len(r)>stock_col and str(r[code_col] or '').strip()]
    return {'status': 'downloaded', 'path': str(canonical), 'compatibility_path': str(compatible), 'rows': count,
            'product_codes':len({str(r[code_col]).strip() for r in data}),
            'total_stock':int(sum(money(r[stock_col]) for r in data)), 'headers':headers}


def fallback(reason):
    folder = ROOT / 'raw_data/inventory'
    candidates = []
    for path in folder.glob('*'):
        if not re.search(r'(gaemi|3pl)', path.name, re.I) or path.suffix.lower() not in ('.xlsx','.xls','.csv') or '.partial.' in path.name:
            continue
        try:
            stamp = datetime.strptime(path.name[:10], '%Y-%m-%d').date()
            candidates.append((stamp, path.stat().st_mtime_ns, path))
        except ValueError:
            continue
    for _, _, path in sorted(candidates, reverse=True):
        try:
            inventory_sheet(path)
        except Exception:
            continue
        LOG.warning('개미창고 수집 실패: %s. 기존 최신 파일 유지: %s. 후속 결산 계속', reason, path)
        return {'status': 'fallback', 'path': str(path), 'reason': reason}
    LOG.warning('개미창고 수집 실패: %s. 기존 유효 재고 파일 없음. 후속 결산 계속', reason)
    return {'status': 'fallback', 'path': None, 'reason': reason}


def snapshot_summary(report_date, root=None):
    """Expose the selected physical inventory snapshot in the formal JSON."""
    root = root or ROOT
    candidates = []
    for path in (root/'raw_data/inventory').glob('*_inventory.xlsx'):
        if not re.search(r'_(gaemi|3pl)_inventory\.xlsx$',path.name):
            continue
        try:
            stamp = datetime.strptime(path.name[:10],'%Y-%m-%d').date()
            if stamp <= report_date:
                candidates.append((stamp, '_gaemi_' in path.name, path))
        except ValueError:
            continue
    if not candidates:
        return None
    stamp,_,path=max(candidates)
    _,rows,headers,count=inventory_sheet(path)
    code_col,stock_col=headers.index('상품코드'),headers.index('재고량')
    data=[r for r in rows[1:] if len(r)>stock_col and str(r[code_col] or '').strip()]
    try:
        from .channel_whitelist import load_whitelist, normalize_code
    except ImportError:
        from channel_whitelist import load_whitelist, normalize_code
    allowed=load_whitelist(root/'master_data/오키드상품정보.xlsx')['gaemi']
    company=[r for r in data if normalize_code(r[code_col]) in allowed]
    return dict(source=str(path),snapshot_date=str(stamp),rows=count,
                product_codes=len({str(r[code_col]).strip() for r in data}),
                total_stock=int(sum(money(r[stock_col]) for r in data)),
                company_rows=len(company),company_product_codes=len({normalize_code(r[code_col]) for r in company}),
                company_stock=int(sum(money(r[stock_col]) for r in company)))


def frame_action(page, action):
    frame = next((f for f in page.frames if f.url == action.get('frame_url')), None)
    if frame is None:
        frames = page.frames
        index = action.get('frame_index', 0)
        if index >= len(frames):
            raise RuntimeError('저장된 iframe을 찾을 수 없습니다. --login-init 재실행 필요')
        frame = frames[index]
    return frame.locator(action['selector'])


def _inspect_inventory(page):
    """Capture controls, not input values, cookies, account text, or credentials."""
    found = []
    inventory_screen = False
    for index, frame in enumerate(page.frames):
        if frame.locator('input[type="password"]:visible').count():
            return None
        controls = frame.evaluate(CONTROL_SCRIPT)
        body = frame.locator('body').inner_text(timeout=3000)
        header = bool(re.search(r'상품\s*코드', body) and re.search(r'재고\s*(량|수량)', body))
        title = bool(re.search(r'(재고|상품)\s*(조회|현황|목록|관리)', frame.title()) or
                     frame.get_by_role('heading', name=re.compile(r'(재고|상품)\s*(조회|현황|목록|관리)')).count() or
                     ('product_list.html' in frame.url and '상품코드' in body))
        inventory_screen |= header or title
        for control in controls:
            control.update(frame_url=frame.url, frame_index=index)
            found.append(control)
    exports = [c for c in found if re.search(r'엑셀|excel|xlsx|csv', c['text'], re.I) and
               not re.search(r'업로드|등록|가이드|양식', c['text'])]
    queries = [c for c in found if c['text'] in ('조회','검색','재고조회','재고 조회')]
    override = os.getenv('GAEMI_WMS_EXPORT_SELECTOR')
    if override and page.locator(override).count():
        exports.insert(0, dict(selector=override, frame_url=page.url, frame_index=0, text='configured export'))
        inventory_screen = True
    if not inventory_screen or not exports:
        return None
    exports.sort(key=lambda c: (not bool(re.search('다운로드|저장|내려받기', c['text'])), len(c['text'])))
    if override:
        exports = sorted(exports, key=lambda c: c['selector'] != override)
    return dict(version=1, captured_at=datetime.now(KST).isoformat(), page_url=page.url,
                query=queries[0] if queries else None, export=exports[0],
                query_candidates=queries, export_candidates=exports, controls=found)


def inspect_inventory(page):
    try:
        return _inspect_inventory(page)
    except Exception:
        # Manual navigation can destroy a frame while the init loop inspects it.
        return None


def auto_login(page, start_url):
    user, password = os.getenv('GAEMI_WMS_ID'), os.getenv('GAEMI_WMS_PW')
    if not (user and password):
        return False
    for frame in page.frames:
        if urlsplit(frame.url).netloc != urlsplit(start_url).netloc:
            continue
        pw = frame.locator(os.getenv('GAEMI_WMS_PASSWORD_SELECTOR', 'input[type="password"]:visible'))
        if not pw.count():
            continue
        selector = os.getenv('GAEMI_WMS_ID_SELECTOR')
        username = frame.locator(selector or 'input[autocomplete="username"]:visible, input[type="email"]:visible, input[name*="user" i]:visible, input[name*="login" i]:visible, input[id*="user" i]:visible')
        if not username.count():
            form = pw.first.locator('xpath=ancestor::form')
            username = (form if form.count() else frame).locator('input[type="text"]:visible, input:not([type]):visible')
        if username.count() != 1:
            raise RuntimeError('로그인 ID 입력란을 특정할 수 없습니다. GAEMI_WMS_ID_SELECTOR 설정 필요')
        username.fill(user)
        pw.first.fill(password)
        submit = os.getenv('GAEMI_WMS_LOGIN_SELECTOR')
        if submit:
            frame.locator(submit).click()
        else:
            login = frame.get_by_role('button', name=re.compile(r'^\s*(로그인|LOGIN|Sign in)\s*$', re.I))
            if login.count() == 1:
                login.click()
            else:
                pw.first.press('Enter')
        LOG.info('개미창고 자동 로그인 제출 완료 (계정 정보는 기록하지 않음)')
        return True
    return False


def navigate_products(page):
    """Use the observed menu href; supports menus inside frames too."""
    for frame in page.frames:
        menu = frame.get_by_role('link', name=re.compile(r'^상품\s*(관리|목록|조회)$'))
        for index in range(menu.count()):
            item = menu.nth(index)
            href = item.get_attribute('href')
            if href and not href.startswith(('#','javascript:')):
                target = urljoin(frame.url, href)
                if urlsplit(target).netloc != urlsplit(frame.url).netloc:
                    continue
                page.goto(target, wait_until='domcontentloaded')
                LOG.info('개미창고 상품관리 메뉴 이동 완료')
                return
    if urlsplit(page.url).hostname in ('www.gemichango.com','gemichango.com'):
        page.goto(urljoin(page.url, PRODUCT_PATH), wait_until='domcontentloaded')
        return
    raise RuntimeError('상품관리/상품목록 메뉴를 찾을 수 없습니다')


def run(login_init=False, headed=False, login_timeout=900):
    try:
        load_env()
        url = os.getenv('GAEMI_WMS_URL', '').strip()
        if not urlsplit(url).hostname or urlsplit(url).scheme not in ('https','http') or urlsplit(url).username:
            raise ValueError('config/.env의 GAEMI_WMS_URL 설정 필요 (계정 정보 없는 http(s) URL)')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            PROFILE.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(str(PROFILE),
                channel=os.getenv('GAEMI_BROWSER_CHANNEL','chrome'), headless=not (login_init or headed),
                accept_downloads=True, locale='ko-KR', timezone_id='Asia/Seoul',
                args=['--restore-last-session'], timeout=60000)
            try:
                state = PROFILE / 'gaemi_session_state.json'
                if not login_init and state.exists():
                    context.add_cookies(json.loads(state.read_text(encoding='utf-8'))['cookies'])
                page = context.new_page()
                page.set_default_timeout(15000)
                page.set_default_navigation_timeout(60000)
                page.goto(url, wait_until='domcontentloaded')
                # Wait for form rendering, but do not delay an already authenticated page.
                deadline = time.monotonic() + 10
                submitted = False
                while time.monotonic() < deadline:
                    if inspect_inventory(page):
                        break
                    if not submitted and auto_login(page, url):
                        submitted = True
                        break
                    page.wait_for_timeout(500)
                if submitted:
                    for frame in page.frames:
                        frame.locator('input[type="password"]:visible').wait_for(state='hidden', timeout=30000)
                if urlsplit(page.url).hostname in ('www.gemichango.com','gemichango.com') or not inspect_inventory(page):
                    navigate_products(page)
                # Discover and refresh selectors in normal unattended mode too.
                deadline = time.monotonic() + 30
                snapshot = None
                while time.monotonic() < deadline:
                    snapshot = inspect_inventory(page)
                    if snapshot:
                        snapshot['configured_origin'] = urlsplit(url).netloc
                        write_json(SNAPSHOT, snapshot)
                        break
                    page.wait_for_timeout(500)
                if login_init:
                    LOG.info('상품관리 화면 자동 탐색 중. 최대 %s초 대기합니다.', login_timeout)
                    deadline = time.monotonic() + login_timeout
                    while time.monotonic() < deadline:
                        for candidate in context.pages:
                            snapshot = inspect_inventory(candidate)
                            if snapshot:
                                snapshot['configured_origin'] = urlsplit(url).netloc
                                write_json(SNAPSHOT, snapshot)
                                context.storage_state(path=str(state))
                                LOG.info('재고 화면 URL 및 조회/엑셀 선택자 저장: %s', SNAPSHOT)
                                return {'status':'initialized', 'snapshot':str(SNAPSHOT), 'query_detected':bool(snapshot['query'])}
                        page.wait_for_timeout(750)
                    raise RuntimeError('상품관리 화면 캡처 시간 초과. 로그인/2차 인증 및 엑셀 버튼 확인 필요')
                if not SNAPSHOT.exists():
                    raise RuntimeError('재고 화면 DOM 미등록. 아침자동실행.bat --gaemi-login-init 실행 필요')
                snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
                if snapshot.get('configured_origin', urlsplit(snapshot['page_url']).netloc) != urlsplit(url).netloc:
                    raise RuntimeError('WMS 호스트가 캡처 당시와 다릅니다. --login-init 재실행 필요')
                page.goto(snapshot['page_url'], wait_until='domcontentloaded')
                if page.locator('input[type="password"]:visible').count():
                    raise RuntimeError('세션 만료 또는 로그인 실패. --gaemi-login-init 재실행 필요')
                query = snapshot.get('query')
                keyword=page.locator('input[name="stxt"]:visible')
                if urlsplit(page.url).hostname in ('www.gemichango.com','gemichango.com') and keyword.count()==1:
                    keyword.fill('')
                if os.getenv('GAEMI_WMS_QUERY_SELECTOR'):
                    query = dict(selector=os.environ['GAEMI_WMS_QUERY_SELECTOR'], frame_url=page.url)
                if query:
                    frame_action(page, query).click()
                    try:
                        page.wait_for_load_state('networkidle', timeout=15000)
                    except Exception:
                        raise RuntimeError('재고 조회 완료를 확인하지 못했습니다. 기존 파일을 유지합니다') from None
                export = snapshot['export']
                if os.getenv('GAEMI_WMS_EXPORT_SELECTOR'):
                    export = dict(selector=os.environ['GAEMI_WMS_EXPORT_SELECTOR'], frame_url=page.url)
                downloads = []
                def export_dialog(dialog):
                    if dialog.type == 'confirm' and dialog.message == '검색 조건이 없습니다. 현재 화면에 표시된 모든 데이터를 다운로드하시겠습니까?':
                        dialog.accept()
                        LOG.info('상품관리 전체 데이터 다운로드 확인창 승인')
                    else:
                        dialog.dismiss()
                        LOG.warning('예상하지 못한 WMS 확인창을 닫았습니다. 다운로드 흐름 확인 필요')
                def track(new_page):
                    new_page.on('download', lambda item: downloads.append(item))
                    new_page.on('dialog',export_dialog)
                for existing in context.pages:
                    track(existing)
                context.on('page', track)
                frame_action(page, export).click()
                deadline = time.monotonic()+180
                while not downloads and time.monotonic()<deadline:
                    page.wait_for_timeout(500)
                if not downloads:
                    raise RuntimeError('엑셀 다운로드 시간 초과: 버튼/공지 팝업/세션 확인 필요')
                folder = ROOT/'raw_data/inventory'
                folder.mkdir(parents=True,exist_ok=True)
                temp = folder/'gaemi_download.partial.xlsx'
                try:
                    downloads[0].save_as(str(temp))
                    result = publish(temp)
                    if result['headers'] != EXPORT_HEADERS:
                        LOG.warning('개미창고 추가/변경된 헤더 감지: %s',result['headers'])
                    snapshot['verified_headers'] = result['headers']
                    snapshot['download_verified_at'] = datetime.now(KST).isoformat()
                    snapshot['download_filename'] = downloads[0].suggested_filename
                    snapshot['download_summary'] = {k:result[k] for k in ('rows','product_codes','total_stock')}
                    write_json(SNAPSHOT,snapshot)
                    context.storage_state(path=str(state))
                    LOG.info('개미창고 재고 %s행 저장 완료: %s',result['rows'],result['path'])
                    return result
                finally:
                    temp.unlink(missing_ok=True)
            finally:
                context.close()
    except Exception as exc:
        reason = safe_reason(exc)
        if login_init:
            LOG.error('개미창고 초기 설정 실패: %s', reason)
            return {'status':'init_failed','reason':reason}
        return fallback(reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login-init',action='store_true')
    parser.add_argument('--headed',action='store_true')
    parser.add_argument('--login-timeout',type=int,default=900)
    parser.add_argument('--require-download','--require-downloads',dest='require_download',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s [%(levelname)s] %(message)s')
    result=run(args.login_init,args.headed,args.login_timeout)
    print(json.dumps(result,ensure_ascii=False))
    return 1 if result['status']=='init_failed' else (2 if args.require_download and result['status']!='downloaded' else 0)


if __name__=='__main__':
    sys.exit(main())
