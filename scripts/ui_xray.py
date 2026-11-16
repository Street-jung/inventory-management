"""Dashboard screenshots and heuristic DOM diagnostics. Run with --help."""
import argparse
import json
import sys
import shutil
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent / '.wing_deps'))
    from playwright.sync_api import sync_playwright

DEFAULT_URL = 'https://inventory-management-q7bp.onrender.com/share/inventory'
MEASURE = r"""() => {
 const vw = document.documentElement.clientWidth;
 const label = e => e.tagName.toLowerCase() + (e.id ? '#'+e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.'+e.className.trim().split(/\s+/).join('.') : '');
 const visible = e => {const r=e.getBoundingClientRect(), s=getComputedStyle(e); return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden' && s.opacity!=='0'};
 const elements=[...document.body.querySelectorAll('*')].filter(visible);
 const box = e => { const r=e.getBoundingClientRect(); return {element:label(e),width:+r.width.toFixed(2),height:+r.height.toFixed(2),x:+r.x.toFixed(2),y:+(r.y+scrollY).toFixed(2)}; };
 const overflow=elements.filter(e=>{const r=e.getBoundingClientRect();return r.right>vw+1 || r.left < -1}).map(box);
 const textEls=elements.filter(e=>[...e.childNodes].some(n=>n.nodeType===3 && n.textContent.trim()));
 const small=textEls.filter(e=>parseFloat(getComputedStyle(e).fontSize)<12).map(e=>({...box(e),fontSize:getComputedStyle(e).fontSize,text:e.textContent.trim().slice(0,100)}));
 // Leaf text/controls only. Exclude ancestors and deliberately clipped content.
 const candidates=elements.filter(e=>e.matches('button,input,select,textarea,img') || (textEls.includes(e)&&!e.querySelector('button,input,select,textarea')));
 const limit=1500, sample=candidates.slice(0,limit), overlaps=[];
 const clipped=e=>{for(let p=e.parentElement;p;p=p.parentElement){const s=getComputedStyle(p);if (/(hidden|clip|auto|scroll)/.test(s.overflow+s.overflowX+s.overflowY)) return true;}return false;};
 const rects=sample.map(e=>e.getBoundingClientRect());
 let total=0;
 for(let i=0;i<sample.length;i++) for(let j=i+1;j<sample.length;j++){
   const a=sample[i],b=sample[j]; if(a.contains(b)||b.contains(a))continue;
   const r=rects[i],s=rects[j],w=Math.min(r.right,s.right)-Math.max(r.left,s.left),h=Math.min(r.bottom,s.bottom)-Math.max(r.top,s.top);
   if(w>2&&h>2&&!clipped(a)&&!clipped(b)){total++;if(overlaps.length<200)overlaps.push({a:box(a),b:box(b),intersection:{width:+w.toFixed(2),height:+h.toFixed(2)}});}
 }
 const lines=[];
 function tree(e,d){if(d>4||lines.length>=100||!visible(e))return;lines.push('  '.repeat(d)+label(e)+' '+Math.round(e.getBoundingClientRect().width)+'×'+Math.round(e.getBoundingClientRect().height));for(const c of e.children)tree(c,d+1);}
 tree(document.body,0);
 return {url:location.href,title:document.title,viewport:vw,scrollWidth:document.documentElement.scrollWidth,overflowDetected:document.documentElement.scrollWidth>vw+1,overflow,small,overlaps,overlapTotal:total,overlapCandidates:candidates.length,overlapSample:sample.length,dom:lines,iframes:[...document.querySelectorAll('iframe')].map(e=>({src:e.src,title:e.title}))};
}"""


def settle(page, timeout):
    page.wait_for_load_state('domcontentloaded', timeout=timeout)
    try:
        page.wait_for_load_state('networkidle', timeout=min(timeout, 8000))
    except Exception:
        pass  # Polling dashboards may never become idle.
    page.wait_for_timeout(2000)
    page.evaluate('async () => { if(document.fonts) await document.fonts.ready; }')
    # Trigger lazy-loaded sections without relying on an unbounded scroll loop.
    page.evaluate('async () => {const h=Math.min(document.documentElement.scrollHeight,30000);for(let y=0;y<h;y+=innerHeight){scrollTo(0,y);await new Promise(r=>setTimeout(r,80));}scrollTo(0,0);}')
    page.wait_for_timeout(800)


def select_dashboard(page):
    candidates = []
    for frame in page.frames:
        if frame == page.main_frame or not frame.url.startswith(('http://', 'https://')):
            continue
        try:
            el = frame.frame_element()
            bounds = el.bounding_box()
            if not bounds or bounds['width'] < 250 or bounds['height'] < 120 or not el.is_visible():
                continue
            info = frame.evaluate('() => ({text:document.body?.innerText.length||0,controls:document.querySelectorAll("table,canvas,svg,input,button,[role=grid]").length})')
            depth, p = 0, frame.parent_frame
            while p:
                depth += 1
                p = p.parent_frame
            score = bounds['width'] * bounds['height'] + min(info['text'], 20000)*20 + min(info['controls'],100)*2000 + depth*10000
            if any(s in frame.url.lower() for s in ('accounts.google.', 'doubleclick.', 'recaptcha')):
                continue
            candidates.append((score, frame.url))
        except Exception:
            continue
    return max(candidates)[1] if candidates else None


def capture(browser, args, mobile, tab=None):
    name = 'mobile' if mobile else 'pc'
    filename = f'{name}_{tab}.png' if tab else f'{name}_view.png'
    options = dict(viewport={'width':390 if mobile else 1920,'height':844 if mobile else 1080}, device_scale_factor=1)
    if args.storage_state:
        options['storage_state'] = str(args.storage_state)
    if mobile:
        options.update(is_mobile=True, has_touch=True, user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
    context = browser.new_context(**options)
    page = context.new_page()
    errors, trace, warnings = [], [], []
    page.on('console', lambda msg: errors.append({'type':'console.error','message':msg.text,'location':msg.location}) if msg.type=='error' else None)
    page.on('pageerror', lambda exc: errors.append({'type':'pageerror','message':str(exc)}))
    try:
        url = args.url
        for step in range(5):
            response = page.goto(url, wait_until='domcontentloaded', timeout=args.timeout)
            if mobile:
                injected = page.evaluate('''() => {
                    if(document.querySelector('meta[name="viewport"]')) return false;
                    const m=document.createElement('meta');m.name='viewport';
                    m.content='width=device-width, initial-scale=1';document.head.appendChild(m);return true;
                }''')
                if injected:
                    warnings.append('390px 계측을 위해 viewport 메타가 없는 문서에 width=device-width를 임시 적용했습니다.')
            if response and response.status >= 400:
                warnings.append(f'HTTP {response.status}: {url}')
            settle(page, args.timeout)
            trace.append(page.url)
            target = select_dashboard(page)
            if not target or target in trace:
                break
            if step == 4:
                warnings.append('iframe 추적 한도(5단계) 도달. 최종 프레임 확인 필요.')
                break
            url = target
        if 'accounts.google.com' in page.url:
            warnings.append('로그인 화면이 표시되었습니다. 실제 대시보드는 검증되지 않았습니다.')
        if tab:
            page.wait_for_function("typeof switchDashboardTab === 'function' && typeof currentDashboardData !== 'undefined' && currentDashboardData !== null && typeof allMasterData !== 'undefined' && allMasterData.length > 0", timeout=args.timeout)
            page.evaluate('(tab) => switchDashboardTab(tab)', tab)
            page.locator(f'#tabContent_{tab}').wait_for(state='visible', timeout=args.timeout)
            if tab == 'inventory':
                page.evaluate('() => { setMobileInventoryView(false); renderMasterTable(); }')
            settle(page, args.timeout)
        data = page.evaluate(MEASURE)
        data['dashboardChecks'] = page.evaluate('''() => {
            const table=document.getElementById('masterTable'), wrap=document.getElementById('masterTableScrollWrap');
            const cards=[...document.querySelectorAll('#mobileMasterList article')];
            const shown=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
            return {activeTab:[...document.querySelectorAll('[id^="tabContent_"]')].filter(shown).map(e=>e.id),
                tableWidth:table?.getBoundingClientRect().width,containerWidth:wrap?.clientWidth,
                compactDefault:!document.body.classList.contains('mobile-full-table'),
                renderedCards:cards.length,visibleCards:cards.filter(shown).length,
                clippedNames:cards.filter(shown).filter(e=>{const h=e.querySelector('h3');return h.scrollWidth>h.clientWidth+1}).length,
                clippedStatus:[...document.querySelectorAll('.master-status-badge')].filter(shown).filter(e=>e.scrollWidth>e.clientWidth+1).length};
        }''')
        page.screenshot(path=str(args.output / filename), full_page=True, timeout=args.timeout)
        if tab == 'inventory':
            shutil.copyfile(args.output / filename, args.output / f'{name}_view.png')
        data['screenshot'] = filename
        return dict(data=data, trace=trace, errors=errors, warnings=warnings)
    finally:
        context.close()


def block(value):
    return '```json\n' + json.dumps(value, ensure_ascii=False, indent=2) + '\n```\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?', default=DEFAULT_URL, help='Target URL (local server URLs supported)')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'ui_preview')
    parser.add_argument('--timeout', type=int, default=60000, help='Navigation/screenshot timeout in milliseconds')
    parser.add_argument('--storage-state', type=Path, help='Existing Playwright authentication state JSON (keep private)')
    parser.add_argument('--all-tabs', action='store_true', help='Capture sales/inventory/cashflow/pnl on the main dashboard URL')
    args = parser.parse_args()
    if args.all_tabs:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(args.url)
        args.url = urlunsplit((parts.scheme, parts.netloc, '/', '', ''))
    args.output.mkdir(parents=True, exist_ok=True)
    report = ['# UI X-Ray 상태 리포트', f'실행 시각: {datetime.now().astimezone().isoformat()}', f'요청 URL: {args.url}', '모바일: 390×844 CSS px, iPhone 모바일 UA/터치 에뮬레이션. PC: 1920×1080. PNG 배율: 1. Chromium 기반이며 실제 iOS Safari 검증은 아닙니다.', 'iframe은 표시 면적·콘텐츠·깊이를 기준으로 선택하고 해당 URL로 이동해 전체 페이지를 촬영합니다. 원본 임베드 영역의 크기 제약과는 다를 수 있습니다. 추적 경로를 확인하세요.']
    failed = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                jobs = [(mobile, tab) for tab in ('sales', 'inventory', 'cashflow', 'pnl') for mobile in (False, True)] if args.all_tabs else [(False, None), (True, None)]
                for mobile, tab in jobs:
                    name = ('mobile' if mobile else 'pc') + (f' / {tab}' if tab else '')
                    report.append(f'## {name}')
                    # Remove stale images so an earlier run cannot appear successful.
                    prefix = 'mobile' if mobile else 'pc'
                    (args.output / (f'{prefix}_{tab}.png' if tab else f'{prefix}_view.png')).unlink(missing_ok=True)
                    if tab == 'inventory':
                        (args.output / f'{prefix}_view.png').unlink(missing_ok=True)
                    try:
                        result = capture(browser, args, mobile, tab)
                        d = result['data']
                        report.extend(['### URL / iframe 추적', block(result['trace']), f"제목: {d['title']}", f"가로 Overflow: {'발생' if d['overflowDetected'] else '없음'} (viewport={d['viewport']}px, scrollWidth={d['scrollWidth']}px)", '### 뷰포트 밖으로 나간 요소 (태그/클래스 및 계산된 border-box 크기)', block(d['overflow']), '### 12px 미만 글자', block(d['small']), f"### BoundingBox 충돌 의심: {d['overlapTotal']}건", f"후보 {d['overlapCandidates']}개 중 {d['overlapSample']}개 검사, 결과는 최대 200건 표시. 부모·자식 및 스크롤/클리핑 컨테이너 내 충돌은 제외합니다. 의도적 배지·레이어는 오탐, 제외/샘플링된 영역은 누락 가능하므로 스크린샷과 함께 검토하세요.", block(d['overlaps']), '### 브라우저 콘솔 및 JavaScript 오류', block(result['errors']), '### 로딩/인증 경고', block(result['warnings']), '### 주요 컨테이너 DOM (깊이 4, 최대 100개)', '```text\n'+'\n'.join(d['dom'])+'\n```', '### 남아 있는 iframe', block(d['iframes'])])
                        report.extend(['### 대시보드 뷰 검증', block(d['dashboardChecks']), f"스크린샷: {d['screenshot']}"])
                        print(f'Created {d["screenshot"]} ({d["url"]})', flush=True)
                    except Exception as exc:
                        failed = True
                        report.append(f'**캡처 실패**: {type(exc).__name__}: {exc}')
            finally:
                browser.close()
    except Exception as exc:
        failed = True
        report.append(f'**브라우저 실행 실패**: {exc}')
    finally:
        (args.output / 'ui_report.md').write_text('\n\n'.join(report)+'\n', encoding='utf-8')
    print(f'Report: {args.output / "ui_report.md"}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
