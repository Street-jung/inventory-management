"""Inspect an authenticated export menu using the test state snapshot."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
state = json.loads((root/'scripts/wing_test_profile_restore/wing_session_state.json').read_text(encoding='utf-8'))
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(str(root/'scripts/wing_probe_profile'),channel='chrome',headless=False)
    try:
        context.add_cookies(state['cookies'])
        page = context.new_page()
        page.goto('https://wing.coupang.com/tenants/business-insight/sales-analysis?start_date=2026-11-01&end_date=2026-11-15',wait_until='domcontentloaded')
        page.get_by_text('엑셀 다운로드',exact=True).wait_for(timeout=45000)
        page.get_by_text('엑셀 다운로드',exact=True).click()
        page.wait_for_timeout(1000)
        data = page.evaluate('''() => [...document.querySelectorAll('*')].filter(e=>e.offsetWidth&&e.offsetHeight&&e.children.length===0&&/엑셀|상품별|다운로드|판매자배송|로켓그로스/.test(e.innerText||'')).map(e=>e.outerHTML)''')
        (root/'scripts/wing_diagnostics/export_menu.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(data,ensure_ascii=False,indent=2),flush=True)
    finally:
        context.close()
