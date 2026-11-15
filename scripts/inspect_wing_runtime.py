"""One-shot DOM inspection for the explicitly selected test profile."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
profile = Path(os.environ['COUPANG_PROFILE_DIR'])
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(str(profile), channel='chrome', headless=False,
                                                  args=['--restore-last-session'], accept_downloads=True)
    try:
        page = context.new_page()
        for name, url in [('sales', 'https://wing.coupang.com/tenants/business-insight/sales-analysis?start_date=2026-11-01&end_date=2026-11-15'),
                          ('inventory', 'https://wing.coupang.com/tenants/rfm-inventory/management/list')]:
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(8000)
            details = {'url': page.url, 'body': page.locator('body').inner_text(),
                       'controls': page.locator('button,a,input,select,[role="button"]').evaluate_all(
                           '(els)=>els.filter(e=>e.offsetWidth&&e.offsetHeight).map(e=>({tag:e.tagName,text:(e.innerText||"").trim(),id:e.id,cls:e.className,href:e.getAttribute("href"),type:e.type,placeholder:e.getAttribute("placeholder"),value:e.type==="date"?e.value:undefined}))')}
            (ROOT / 'scripts/wing_diagnostics' / f'inspect_{name}.json').write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding='utf-8')
            print(name, page.url, details['body'][-12000:], flush=True)
            if 'wing.coupang.com' == __import__('urllib.parse',fromlist=['urlparse']).urlparse(page.url).hostname:
                context.storage_state(path=str(profile/'wing_session_state.json'))
    finally:
        context.close()
