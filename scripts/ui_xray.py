"""Capture three dashboard tabs on desktop/mobile and report DOM diagnostics."""
import argparse
import json
import os
import sys
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent / '.wing_deps'))
    from playwright.sync_api import sync_playwright

REMOTE_URL = 'https://inventory-management-q7bp.onrender.com/'
TABS = {'sales': '일일 상품 매출', 'cashflow': '일일 현금흐름', 'pnl': '월 손익결산'}


def default_url():
    try:
        with socket.create_connection(('127.0.0.1', 5000), timeout=1):
            return 'http://127.0.0.1:5000/'
    except OSError:
        return REMOTE_URL


def block(value):
    return '```json\n' + json.dumps(value, ensure_ascii=False, indent=2) + '\n```'


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

def capture(browser, args, mobile, report):
    name = 'mobile' if mobile else 'pc'
    options = dict(viewport={'width': 390 if mobile else 1920, 'height': 844 if mobile else 1080}, device_scale_factor=1)
    if mobile:
        options.update(is_mobile=True, has_touch=True, user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
    if args.storage_state:
        options['storage_state'] = str(args.storage_state)
    context = browser.new_context(**options)
    errors, responses = [], []
    failed = False
    try:
        origin = urlsplit(args.url)
        auth = context.request.post(f'{origin.scheme}://{origin.netloc}/api/admin/verify-pin', data={'pin': os.environ.get('ADMIN_PIN', '1500')}, timeout=args.timeout)
        token = auth.json().get('token') if auth.ok else None
        if not token:
            raise RuntimeError(f'관리자 PIN 인증 실패: HTTP {auth.status} (PIN/토큰은 리포트에 저장하지 않음)')
        context.add_init_script('sessionStorage.setItem("iljeomodo_admin_token", ' + json.dumps(token) + ');')
        # Track actual page requests and completion, including failed/empty responses.
        context.add_init_script('''(() => {
            window.__xrayApi = {};
            const original = window.fetch;
            window.fetch = async function(...args) {
                const path = new URL(typeof args[0] === 'string' ? args[0] : args[0].url, location.href).pathname;
                if (!['/api/dashboard-data','/api/summary'].includes(path)) return original.apply(this,args);
                window.__xrayApi[path] = {done:false};
                try {
                    const r = await original.apply(this,args);
                    let body = null; try { body = await r.clone().json(); } catch(e) {}
                    window.__xrayApi[path] = {done:true,status:r.status,body};
                    return r;
                } catch(e) { window.__xrayApi[path] = {done:true,error:String(e)}; throw e; }
            };
        })();''')
        page = context.new_page()
        page.on('console', lambda m: errors.append({'type':'console.error','message':m.text}) if m.type == 'error' else None)
        page.on('pageerror', lambda e: errors.append({'type':'pageerror','message':str(e)}))
        page.on('response', lambda r: responses.append({'path':urlsplit(r.url).path,'status':r.status}) if urlsplit(r.url).path in ('/api/dashboard-data','/api/summary') else None)
        page.goto(args.url, wait_until='domcontentloaded', timeout=args.timeout)
        warnings = []
        try:
            page.wait_for_function("['/api/dashboard-data','/api/summary'].every(p => window.__xrayApi?.[p]?.done)", timeout=args.timeout)
            page.wait_for_function("typeof switchDashboardTab === 'function' && (window.__xrayApi['/api/dashboard-data'].status !== 200 || (typeof currentDashboardData !== 'undefined' && currentDashboardData !== null)) && (window.__xrayApi['/api/summary'].status !== 200 || typeof allMasterData !== 'undefined')", timeout=args.timeout)
        except Exception:
            warnings.append('API 응답 또는 렌더링 대기 시간 초과. 아래 로드 상태 확인 필요.')
            failed = True
        page.evaluate('async () => {if(document.fonts) await document.fonts.ready;}')
        for tab, title in TABS.items():
            report.append(f'## {title} / {name}')
            try:
                page.evaluate('(tab) => switchDashboardTab(tab)', tab)
                page.locator(f'#tabContent_{tab}').wait_for(state='visible', timeout=args.timeout)
                page.wait_for_timeout(500)
                if page.locator('#pinModal').is_visible():
                    raise RuntimeError('PIN 모달이 표시되어 촬영을 중단함')
                page.evaluate('window.scrollTo(0,0)')
                data = page.evaluate(MEASURE)
                api = page.evaluate('''(tab) => {
                    const numeric = v => typeof v === 'number' ? [v] : v && typeof v === 'object' ? Object.values(v).flatMap(numeric) : [];
                    const states = Object.fromEntries(Object.entries(window.__xrayApi || {}).map(([k,v]) => {
                        const nums = numeric(v.body);
                        return [k, {done:v.done,status:v.status,error:v.error,keys:v.body && typeof v.body === 'object' ? Object.keys(v.body) : [],numericCount:nums.length,nonzeroNumericCount:nums.filter(n=>n!==0).length,mastersCount:v.body?.masters?.length}];
                    }));
                    const el = document.querySelector('#tabContent_'+tab);
                    return {states, dashboardAssigned:typeof currentDashboardData !== 'undefined' && currentDashboardData !== null, renderedText:el.innerText.slice(0,12000),zeroWonOccurrences:(el.innerText.match(/(?:^|[^0-9,])0원/g)||[]).length};
                }''', tab)
                filename = f'{tab}_{name}.png'
                page.screenshot(path=str(args.output / filename), full_page=True, timeout=args.timeout)
                state = api['states'].get('/api/dashboard-data', {})
                status = '정상 응답 및 데이터 렌더링' if state.get('status') == 200 and api['dashboardAssigned'] and state.get('nonzeroNumericCount', 0) else '404/로드 실패/빈 데이터 또는 0원 표시 확인 필요'
                report.extend([f'스크린샷: [{filename}]({filename})', f"가로 Overflow: {'발생' if data['overflowDetected'] else '없음'} (viewport={data['viewport']}px, scrollWidth={data['scrollWidth']}px)", '### 뷰포트 밖 요소와 계산된 width (CSS px)', block(data['overflow']), '### 12px 미만 글자', block(data['small']), f"### BoundingBox 겹침 경고: {data['overlapTotal']}건", f"후보 {data['overlapCandidates']}개 중 {data['overlapSample']}개 검사, 최대 200건 표시. 조상·자손 및 스크롤/클리핑 컨테이너 내부는 제외한 휴리스틱이므로 이미지와 함께 검토.", block(data['overlaps']), f'### 데이터 로드 상태: {status}', block(api), '### API HTTP 응답', block(responses), '### 브라우저 콘솔/JavaScript 오류 (해상도 세션 누적)', block(errors), '### 로딩 경고', block(warnings)])
                print(f'Created {filename}: viewport={data["viewport"]}, scrollWidth={data["scrollWidth"]}, API={state.get("status")}', flush=True)
            except Exception as exc:
                failed = True
                report.append(f'**촬영 실패**: {type(exc).__name__}: {exc}')
    finally:
        context.close()
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', nargs='?', help='Target URL (legacy positional argument)')
    parser.add_argument('--url', help='Target URL; default local root if listening, otherwise Render root')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'ui_preview')
    parser.add_argument('--timeout', type=int, default=60000)
    parser.add_argument('--storage-state', type=Path)
    parser.add_argument('--all-tabs', action='store_true', help='Compatibility flag; three dashboard tabs are always captured')
    args = parser.parse_args()
    args.url = args.url or args.target or default_url()
    args.output.mkdir(parents=True, exist_ok=True)
    for tab in TABS:
        for name in ('pc', 'mobile'):
            (args.output / f'{tab}_{name}.png').unlink(missing_ok=True)
    report = ['# 메인 대시보드 UI X-Ray 상태 리포트', f'실행: {datetime.now().astimezone().isoformat()}', f'대상: {args.url}', 'PC 1920×1080 / 모바일 390×844 CSS px, iPhone UA·터치 에뮬레이션. Chromium 기반이며 실제 iOS Safari 검증은 별도. 인증 토큰은 저장하지 않음. 가로 스크롤 영역 내부 요소도 뷰포트 이탈 목록에 포함.']
    failed = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for mobile in (False, True):
                    try:
                        failed = capture(browser, args, mobile, report) or failed
                    except Exception as exc:
                        failed = True
                        report.append(f'**{"mobile" if mobile else "pc"} 세션 실패**: {type(exc).__name__}: {exc}')
            finally:
                browser.close()
    except Exception as exc:
        failed = True
        report.append(f'**실행 실패**: {exc}')
    path = args.output / 'ui_report_tabs.md'
    path.write_text('\n\n'.join(report) + '\n', encoding='utf-8')
    print(f'Report: {path}', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
