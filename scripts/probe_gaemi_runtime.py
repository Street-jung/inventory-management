"""Inspect the configured WMS with its own saved session; never log credentials."""
import argparse
import json
from urllib.parse import urlsplit, urljoin
from playwright.sync_api import sync_playwright
import fetch_gaemi_wms as gaemi

parser=argparse.ArgumentParser()
parser.add_argument('--path')
parser.add_argument('--isolated',action='store_true')
parser.add_argument('--export-inspect',action='store_true')
args=parser.parse_args()
gaemi.load_env()
url=gaemi.os.environ['GAEMI_WMS_URL']
with sync_playwright() as p:
    profile=gaemi.ROOT/'scripts/gaemi_probe_profile' if args.isolated else gaemi.PROFILE
    context=p.chromium.launch_persistent_context(str(profile),channel='chrome',headless=True,
                                               args=['--restore-last-session'],accept_downloads=True,timeout=60000)
    try:
        state=gaemi.PROFILE/'gaemi_session_state.json'
        if state.exists():
            context.add_cookies(json.loads(state.read_text(encoding='utf-8'))['cookies'])
        page=context.new_page()
        page.goto(url,wait_until='domcontentloaded',timeout=60000)
        page.wait_for_timeout(1500)
        if gaemi.auto_login(page,url):
            for frame in page.frames:
                frame.locator('input[type="password"]:visible').wait_for(state='hidden',timeout=30000)
        page.wait_for_timeout(2000)
        if args.path:
            target=urljoin(page.url,args.path)
            if urlsplit(target).netloc!=urlsplit(url).netloc:
                raise RuntimeError('Different WMS host')
            page.goto(target,wait_until='domcontentloaded',timeout=60000)
            page.wait_for_timeout(2000)
        result={'page_url':page.url,'frames':[]}
        if args.export_inspect:
            downloads=[]
            dialogs=[]
            def dialog(item):
                dialogs.append(item.message)
                if gaemi.re.search('엑셀|다운로드|csv',item.message,gaemi.re.I):
                    item.accept()
                else:
                    item.dismiss()
            page.on('dialog',dialog)
            page.on('download',lambda item:downloads.append(item))
            page.get_by_role('button',name='엑셀다운로드',exact=True).click()
            page.wait_for_timeout(3000)
            print('DIALOGS',dialogs,flush=True)
            if downloads:
                source=gaemi.ROOT/'scripts/wing_diagnostics/gaemi_probe_download.xlsx'
                downloads[0].save_as(str(source))
                print('DOWNLOAD',downloads[0].suggested_filename,gaemi.inventory_sheet(source)[2:],flush=True)
        for frame in page.frames:
            controls=frame.evaluate(gaemi.CONTROL_SCRIPT)
            links=frame.locator('a').evaluate_all('(els)=>els.filter(e=>e.getClientRects().length).map(e=>({text:(e.innerText||e.title||"").trim(),href:e.getAttribute("href")})).filter(e=>e.text)')
            result['frames'].append({'url':frame.url,'title':frame.title(),'controls':controls,'links':links,
                                      'body':frame.locator('body').inner_text(),
                                      'exports':frame.get_by_role('button',name=gaemi.re.compile('엑셀|CSV',gaemi.re.I)).evaluate_all('(els)=>els.map(e=>e.outerHTML)'),
                                      'export_scripts':frame.locator('script').evaluate_all('(els)=>els.map(e=>e.textContent||"").filter(t=>/function\\s+(export|download)|csv|엑셀다운로드/i.test(t))')})
        output=gaemi.ROOT/'scripts/wing_diagnostics/gaemi_probe.json'
        gaemi.write_json(output,result)
        for frame in result['frames']:
            print('PAGE',frame['url'],'TITLE',frame['title'])
            print('LINKS',json.dumps([c for c in frame['links'] if gaemi.re.search('상품|엑셀|CSV|csv|다운|조회|검색',c['text'])][:30],ensure_ascii=False))
            print('CONTROLS',json.dumps([c for c in frame['controls'] if gaemi.re.search('엑셀|CSV|csv|다운|조회|검색',c['text'])],ensure_ascii=False))
            print('BODY END',frame['body'][-2500:])
            print('EXPORT HTML',frame['exports'])
        context.storage_state(path=str(state))
    finally:
        context.close()
