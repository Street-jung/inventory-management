import os
from playwright.sync_api import sync_playwright, TimeoutError

# ---------------------------------------------------------
# [이곳이 제품을 추가하는 '칸' 입니다]
# 엑셀이나 텍스트에서 복사한 옵션 ID를 그대로 아래에 붙여넣으시면 됩니다.
# 나중에 제품이 추가되면 엔터를 치고 계속 번호를 적어주세요.
# ---------------------------------------------------------
TARGET_IDS_INPUT = """
95893436821
95893661910
95893661912
95893435622
95893649650
95893649651
95893433438
95893642827
95893642826
95893427561
95893640100
95893640102
95856286081
95856286079
95856286082
95856286078
95856286080
95856286077
95856264111
95856264109
95856264110
95856248619
95856248617
95856248618
95856228901
95856228900
95856228899
95844803290
95849997061
95849997062
"""
# (위 목록은 알려주신 ID 중 일부 예시입니다. 가지고 계신 전체 목록을 저 사이에 덮어쓰기 하시면 됩니다.)

# 텍스트를 줄바꿈 단위로 자르고, 빈 칸을 없앤 뒤, 중복을 자동 제거(set) 합니다.
TARGET_OPTION_IDS = set([opt_id.strip() for opt_id in TARGET_IDS_INPUT.strip().split('\n') if opt_id.strip()])
print(f"📦 설정된 고유 타겟 옵션 ID는 총 {len(TARGET_OPTION_IDS)}개 입니다. (중복 자동 제거됨)")
# ---------------------------------------------------------

AUTH_FILE = 'auth.json'
LOGIN_URL = 'https://wing.coupang.com/login'
# 실제 쿠팡 윙의 '재고 관리' 또는 '상품 조회' 메뉴 URL로 확인 후 변경 필요
INVENTORY_URL = 'https://wing.coupang.com/vendor-inventory/inventory-management'

def main():
    with sync_playwright() as p:
        # 브라우저 띄우기 (headless=False)
        browser = p.chromium.launch(headless=False)
        
        # [STEP 1] 세션(쿠키) 로드 또는 초기화
        if os.path.exists(AUTH_FILE):
            print("🔒 기존 세션(auth.json)을 불러옵니다. 로그인을 건너뜁니다.")
            context = browser.new_context(storage_state=AUTH_FILE)
            page = context.new_page()
        else:
            print("🔑 저장된 세션이 없습니다. 수동 로그인을 진행해 주세요.")
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL)
            
            print("⏳ 브라우저에서 아이디/비밀번호 입력 및 OTP 인증을 완료해 주세요. (대기 시간 3분)")
            try:
                # 로그인 완료 후 쿠팡 윙 대시보드 URL로 변경될 때까지 대기
                page.wait_for_url("**/dashboard**", timeout=180000) 
                print("✅ 로그인이 감지되었습니다!")
            except TimeoutError:
                # URL 패턴이 다르거나 시간이 초과된 경우 수동 개입
                print("❌ 자동 로그인 감지 시간(3분)이 초과되었거나 대시보드 URL 패턴이 다릅니다.")
                input("브라우저에서 로그인이 완료되었다면 터미널 창에서 [Enter] 키를 눌러주세요...")
            
            # 로그인 완료된 현재 세션을 파일로 저장
            context.storage_state(path=AUTH_FILE)
            print("💾 로그인 세션이 auth.json 파일에 안전하게 저장되었습니다.")

        # [STEP 2] 재고 페이지로 이동 및 데이터 추출
        print(f"📦 재고 페이지로 이동합니다...")
        page.goto(INVENTORY_URL)
        
        # 페이지 내 데이터가 완전히 로딩될 때까지 대기
        page.wait_for_load_state("networkidle")
        print("📊 전체 재고 테이블을 불러와 타겟 제품만 필터링합니다...")
        
        # 💡 [중요] 실제 일쩜오도마켓 상품이 노출되는 HTML 태그에 맞춰 CSS Selector 수정 필요
        rows = page.locator("table tbody tr")
        row_count = rows.count()
        
        if row_count == 0:
            print("⚠️ 재고 데이터를 찾을 수 없거나 CSS Selector가 일치하지 않습니다.")
            print("개발자 도구(F12)를 열어 실제 데이터가 있는 태그를 확인하세요.")
        else:
            print(f"총 {row_count}개의 데이터 행을 스캔합니다.")
            
            collected_data = []

            for i in range(row_count):
                row_text = rows.nth(i).inner_text()
                
                # 행 텍스트 안에 TARGET_OPTION_IDS 중 일치하는게 하나라도 있는지 검사
                matched_id = next((opt_id for opt_id in TARGET_OPTION_IDS if opt_id in row_text), None)
                
                if matched_id:
                    # 텍스트 추출 후 깔끔하게 병합
                    cleaned_text = " | ".join([t for t in row_text.split('\n') if t.strip()])
                    print(f"🎯 [옵션ID: {matched_id}] 데이터 수집됨: {cleaned_text}")
                    collected_data.append(cleaned_text)
            
            print(f"✅ 필터링 완료! 총 {len(collected_data)}개의 타겟 제품 데이터를 수집했습니다.")

        # 브라우저 종료
        browser.close()

if __name__ == "__main__":
    main()

