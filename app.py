import os
import sys

# Windows 콘솔 UTF-8 출력 보장
if sys.platform == 'win32':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import time
import json
import csv
import io
import re
import threading
import webbrowser
from datetime import datetime, date
from flask import Flask, render_template_string, request, jsonify, Response, send_file

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'inventory_master_store.json')
LOGO_FILE = os.path.join(BASE_DIR, 'logo.png')

EXP_DATE_PATTERN = re.compile(r'(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})')

def extract_expiry_date(text):
    if not text:
        return None
    m = EXP_DATE_PATTERN.search(str(text))
    if m:
        y, mo, d = m.groups()
        y = int(y)
        if y < 100:
            y += 2000
        try:
            return date(y, int(mo), int(d))
        except Exception:
            return None
    return None

# -------------------------------------------------------------
# 세트/B2B 상품 실물 재고 연동 매핑 규칙
# (기본 상품과 재고를 100% 공유하며, 판매 시 배수만큼 실물 소진)
# -------------------------------------------------------------
SHARED_STOCK_MAP = {
    "S100005": {"base_code": "10GL02", "multiplier": 12, "desc": "10GL02 12개 묶음 B2B"},
    "S100009": {"base_code": "10GL04", "multiplier": 12, "desc": "10GL04 12개 묶음 B2B"},
    "S100011": {"base_code": "10GL05", "multiplier": 12, "desc": "10GL05 12개 묶음 B2B"},
    "S100033": {"base_code": "10GL06", "multiplier": 12, "desc": "10GL06 12개 묶음 B2B"},
    "S200018": {"base_code": "20TZ01", "multiplier": 2, "desc": "20TZ01 2병 선물세트"},
    "S200019": {"base_code": "20EO02", "multiplier": 2, "desc": "20EO02 2병 선물세트"},
}

def get_initial_master_products():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, dict) and "master_products" in d:
                    return d["master_products"]
                elif isinstance(d, list):
                    return d
        except Exception:
            pass
    return []

# 전역 재고 상태 데이터 관리
data_lock = threading.Lock()
app_state = {
    "master_products": get_initial_master_products(),
    "gaemi_inventory": {},     # { code: [ { option_name, stock, reg_date, status, expiry_date, days_left }, ... ] }
    "coupang_inventory": {},   # { option_id: { stock, inbound, sales_7d, sales_30d, doc, name, opt_name } }
    "sales_inventory": {},     # { seller_code: { total_7d, orders: [ { date, qty, mall, name } ] } }
    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] 일쩜오도 재고 관리 시스템이 준비되었습니다."],
    "public_url": os.environ.get("RENDER_EXTERNAL_URL", "https://inventory-management-q7bp.onrender.com")
}

def add_log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    with data_lock:
        app_state["logs"].append(entry)
        if len(app_state["logs"]) > 300:
            app_state["logs"].pop(0)
    try:
        print(entry)
    except Exception:
        pass

# 영구 데이터 로드/저장
def load_state():
    global app_state
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    app_state["master_products"] = saved.get("master_products", app_state["master_products"])
                    app_state["gaemi_inventory"] = saved.get("gaemi_inventory", {})
                    app_state["coupang_inventory"] = saved.get("coupang_inventory", {})
                    app_state["sales_inventory"] = saved.get("sales_inventory", {})
                add_log("💾 저장된 일쩜오도 재고 및 매핑 데이터를 불러왔습니다.")
        except Exception as e:
            add_log(f"⚠️ 데이터 로드 실패: {e}")

load_state()

def save_state():
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "title": "일쩜오도 재고현황",
                "master_products": app_state["master_products"],
                "gaemi_inventory": app_state["gaemi_inventory"],
                "coupang_inventory": app_state["coupang_inventory"],
                "sales_inventory": app_state["sales_inventory"],
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log(f"⚠️ 데이터 저장 오류: {e}")

# -------------------------------------------------------------
# 구글 사이트(Google Sites) iframe 임베드 보안 헤더 허용
# -------------------------------------------------------------
@app.after_request
def allow_iframe_embedding(response):
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors * 'self' https://*.google.com https://*.googleusercontent.com https://*.usercontent.google.com https://sites.google.com"
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

# -------------------------------------------------------------
# 로고 이미지 서빙 라우트
# -------------------------------------------------------------
@app.route("/logo.png")
def serve_logo():
    if os.path.exists(LOGO_FILE):
        return send_file(LOGO_FILE, mimetype="image/png")
    return "", 404

# -------------------------------------------------------------
# 파서 1: 개미창고 CSV/엑셀 파서 (소비기한 및 잔여 D-Day 자동 분석)
# -------------------------------------------------------------
def parse_gaemi_rows(rows):
    """
    개미창고 파일 컬럼: 번호, 상품코드, 상품명, 옵션명, 상태, 입고량, 출고량, 재고량, 등록일
    옵션명에서 소비기한(예: 27.03.28, 2027-04-30 등)을 자동 추출하여 잔여일(D-Day) 산출
    """
    if not rows or len(rows) < 2:
        return {}

    header_idx = -1
    for i, r in enumerate(rows[:6]):
        line_str = " ".join([str(c) for c in r])
        if "상품코드" in line_str and "재고량" in line_str:
            header_idx = i
            break
            
    if header_idx == -1:
        return {}
        
    headers = [str(c).strip() for c in rows[header_idx]]
    col_code = next((i for i, h in enumerate(headers) if "상품코드" in h), 1)
    col_pname = next((i for i, h in enumerate(headers) if "상품명" in h), 2)
    col_oname = next((i for i, h in enumerate(headers) if "옵션명" in h), 3)
    col_stock = next((i for i, h in enumerate(headers) if "재고량" in h), 7)
    col_reg = next((i for i, h in enumerate(headers) if "등록일" in h), 8)

    gaemi_dict = {}
    today = date.today()

    for r in rows[header_idx + 1:]:
        if len(r) <= max(col_code, col_stock):
            continue
        code = str(r[col_code]).strip()
        if not code or code == "상품코드":
            continue
            
        pname = str(r[col_pname]).strip() if len(r) > col_pname else ""
        oname = str(r[col_oname]).strip() if len(r) > col_oname else ""
        stock_s = str(r[col_stock]).replace(",", "").strip()
        stock = int(stock_s) if stock_s.isdigit() else 0
        reg_date = str(r[col_reg]).strip() if len(r) > col_reg else ""

        # 소비기한 추출 및 잔여일수(D-Day) 계산
        exp_d = extract_expiry_date(oname)
        exp_str = exp_d.strftime("%Y-%m-%d") if exp_d else ""
        days_left = (exp_d - today).days if exp_d else None

        if code not in gaemi_dict:
            gaemi_dict[code] = []

        gaemi_dict[code].append({
            "product_name": pname,
            "option_name": oname,
            "stock": stock,
            "reg_date": reg_date,
            "expiry_date": exp_str,
            "days_left": days_left
        })

    return gaemi_dict

# -------------------------------------------------------------
# 파서 2: 쿠팡 윙 재고 건강도 CSV 파서
# -------------------------------------------------------------
def parse_coupang_rows(rows):
    """
    쿠팡 파일: 옵션 ID, 등록상품명, 옵션명, 판매가능재고 (실시간 기준), 입고예정재고, 최근 판매수량(지난 7일, 지난 30일), 예상 소진일(DOC)
    """
    if not rows or len(rows) < 2:
        return {}

    header_idx = -1
    for i, r in enumerate(rows[:6]):
        line_str = " ".join([str(c) for c in r])
        if "옵션 ID" in line_str or "옵션ID" in line_str:
            header_idx = i
            break

    if header_idx == -1:
        return {}

    headers = [str(c).strip() for c in rows[header_idx]]
    col_oid = next((i for i, h in enumerate(headers) if "옵션 ID" in h or "옵션ID" in h), 2)
    col_pname = next((i for i, h in enumerate(headers) if "등록상품명" in h or "상품명" in h), 4)
    col_oname = next((i for i, h in enumerate(headers) if "옵션명" in h), 5)
    col_stock = next((i for i, h in enumerate(headers) if "판매가능재고" in h), 7)
    col_inbound = next((i for i, h in enumerate(headers) if "입고예정재고" in h), 8)
    
    col_sales7 = 12
    col_sales30 = 13
    col_doc = 14
    
    for i, h in enumerate(headers):
        if "판매수량" in h:
            col_sales7 = i
            col_sales30 = i + 1
            break
        elif "DOC" in h or "소진일" in h:
            col_doc = i

    coupang_dict = {}
    for r in rows[header_idx + 1:]:
        if len(r) <= col_oid:
            continue
        oid = str(r[col_oid]).strip()
        if not oid or not oid.isdigit():
            continue
            
        pname = str(r[col_pname]).strip() if len(r) > col_pname else ""
        oname = str(r[col_oname]).strip() if len(r) > col_oname else ""
        stock_s = str(r[col_stock]).replace(",", "").strip() if len(r) > col_stock else "0"
        inbound_s = str(r[col_inbound]).replace(",", "").strip() if len(r) > col_inbound else "0"
        sales7_s = str(r[col_sales7]).replace(",", "").strip() if len(r) > col_sales7 else "0"
        sales30_s = str(r[col_sales30]).replace(",", "").strip() if len(r) > col_sales30 else "0"
        doc_s = str(r[col_doc]).strip() if len(r) > col_doc else "-"

        stock = int(stock_s) if stock_s.isdigit() else 0
        inbound = int(inbound_s) if inbound_s.isdigit() else 0
        sales7 = int(sales7_s) if sales7_s.isdigit() else 0
        sales30 = int(sales30_s) if sales30_s.isdigit() else 0
        doc = int(doc_s) if doc_s.isdigit() else 0

        coupang_dict[oid] = {
            "product_name": pname,
            "option_name": oname,
            "stock": stock,
            "inbound": inbound,
            "sales_7d": sales7,
            "sales_30d": sales30,
            "doc": doc,
            "updated_at": datetime.now().strftime("%m-%d %H:%M")
        }

    return coupang_dict

# -------------------------------------------------------------
# 파서 3: 쿠팡외 통합 매출 데이터 CSV 파서
# -------------------------------------------------------------
def parse_sales_rows(rows):
    """
    매출정보 CSV:
    주문일, 묶음번호, 쇼핑몰, 온라인상품명, 주문수량, 금액, 할인금액, 배송비, 실결제금액, 수령자명, 배송완료일, 세트코드, 판매자관리코드
    """
    if not rows or len(rows) < 2:
        return {}

    header_idx = -1
    for i, r in enumerate(rows[:5]):
        line_str = " ".join([str(c) for c in r])
        if "판매자관리코드" in line_str or "주문수량" in line_str or "온라인상품명" in line_str:
            header_idx = i
            break

    if header_idx == -1:
        return {}

    headers = [str(c).strip() for c in rows[header_idx]]
    col_code = next((i for i, h in enumerate(headers) if "판매자관리코드" in h or "판매자코드" in h), -1)
    col_qty = next((i for i, h in enumerate(headers) if "주문수량" in h or "수량" in h), 4)
    col_date = next((i for i, h in enumerate(headers) if "주문일" in h), 0)
    col_mall = next((i for i, h in enumerate(headers) if "쇼핑몰" in h), 2)
    col_name = next((i for i, h in enumerate(headers) if "상품명" in h), 3)

    if col_code == -1:
        col_code = len(headers) - 1

    sales_dict = {}
    for r in rows[header_idx + 1:]:
        if len(r) <= max(col_code, col_qty):
            continue
        code = str(r[col_code]).strip().upper()
        name = str(r[col_name]).strip() if len(r) > col_name else ""
        mall = str(r[col_mall]).strip() if len(r) > col_mall else ""
        date_s = str(r[col_date]).strip() if len(r) > col_date else ""
        qty_s = str(r[col_qty]).replace(",", "").strip()
        qty = int(qty_s) if qty_s.isdigit() else 0

        if not code:
            continue

        if code not in sales_dict:
            sales_dict[code] = {
                "total_7d": 0,
                "orders": []
            }

        sales_dict[code]["total_7d"] += qty
        sales_dict[code]["orders"].append({
            "date": date_s,
            "qty": qty,
            "mall": mall,
            "name": name
        })

    return sales_dict

# -------------------------------------------------------------
# REST API 라우트
# -------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/summary")
def get_summary():
    """
    마스터 상품 단위 집계, B2B/세트 상품 실물 재고 연동, 수입 110일 발주 경보,
    개미/쿠팡 창고별 개별 런웨이, 소비기한(FIFO) 추적 및 소진 시뮬레이션
    """
    with data_lock:
        master_list = app_state["master_products"]
        gaemi = app_state["gaemi_inventory"]
        coupang = app_state["coupang_inventory"]
        sales = app_state["sales_inventory"]
        logs = app_state["logs"]
        public_url = app_state["public_url"]
        last_updated = app_state.get("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # 1차 패스: 기본 품목들의 실물 재고, 소비기한 로트 및 쿠팡/타채널 직접 판매량 계산
    base_calc_map = {}
    today = date.today()

    for m in master_list:
        seller_code = m.get("seller_code", m.get("id", "")).strip()
        norm_code = seller_code.upper()
        
        # 기본 개미창고 재고 및 로트별 소비기한 수집
        g_total = 0
        g_details = []
        gaemi_lots = []
        for g_code in m.get("gaemi_codes", []):
            if g_code in gaemi:
                for item in gaemi[g_code]:
                    opt_filter = m.get("gaemi_filter", "").strip()
                    if opt_filter and opt_filter not in item["option_name"]:
                        continue
                    st = item["stock"]
                    g_total += st
                    g_details.append(f"{item['option_name']}: {st}개")
                    gaemi_lots.append({
                        "option_name": item["option_name"],
                        "stock": st,
                        "reg_date": item.get("reg_date", ""),
                        "expiry_date": item.get("expiry_date", ""),
                        "days_left": item.get("days_left")
                    })

        # 유효 재고(stock > 0)가 있는 로트들 우선 분석
        active_lots = [l for l in gaemi_lots if l["stock"] > 0]
        lot_count = len(active_lots)
        is_mixed_lots = lot_count > 1

        # 소비기한이 추출된 로트 선별 (선입선출: 가장 빠른 기한 우선)
        exp_lots = [l for l in active_lots if l.get("days_left") is not None]
        if not exp_lots:
            exp_lots = [l for l in gaemi_lots if l.get("days_left") is not None]

        if exp_lots:
            exp_lots.sort(key=lambda x: x["days_left"])
            nearest_lot = exp_lots[0]
            nearest_expiry_date = nearest_lot["expiry_date"]
            nearest_days_left = nearest_lot["days_left"]
            nearest_lot_stock = nearest_lot["stock"]
        else:
            nearest_expiry_date = ""
            nearest_days_left = None
            nearest_lot_stock = 0

        # 쿠팡 옵션 재고 및 7일 출고량 계산
        c_package_total = 0
        c_physical_total = 0
        c_sales_7d_total = 0
        c_sales_30d_total = 0
        option_details = []

        for opt in m.get("options", []):
            oid = opt["id"]
            bundle = opt.get("bundle", 1)
            c_data = coupang.get(oid, {})
            c_stock = c_data.get("stock", 0)
            c_inbound = c_data.get("inbound", 0)
            c_sales7 = c_data.get("sales_7d", 0)
            c_sales30 = c_data.get("sales_30d", 0)
            doc = c_data.get("doc", 0)

            c_package_total += c_stock
            c_physical_total += (c_stock * bundle)
            c_sales_7d_total += (c_sales7 * bundle)
            c_sales_30d_total += (c_sales30 * bundle)

            option_details.append({
                "id": oid,
                "bundle": bundle,
                "name": opt.get("name", f"{bundle}개입"),
                "coupang_stock": c_stock,
                "physical_stock": c_stock * bundle,
                "inbound": c_inbound,
                "sales_7d": c_sales7,
                "sales_7d_physical": c_sales7 * bundle,
                "sales_30d": c_sales30,
                "doc": doc
            })

        s_data = sales.get(norm_code, {"total_7d": 0, "orders": []})
        raw_other_sales = s_data.get("total_7d", 0)
        recent_orders = s_data.get("orders", [])[:5]

        base_calc_map[norm_code] = {
            "gaemi_total": g_total,
            "gaemi_details": ", ".join(g_details) if g_details else "등록 정보 없음",
            "gaemi_lots": gaemi_lots,
            "nearest_expiry_date": nearest_expiry_date,
            "nearest_days_left": nearest_days_left,
            "nearest_lot_stock": nearest_lot_stock,
            "is_mixed_lots": is_mixed_lots,
            "lot_count": lot_count,
            "coupang_package_total": c_package_total,
            "coupang_physical_total": c_physical_total,
            "master_physical_total": g_total + c_physical_total,
            "coupang_sales_7d": c_sales_7d_total,
            "other_sales_7d": raw_other_sales,
            "raw_other_orders": raw_other_sales,
            "options": option_details,
            "recent_orders": recent_orders
        }

    # 2차 패스: 44개 전체 상품의 최종 지표 종합 (세트/B2B 상품은 기본 상품과 재고 100% 일치)
    master_summaries = []
    total_gaemi_stock = 0
    total_coupang_stock = 0
    total_physical_stock = 0
    total_coupang_7d = 0
    total_other_7d = 0
    total_sales_7d = 0

    urgent_order_count = 0       # 🔴 긴급 발주 (110일 미만 / 결품)
    order_prepare_count = 0      # 🟡 발주 준비 (110일~130일 안전재고 버퍼)
    coupang_transfer_count = 0   # 🔵 쿠팡 채우기 필요 (창고 간 불균형)
    expiry_warning_count = 0     # ⏳ 소비기한 주의 (120일 미만 or 미소진 위험)
    safe_count = 0               # 🟢 정상 안전

    for m in master_list:
        seller_code = m.get("seller_code", m.get("id", "")).strip()
        norm_code = seller_code.upper()
        product_type = m.get("product_type", "사입").strip() or "사입"
        note = m.get("note", "").strip()

        calc = base_calc_map.get(norm_code, {})
        is_shared = norm_code in SHARED_STOCK_MAP
        shared_info = SHARED_STOCK_MAP.get(norm_code)

        if is_shared:
            base_code = shared_info["base_code"]
            mult = shared_info["multiplier"]
            base_calc = base_calc_map.get(base_code, {})

            # 1. 재고 및 소비기한 로트는 기본 상품과 100% 동일하게 공유!
            g_total = base_calc.get("gaemi_total", 0)
            g_details = base_calc.get("gaemi_details", "")
            gaemi_lots = base_calc.get("gaemi_lots", [])
            nearest_expiry_date = base_calc.get("nearest_expiry_date", "")
            nearest_days_left = base_calc.get("nearest_days_left")
            nearest_lot_stock = base_calc.get("nearest_lot_stock", 0)
            is_mixed_lots = base_calc.get("is_mixed_lots", False)
            lot_count = base_calc.get("lot_count", 0)

            c_package_total = base_calc.get("coupang_package_total", 0)
            c_physical_total = base_calc.get("coupang_physical_total", 0)
            master_physical_total = base_calc.get("master_physical_total", 0)
            option_details = base_calc.get("options", [])

            # 2. B2B/세트 판매량: 해당 세트 주문 1건당 mult(12개/2개) 실물 소진 반영
            raw_orders = calc.get("raw_other_orders", 0)
            other_sales_7d = raw_orders * mult
            c_sales_7d = 0
            recent_orders = calc.get("recent_orders", [])
        else:
            g_total = calc.get("gaemi_total", 0)
            g_details = calc.get("gaemi_details", "")
            gaemi_lots = calc.get("gaemi_lots", [])
            nearest_expiry_date = calc.get("nearest_expiry_date", "")
            nearest_days_left = calc.get("nearest_days_left")
            nearest_lot_stock = calc.get("nearest_lot_stock", 0)
            is_mixed_lots = calc.get("is_mixed_lots", False)
            lot_count = calc.get("lot_count", 0)

            c_package_total = calc.get("coupang_package_total", 0)
            c_physical_total = calc.get("coupang_physical_total", 0)
            master_physical_total = calc.get("master_physical_total", 0)
            option_details = calc.get("options", [])
            c_sales_7d = calc.get("coupang_sales_7d", 0)
            other_sales_7d = calc.get("other_sales_7d", 0)
            recent_orders = calc.get("recent_orders", [])

        # 3. 7일 총 출고량 및 창고별 일평균 출고량 산출
        m_sales_7d = c_sales_7d + other_sales_7d
        daily_sales = round(m_sales_7d / 7.0, 2)
        coupang_daily = round(c_sales_7d / 7.0, 2)

        # 전체 통계 합산 (공유 재고 중복 집계 방지: 고유 실물 재고만 합산)
        if not is_shared:
            total_gaemi_stock += g_total
            total_coupang_stock += c_physical_total
            total_physical_stock += master_physical_total

        total_coupang_7d += c_sales_7d
        total_other_7d += other_sales_7d
        total_sales_7d += m_sales_7d

        # 4. 창고별 개별 런웨이 및 통합 런웨이 계산
        # 통합 런웨이
        if daily_sales > 0:
            total_runway = round(master_physical_total / daily_sales, 1)
            total_runway_str = f"{total_runway}일"
            gaemi_runway = round(g_total / daily_sales, 1)
            gaemi_runway_str = f"{gaemi_runway}일"
        else:
            total_runway = 999
            total_runway_str = "소진무" if master_physical_total > 0 else "재고없음"
            gaemi_runway = 999
            gaemi_runway_str = "소진무" if g_total > 0 else "재고없음"

        # 쿠팡 독자 런웨이
        if coupang_daily > 0:
            coupang_runway = round(c_physical_total / coupang_daily, 1)
            coupang_runway_str = f"{coupang_runway}일"
        else:
            coupang_runway = 999
            coupang_runway_str = "소진무" if c_physical_total > 0 else "재고없음"

        # 5. 소비기한 vs 판매속도 소진 시뮬레이션
        expiry_risk = False
        expiry_risk_qty = 0
        expiry_note = ""
        projected_sales_to_exp = 0

        if nearest_days_left is not None:
            if daily_sales > 0:
                projected_sales_to_exp = int(daily_sales * nearest_days_left)
                if nearest_lot_stock > projected_sales_to_exp:
                    expiry_risk = True
                    expiry_risk_qty = nearest_lot_stock - projected_sales_to_exp
                    expiry_note = f"기한내 {expiry_risk_qty}개 미소진 위험"
                elif nearest_days_left <= 120:
                    expiry_risk = True
                    expiry_note = f"기한 잔여 {nearest_days_left}일 주의"
            else:
                if nearest_days_left <= 120 and nearest_lot_stock > 0:
                    expiry_risk = True
                    expiry_risk_qty = nearest_lot_stock
                    expiry_note = f"잔여 {nearest_days_left}일 (판매 정체)"

        # 6. 결품 방지 및 발주 의사결정 액션(Action) 판정 트리
        is_urgent = False
        is_order_prepare = False
        is_coupang_transfer = False
        is_exp_warn = False
        is_safe = False

        if master_physical_total == 0:
            # 🔴 결품 (0개)
            status = "🔴 결품 (0개)"
            status_color = "red"
            action_cta = "🚨 결품 즉시 발주"
            action_cta_class = "bg-rose-600 hover:bg-rose-700 text-white"
            risk_score = 1000
            is_urgent = True
        elif product_type == "수입" and total_runway < 110:
            # 🔴 수입 리드타임 110일 미만 긴급 발주
            status = f"🔴 긴급 발주 (D-{int(total_runway)}일)"
            status_color = "red"
            action_cta = "🚨 수입 발주 기안"
            action_cta_class = "bg-rose-600 hover:bg-rose-700 text-white"
            risk_score = 900 - total_runway
            is_urgent = True
        elif product_type == "사입" and total_runway < 7:
            # 🔴 사입 국내 배송 리드타임 미만 긴급 발주
            status = f"🔴 긴급 발주 (D-{int(total_runway)}일)"
            status_color = "red"
            action_cta = "🚨 사입 긴급 발주"
            action_cta_class = "bg-rose-600 hover:bg-rose-700 text-white"
            risk_score = 850 - total_runway
            is_urgent = True
        elif g_total >= 10 and (coupang_runway <= 14 or (c_physical_total == 0 and c_sales_7d > 0)):
            # 🔵 창고 간 불균형: 개미창고 실물은 있으나 쿠팡 재고 바닥 (14일 이하)
            status = f"🔵 쿠팡 채우기 (쿠팡 D-{int(coupang_runway)}일)"
            status_color = "blue"
            action_cta = "📦 쿠팡 입고 요청"
            action_cta_class = "bg-blue-600 hover:bg-blue-700 text-white"
            risk_score = 700 - coupang_runway
            is_coupang_transfer = True
        elif product_type == "수입" and total_runway <= 130:
            # 🟡 수입 안전재고 20일 구간 (110~130일) 발주 준비
            status = f"🟡 발주 준비 (D-{int(total_runway)}일)"
            status_color = "amber"
            action_cta = "📝 발주서 작성"
            action_cta_class = "bg-amber-500 hover:bg-amber-600 text-white"
            risk_score = 500 - total_runway
            is_order_prepare = True
        elif product_type == "사입" and total_runway <= 14:
            # 🟡 사입 7~14일 안전재고 구간
            status = f"🟡 사입 주의 (D-{int(total_runway)}일)"
            status_color = "amber"
            action_cta = "📝 사입 발주 검토"
            action_cta_class = "bg-amber-500 hover:bg-amber-600 text-white"
            risk_score = 450 - total_runway
            is_order_prepare = True
        elif expiry_risk:
            # ⏳ 소비기한 경보
            status = f"⏳ 기한 주의 ({expiry_note})"
            status_color = "purple"
            action_cta = "🏷️ 임박 특가/프로모션"
            action_cta_class = "bg-purple-600 hover:bg-purple-700 text-white"
            risk_score = 350
            is_exp_warn = True
        else:
            # 🟢 정상 안전
            safe_d = f"D-{int(total_runway)}일" if total_runway < 900 else "안전(소진무)"
            status = f"✅ 정상 안전 ({safe_d})"
            status_color = "green"
            action_cta = "✅ 정상 유지"
            action_cta_class = "bg-emerald-100 text-emerald-800"
            risk_score = 100 - min(total_runway, 200)
            is_safe = True

        if is_urgent:
            urgent_order_count += 1
        elif is_coupang_transfer:
            coupang_transfer_count += 1
        elif is_order_prepare:
            order_prepare_count += 1
        elif is_exp_warn:
            expiry_warning_count += 1
        elif is_safe:
            safe_count += 1

        master_summaries.append({
            "id": m.get("id", seller_code),
            "seller_code": seller_code,
            "name": m.get("name", ""),
            "product_type": product_type,
            "note": note,
            "is_shared": is_shared,
            "shared_base": shared_info["base_code"] if is_shared else None,
            "shared_mult": shared_info["multiplier"] if is_shared else None,
            "category": m.get("category", "기타"),
            "gaemi_codes": m.get("gaemi_codes", []),
            "gaemi_total": g_total,
            "gaemi_details": g_details,
            "gaemi_lots": gaemi_lots,
            "nearest_expiry_date": nearest_expiry_date,
            "nearest_days_left": nearest_days_left,
            "nearest_lot_stock": nearest_lot_stock,
            "is_mixed_lots": is_mixed_lots,
            "lot_count": lot_count,
            "expiry_risk": expiry_risk,
            "expiry_risk_qty": expiry_risk_qty,
            "expiry_note": expiry_note,
            "projected_sales_to_exp": projected_sales_to_exp,
            "coupang_package_total": c_package_total,
            "coupang_physical_total": c_physical_total,
            "master_physical_total": master_physical_total,
            "coupang_sales_7d": c_sales_7d,
            "other_sales_7d": other_sales_7d,
            "total_sales_7d": m_sales_7d,
            "daily_sales": daily_sales,
            "coupang_daily": coupang_daily,
            "total_runway": total_runway,
            "total_runway_str": total_runway_str,
            "gaemi_runway": gaemi_runway,
            "gaemi_runway_str": gaemi_runway_str,
            "coupang_runway": coupang_runway,
            "coupang_runway_str": coupang_runway_str,
            "doc": total_runway,
            "doc_str": total_runway_str,
            "status": status,
            "status_color": status_color,
            "action_cta": action_cta,
            "action_cta_class": action_cta_class,
            "risk_score": risk_score,
            "options": option_details,
            "recent_orders": recent_orders
        })

    # 액션보드 핵심: 위험도 순(Risk Score) 강제 정렬
    master_summaries.sort(key=lambda x: x["risk_score"], reverse=True)

    # 하위 호환성 유지 카운터 (테스트 코드 호환)
    legacy_leadtime_count = sum(1 for m in master_summaries if m["product_type"] == "수입" and m["total_runway"] <= 130)

    return jsonify({
        "masters": master_summaries,
        "stats": {
            "total_masters": len(master_list),
            "total_gaemi_stock": total_gaemi_stock,
            "total_coupang_stock": total_coupang_stock,
            "total_physical_stock": total_physical_stock,
            "total_coupang_7d": total_coupang_7d,
            "total_other_7d": total_other_7d,
            "total_sales_7d": total_sales_7d,
            "urgent_order_count": urgent_order_count,
            "order_prepare_count": order_prepare_count,
            "coupang_transfer_count": coupang_transfer_count,
            "expiry_warning_count": expiry_warning_count,
            "safe_count": safe_count,
            "leadtime_count": legacy_leadtime_count,
            "urgent_count": urgent_order_count,
            "danger_count": urgent_order_count,
            "warning_count": order_prepare_count,
            "last_updated": last_updated,
            "has_gaemi_data": len(app_state["gaemi_inventory"]) > 0,
            "has_coupang_data": len(app_state["coupang_inventory"]) > 0,
            "has_sales_data": len(app_state["sales_inventory"]) > 0
        },
        "logs": logs,
        "public_url": public_url
    })

@app.route("/api/upload-file-data", methods=["POST"])
def upload_file_data():
    """
    파싱된 2차원 배열 데이터(개미창고, 쿠팡, 쿠팡외 매출) 반영
    """
    body = request.get_json() or {}
    source = body.get("source") # 'gaemi', 'coupang', 'sales'
    rows = body.get("rows", [])

    with data_lock:
        app_state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    if source == "gaemi":
        parsed = parse_gaemi_rows(rows)
        if not parsed:
            return jsonify({"success": False, "message": "개미창고 파일 형식(헤더: 상품코드, 재고량 등)을 인식하지 못했습니다."}), 400
        with data_lock:
            app_state["gaemi_inventory"] = parsed
        save_state()
        total_items = sum(len(v) for v in parsed.values())
        add_log(f"📦 [개미창고(3PL)] {len(parsed)}개 상품코드(세부 {total_items}건) 재고 및 소비기한 데이터가 업데이트되었습니다!")
        return jsonify({"success": True, "message": f"개미창고 {total_items}개 품목 재고가 반영되었습니다."})

    elif source == "coupang":
        parsed = parse_coupang_rows(rows)
        if not parsed:
            return jsonify({"success": False, "message": "쿠팡 윙 파일 형식(헤더: 옵션 ID, 판매가능재고 등)을 인식하지 못했습니다."}), 400
        with data_lock:
            app_state["coupang_inventory"].update(parsed)
        save_state()
        add_log(f"🚀 [쿠팡 윙] {len(parsed)}개 옵션 ID의 실시간 재고 및 7일 판매량 데이터가 업데이트되었습니다!")
        return jsonify({"success": True, "message": f"쿠팡 윙 {len(parsed)}개 옵션 데이터가 반영되었습니다."})

    elif source == "sales":
        parsed = parse_sales_rows(rows)
        if not parsed:
            return jsonify({"success": False, "message": "매출정보 파일 형식(헤더: 판매자관리코드, 주문수량 등)을 인식하지 못했습니다."}), 400
        with data_lock:
            app_state["sales_inventory"] = parsed
        save_state()
        total_qty = sum(v["total_7d"] for v in parsed.values())
        add_log(f"📈 [쿠팡외 통합 매출] {len(parsed)}개 상품코드(총 {total_qty}개 주문 출고) 데이터가 반영되었습니다!")
        return jsonify({"success": True, "message": f"쿠팡외 매출 {len(parsed)}개 품목({total_qty}개 출고)이 반영되었습니다."})

    return jsonify({"success": False, "message": "유효하지 않은 데이터 소스입니다."}), 400

@app.route("/api/load-sample", methods=["POST"])
def load_sample():
    """원클릭 샘플 데이터 로드"""
    loaded = []
    with data_lock:
        app_state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    gaemi_sample = os.path.join(BASE_DIR, "개미창고_샘플.csv")
    if os.path.exists(gaemi_sample):
        try:
            with open(gaemi_sample, encoding="utf-8") as f:
                rows = list(csv.reader(f))
                parsed = parse_gaemi_rows(rows)
                if parsed:
                    with data_lock:
                        app_state["gaemi_inventory"] = parsed
                    loaded.append("개미창고")
        except Exception as e:
            add_log(f"개미창고 샘플 로드 오류: {e}")

    coupang_sample = os.path.join(BASE_DIR, "쿠팡_샘플.csv")
    if os.path.exists(coupang_sample):
        try:
            with open(coupang_sample, encoding="utf-8") as f:
                rows = list(csv.reader(f))
                parsed = parse_coupang_rows(rows)
                if parsed:
                    with data_lock:
                        app_state["coupang_inventory"] = parsed
                    loaded.append("쿠팡윙")
        except Exception as e:
            add_log(f"쿠팡 샘플 로드 오류: {e}")

    sales_sample = os.path.join(BASE_DIR, "매출정보_샘플.csv")
    if os.path.exists(sales_sample):
        try:
            with open(sales_sample, encoding="utf-8") as f:
                rows = list(csv.reader(f))
                parsed = parse_sales_rows(rows)
                if parsed:
                    with data_lock:
                        app_state["sales_inventory"] = parsed
                    loaded.append("쿠팡외매출")
        except Exception as e:
            add_log(f"매출정보 샘플 로드 오류: {e}")

    save_state()
    add_log(f"✨ [원클릭 샘플 로드 완료] {' / '.join(loaded)} 데이터가 액션보드에 즉시 반영되었습니다!")
    return jsonify({"success": True, "message": f"{', '.join(loaded)} 샘플 데이터 로드 완료"})

@app.route("/api/clear-logs", methods=["POST"])
def clear_logs():
    with data_lock:
        app_state["logs"] = [f"[{datetime.now().strftime('%H:%M:%S')}] 콘솔 로그가 초기화되었습니다."]
    return jsonify({"success": True})

@app.route("/api/export-master-csv")
def export_master_csv():
    """일쩜오도 발주 의사결정 액션보드 통합 리포트 CSV 다운로드"""
    summary_resp = get_summary().json
    masters = summary_resp.get("masters", [])

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([
        "판매자관리코드", "구분", "마스터 상품명", "총 가용재고", "통합 런웨이(일)",
        "개미(3PL) 재고", "개미 런웨이(일)", "최단 소비기한",
        "쿠팡(실물) 재고", "쿠팡 런웨이(일)", "7일 출고량", 
        "일평균 출고량", "발주 권고 상태", "추천 조치"
    ])

    for m in masters:
        cw.writerow([
            m["seller_code"], m.get("product_type", "사입"), m["name"],
            m["master_physical_total"], m["total_runway_str"],
            m["gaemi_total"], m["gaemi_runway_str"], m.get("nearest_expiry_date") or "-",
            m["coupang_physical_total"], m["coupang_runway_str"],
            m["total_sales_7d"], m["daily_sales"], m["status"], m["action_cta"]
        ])

    output = si.getvalue().encode('utf-8-sig')
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=iljeomodo_inventory_action_report.csv"}
    )

# -------------------------------------------------------------
# 모던 일쩜오도 발주 의사결정 액션보드 HTML
# -------------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>일쩜오도 재고현황 | 발주 의사결정 액션보드</title>
    <link rel="icon" type="image/png" href="/logo.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800;900&display=swap');
        html, body { 
            font-family: 'Pretendard', sans-serif; 
            overflow-x: hidden;
        }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #f8fafc; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
        .log-box::-webkit-scrollbar { width: 5px; }
        .log-box::-webkit-scrollbar-track { background: #0f172a; }
        .log-box::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
        .drag-col-over { border-left: 3px solid #059669 !important; background-color: #d1fae5 !important; }
        .drag-row-over { border-top: 3px solid #059669 !important; }
        :fullscreen body { background-color: #f1f5f9; }
        :-webkit-full-screen body { background-color: #f1f5f9; }
    </style>
</head>
<body class="bg-slate-100 text-slate-800 min-h-screen flex flex-col">

    <!-- 상단 헤더 (일쩜오도 BI 로고, 실시간 재고 업데이트 일시, 액션 버튼) -->
    <header class="bg-slate-900 text-white shadow-md border-b border-slate-800 sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-4 py-2.5 flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center space-x-3">
                <div class="h-10 px-2 py-1 bg-white rounded-xl shadow-sm border border-slate-700 flex items-center justify-center">
                    <img src="/logo.png" alt="일쩜오도 로고" class="h-7 w-auto object-contain">
                </div>
                <div>
                    <div class="flex items-center space-x-2">
                        <h1 class="text-base sm:text-lg font-bold tracking-tight text-white">일쩜오도 재고현황 | 발주 의사결정 액션보드</h1>
                        <span class="text-[10px] bg-rose-500/20 text-rose-300 border border-rose-500/30 font-semibold px-2 py-0.5 rounded-full">결품 제로(Zero) 경보</span>
                    </div>
                    <div class="flex items-center space-x-2 mt-0.5">
                        <p class="text-[11px] text-slate-400">수입 리드타임 110일 발주 경보 · 창고 간 불균형 해소 · 소비기한 선입선출(FIFO) 관리</p>
                        <span class="text-slate-600">|</span>
                        <!-- 1. 재고 업데이트 일자 실시간 표기 -->
                        <span class="inline-flex items-center text-[11px] text-emerald-400 font-semibold bg-emerald-950/60 border border-emerald-500/30 px-2 py-0.5 rounded-md">
                            <i class="fa-regular fa-clock mr-1 text-emerald-400"></i>
                            <span>재고 업데이트: </span>
                            <span id="headerLastUpdated" class="ml-1 font-mono text-white">확인 중...</span>
                        </span>
                    </div>
                </div>
            </div>
            
            <div class="flex items-center space-x-2">
                <button onclick="toggleFullScreen()" id="fullscreenBtn" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm" title="전체화면으로 시원하게 보기">
                    <i class="fa-solid fa-expand" id="fullscreenIcon"></i>
                    <span id="fullscreenText">전체화면</span>
                </button>
                <button onclick="openInNewTab()" class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm" title="새 탭에서 100% 꽉 차게 열기">
                    <i class="fa-solid fa-arrow-up-right-from-square"></i>
                    <span>새 탭으로 열기</span>
                </button>
                <button onclick="loadSampleData()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm">
                    <i class="fa-solid fa-wand-magic-sparkles"></i>
                    <span>샘플 데이터 즉시 로드</span>
                </button>
                <a href="/api/export-master-csv" class="px-3.5 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm">
                    <i class="fa-solid fa-file-excel"></i>
                    <span>액션 리포트 다운로드</span>
                </a>
            </div>
        </div>
    </header>

    <!-- 구글 사이트 연동 안내 배너 -->
    <div id="tunnelBanner" class="bg-gradient-to-r from-emerald-800 via-teal-800 to-slate-900 text-white shadow-inner px-4 py-2">
        <div class="max-w-7xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-2 text-xs">
            <div class="flex items-center space-x-2">
                <span class="bg-emerald-500/30 text-emerald-200 border border-emerald-400/30 px-2 py-0.5 rounded font-bold uppercase tracking-wider text-[10px]">Google Sites 24시간 연동 URL</span>
                <span id="publicUrlText" class="font-mono bg-black/40 px-2.5 py-0.5 rounded select-all font-semibold text-emerald-300">
                    https://inventory-management-q7bp.onrender.com
                </span>
            </div>
            <button onclick="copyPublicUrl()" class="px-2.5 py-0.5 bg-white text-emerald-800 rounded font-semibold hover:bg-emerald-50 transition flex items-center space-x-1 shadow-sm">
                <i class="fa-regular fa-copy"></i>
                <span>주소 복사</span>
            </button>
        </div>
    </div>

    <!-- 메인 대시보드 컨테이너 -->
    <main class="max-w-7xl mx-auto px-4 py-4 flex-1 w-full space-y-4">

        <!-- 1. 핵심 5대 신호등 Action KPI 위젯 (상단 배치) -->
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
            
            <!-- 🔴 긴급 발주 요망 (통합 D-Day < 110일) -->
            <div onclick="setStatusFilter('urgent')" class="bg-rose-50/70 rounded-xl shadow-sm border-2 border-rose-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-rose-100/80 transition relative overflow-hidden group">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-rose-900 flex items-center">
                        <i class="fa-solid fa-triangle-exclamation text-rose-600 mr-1.5 animate-bounce"></i>긴급 발주 요망
                    </span>
                    <span class="text-[9px] bg-rose-200 text-rose-900 px-1.5 py-0.5 rounded font-bold">110일 미만/결품</span>
                </div>
                <div class="flex items-baseline justify-between mt-2">
                    <div>
                        <span id="statUrgentOrders" class="text-3xl font-black text-rose-700">0</span>
                        <span class="text-xs text-rose-600 font-bold ml-1">개 품목</span>
                    </div>
                    <i class="fa-solid fa-fire text-rose-400 text-2xl group-hover:scale-110 transition"></i>
                </div>
                <div class="mt-1 flex items-center justify-between text-[10px] text-rose-700 font-semibold border-t border-rose-200/60 pt-1">
                    <span>수입 리드타임 결품 초읽기</span>
                    <span class="text-rose-900 group-hover:underline">클릭 시 필터</span>
                </div>
            </div>

            <!-- 🟡 발주 준비 (110일 ~ 130일) -->
            <div onclick="setStatusFilter('prepare')" class="bg-amber-50/70 rounded-xl shadow-sm border-2 border-amber-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-amber-100/80 transition relative overflow-hidden group">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-amber-900 flex items-center">
                        <i class="fa-solid fa-clipboard-list text-amber-600 mr-1.5"></i>발주 준비
                    </span>
                    <span class="text-[9px] bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded font-bold">110~130일</span>
                </div>
                <div class="flex items-baseline justify-between mt-2">
                    <div>
                        <span id="statOrderPrepare" class="text-3xl font-black text-amber-700">0</span>
                        <span class="text-xs text-amber-600 font-bold ml-1">개 품목</span>
                    </div>
                    <i class="fa-solid fa-cart-flatbed text-amber-400 text-2xl group-hover:scale-110 transition"></i>
                </div>
                <div class="mt-1 flex items-center justify-between text-[10px] text-amber-700 font-semibold border-t border-amber-200/60 pt-1">
                    <span>안전재고 20일 구간 (기안 준비)</span>
                    <span class="text-amber-900 group-hover:underline">클릭 시 필터</span>
                </div>
            </div>

            <!-- 🔵 쿠팡 채우기 필요 (창고 간 불균형) -->
            <div onclick="setStatusFilter('transfer')" class="bg-blue-50/70 rounded-xl shadow-sm border-2 border-blue-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-blue-100/80 transition relative overflow-hidden group">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-blue-900 flex items-center">
                        <i class="fa-solid fa-truck-arrow-right text-blue-600 mr-1.5"></i>쿠팡 채우기 필요
                    </span>
                    <span class="text-[9px] bg-blue-200 text-blue-900 px-1.5 py-0.5 rounded font-bold">창고 불균형</span>
                </div>
                <div class="flex items-baseline justify-between mt-2">
                    <div>
                        <span id="statCoupangTransfer" class="text-3xl font-black text-blue-700">0</span>
                        <span class="text-xs text-blue-600 font-bold ml-1">개 품목</span>
                    </div>
                    <i class="fa-solid fa-boxes-packing text-blue-400 text-2xl group-hover:scale-110 transition"></i>
                </div>
                <div class="mt-1 flex items-center justify-between text-[10px] text-blue-700 font-semibold border-t border-blue-200/60 pt-1">
                    <span>개미 실물 有 · 쿠팡 D-14 이하</span>
                    <span class="text-blue-900 group-hover:underline">클릭 시 필터</span>
                </div>
            </div>

            <!-- ⏳ 소비기한 주의 (120일 미만 or 미소진 위험) -->
            <div onclick="setStatusFilter('expiry')" class="bg-purple-50/70 rounded-xl shadow-sm border-2 border-purple-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-purple-100/80 transition relative overflow-hidden group">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-purple-900 flex items-center">
                        <i class="fa-solid fa-hourglass-half text-purple-600 mr-1.5"></i>소비기한 주의
                    </span>
                    <span class="text-[9px] bg-purple-200 text-purple-900 px-1.5 py-0.5 rounded font-bold">선입선출</span>
                </div>
                <div class="flex items-baseline justify-between mt-2">
                    <div>
                        <span id="statExpiryWarning" class="text-3xl font-black text-purple-700">0</span>
                        <span class="text-xs text-purple-600 font-bold ml-1">개 품목</span>
                    </div>
                    <i class="fa-solid fa-calendar-xmark text-purple-400 text-2xl group-hover:scale-110 transition"></i>
                </div>
                <div class="mt-1 flex items-center justify-between text-[10px] text-purple-700 font-semibold border-t border-purple-200/60 pt-1">
                    <span>잔여 120일 미만 or 미소진 위험</span>
                    <span class="text-purple-900 group-hover:underline">클릭 시 필터</span>
                </div>
            </div>

            <!-- 🟢 정상 안전 (130일 이상) -->
            <div onclick="setStatusFilter('safe')" class="bg-emerald-50/70 rounded-xl shadow-sm border-2 border-emerald-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-emerald-100/80 transition relative overflow-hidden group col-span-2 sm:col-span-1">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-emerald-900 flex items-center">
                        <i class="fa-solid fa-circle-check text-emerald-600 mr-1.5"></i>정상 안전
                    </span>
                    <span class="text-[9px] bg-emerald-200 text-emerald-900 px-1.5 py-0.5 rounded font-bold">130일 이상</span>
                </div>
                <div class="flex items-baseline justify-between mt-2">
                    <div>
                        <span id="statSafeStock" class="text-3xl font-black text-emerald-700">0</span>
                        <span class="text-xs text-emerald-600 font-bold ml-1">개 품목</span>
                    </div>
                    <i class="fa-solid fa-shield-halved text-emerald-400 text-2xl group-hover:scale-110 transition"></i>
                </div>
                <div class="mt-1 flex items-center justify-between text-[10px] text-emerald-700 font-semibold border-t border-emerald-200/60 pt-1">
                    <span>안정적 재고 유지 상태</span>
                    <span class="text-emerald-900 group-hover:underline">클릭 시 필터</span>
                </div>
            </div>

        </div>

        <!-- 2. 재고 요약 바 (마스터 품목수 및 창고별 합계 스트립) -->
        <div class="bg-white rounded-xl shadow-xs border border-slate-200 px-4 py-2.5 flex flex-wrap items-center justify-between gap-3 text-xs">
            <div class="flex flex-wrap items-center gap-4 text-slate-600">
                <div class="flex items-center space-x-1.5">
                    <span class="font-bold text-slate-900">관리 대상:</span>
                    <span id="statTotalMasters" class="font-bold text-indigo-700">44개</span>
                    <span class="text-slate-400 text-[11px]">(수입 17 / 사입 27)</span>
                </div>
                <span class="text-slate-300">|</span>
                <div class="flex items-center space-x-1.5">
                    <span class="font-bold text-slate-900">총 가용재고:</span>
                    <span id="statTotalStock" class="font-bold text-emerald-700 text-sm">0개</span>
                    <span class="text-slate-400 text-[11px]">(개미 <span id="statGaemiStock" class="font-semibold text-amber-700">0</span> + 쿠팡실물 <span id="statCoupangStock" class="font-semibold text-blue-700">0</span>)</span>
                </div>
                <span class="text-slate-300">|</span>
                <div class="flex items-center space-x-1.5">
                    <span class="font-bold text-slate-900">7일 총 출고:</span>
                    <span id="statTotalSales7d" class="font-bold text-indigo-700 text-sm">0개</span>
                    <span id="statSalesSubtext" class="text-slate-400 text-[11px]">(쿠팡 0 + 외 0)</span>
                </div>
            </div>
            <div class="text-[11px] text-slate-500 flex items-center space-x-2">
                <span class="inline-flex items-center text-slate-600 bg-slate-100 px-2 py-0.5 rounded font-mono">
                    <i class="fa-solid fa-arrow-down-short-wide mr-1 text-slate-400"></i>위험도(Risk Score) 순 우선 정렬됨
                </span>
            </div>
        </div>

        <!-- 3. 메인 발주 의사결정 액션보드 테이블 (4. 열 칸 드래그 이동 가능) -->
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
            
            <!-- 필터 & 검색 툴바 -->
            <div class="p-3 border-b border-slate-200 space-y-2">
                <div class="flex flex-wrap items-center justify-between gap-3">
                    <div class="flex items-center space-x-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-rose-500 inline-block animate-ping"></span>
                        <h2 class="font-bold text-slate-900 text-sm">일쩜오도 마스터 상품 재고 & 발주 의사결정 보드</h2>
                        <span id="filteredCountText" class="text-xs text-slate-400 font-normal">| 총 44개 상품</span>
                        <span class="text-[10px] bg-slate-100 text-slate-600 px-2 py-0.5 rounded font-mono border border-slate-200">
                            <i class="fa-solid fa-grip mr-1 text-slate-400"></i>열 헤더 드래그로 순서 변경 가능
                        </span>
                    </div>

                    <div class="flex items-center space-x-2">
                        <!-- 검색창 -->
                        <div class="relative">
                            <i class="fa-solid fa-search absolute left-2.5 top-2.5 text-slate-400 text-xs"></i>
                            <input type="text" id="searchInput" oninput="applyFilters()" placeholder="상품명 또는 관리코드 검색..." class="text-xs pl-7 pr-3 py-1.5 border border-slate-200 rounded-lg w-48 sm:w-60 focus:outline-none focus:ring-1 focus:ring-emerald-500">
                        </div>

                        <!-- 열 순서 리셋 버튼 -->
                        <button onclick="resetColumnOrder()" title="열 순서를 기본값으로 초기화" class="text-xs px-2.5 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg font-semibold transition flex items-center space-x-1">
                            <i class="fa-solid fa-rotate-left"></i>
                            <span>열 순서 초기화</span>
                        </button>

                        <!-- 전체 펼치기/접기 -->
                        <button onclick="toggleAllOptionDetails()" class="text-xs px-2.5 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg font-semibold transition flex items-center space-x-1">
                            <i class="fa-solid fa-list-ul"></i>
                            <span id="expandBtnText">소비기한 세부내역</span>
                        </button>
                    </div>
                </div>

                <!-- 카테고리 및 상태 필터 탭 -->
                <div class="flex flex-wrap items-center justify-between gap-2 pt-1.5 border-t border-slate-100 text-xs">
                    <div class="flex flex-wrap items-center gap-1.5" id="categoryPills">
                        <button onclick="setCategoryFilter('all')" class="category-pill active px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-800 text-white">전체 (44)</button>
                        <button onclick="setCategoryFilter('수입')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-purple-100 text-purple-900 border border-purple-200 hover:bg-purple-200">🚢 수입제품 (17)</button>
                        <button onclick="setCategoryFilter('사입')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-blue-100 text-blue-900 border border-blue-200 hover:bg-blue-200">📦 사입제품 (27)</button>
                        <button onclick="setCategoryFilter('오일/식용유')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200">오일/식용유</button>
                        <button onclick="setCategoryFilter('파스타')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200">파스타</button>
                        <button onclick="setCategoryFilter('식초/애사비')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200">식초/애사비</button>
                        <button onclick="setCategoryFilter('발사믹')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200">발사믹</button>
                        <button onclick="setCategoryFilter('쿠키/스낵')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200">쿠키/스낵</button>
                    </div>

                    <div class="flex items-center space-x-1.5" id="statusFilterPills">
                        <button onclick="setStatusFilter('all')" class="status-pill active px-2 py-0.5 rounded text-[11px] font-bold bg-slate-800 text-white">전체보기</button>
                        <button onclick="setStatusFilter('urgent')" class="status-pill px-2 py-0.5 rounded text-[11px] font-bold bg-rose-100 text-rose-800 border border-rose-300 hover:bg-rose-200">🔴 긴급 발주</button>
                        <button onclick="setStatusFilter('prepare')" class="status-pill px-2 py-0.5 rounded text-[11px] font-bold bg-amber-100 text-amber-800 border border-amber-300 hover:bg-amber-200">🟡 발주 준비</button>
                        <button onclick="setStatusFilter('transfer')" class="status-pill px-2 py-0.5 rounded text-[11px] font-bold bg-blue-100 text-blue-800 border border-blue-300 hover:bg-blue-200">🔵 쿠팡 이동</button>
                        <button onclick="setStatusFilter('expiry')" class="status-pill px-2 py-0.5 rounded text-[11px] font-bold bg-purple-100 text-purple-900 border border-purple-300 hover:bg-purple-200">⏳ 소비기한 주의</button>
                        <button onclick="setStatusFilter('safe')" class="status-pill px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-300 hover:bg-emerald-200">🟢 정상 안전</button>
                    </div>
                </div>
            </div>

            <!-- 이동 가능한 액션보드 테이블 (13개 열) -->
            <div class="overflow-x-auto">
                <table id="masterTable" class="w-full text-left text-xs border-collapse">
                    <thead id="masterTableHead" class="bg-slate-50 text-slate-600 font-semibold uppercase tracking-wider border-b border-slate-200 select-none">
                        <!-- 동적 열 헤더 -->
                    </thead>
                    <tbody id="masterTableBody" class="divide-y divide-slate-100 font-sans">
                        <!-- 행 렌더링 -->
                    </tbody>
                    <tfoot id="masterTableFoot" class="bg-slate-100 font-bold border-t-2 border-slate-300 text-slate-800">
                        <!-- 하단 합계란 -->
                    </tfoot>
                </table>
            </div>
        </div>

        <!-- 4. 실시간 로그 콘솔 -->
        <div class="bg-slate-900 rounded-xl shadow border border-slate-800 p-3">
            <div class="flex items-center justify-between pb-2 border-b border-slate-800 text-xs text-slate-400">
                <div class="flex items-center space-x-1.5">
                    <span class="w-2.5 h-2.5 rounded-full bg-rose-500 inline-block"></span>
                    <span class="w-2.5 h-2.5 rounded-full bg-amber-500 inline-block"></span>
                    <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block"></span>
                    <span class="font-mono text-slate-300 font-semibold ml-1.5 text-[11px]">일쩜오도 시스템 처리 로그</span>
                </div>
                <button onclick="clearConsoleLogs()" class="hover:text-white transition text-[11px]">
                    <i class="fa-solid fa-eraser mr-1"></i>로그 초기화
                </button>
            </div>
            <div id="logConsole" class="log-box font-mono text-xs text-emerald-400 h-20 overflow-y-auto mt-2 space-y-0.5">
                <!-- 콘솔 내용 -->
            </div>
        </div>

        <!-- 5. [수정사항 2] CSV 수기 업데이트 존 (화면 최하단에 배치) -->
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-4 space-y-3">
            <div class="flex items-center justify-between pb-2 border-b border-slate-100">
                <div class="flex items-center space-x-2">
                    <i class="fa-solid fa-cloud-arrow-up text-indigo-600 text-sm"></i>
                    <h3 class="font-bold text-slate-800 text-xs sm:text-sm">재고 및 매출현황 파일 수기 업로드 (최신 데이터 갱신)</h3>
                </div>
                <span class="text-[11px] text-slate-400">파일을 상자에 드래그하거나 클릭하여 등록하면 실시간 자동 분석됩니다.</span>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                <!-- 1) 개미창고 파일 업로드 존 -->
                <div id="dropGaemi" class="bg-amber-50/30 rounded-xl border-2 border-dashed border-amber-300 hover:border-amber-500 p-3 transition cursor-pointer flex items-center space-x-3 group">
                    <input type="file" id="fileGaemi" accept=".csv, .xlsx, .xls" class="hidden">
                    <div class="w-9 h-9 rounded-lg bg-amber-500 text-white flex items-center justify-center text-base shrink-0 shadow-sm group-hover:scale-105 transition">
                        <i class="fa-solid fa-warehouse"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center space-x-1.5">
                            <span class="text-[10px] font-bold uppercase tracking-wider bg-amber-100 text-amber-800 px-1.5 py-0.2 rounded">3PL</span>
                            <h4 class="font-bold text-slate-800 text-xs">개미창고 상품목록 (클릭)</h4>
                        </div>
                        <p class="text-[11px] text-slate-500 truncate">
                            <span class="font-semibold text-amber-800">개미창고_상품목록_....csv</span>
                        </p>
                        <span id="gaemiStatusText" class="text-[10px] text-slate-400 font-medium">소비기한 및 재고 파싱 대기 중</span>
                    </div>
                </div>

                <!-- 2) 쿠팡 윙 파일 업로드 존 -->
                <div id="dropCoupang" class="bg-blue-50/30 rounded-xl border-2 border-dashed border-blue-300 hover:border-blue-500 p-3 transition cursor-pointer flex items-center space-x-3 group">
                    <input type="file" id="fileCoupang" accept=".csv, .xlsx, .xls" class="hidden">
                    <div class="w-9 h-9 rounded-lg bg-blue-600 text-white flex items-center justify-center text-base shrink-0 shadow-sm group-hover:scale-105 transition">
                        <i class="fa-solid fa-truck-fast"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center space-x-1.5">
                            <span class="text-[10px] font-bold uppercase tracking-wider bg-blue-100 text-blue-800 px-1.5 py-0.2 rounded">쿠팡물류</span>
                            <h4 class="font-bold text-slate-800 text-xs">쿠팡 재고건전성 (클릭)</h4>
                        </div>
                        <p class="text-[11px] text-slate-500 truncate">
                            <span class="font-semibold text-blue-800">쿠팡_inventory_health_....csv</span>
                        </p>
                        <span id="coupangStatusText" class="text-[10px] text-slate-400 font-medium">실시간 재고 및 7일 판매량 대기</span>
                    </div>
                </div>

                <!-- 3) 쿠팡외 통합 매출 파일 업로드 존 -->
                <div id="dropSales" class="bg-indigo-50/30 rounded-xl border-2 border-dashed border-indigo-300 hover:border-indigo-500 p-3 transition cursor-pointer flex items-center space-x-3 group">
                    <input type="file" id="fileSales" accept=".csv, .xlsx, .xls" class="hidden">
                    <div class="w-9 h-9 rounded-lg bg-indigo-600 text-white flex items-center justify-center text-base shrink-0 shadow-sm group-hover:scale-105 transition">
                        <i class="fa-solid fa-chart-line"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center space-x-1.5">
                            <span class="text-[10px] font-bold uppercase tracking-wider bg-indigo-100 text-indigo-800 px-1.5 py-0.2 rounded">통합 매출</span>
                            <h4 class="font-bold text-slate-800 text-xs">쿠팡외 매출정보 (클릭)</h4>
                        </div>
                        <p class="text-[11px] text-slate-500 truncate">
                            <span class="font-semibold text-indigo-800">매출정보CSV_....csv</span>
                        </p>
                        <span id="salesStatusText" class="text-[10px] text-slate-400 font-medium">스마트스토어 등 타채널 매출 대기</span>
                    </div>
                </div>
            </div>
        </div>

    </main>

    <!-- 스크립트 로직 -->
    <script>
        // 열 정의 (13개 열): 개미창고와 쿠팡창고 각각의 재고량, 런웨이, 소비기한 미니태그, 추천 조치 버튼 포함
        const COLUMN_DEFS = {
            'no': { id: 'no', label: 'No', thClass: 'text-center w-10 text-[11px]', tdClass: 'text-center font-mono text-slate-400 text-[11px]' },
            'status': { id: 'status', label: '발주 권고 상태', thClass: 'text-center w-36', tdClass: 'text-center' },
            'type': { id: 'type', label: '구분', thClass: 'text-center w-14', tdClass: 'text-center' },
            'code': { id: 'code', label: '관리코드', thClass: 'text-center w-20 font-mono text-[11px]', tdClass: 'text-center font-mono font-semibold text-slate-600 text-[11px]' },
            'name': { id: 'name', label: '마스터 상품명', thClass: 'text-left min-w-[210px]', tdClass: 'text-left' },
            'total_stock': { id: 'total_stock', label: '총 가용재고', thClass: 'text-right w-24 font-black bg-emerald-50/60 text-emerald-950 text-xs', tdClass: 'text-right font-mono font-black text-xs bg-emerald-50/30 text-emerald-950' },
            'gaemi_stock': { id: 'gaemi_stock', label: '개미창고(3PL)', thClass: 'text-right w-28 font-bold bg-amber-50/60 text-amber-900', tdClass: 'text-right bg-amber-50/20 text-amber-900' },
            'gaemi_runway': { id: 'gaemi_runway', label: '개미 런웨이', thClass: 'text-center w-20 text-amber-800 text-[11px] font-bold', tdClass: 'text-center font-mono font-semibold text-amber-800 text-[11px]' },
            'coupang_stock': { id: 'coupang_stock', label: '쿠팡창고(실물)', thClass: 'text-right w-24 font-bold bg-blue-50/60 text-blue-900', tdClass: 'text-right bg-blue-50/20 text-blue-900' },
            'coupang_runway': { id: 'coupang_runway', label: '쿠팡 런웨이', thClass: 'text-center w-20 text-blue-800 text-[11px] font-bold', tdClass: 'text-center font-mono font-semibold text-blue-800 text-[11px]' },
            'total_sales': { id: 'total_sales', label: '7일 출고량', thClass: 'text-right w-24 font-black bg-indigo-50/60 text-indigo-950', tdClass: 'text-right font-mono font-bold text-indigo-900 bg-indigo-50/20' },
            'total_runway': { id: 'total_runway', label: '통합 런웨이(D-Day)', thClass: 'text-center w-28 font-black text-slate-800', tdClass: 'text-center' },
            'action_cta': { id: 'action_cta', label: '추천 조치 (Action)', thClass: 'text-center w-32 font-bold', tdClass: 'text-center' }
        };

        const DEFAULT_COLUMN_ORDER = [
            'no', 'status', 'type', 'code', 'name', 
            'total_stock', 'gaemi_stock', 'gaemi_runway', 
            'coupang_stock', 'coupang_runway', 
            'total_sales', 'total_runway', 'action_cta'
        ];

        let currentColumnOrder = [...DEFAULT_COLUMN_ORDER];
        
        // localStorage에서 저장된 열 순서 불러오기 (v6 액션보드 전용)
        try {
            const savedOrder = localStorage.getItem('iljeomodo_col_order_v6');
            if (savedOrder) {
                const parsed = JSON.parse(savedOrder);
                if (Array.isArray(parsed) && parsed.length === DEFAULT_COLUMN_ORDER.length && parsed.includes('gaemi_runway')) {
                    currentColumnOrder = parsed;
                }
            }
        } catch (e) {}

        let allMasterData = [];
        let publicTunnelUrl = null;
        let expandAllOptions = false;
        let activeCategory = 'all';
        let activeStatus = 'all';

        // 3개 드래그앤드롭 이벤트 리스너 설정
        const setupDropZone = (dropId, fileId, source) => {
            const dropEl = document.getElementById(dropId);
            const fileEl = document.getElementById(fileId);
            dropEl.addEventListener('click', () => fileEl.click());

            ['dragover', 'dragenter'].forEach(ev => {
                dropEl.addEventListener(ev, (e) => { e.preventDefault(); dropEl.classList.add('border-emerald-500', 'bg-emerald-50/30'); });
            });
            ['dragleave', 'drop'].forEach(ev => {
                dropEl.addEventListener(ev, (e) => { e.preventDefault(); dropEl.classList.remove('border-emerald-500', 'bg-emerald-50/30'); });
            });
            dropEl.addEventListener('drop', (e) => {
                if (e.dataTransfer.files.length > 0) processFile(e.dataTransfer.files[0], source);
            });
            fileEl.addEventListener('change', (e) => {
                if (e.target.files.length > 0) processFile(e.target.files[0], source);
            });
        };

        setupDropZone('dropGaemi', 'fileGaemi', 'gaemi');
        setupDropZone('dropCoupang', 'fileCoupang', 'coupang');
        setupDropZone('dropSales', 'fileSales', 'sales');

        // SheetJS 파일 파싱
        function processFile(file, source) {
            let statusEl;
            if (source === 'gaemi') statusEl = document.getElementById('gaemiStatusText');
            else if (source === 'coupang') statusEl = document.getElementById('coupangStatusText');
            else statusEl = document.getElementById('salesStatusText');

            statusEl.innerText = `⏳ ${file.name} 분석 중...`;

            const reader = new FileReader();
            reader.onload = async function(e) {
                try {
                    const data = new Uint8Array(e.target.result);
                    const wb = XLSX.read(data, { type: 'array' });
                    const sheet = wb.Sheets[wb.SheetNames[0]];
                    const rows = XLSX.utils.sheet_to_json(sheet, { header: 1 });

                    const res = await fetch('/api/upload-file-data', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ source: source, rows: rows })
                    });
                    const resJson = await res.json();
                    if (resJson.success) {
                        statusEl.innerText = `✅ ${file.name} 반영 완료!`;
                        fetchSummary();
                    } else {
                        statusEl.innerText = `❌ ${resJson.message}`;
                        alert(resJson.message);
                    }
                } catch (err) {
                    statusEl.innerText = `❌ 오류: ${err.message}`;
                    alert("파일 파싱 실패: " + err.message);
                }
            };
            reader.readAsArrayBuffer(file);
        }

        async function loadSampleData() {
            try {
                const res = await fetch('/api/load-sample', { method: 'POST' });
                const json = await res.json();
                if (json.success) {
                    alert("✨ 개미창고(소비기한 포함), 쿠팡윙, 타채널 매출 샘플 데이터가 성공적으로 로드되었습니다!");
                    fetchSummary();
                } else {
                    alert("로드 실패: " + json.message);
                }
            } catch (err) {
                alert("샘플 로드 오류: " + err.message);
            }
        }

        function copyPublicUrl() {
            if (!publicTunnelUrl) {
                alert("아직 연동 주소가 생성되지 않았습니다.");
                return;
            }
            navigator.clipboard.writeText(publicTunnelUrl).then(() => {
                alert("구글 사이트 연동용 HTTPS 주소가 복사되었습니다!\\n\\n" + publicTunnelUrl);
            });
        }

        function toggleAllOptionDetails() {
            expandAllOptions = !expandAllOptions;
            document.getElementById('expandBtnText').innerText = expandAllOptions ? "소비기한 접기" : "소비기한 세부내역";
            renderMasterTable();
        }

        function toggleSingleOption(prodId) {
            const row = document.getElementById(`subrow_${prodId}`);
            if (row) row.classList.toggle('hidden');
        }

        function setCategoryFilter(cat) {
            activeCategory = cat;
            document.querySelectorAll('#categoryPills .category-pill').forEach(btn => {
                btn.classList.remove('bg-slate-800', 'text-white');
                btn.classList.add('bg-slate-100', 'text-slate-600');
            });
            event.target.classList.remove('bg-slate-100', 'text-slate-600');
            event.target.classList.add('bg-slate-800', 'text-white');
            applyFilters();
        }

        function setStatusFilter(stat) {
            activeStatus = stat;
            document.querySelectorAll('#statusFilterPills .status-pill').forEach(btn => {
                btn.classList.remove('bg-slate-800', 'text-white');
            });
            const targetBtn = Array.from(document.querySelectorAll('#statusFilterPills .status-pill')).find(b => b.getAttribute('onclick').includes(stat));
            if (targetBtn) targetBtn.classList.add('bg-slate-800', 'text-white');
            applyFilters();
        }

        function applyFilters() {
            renderMasterTable();
        }

        function resetColumnOrder() {
            currentColumnOrder = [...DEFAULT_COLUMN_ORDER];
            localStorage.removeItem('iljeomodo_col_order_v6');
            renderTableHeader();
            renderMasterTable();
        }

        async function clearConsoleLogs() {
            await fetch('/api/clear-logs', { method: 'POST' });
            fetchSummary();
        }

        // -------------------------------------------------------------
        // 열(Column) 드래그 앤 드롭 구현
        // -------------------------------------------------------------
        let draggedColId = null;

        function renderTableHeader() {
            const thead = document.getElementById('masterTableHead');
            let trHtml = "<tr class='select-none'>";
            currentColumnOrder.forEach(colId => {
                const col = COLUMN_DEFS[colId];
                trHtml += `
                    <th draggable="true" 
                        data-col="${colId}" 
                        class="py-2.5 px-3 cursor-move hover:bg-slate-200 transition ${col.thClass}"
                        title="드래그하여 열 순서를 자유롭게 변경할 수 있습니다">
                        <div class="flex items-center justify-between gap-1">
                            <span class="truncate">${col.label}</span>
                            <i class="fa-solid fa-grip text-[9px] text-slate-400"></i>
                        </div>
                    </th>
                `;
            });
            trHtml += "</tr>";
            thead.innerHTML = trHtml;

            // 이벤트 바인딩
            thead.querySelectorAll('th').forEach(th => {
                th.addEventListener('dragstart', (e) => {
                    draggedColId = th.getAttribute('data-col');
                    e.dataTransfer.effectAllowed = 'move';
                    th.classList.add('opacity-50');
                });
                th.addEventListener('dragend', () => {
                    th.classList.remove('opacity-50');
                    thead.querySelectorAll('th').forEach(h => h.classList.remove('drag-col-over'));
                });
                th.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    th.classList.add('drag-col-over');
                });
                th.addEventListener('dragleave', () => {
                    th.classList.remove('drag-col-over');
                });
                th.addEventListener('drop', (e) => {
                    e.preventDefault();
                    th.classList.remove('drag-col-over');
                    const targetColId = th.getAttribute('data-col');
                    if (draggedColId && targetColId && draggedColId !== targetColId) {
                        const fromIdx = currentColumnOrder.indexOf(draggedColId);
                        const toIdx = currentColumnOrder.indexOf(targetColId);
                        currentColumnOrder.splice(fromIdx, 1);
                        currentColumnOrder.splice(toIdx, 0, draggedColId);
                        localStorage.setItem('iljeomodo_col_order_v6', JSON.stringify(currentColumnOrder));
                        renderTableHeader();
                        renderMasterTable();
                    }
                });
            });
        }

        // -------------------------------------------------------------
        // 행(Row) 드래그 앤 드롭 구현
        // -------------------------------------------------------------
        let draggedRowIdx = null;

        function setupRowDragEvents() {
            const tbody = document.getElementById('masterTableBody');
            tbody.querySelectorAll('tr[draggable="true"]').forEach(tr => {
                tr.addEventListener('dragstart', (e) => {
                    draggedRowIdx = parseInt(tr.getAttribute('data-row-idx'));
                    e.dataTransfer.effectAllowed = 'move';
                    tr.classList.add('opacity-40');
                });
                tr.addEventListener('dragend', () => {
                    tr.classList.remove('opacity-40');
                    tbody.querySelectorAll('tr').forEach(r => r.classList.remove('drag-row-over'));
                });
                tr.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    tr.classList.add('drag-row-over');
                });
                tr.addEventListener('dragleave', () => {
                    tr.classList.remove('drag-row-over');
                });
                tr.addEventListener('drop', (e) => {
                    e.preventDefault();
                    tr.classList.remove('drag-row-over');
                    const targetRowIdx = parseInt(tr.getAttribute('data-row-idx'));
                    if (draggedRowIdx !== null && targetRowIdx !== null && draggedRowIdx !== targetRowIdx) {
                        const item = allMasterData.splice(draggedRowIdx, 1)[0];
                        allMasterData.splice(targetRowIdx, 0, item);
                        renderMasterTable();
                    }
                });
            });
        }

        // 데이터 주기적 갱신
        async function fetchSummary() {
            try {
                const res = await fetch('/api/summary');
                const data = await res.json();
                allMasterData = data.masters || [];

                // 1. 헤더 재고 업데이트 일시 표기
                if (data.stats.last_updated) {
                    document.getElementById('headerLastUpdated').innerText = data.stats.last_updated;
                }

                // 2. 5대 Action KPI 통계 갱신
                document.getElementById('statUrgentOrders').innerText = (data.stats.urgent_order_count || 0);
                document.getElementById('statOrderPrepare').innerText = (data.stats.order_prepare_count || 0);
                document.getElementById('statCoupangTransfer').innerText = (data.stats.coupang_transfer_count || 0);
                document.getElementById('statExpiryWarning').innerText = (data.stats.expiry_warning_count || 0);
                document.getElementById('statSafeStock').innerText = (data.stats.safe_count || 0);

                // 3. 재고 요약 바 갱신
                document.getElementById('statTotalMasters').innerText = data.stats.total_masters + '개';
                document.getElementById('statGaemiStock').innerText = data.stats.total_gaemi_stock.toLocaleString() + '개';
                document.getElementById('statCoupangStock').innerText = data.stats.total_coupang_stock.toLocaleString() + '개';
                document.getElementById('statTotalStock').innerText = data.stats.total_physical_stock.toLocaleString() + '개';
                document.getElementById('statTotalSales7d').innerText = data.stats.total_sales_7d.toLocaleString() + '개';
                document.getElementById('statSalesSubtext').innerText = `(쿠팡 ${data.stats.total_coupang_7d} + 외 ${data.stats.total_other_7d})`;

                // 4. 하단 소스 상태 배지
                if (data.stats.has_gaemi_data) {
                    document.getElementById('gaemiStatusText').innerText = "✅ 개미창고 소비기한 & 재고 로드됨";
                    document.getElementById('gaemiStatusText').className = "text-[10px] text-amber-700 font-semibold";
                }
                if (data.stats.has_coupang_data) {
                    document.getElementById('coupangStatusText').innerText = "✅ 쿠팡 윙 재고 로드됨";
                    document.getElementById('coupangStatusText').className = "text-[10px] text-blue-700 font-semibold";
                }
                if (data.stats.has_sales_data) {
                    document.getElementById('salesStatusText').innerText = "✅ 쿠팡외 통합 매출 로드됨";
                    document.getElementById('salesStatusText').className = "text-[10px] text-indigo-700 font-semibold";
                }

                // 5. 터널 주소
                if (data.public_url) {
                    publicTunnelUrl = data.public_url;
                    document.getElementById('publicUrlText').innerText = data.public_url;
                }

                // 6. 로그
                const logBox = document.getElementById('logConsole');
                logBox.innerHTML = (data.logs || []).map(l => `<div>${escapeHtml(l)}</div>`).join('');
                logBox.scrollTop = logBox.scrollHeight;

                renderMasterTable();
            } catch (err) {
                console.error("데이터 로드 실패:", err);
            }
        }

        function renderMasterTable() {
            const tbody = document.getElementById('masterTableBody');
            const tfoot = document.getElementById('masterTableFoot');
            const query = (document.getElementById('searchInput').value || '').toLowerCase().trim();

            let filtered = allMasterData.filter(m => {
                if (query) {
                    const matchName = (m.name || '').toLowerCase().includes(query);
                    const matchCode = (m.seller_code || '').toLowerCase().includes(query);
                    const matchNote = (m.note || '').toLowerCase().includes(query);
                    if (!matchName && !matchCode && !matchNote) return false;
                }
                if (activeCategory !== 'all') {
                    if (activeCategory === '수입') {
                        if (m.product_type !== '수입') return false;
                    } else if (activeCategory === '사입') {
                        if (m.product_type !== '사입') return false;
                    } else if (!m.category.includes(activeCategory)) {
                        return false;
                    }
                }
                if (activeStatus === 'urgent') {
                    if (m.status_color !== 'red') return false;
                } else if (activeStatus === 'prepare') {
                    if (m.status_color !== 'amber') return false;
                } else if (activeStatus === 'transfer') {
                    if (m.status_color !== 'blue') return false;
                } else if (activeStatus === 'expiry') {
                    if (m.status_color !== 'purple') return false;
                } else if (activeStatus === 'safe') {
                    if (m.status_color !== 'green') return false;
                }
                return true;
            });

            document.getElementById('filteredCountText').innerText = `| ${filtered.length}개 상품 표시 (총 ${allMasterData.length}개)`;

            if (filtered.length === 0) {
                tbody.innerHTML = `<tr><td colspan="${currentColumnOrder.length}" class="py-12 text-center text-slate-400">선택하신 조건에 일치하는 품목이 없습니다.</td></tr>`;
                tfoot.innerHTML = "";
                return;
            }

            // 합계 계산 (실물 재고는 공유 재고 품목 제외 합산하여 실물 중복 방지)
            let sumTotalStock = 0;
            let sumGaemiStock = 0;
            let sumCoupangStock = 0;
            let sumCoupangSales = 0;
            let sumOtherSales = 0;
            let sumTotalSales = 0;
            let sumDailySales = 0;

            let html = "";
            filtered.forEach((m, idx) => {
                if (!m.is_shared) {
                    sumTotalStock += m.master_physical_total;
                    sumGaemiStock += m.gaemi_total;
                    sumCoupangStock += m.coupang_physical_total;
                }
                sumCoupangSales += m.coupang_sales_7d;
                sumOtherSales += m.other_sales_7d;
                sumTotalSales += m.total_sales_7d;
                sumDailySales += m.daily_sales;

                let badgeClass = "bg-slate-100 text-slate-700";
                if (m.status_color === 'red') badgeClass = "bg-rose-100 text-rose-800 font-bold border border-rose-300 animate-pulse";
                else if (m.status_color === 'blue') badgeClass = "bg-blue-100 text-blue-900 font-bold border border-blue-300";
                else if (m.status_color === 'amber') badgeClass = "bg-amber-100 text-amber-900 font-bold border border-amber-300";
                else if (m.status_color === 'purple') badgeClass = "bg-purple-100 text-purple-900 font-bold border border-purple-300";
                else if (m.status_color === 'green') badgeClass = "bg-emerald-100 text-emerald-800 font-semibold border border-emerald-200";

                let ptypeBadge = m.product_type === '수입'
                    ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-purple-100 text-purple-900 border border-purple-200">수입</span>'
                    : '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-slate-100 text-slate-600 border border-slate-200">사입</span>';

                let sharedBadge = m.is_shared 
                    ? `<span class="px-1 py-0.2 rounded text-[9px] font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200 ml-1" title="${m.shared_base} 실물재고와 연동됨 (${m.shared_mult}개 묶음)">${m.shared_base} 연동(x${m.shared_mult})</span>` 
                    : '';

                // 개미 소비기한 미니 태그
                let expBadgeHtml = "";
                if (m.nearest_expiry_date) {
                    let dDayColor = "bg-emerald-50 text-emerald-700 border-emerald-200";
                    if (m.nearest_days_left <= 90 || m.expiry_risk) {
                        dDayColor = "bg-rose-100 text-rose-800 border-rose-300 font-bold";
                    } else if (m.nearest_days_left <= 180) {
                        dDayColor = "bg-amber-100 text-amber-800 border-amber-300 font-semibold";
                    }
                    const expShort = m.nearest_expiry_date.length >= 10 ? m.nearest_expiry_date.slice(2) : m.nearest_expiry_date;
                    expBadgeHtml = `
                        <div class="mt-0.5 flex flex-wrap justify-end gap-1">
                            <span class="inline-flex items-center text-[9px] px-1.5 py-0.2 rounded border ${dDayColor}" title="최단 소비기한: ${m.nearest_expiry_date}">
                                <i class="fa-regular fa-clock mr-1 text-[8px]"></i>${expShort} (D-${m.nearest_days_left})
                            </span>
                            ${m.is_mixed_lots ? `<span class="text-[9px] bg-slate-100 text-slate-600 border border-slate-200 px-1 rounded font-mono" title="${m.lot_count}개 로트/기한 혼합">${m.lot_count}개 기한</span>` : ''}
                        </div>
                    `;
                }

                // 쿠팡 런웨이 경고
                let coupangRunwayHtml = m.coupang_runway_str;
                if (m.coupang_runway <= 7 && m.coupang_physical_total > 0) {
                    coupangRunwayHtml = `<span class="text-rose-600 font-bold animate-pulse">🚨 ${m.coupang_runway_str}</span>`;
                }

                // 통합 런웨이 프로그레스 바 시각화
                let runwayBarColor = "bg-emerald-500";
                if (m.total_runway < 110) runwayBarColor = "bg-rose-500";
                else if (m.total_runway <= 130) runwayBarColor = "bg-amber-500";
                const barPercent = Math.min(100, Math.max(8, (m.total_runway / 180) * 100));

                html += `
                    <tr draggable="true" data-row-idx="${allMasterData.indexOf(m)}" class="hover:bg-slate-50 transition cursor-pointer select-none" onclick="toggleSingleOption('${m.id}')">
                `;

                currentColumnOrder.forEach(colId => {
                    const col = COLUMN_DEFS[colId];
                    if (colId === 'no') {
                        html += `<td class="${col.tdClass}"><i class="fa-solid fa-grip-vertical text-slate-300 mr-1 text-[9px]"></i>${idx + 1}</td>`;
                    } else if (colId === 'status') {
                        html += `<td class="${col.tdClass}"><span class="inline-block px-2 py-0.5 rounded-full text-[10px] ${badgeClass}">${m.status}</span></td>`;
                    } else if (colId === 'type') {
                        html += `<td class="${col.tdClass}">${ptypeBadge}</td>`;
                    } else if (colId === 'code') {
                        html += `<td class="${col.tdClass}">${escapeHtml(m.seller_code)}${sharedBadge}</td>`;
                    } else if (colId === 'name') {
                        html += `
                            <td class="${col.tdClass}">
                                <div class="font-bold text-slate-800 flex items-center space-x-1">
                                    <i class="fa-solid fa-angle-right text-[10px] text-slate-400"></i>
                                    <span>${escapeHtml(m.name)}</span>
                                </div>
                                <div class="text-[10px] text-slate-400 mt-0.5 truncate max-w-xs">
                                    ${escapeHtml(m.category)} | ${escapeHtml(m.gaemi_details)}
                                </div>
                            </td>
                        `;
                    } else if (colId === 'total_stock') {
                        html += `<td class="${col.tdClass}">${m.master_physical_total.toLocaleString()}</td>`;
                    } else if (colId === 'gaemi_stock') {
                        html += `
                            <td class="${col.tdClass}">
                                <div class="font-bold font-mono">${m.gaemi_total.toLocaleString()}</div>
                                ${expBadgeHtml}
                            </td>
                        `;
                    } else if (colId === 'gaemi_runway') {
                        html += `<td class="${col.tdClass}">${m.gaemi_runway_str}</td>`;
                    } else if (colId === 'coupang_stock') {
                        html += `
                            <td class="${col.tdClass}">
                                <div class="font-bold font-mono">${m.coupang_physical_total.toLocaleString()}</div>
                                ${m.coupang_package_total > 0 ? `<div class="text-[9px] text-slate-400 font-normal">(${m.coupang_package_total}세트)</div>` : ''}
                            </td>
                        `;
                    } else if (colId === 'coupang_runway') {
                        html += `<td class="${col.tdClass}">${coupangRunwayHtml}</td>`;
                    } else if (colId === 'total_sales') {
                        html += `
                            <td class="${col.tdClass}">
                                <div>${m.total_sales_7d.toLocaleString()}</div>
                                <div class="text-[9px] text-slate-500 font-normal">일평균 ${m.daily_sales}개</div>
                            </td>
                        `;
                    } else if (colId === 'total_runway') {
                        html += `
                            <td class="${col.tdClass}">
                                <div class="font-bold font-mono text-[11px] text-slate-800">${m.total_runway_str}</div>
                                <div class="w-16 h-1.5 bg-slate-200 rounded-full mx-auto mt-1 overflow-hidden">
                                    <div class="${runwayBarColor} h-full rounded-full" style="width: ${barPercent}%"></div>
                                </div>
                            </td>
                        `;
                    } else if (colId === 'action_cta') {
                        html += `
                            <td class="${col.tdClass}">
                                <button class="px-2.5 py-1 rounded-md text-[10px] font-bold transition shadow-xs ${m.action_cta_class}">
                                    ${m.action_cta}
                                </button>
                            </td>
                        `;
                    }
                });

                html += `</tr>`;

                // 세부 확장 행 (소비기한 로트 목록, 선입선출 우선순위, 소진 시뮬레이션 및 쿠팡 번들 내역)
                html += `
                    <tr id="subrow_${m.id}" class="${expandAllOptions ? '' : 'hidden'} bg-slate-50 border-b border-slate-200">
                        <td colspan="${currentColumnOrder.length}" class="py-3 px-5">
                            <div class="space-y-3">
                                
                                <!-- 1. 개미창고 소비기한 & 로트별 선입선출(FIFO) 현황판 -->
                                ${m.gaemi_lots && m.gaemi_lots.length > 0 ? `
                                    <div class="bg-white rounded-lg border border-amber-200 p-3 shadow-xs">
                                        <div class="flex items-center justify-between mb-2">
                                            <div class="flex items-center space-x-1.5 text-xs font-bold text-amber-900">
                                                <i class="fa-solid fa-clock-rotate-left text-amber-600"></i>
                                                <span>개미창고 옵션별 소비기한 및 선입선출(FIFO) 출고 관리</span>
                                            </div>
                                            <span class="text-[10px] bg-amber-100 text-amber-900 font-semibold px-2 py-0.5 rounded">
                                                총 ${m.gaemi_lots.length}개 로트/옵션
                                            </span>
                                        </div>

                                        <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2 text-xs">
                                            ${m.gaemi_lots.map((lot, lIdx) => {
                                                const isNearest = (lot.expiry_date && lot.expiry_date === m.nearest_expiry_date);
                                                let fifoBadge = '<span class="text-[9px] bg-slate-100 text-slate-500 px-1.5 py-0.2 rounded font-medium">출고 대기</span>';
                                                let cardBorder = 'border-slate-200 bg-slate-50/50';

                                                if (isNearest && lot.stock > 0) {
                                                    fifoBadge = '<span class="text-[9px] bg-rose-100 text-rose-800 font-bold px-2 py-0.5 rounded-full border border-rose-200 animate-pulse">🥇 1순위 우선 출고</span>';
                                                    cardBorder = 'border-rose-300 bg-rose-50/40 ring-1 ring-rose-200';
                                                }

                                                const expText = lot.expiry_date ? `${lot.expiry_date} (D-${lot.days_left}일)` : '미기재/로트명';

                                                return `
                                                    <div class="p-2.5 rounded-lg border ${cardBorder} flex justify-between items-center">
                                                        <div class="min-w-0 pr-2">
                                                            <div class="font-bold text-slate-800 truncate">${escapeHtml(lot.option_name)}</div>
                                                            <div class="text-[10px] text-slate-500 font-mono mt-0.5 flex items-center">
                                                                <i class="fa-regular fa-calendar-check mr-1 text-slate-400"></i>
                                                                <span>${expText}</span>
                                                            </div>
                                                        </div>
                                                        <div class="text-right shrink-0">
                                                            <div class="font-bold font-mono text-slate-800">${lot.stock.toLocaleString()}개</div>
                                                            <div class="mt-1">${fifoBadge}</div>
                                                        </div>
                                                    </div>
                                                `;
                                            }).join('')}
                                        </div>

                                        <!-- 소비기한 vs 판매속도 소진 시뮬레이션 카드 -->
                                        ${m.nearest_expiry_date ? `
                                            <div class="mt-2.5 p-2.5 rounded-lg bg-amber-50/70 border border-amber-200 text-xs text-amber-950 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2">
                                                <div class="flex items-center space-x-2">
                                                    <i class="fa-solid fa-chart-pie text-amber-600 text-sm"></i>
                                                    <div>
                                                        <span class="font-bold">판매속도 대비 소진 시뮬레이션:</span>
                                                        <span>현재 일평균 <strong>${m.daily_sales}개</strong> 판매 시, 최단 소비기한(<strong>${m.nearest_expiry_date}</strong>, D-${m.nearest_days_left}일)까지 <strong>${m.projected_sales_to_exp}개</strong> 소진 예상 (로트 재고 ${m.nearest_lot_stock}개)</span>
                                                    </div>
                                                </div>
                                                <div>
                                                    ${m.expiry_risk_qty > 0 ? `
                                                        <span class="inline-block bg-rose-600 text-white font-bold px-2.5 py-1 rounded text-[11px] shadow-xs">
                                                            🚨 기한 내 약 ${m.expiry_risk_qty}개 미소진 위험 (특가/번들 할인 필요)
                                                        </span>
                                                    ` : `
                                                        <span class="inline-block bg-emerald-600 text-white font-bold px-2.5 py-1 rounded text-[11px] shadow-xs">
                                                            ✅ 기한 내 100% 정상 소진 예상
                                                        </span>
                                                    `}
                                                </div>
                                            </div>
                                        ` : ''}
                                    </div>
                                ` : ''}

                                <!-- 2. 실물 재고 연동 안내 바 -->
                                ${m.is_shared ? `
                                    <div class="bg-indigo-50/70 rounded-lg border border-indigo-200 p-2 text-[11px] text-indigo-900 flex items-center justify-between">
                                        <div class="flex items-center space-x-1.5">
                                            <i class="fa-solid fa-link text-indigo-600"></i>
                                            <span class="font-bold">실물 재고 연동 상품:</span>
                                            <span>이 상품은 <strong>${m.shared_base}</strong> 제품이 <strong>${m.shared_mult}개</strong> 묶인 상품으로, 창고 실물 재고 및 소비기한을 기본 상품과 100% 공유합니다.</span>
                                        </div>
                                        <span class="text-[10px] bg-indigo-200 text-indigo-800 px-2 py-0.5 rounded font-bold">주문 1건 = 실물 ${m.shared_mult}개 차감</span>
                                    </div>
                                ` : ''}

                                ${m.note && !m.is_shared ? `
                                    <div class="bg-purple-50/50 rounded border border-purple-200 p-2 text-[11px] text-purple-900">
                                        <i class="fa-solid fa-circle-info mr-1 text-purple-600"></i>
                                        <span class="font-bold">제품 참고사항:</span> ${escapeHtml(m.note)}
                                    </div>
                                ` : ''}

                                <!-- 3. 쿠팡 옵션 번들 내역 -->
                                ${m.options && m.options.length > 0 ? `
                                    <div class="bg-white rounded-lg border border-slate-200 p-2.5 shadow-sm">
                                        <div class="text-[11px] font-bold text-blue-700 mb-1 flex items-center space-x-1">
                                            <i class="fa-solid fa-cubes"></i>
                                            <span>쿠팡 윙 등록 옵션 ID별 번들 현황</span>
                                        </div>
                                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 text-[11px]">
                                            ${m.options.map(opt => `
                                                <div class="bg-blue-50/40 p-2 rounded border border-blue-100 flex justify-between items-center">
                                                    <div>
                                                        <span class="font-bold text-slate-800">${opt.name}</span>
                                                        <span class="text-slate-400 font-mono block text-[10px]">${opt.id} (${opt.bundle}개입)</span>
                                                    </div>
                                                    <div class="text-right">
                                                        <span class="font-bold text-blue-700 font-mono">${opt.coupang_stock}세트</span>
                                                        <span class="text-[10px] text-slate-500 block">실물 ${opt.physical_stock}개 | 7일출고: ${opt.sales_7d_physical}개</span>
                                                    </div>
                                                </div>
                                            `).join('')}
                                        </div>
                                    </div>
                                ` : ''}

                                <!-- 4. 타채널 주문 내역 -->
                                ${m.recent_orders && m.recent_orders.length > 0 ? `
                                    <div class="bg-white rounded-lg border border-slate-200 p-2.5 shadow-sm">
                                        <div class="text-[11px] font-bold text-indigo-700 mb-1 flex items-center space-x-1">
                                            <i class="fa-solid fa-receipt"></i>
                                            <span>쿠팡외 최근 주문 내역 (${m.other_sales_7d}개 출고)</span>
                                        </div>
                                        <div class="flex flex-wrap gap-1.5 text-[10px]">
                                            ${m.recent_orders.map(ord => `
                                                <span class="bg-indigo-50 border border-indigo-100 px-2 py-0.5 rounded text-slate-700">
                                                    <span class="font-semibold text-indigo-800">${ord.mall || '스마트스토어'}</span>: ${ord.qty}건 (${ord.date})
                                                </span>
                                            `).join('')}
                                        </div>
                                    </div>
                                ` : ''}
                            </div>
                        </td>
                    </tr>
                `;
            });

            tbody.innerHTML = html;
            setupRowDragEvents();

            // 5. 하단 합계란(tfoot) 렌더링
            let footHtml = "<tr>";
            currentColumnOrder.forEach(colId => {
                if (colId === 'no') {
                    footHtml += `<td class="py-2.5 px-3 text-center text-[11px] font-bold">합계</td>`;
                } else if (colId === 'status') {
                    footHtml += `<td class="py-2.5 px-3 text-center text-[11px] font-bold text-slate-600">위험도 정렬</td>`;
                } else if (colId === 'name') {
                    footHtml += `<td class="py-2.5 px-3 text-left font-black text-slate-900">총 ${filtered.length}개 품목 (실물 합계)</td>`;
                } else if (colId === 'total_stock') {
                    footHtml += `<td class="py-2.5 px-3 text-right font-black text-emerald-800 text-xs bg-emerald-100/60" title="공유 세트 중복 제외 실물 기준 합계">${sumTotalStock.toLocaleString()}</td>`;
                } else if (colId === 'gaemi_stock') {
                    footHtml += `<td class="py-2.5 px-3 text-right font-black text-amber-800 text-xs bg-amber-100/60">${sumGaemiStock.toLocaleString()}</td>`;
                } else if (colId === 'gaemi_runway') {
                    const avgGRunway = sumDailySales > 0 ? (sumGaemiStock / sumDailySales).toFixed(1) + '일' : '-';
                    footHtml += `<td class="py-2.5 px-3 text-center font-bold text-amber-900 text-[11px]">${avgGRunway}</td>`;
                } else if (colId === 'coupang_stock') {
                    footHtml += `<td class="py-2.5 px-3 text-right font-black text-blue-800 text-xs bg-blue-100/60">${sumCoupangStock.toLocaleString()}</td>`;
                } else if (colId === 'coupang_runway') {
                    const avgCRunway = sumCoupangSales > 0 ? (sumCoupangStock / (sumCoupangSales / 7.0)).toFixed(1) + '일' : '-';
                    footHtml += `<td class="py-2.5 px-3 text-center font-bold text-blue-900 text-[11px]">${avgCRunway}</td>`;
                } else if (colId === 'total_sales') {
                    footHtml += `<td class="py-2.5 px-3 text-right font-black text-indigo-950 text-xs bg-indigo-100/60">${sumTotalSales.toLocaleString()}</td>`;
                } else if (colId === 'total_runway') {
                    const avgDoc = sumDailySales > 0 ? (sumTotalStock / sumDailySales).toFixed(1) + '일' : '안전';
                    footHtml += `<td class="py-2.5 px-3 text-center font-bold text-slate-800 text-[11px]">평균 ${avgDoc}</td>`;
                } else if (colId === 'action_cta') {
                    footHtml += `<td class="py-2.5 px-3 text-center text-slate-500 font-semibold text-[10px]">결품 0 목표</td>`;
                } else {
                    footHtml += `<td class="py-2.5 px-3 text-center text-slate-400">-</td>`;
                }
            });
            footHtml += "</tr>";
            tfoot.innerHTML = footHtml;
        }

        function escapeHtml(text) {
            return (text || '').replace(/[&<>"']/g, function(m) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
            });
        }

        // 전체화면 토글 (HTML5 Fullscreen API + Fallback)
        async function toggleFullScreen() {
            try {
                if (!document.fullscreenElement && !document.webkitFullscreenElement) {
                    const elem = document.documentElement;
                    if (elem.requestFullscreen) {
                        await elem.requestFullscreen();
                    } else if (elem.webkitRequestFullscreen) {
                        await elem.webkitRequestFullscreen();
                    } else if (elem.msRequestFullscreen) {
                        await elem.msRequestFullscreen();
                    }
                } else {
                    if (document.exitFullscreen) {
                        await document.exitFullscreen();
                    } else if (document.webkitExitFullscreen) {
                        await document.webkitExitFullscreen();
                    }
                }
            } catch (err) {
                console.warn("구글 사이트 iframe sandbox 정책으로 전체화면이 차단되어 새 탭으로 엽니다:", err);
                window.open(window.location.href, '_blank');
            }
        }

        function updateFullscreenUI() {
            const isFull = !!(document.fullscreenElement || document.webkitFullscreenElement);
            const icon = document.getElementById('fullscreenIcon');
            const text = document.getElementById('fullscreenText');
            if (icon && text) {
                if (isFull) {
                    icon.className = 'fa-solid fa-compress';
                    text.innerText = '화면 축소';
                } else {
                    icon.className = 'fa-solid fa-expand';
                    text.innerText = '전체화면';
                }
            }
        }

        document.addEventListener('fullscreenchange', updateFullscreenUI);
        document.addEventListener('webkitfullscreenchange', updateFullscreenUI);

        function openInNewTab() {
            window.open(window.location.href, '_blank');
        }

        // 구글 사이트 등 iframe 내부에 삽입된 경우 세로 공간 절약을 위해 상단 배너 자동 숨김
        if (window.self !== window.top) {
            const banner = document.getElementById('tunnelBanner');
            if (banner) banner.classList.add('hidden');
        }

        // 초기화
        renderTableHeader();
        setInterval(fetchSummary, 3000);
        fetchSummary();
    </script>
</body>
</html>
"""

def open_browser():
    if os.environ.get("RENDER") or os.environ.get("RAILWAY_STATIC_URL") or os.environ.get("HEADLESS"):
        return
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    print("=" * 70)
    print("🌱 [일쩜오도 재고현황 | 통합 재고 & 출고 관리 대시보드 실행]")
    print("👉 로컬 접속: http://127.0.0.1:5000")
    print("=" * 70)
    
    # 브라우저 자동 오픈
    threading.Thread(target=open_browser, daemon=True).start()
    
    # 플라스크 서버 구동
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
