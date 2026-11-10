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
import copy
import hmac
import uuid
import hashlib
import threading
import webbrowser
import urllib.request
import urllib.error
import http.cookiejar
from functools import wraps
from datetime import datetime, date, timedelta, timezone
from flask import Flask, render_template_string, request, jsonify, Response, send_file

KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(KST)

def today_kst():
    return now_kst().date()

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'inventory_master_store.json')
LOGO_FILE = os.path.join(BASE_DIR, 'logo.png')
GAS_SCRIPT_FILE = os.path.join(BASE_DIR, 'google_apps_script_bridge.gs')

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

def normalize_date_str(val):
    """YYYY-MM-DD 형식으로 날짜 문자열 정규화 (유효하지 않으면 빈 문자열)"""
    if not val:
        return ""
    s = str(val).strip()
    if not s or s == "__CLEAR__":
        return s
    d = extract_expiry_date(s)
    return d.strftime("%Y-%m-%d") if d else ""

# -------------------------------------------------------------
# 세트/B2B 상품 실물 재고 연동 기본 매핑 규칙 (관리자 모드에서 수정 가능)
# -------------------------------------------------------------
DEFAULT_SHARED_STOCK_MAP = {
    "S100005": {"base_code": "10GL02", "multiplier": 12, "desc": "10GL02 12개 묶음 B2B"},
    "S100009": {"base_code": "10GL04", "multiplier": 12, "desc": "10GL04 12개 묶음 B2B"},
    "S100011": {"base_code": "10GL05", "multiplier": 12, "desc": "10GL05 12개 묶음 B2B"},
    "S100033": {"base_code": "10GL06", "multiplier": 12, "desc": "10GL06 12개 묶음 B2B"},
    "S200018": {"base_code": "20TZ01", "multiplier": 2, "desc": "20TZ01 2병 선물세트"},
    "S200019": {"base_code": "20EO02", "multiplier": 2, "desc": "20EO02 2병 선물세트"},
}

SHARED_STOCK_MAP = copy.deepcopy(DEFAULT_SHARED_STOCK_MAP)

BASE_TO_SHARED_MAP = {}
for _s_code, _info in SHARED_STOCK_MAP.items():
    _b_code = _info["base_code"]
    if _b_code not in BASE_TO_SHARED_MAP:
        BASE_TO_SHARED_MAP[_b_code] = []
    BASE_TO_SHARED_MAP[_b_code].append({
        "s_code": _s_code,
        "multiplier": _info["multiplier"],
        "desc": _info["desc"]
    })

DEFAULT_SETTINGS = {
    "default_import_lead_time": 110,
    "default_import_buffer": 20,
    "default_purchase_lead_time": 7,
    "default_purchase_buffer": 7,
    "default_expiry_warn_days": 120,
    "admin_pin_custom": ""
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
drive_sync_lock = threading.Lock()

app_state = {
    "master_products": get_initial_master_products(),
    "b2b_mappings": copy.deepcopy(DEFAULT_SHARED_STOCK_MAP),
    "lot_expiry_overrides": {},  # { "gaemi_code||option_name[||reg_date]": "YYYY-MM-DD" | "__CLEAR__" }
    "settings": copy.deepcopy(DEFAULT_SETTINGS),
    "gaemi_inventory": {},     # { code: [ { option_name, stock, reg_date, status, expiry_date, days_left }, ... ] }
    "coupang_inventory": {},   # { option_id: { stock, inbound, sales_7d, sales_30d, doc, name, opt_name } }
    "sales_inventory": {},     # { seller_code: { total_7d, orders: [ { date, qty, mall, name } ] } }
    "last_updated": "",
    "data_uploaded_at": "",
    "upload_timestamps": {},   # { "gaemi": "YYYY-MM-DD HH:MM", "coupang": "...", "sales": "..." }
    "logs": [f"[{now_kst().strftime('%H:%M:%S')}] 일쩜오도 재고 관리 시스템이 준비되었습니다."],
    "public_url": os.environ.get("RENDER_EXTERNAL_URL", "https://inventory-management-q7bp.onrender.com")
}

drive_sync_state = {
    "configured": False,
    "status": "disabled",      # disabled | idle | syncing | synced | error
    "last_synced_at": "",
    "last_loaded_at": "",
    "last_error": "",
    "file_id": ""
}

def get_active_b2b_mappings():
    m = app_state.get("b2b_mappings")
    return m if isinstance(m, dict) else DEFAULT_SHARED_STOCK_MAP

def get_base_to_shared_map(mappings=None):
    src = mappings if mappings is not None else get_active_b2b_mappings()
    res = {}
    for s_code, info in src.items():
        b_code = str(info.get("base_code", "")).strip().upper()
        if not b_code:
            continue
        if b_code not in res:
            res[b_code] = []
        res[b_code].append({
            "s_code": str(s_code).strip().upper(),
            "multiplier": int(info.get("multiplier", 1)),
            "desc": info.get("desc", "")
        })
    return res

def is_s_product_code(code):
    """관리코드에 S가 붙은 상품 또는 b2b_mappings에 등록된 세트/B2B 상품 판별"""
    if not code:
        return False
    c = str(code).strip().upper()
    return c.startswith("S") or c in get_active_b2b_mappings()

def get_matching_gaemi_items(m, gaemi_inv):
    """
    마스터 상품과 매칭되는 개미창고 (gaemi_code, item) 튜플 목록 반환.
    우선순위:
    1) 관리자 명시 매핑 gaemi_mappings ([{gaemi_code, option_name}]) - 상품코드+옵션명 정확 매칭 (option_name이 '' 또는 '*'면 전체 옵션)
    2) 하위 호환 키워드 매핑 gaemi_codes + gaemi_filter
    """
    items = []
    gaemi_mappings = m.get("gaemi_mappings")
    if isinstance(gaemi_mappings, list) and len(gaemi_mappings) > 0:
        upper_code_map = {str(k).strip().upper(): (k, v) for k, v in gaemi_inv.items()}
        seen_refs = set()
        for rule in gaemi_mappings:
            if not isinstance(rule, dict):
                continue
            req_code = str(rule.get("gaemi_code") or rule.get("code") or "").strip()
            req_opt = str(rule.get("option_name", "")).strip()
            if not req_code:
                continue
            matched = gaemi_inv.get(req_code)
            actual_code = req_code
            if matched is None:
                found = upper_code_map.get(req_code.upper())
                if found:
                    actual_code, matched = found
            if not matched:
                continue
            for idx, item in enumerate(matched):
                item_opt = str(item.get("option_name", "")).strip()
                if req_opt in ("", "*") or item_opt == req_opt:
                    ref = (actual_code, idx)
                    if ref not in seen_refs:
                        seen_refs.add(ref)
                        items.append((actual_code, item))
        return items

    opt_filter = (m.get("gaemi_filter") or "").strip()
    seen_legacy = set()
    for g_code in m.get("gaemi_codes", []):
        g_c = str(g_code).strip()
        if not g_c or g_c in seen_legacy:
            continue
        seen_legacy.add(g_c)
        for item in gaemi_inv.get(g_c, []):
            if opt_filter and opt_filter not in item.get("option_name", ""):
                continue
            items.append((g_c, item))
    return items

def make_gaemi_lot_keys(g_code, item):
    opt = str(item.get("option_name", "")).strip()
    reg = str(item.get("reg_date", "")).strip()
    exact_key = f"{g_code}||{opt}||{reg}" if reg else f"{g_code}||{opt}"
    opt_key = f"{g_code}||{opt}"
    return exact_key, opt_key

def add_log(msg):
    timestamp = now_kst().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    with data_lock:
        app_state["logs"].append(entry)
        if len(app_state["logs"]) > 300:
            app_state["logs"].pop(0)
    try:
        print(entry)
    except Exception:
        pass

# -------------------------------------------------------------
# 관리자 PIN 인증 (토큰 서명 기반)
# -------------------------------------------------------------
_admin_revoked_before = 0
_admin_last_iat_ms = 0

def get_effective_admin_pin():
    """우선순위: 환경변수 ADMIN_PIN -> 관리자 저장 커스텀 PIN -> 기본값 '1500'"""
    env_pin = os.environ.get("ADMIN_PIN", "").strip()
    if env_pin:
        return env_pin, "env"
    with data_lock:
        custom_pin = str(app_state.get("settings", {}).get("admin_pin_custom", "")).strip()
    if custom_pin:
        return custom_pin, "custom"
    return "1500", "default"

def issue_admin_token(ttl_seconds=43200):
    global _admin_last_iat_ms
    now_s = int(time.time())
    iat_ms = max(int(time.time() * 1000), _admin_revoked_before + 1)
    _admin_last_iat_ms = iat_ms
    exp = now_s + ttl_seconds
    pin, _ = get_effective_admin_pin()
    secret = hashlib.sha256(f"{pin}:iljeomodo_admin_v1".encode("utf-8")).digest()
    msg = f"{iat_ms}.{exp}"
    sig = hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{iat_ms}.{exp}.{sig}", ttl_seconds

def verify_admin_token(token):
    if not token or str(token).count(".") != 2:
        return False
    try:
        iat_s, exp_s, sig = str(token).strip().split(".", 2)
        iat_ms = int(iat_s)
        exp = int(exp_s)
        now = int(time.time())
        if exp < now or iat_ms <= _admin_revoked_before:
            return False
        pin, _ = get_effective_admin_pin()
        secret = hashlib.sha256(f"{pin}:iljeomodo_admin_v1".encode("utf-8")).digest()
        expected = hmac.new(secret, f"{iat_s}.{exp_s}".encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Admin-Token", "").strip()
        if not verify_admin_token(token):
            return jsonify({
                "success": False,
                "code": "ADMIN_AUTH_REQUIRED",
                "message": "관리자 PIN 인증이 필요하거나 세션이 만료되었습니다."
            }), 401
        return fn(*args, **kwargs)
    return wrapper

# -------------------------------------------------------------
# Google Drive (Google Apps Script 웹앱) 영구 저장/복원 브리지
# -------------------------------------------------------------
def get_gas_config():
    url = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "").strip()
    token = os.environ.get("GOOGLE_APPS_SCRIPT_TOKEN", "").strip()
    configured = bool(url and token)
    return {"configured": configured, "url": url, "token": token}

def _call_gas_bridge(action, payload=None, timeout=15):
    cfg = get_gas_config()
    if not cfg["configured"]:
        return {"success": False, "error": "Google Apps Script 환경변수(GOOGLE_APPS_SCRIPT_URL, GOOGLE_APPS_SCRIPT_TOKEN)가 설정되지 않았습니다."}

    body = {
        "token": cfg["token"],
        "action": action
    }
    if payload is not None:
        body["payload"] = payload

    data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"],
        data=data_bytes,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Iljeomodo-Inventory/2.0"
        },
        method="POST"
    )
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)

def build_state_snapshot():
    with data_lock:
        return {
            "title": "일쩜오도 재고현황",
            "schema_version": 2,
            "master_products": copy.deepcopy(app_state["master_products"]),
            "b2b_mappings": copy.deepcopy(app_state["b2b_mappings"]),
            "lot_expiry_overrides": copy.deepcopy(app_state["lot_expiry_overrides"]),
            "settings": copy.deepcopy(app_state["settings"]),
            "gaemi_inventory": copy.deepcopy(app_state["gaemi_inventory"]),
            "coupang_inventory": copy.deepcopy(app_state["coupang_inventory"]),
            "sales_inventory": copy.deepcopy(app_state["sales_inventory"]),
            "last_updated": app_state.get("data_uploaded_at", ""),
            "data_uploaded_at": app_state.get("data_uploaded_at", ""),
            "upload_timestamps": copy.deepcopy(app_state.get("upload_timestamps", {})),
            "saved_at": now_kst().strftime("%Y-%m-%d %H:%M:%S")
        }

def _write_local_snapshot(snapshot):
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_FILE)

def _apply_loaded_state(saved):
    if not isinstance(saved, dict):
        return False
    with data_lock:
        if "master_products" in saved and isinstance(saved["master_products"], list):
            app_state["master_products"] = saved["master_products"]
        if "b2b_mappings" in saved and isinstance(saved["b2b_mappings"], dict):
            app_state["b2b_mappings"] = saved["b2b_mappings"]
        if "lot_expiry_overrides" in saved and isinstance(saved["lot_expiry_overrides"], dict):
            app_state["lot_expiry_overrides"] = saved["lot_expiry_overrides"]
        if "settings" in saved and isinstance(saved["settings"], dict):
            merged_settings = copy.deepcopy(DEFAULT_SETTINGS)
            merged_settings.update(saved["settings"])
            app_state["settings"] = merged_settings
        if "gaemi_inventory" in saved and isinstance(saved["gaemi_inventory"], dict):
            app_state["gaemi_inventory"] = saved["gaemi_inventory"]
        if "coupang_inventory" in saved and isinstance(saved["coupang_inventory"], dict):
            app_state["coupang_inventory"] = saved["coupang_inventory"]
        if "sales_inventory" in saved and isinstance(saved["sales_inventory"], dict):
            app_state["sales_inventory"] = saved["sales_inventory"]
        if "data_uploaded_at" in saved:
            du = str(saved.get("data_uploaded_at") or "").strip()
            app_state["data_uploaded_at"] = du
            app_state["last_updated"] = du
        else:
            app_state["data_uploaded_at"] = ""
            app_state["last_updated"] = ""
        if isinstance(saved.get("upload_timestamps"), dict):
            app_state["upload_timestamps"] = dict(saved["upload_timestamps"])
        else:
            app_state["upload_timestamps"] = {}
    return True

def sync_to_drive(wait=False):
    cfg = get_gas_config()
    if not cfg["configured"]:
        drive_sync_state["configured"] = False
        drive_sync_state["status"] = "disabled"
        return copy.deepcopy(drive_sync_state)

    drive_sync_state["configured"] = True

    def _do_sync():
        with drive_sync_lock:
            drive_sync_state["status"] = "syncing"
            try:
                snap = build_state_snapshot()
                res = _call_gas_bridge("save", payload=snap, timeout=15)
                if res.get("success"):
                    drive_sync_state["status"] = "synced"
                    drive_sync_state["last_synced_at"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
                    drive_sync_state["last_error"] = ""
                    if res.get("fileId"):
                        drive_sync_state["file_id"] = res["fileId"]
                else:
                    drive_sync_state["status"] = "error"
                    drive_sync_state["last_error"] = res.get("error", "알 수 없는 Drive 응답 오류")
                    add_log(f"⚠️ Google Drive 동기화 실패: {drive_sync_state['last_error']}")
            except Exception as e:
                drive_sync_state["status"] = "error"
                drive_sync_state["last_error"] = str(e)
                add_log(f"⚠️ Google Drive 동기화 예외: {e}")

    if wait:
        _do_sync()
    else:
        drive_sync_state["status"] = "syncing"
        threading.Thread(target=_do_sync, daemon=True).start()
    return copy.deepcopy(drive_sync_state)

def pull_from_drive():
    cfg = get_gas_config()
    if not cfg["configured"]:
        drive_sync_state["configured"] = False
        drive_sync_state["status"] = "disabled"
        return {"success": False, "exists": False, "error": "Google Apps Script 미설정"}

    drive_sync_state["configured"] = True
    try:
        with drive_sync_lock:
            res = _call_gas_bridge("load", timeout=15)
        if not res.get("success"):
            err = res.get("error", "Drive 조회 실패")
            drive_sync_state["status"] = "error"
            drive_sync_state["last_error"] = err
            return {"success": False, "exists": False, "error": err}

        if res.get("exists") and isinstance(res.get("data"), dict):
            _apply_loaded_state(res["data"])
            _write_local_snapshot(build_state_snapshot())
            now_s = now_kst().strftime("%Y-%m-%d %H:%M:%S")
            drive_sync_state["status"] = "synced"
            drive_sync_state["last_loaded_at"] = now_s
            drive_sync_state["last_synced_at"] = now_s
            drive_sync_state["last_error"] = ""
            if res.get("fileId"):
                drive_sync_state["file_id"] = res["fileId"]
            return {"success": True, "exists": True, "fileId": res.get("fileId", "")}
        else:
            drive_sync_state["status"] = "idle"
            return {"success": True, "exists": False}
    except Exception as e:
        drive_sync_state["status"] = "error"
        drive_sync_state["last_error"] = str(e)
        return {"success": False, "exists": False, "error": str(e)}

# 영구 데이터 로드/저장
def load_state():
    global app_state
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if _apply_loaded_state(saved):
                    add_log("💾 저장된 일쩜오도 재고 및 매핑 데이터를 불러왔습니다.")
        except Exception as e:
            add_log(f"⚠️ 로컬 데이터 로드 실패: {e}")

    cfg = get_gas_config()
    drive_sync_state["configured"] = cfg["configured"]
    if cfg["configured"] and os.environ.get("ILJEOMODO_SKIP_BOOT_DRIVE") != "1":
        res = pull_from_drive()
        if res.get("success") and res.get("exists"):
            add_log("☁️ Google Drive 영구 저장소에서 최신 관리자/재고 데이터를 자동 복원했습니다.")
        elif res.get("success") and not res.get("exists"):
            add_log("☁️ Google Drive 최초 연결 완료: 초기 기준 데이터를 Drive에 업로드합니다.")
            sync_to_drive(wait=False)
        else:
            add_log(f"⚠️ Google Drive 초기 복원 실패 (로컬 데이터로 계속 구동): {res.get('error')}")
    elif not cfg["configured"]:
        drive_sync_state["status"] = "disabled"

load_state()

def save_state(sync_drive=True, wait_drive=False):
    try:
        snapshot = build_state_snapshot()
        _write_local_snapshot(snapshot)
    except Exception as e:
        add_log(f"⚠️ 로컬 데이터 저장 오류: {e}")

    if sync_drive:
        return sync_to_drive(wait=wait_drive)
    return copy.deepcopy(drive_sync_state)

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
# 24시간 헬스체크 및 무중단 핑 엔드포인트 (Render / UptimeRobot)
# -------------------------------------------------------------
@app.route("/health")
@app.route("/ping")
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "iljeomodo-inventory",
        "drive_configured": get_gas_config()["configured"],
        "drive_status": drive_sync_state.get("status", "disabled"),
        "timestamp": now_kst().strftime("%Y-%m-%d %H:%M:%S")
    }), 200

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
    today = today_kst()

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
            "updated_at": now_kst().strftime("%m-%d %H:%M")
        }

    return coupang_dict

# -------------------------------------------------------------
# 파서 3: 쿠팡외 통합 매출 데이터 CSV 파서 (7일 & 30일 분리 집계)
# -------------------------------------------------------------
def parse_order_datetime(date_str):
    if not date_str:
        return None
    s = str(date_str).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y.%m.%d %H:%M:%S', '%Y.%m.%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    m = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None

def parse_sales_rows(rows):
    """
    매출정보 CSV:
    주문일, 묶음번호, 쇼핑몰, 온라인상품명, 주문수량, 금액, 할인금액, 배송비, 실결제금액, 수령자명, 배송완료일, 세트코드, 판매자관리코드
    현재로부터 30일간의 주문 데이터에서 최신 주문일 기준 직전 7일 출고량과 30일 전체 출고량을 분리 산출
    """
    if not rows or len(rows) < 2:
        return {}

    header_idx = -1
    for i, r in enumerate(rows[:5]):
        line_str = " ".join([str(c) for c in r])
        if "판매자관리코드" in line_str or "상품관리코드" in line_str or "주문수량" in line_str or "온라인상품명" in line_str:
            header_idx = i
            break

    if header_idx == -1:
        return {}

    headers = [str(c).strip() for c in rows[header_idx]]
    col_code = next((i for i, h in enumerate(headers) if "판매자관리코드" in h or "상품관리코드" in h or "판매자코드" in h), -1)
    if col_code == -1:
        col_code = next((i for i, h in enumerate(headers) if "관리코드" in h), -1)
    col_qty = next((i for i, h in enumerate(headers) if "주문수량" in h or "수량" in h), 4)
    col_date = next((i for i, h in enumerate(headers) if "주문일" in h), 0)
    col_mall = next((i for i, h in enumerate(headers) if "쇼핑몰" in h), 2)
    col_name = next((i for i, h in enumerate(headers) if "상품명" in h), 3)

    if col_code == -1:
        col_code = len(headers) - 1

    parsed_entries = []
    valid_dates = []

    for r in rows[header_idx + 1:]:
        if len(r) <= max(col_code, col_qty):
            continue
        code = str(r[col_code]).strip().upper()
        if not code or code in ("판매자관리코드", "상품관리코드", "판매자코드", "관리코드"):
            continue
        name = str(r[col_name]).strip() if len(r) > col_name else ""
        mall = str(r[col_mall]).strip() if len(r) > col_mall else ""
        date_s = str(r[col_date]).strip() if len(r) > col_date else ""
        qty_s = str(r[col_qty]).replace(",", "").strip()
        qty = int(qty_s) if qty_s.isdigit() else 0

        dt = parse_order_datetime(date_s)
        if dt:
            valid_dates.append(dt)

        parsed_entries.append({
            "code": code,
            "name": name,
            "mall": mall,
            "date_s": date_s,
            "dt": dt,
            "qty": qty
        })

    # 최신 주문일 기준 최근 7일 컷오프 (기준 시점)
    max_dt = max(valid_dates) if valid_dates else now_kst().replace(tzinfo=None)
    cutoff_7d = max_dt - timedelta(days=7)

    sales_dict = {}
    for entry in parsed_entries:
        code = entry["code"]
        qty = entry["qty"]
        dt = entry["dt"]

        if code not in sales_dict:
            sales_dict[code] = {
                "total_7d": 0,
                "total_30d": 0,
                "orders": []
            }

        # 30일 전체 출고량 합산
        sales_dict[code]["total_30d"] += qty

        # 직전 7일 출고량 합산
        if dt is None or dt >= cutoff_7d:
            sales_dict[code]["total_7d"] += qty

        sales_dict[code]["orders"].append({
            "date": entry["date_s"],
            "qty": qty,
            "mall": entry["mall"],
            "name": entry["name"]
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
    마스터 상품 단위 집계, B2B/세트 상품 실물 재고 연동, 관리자 기준 정보(상품명/구분/유통기한/입고예정) 최우선 병합,
    개미/쿠팡 창고별 개별 런웨이, 소비기한(FIFO) 추적 및 소진 시뮬레이션
    """
    with data_lock:
        master_list = copy.deepcopy(app_state["master_products"])
        gaemi = copy.deepcopy(app_state["gaemi_inventory"])
        coupang = copy.deepcopy(app_state["coupang_inventory"])
        sales = copy.deepcopy(app_state["sales_inventory"])
        b2b_mappings = copy.deepcopy(get_active_b2b_mappings())
        lot_expiry_overrides = copy.deepcopy(app_state.get("lot_expiry_overrides", {}))
        settings = copy.deepcopy(app_state.get("settings", DEFAULT_SETTINGS))
        logs = list(app_state["logs"])
        public_url = app_state["public_url"]
        last_updated = app_state.get("data_uploaded_at", "") or ""
        upload_timestamps = copy.deepcopy(app_state.get("upload_timestamps", {}))

    base_to_shared_map = get_base_to_shared_map(b2b_mappings)
    default_import_lt = int(settings.get("default_import_lead_time", 110) or 110)
    default_import_buf = int(settings.get("default_import_buffer", 20) or 20)
    default_purchase_lt = int(settings.get("default_purchase_lead_time", 7) or 7)
    default_purchase_buf = int(settings.get("default_purchase_buffer", 7) or 7)
    default_exp_warn = int(settings.get("default_expiry_warn_days", 120) or 120)

    # 1차 패스: 기본 품목들의 실물 재고, 소비기한 로트 및 쿠팡/타채널 직접 판매량 계산
    base_calc_map = {}
    today = today_kst()

    for m in master_list:
        seller_code = m.get("seller_code", m.get("id", "")).strip()
        norm_code = seller_code.upper()

        # 1) 개미창고 CSV 로트 수집 및 관리자 소비기한 오버라이드 병합
        raw_gaemi_items = get_matching_gaemi_items(m, gaemi)
        csv_g_total = 0
        csv_g_details = []
        csv_lots = []

        for g_code, item in raw_gaemi_items:
            st = int(item.get("stock", 0))
            csv_g_total += st
            opt_name = item.get("option_name", "")
            reg_date = item.get("reg_date", "")
            exact_key, opt_key = make_gaemi_lot_keys(g_code, item)
            raw_exp = normalize_date_str(item.get("expiry_date", ""))

            override_val = lot_expiry_overrides.get(exact_key)
            if override_val is None:
                override_val = lot_expiry_overrides.get(opt_key)

            if override_val == "__CLEAR__":
                eff_exp = ""
                is_admin_expiry = True
            elif override_val:
                eff_exp = normalize_date_str(override_val)
                is_admin_expiry = True
            else:
                eff_exp = raw_exp
                is_admin_expiry = False

            exp_d = extract_expiry_date(eff_exp)
            days_left = (exp_d - today).days if exp_d else None

            if st > 0:
                csv_g_details.append(f"{opt_name}: {st}개")
            csv_lots.append({
                "gaemi_code": g_code,
                "lot_key": exact_key,
                "opt_key": opt_key,
                "option_name": opt_name,
                "stock": st,
                "reg_date": reg_date,
                "expiry_date": eff_exp,
                "raw_expiry_date": raw_exp,
                "days_left": days_left,
                "is_admin_expiry": is_admin_expiry,
                "source": "csv"
            })

        # 2) 관리자 수동 분할 로트(manual_lots)가 있는 경우 최우선 로트 구성 + CSV 총량 병합
        manual_lots = m.get("manual_lots") or []
        has_manual_lots = len(manual_lots) > 0
        has_unassigned_stock = False

        if has_manual_lots:
            g_total = csv_g_total if len(raw_gaemi_items) > 0 else sum(max(0, int(l.get("qty", 0))) for l in manual_lots)
            gaemi_lots = []
            manual_sum = 0
            for lot in manual_lots:
                q = max(0, int(lot.get("qty", 0)))
                manual_sum += q
                eff_exp = normalize_date_str(lot.get("expiry_date", ""))
                exp_d = extract_expiry_date(eff_exp)
                days_left = (exp_d - today).days if exp_d else None
                gaemi_lots.append({
                    "lot_id": lot.get("lot_id", ""),
                    "option_name": (lot.get("label") or "수동 등록 로트").strip(),
                    "stock": q,
                    "reg_date": "",
                    "expiry_date": eff_exp,
                    "days_left": days_left,
                    "is_admin_expiry": True,
                    "source": "manual"
                })
            unassigned_qty = max(0, g_total - manual_sum)
            if unassigned_qty > 0:
                has_unassigned_stock = True
                gaemi_lots.append({
                    "lot_id": "__unassigned__",
                    "option_name": "미지정 재고 (소비기한 입력 필요)",
                    "stock": unassigned_qty,
                    "reg_date": "",
                    "expiry_date": "",
                    "days_left": None,
                    "is_admin_expiry": False,
                    "source": "unassigned"
                })
            g_details = [f"{l['option_name']}: {l['stock']}개" for l in gaemi_lots if l["stock"] > 0]
        else:
            g_total = csv_g_total
            gaemi_lots = [l for l in csv_lots if l["stock"] > 0]
            g_details = csv_g_details

        # 유효 재고(stock > 0)가 있는 로트 기준 분석 (소진된 0개 로트 제외)
        active_lots = [l for l in gaemi_lots if l["stock"] > 0]
        lot_count = len(active_lots)
        is_mixed_lots = lot_count > 1

        # 소비기한이 추출된 실재고 로트 선별 (선입선출: 가장 빠른 기한 우선, 0개 소진 로트 제외)
        exp_lots = [l for l in active_lots if l.get("days_left") is not None]

        default_exp = normalize_date_str(m.get("default_expiry_date", ""))
        has_admin_expiry = any(l.get("is_admin_expiry") and l.get("expiry_date") for l in active_lots)

        if exp_lots:
            exp_lots.sort(key=lambda x: x["days_left"])
            nearest_lot = exp_lots[0]
            nearest_expiry_date = nearest_lot["expiry_date"]
            nearest_days_left = nearest_lot["days_left"]
            nearest_lot_stock = nearest_lot["stock"]
        elif default_exp:
            exp_d = extract_expiry_date(default_exp)
            nearest_expiry_date = default_exp
            nearest_days_left = (exp_d - today).days if exp_d else None
            nearest_lot_stock = g_total
            has_admin_expiry = True
        else:
            nearest_expiry_date = ""
            nearest_days_left = None
            nearest_lot_stock = 0

        # 쿠팡 옵션 재고 및 출고량 계산
        c_package_total = 0
        c_physical_total = 0
        c_sales_7d_total = 0
        c_sales_30d_total = 0
        option_details = []

        for opt in m.get("options", []):
            oid = str(opt.get("id", "")).strip()
            bundle = int(opt.get("bundle", 1) or 1)
            c_data = coupang.get(oid, {})
            c_stock = int(c_data.get("stock", 0) or 0)
            c_inbound = int(c_data.get("inbound", 0) or 0)
            c_sales7 = int(c_data.get("sales_7d", 0) or 0)
            c_sales30 = int(c_data.get("sales_30d", 0) or 0)
            doc = int(c_data.get("doc", 0) or 0)

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

        # 쿠팡외 매출정보 상품관리코드 매핑 집계 (explicit sales_codes 또는 기본 [seller_code])
        has_explicit_sales = ("sales_codes" in m and isinstance(m["sales_codes"], list))
        raw_sales_codes = m["sales_codes"] if has_explicit_sales else ([norm_code] if norm_code else [])
        seen_sc = set()
        effective_sales_codes = []
        raw_other_sales_7d = 0
        raw_other_sales_30d = 0
        collected_orders = []

        for sc_raw in raw_sales_codes:
            sc = str(sc_raw).strip().upper()
            if not sc or sc in seen_sc:
                continue
            seen_sc.add(sc)
            effective_sales_codes.append(sc)
            if sc != norm_code and sc in b2b_mappings and str(b2b_mappings[sc].get("base_code", "")).strip().upper() == norm_code:
                continue
            s_data = sales.get(sc, {"total_7d": 0, "total_30d": 0, "orders": []})
            s7 = int(s_data.get("total_7d", 0) or 0)
            s30 = int(s_data.get("total_30d", s7) or 0)
            raw_other_sales_7d += s7
            raw_other_sales_30d += s30
            collected_orders.extend(list(s_data.get("orders", [])[:5]))

        if len(effective_sales_codes) > 1 and len(collected_orders) > 1:
            collected_orders.sort(key=lambda o: str(o.get("date", "")), reverse=True)
        recent_orders = collected_orders[:5]

        base_calc_map[norm_code] = {
            "sales_codes": effective_sales_codes,
            "has_custom_sales_codes": has_explicit_sales,
            "gaemi_total": g_total,
            "gaemi_details": ", ".join(g_details) if g_details else ("보유 재고 0개" if raw_gaemi_items else "등록 정보 없음"),
            "gaemi_lots": gaemi_lots,
            "csv_gaemi_lots": csv_lots,
            "has_manual_lots": has_manual_lots,
            "has_unassigned_stock": has_unassigned_stock,
            "has_admin_expiry": has_admin_expiry,
            "nearest_expiry_date": nearest_expiry_date,
            "nearest_days_left": nearest_days_left,
            "nearest_lot_stock": nearest_lot_stock,
            "is_mixed_lots": is_mixed_lots,
            "lot_count": lot_count,
            "coupang_package_total": c_package_total,
            "coupang_physical_total": c_physical_total,
            "master_physical_total": g_total + c_physical_total,
            "coupang_sales_7d": c_sales_7d_total,
            "coupang_sales_30d": c_sales_30d_total,
            "other_sales_7d": raw_other_sales_7d,
            "other_sales_30d": raw_other_sales_30d,
            "raw_other_orders_7d": raw_other_sales_7d,
            "raw_other_orders_30d": raw_other_sales_30d,
            "options": option_details,
            "recent_orders": recent_orders
        }

    # 1-1. B2B/세트(S코드) 주문 데이터를 연동 단품(base_code) 출고량에 자동 가산 합산
    # S코드 상품은 재고현황판 화면 노출에서는 제외되지만, 주문 데이터는 단품 출고량에 배수만큼 반영
    for s_code_raw, s_info in b2b_mappings.items():
        s_code = str(s_code_raw).strip().upper()
        base_code = str(s_info.get("base_code", "")).strip().upper()
        mult = int(s_info.get("multiplier", 1) or 1)

        s_sales = sales.get(s_code, {})
        s_7d = s_sales.get("total_7d", 0)
        s_30d = s_sales.get("total_30d", s_7d)
        s_orders = s_sales.get("orders", [])

        s_c_calc = base_calc_map.get(s_code, {})
        s_c_7d = s_c_calc.get("coupang_sales_7d", 0)
        s_c_30d = s_c_calc.get("coupang_sales_30d", 0)

        if base_code in base_calc_map:
            target = base_calc_map[base_code]
            if "b2b_shared_sales" not in target:
                target["b2b_shared_sales"] = []

            if s_7d > 0 or s_30d > 0 or s_orders or s_c_7d > 0 or s_c_30d > 0:
                target["other_sales_7d"] += (s_7d * mult)
                target["other_sales_30d"] += (s_30d * mult)
                target["coupang_sales_7d"] += (s_c_7d * mult)
                target["coupang_sales_30d"] += (s_c_30d * mult)

                for ord_item in s_orders:
                    target["recent_orders"].append({
                        "date": ord_item.get("date", ""),
                        "qty": ord_item.get("qty", 0) * mult,
                        "mall": f"{ord_item.get('mall', '타채널')} ({s_code} x{mult})",
                        "name": ord_item.get("name", "")
                    })

                target["b2b_shared_sales"].append({
                    "s_code": s_code,
                    "multiplier": mult,
                    "desc": s_info.get("desc", ""),
                    "orders_count": len(s_orders),
                    "sales_7d": s_7d * mult,
                    "sales_30d": s_30d * mult
                })

    # 2차 패스: 활성 단품(Single Product) 기준 최종 지표 종합 (S코드 및 숨김 처리된 품목 제외)
    single_master_list = [
        m for m in master_list
        if m.get("is_active", True) is not False
        and not (str(m.get("seller_code", m.get("id", ""))).strip().upper().startswith("S")
                 or str(m.get("seller_code", m.get("id", ""))).strip().upper() in b2b_mappings)
    ]
    inactive_count = sum(
        1 for m in master_list
        if m.get("is_active", True) is False
        and not (str(m.get("seller_code", m.get("id", ""))).strip().upper().startswith("S")
                 or str(m.get("seller_code", m.get("id", ""))).strip().upper() in b2b_mappings)
    )

    master_summaries = []
    total_gaemi_stock = 0
    total_coupang_stock = 0
    total_physical_stock = 0
    total_coupang_7d = 0
    total_coupang_30d = 0
    total_other_7d = 0
    total_other_30d = 0
    total_sales_7d = 0
    total_sales_30d = 0

    urgent_order_count = 0       # 🔴 긴급 발주 (리드타임 미만 / 결품)
    order_prepare_count = 0      # 🟡 발주 준비 (안전재고 버퍼 구간)
    coupang_transfer_count = 0   # 🔵 쿠팡 채우기 필요 (창고 간 불균형)
    expiry_warning_count = 0     # ⏳ 소비기한 주의
    safe_count = 0               # 🟢 정상 안전

    for m in single_master_list:
        seller_code = m.get("seller_code", m.get("id", "")).strip()
        norm_code = seller_code.upper()
        product_type = (m.get("product_type") or "사입").strip() or "사입"
        note = (m.get("note") or "").strip()

        # 품목별 맞춤 리드타임/버퍼/소비기한 주의일 (미입력 시 전역 설정 사용)
        custom_lt = m.get("lead_time_days")
        custom_buf = m.get("safety_buffer_days")
        custom_exp_warn = m.get("expiry_warn_days")

        eff_lead_time = int(custom_lt) if custom_lt not in (None, "") else (default_import_lt if product_type == "수입" else default_purchase_lt)
        eff_buffer = int(custom_buf) if custom_buf not in (None, "") else (default_import_buf if product_type == "수입" else default_purchase_buf)
        eff_prepare_limit = eff_lead_time + eff_buffer
        eff_exp_warn = int(custom_exp_warn) if custom_exp_warn not in (None, "") else default_exp_warn

        in_transit_qty = max(0, int(m.get("in_transit_qty") or 0))
        expected_arrival_date = (m.get("expected_arrival_date") or "").strip()
        lot_sync_note = (m.get("lot_sync_note") or "").strip()

        calc = base_calc_map.get(norm_code, {})

        g_total = calc.get("gaemi_total", 0)
        g_details = calc.get("gaemi_details", "")
        gaemi_lots = calc.get("gaemi_lots", [])
        csv_gaemi_lots = calc.get("csv_gaemi_lots", [])
        has_manual_lots = calc.get("has_manual_lots", False)
        has_unassigned_stock = calc.get("has_unassigned_stock", False)
        has_admin_expiry = calc.get("has_admin_expiry", False)
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
        c_sales_30d = calc.get("coupang_sales_30d", 0)
        other_sales_7d = calc.get("other_sales_7d", 0)
        other_sales_30d = calc.get("other_sales_30d", 0)
        recent_orders = calc.get("recent_orders", [])
        sales_codes = calc.get("sales_codes", [seller_code.upper()] if seller_code else [])
        has_custom_sales_codes = calc.get("has_custom_sales_codes", False)
        b2b_shared_sales = calc.get("b2b_shared_sales", [])
        b2b_linked_codes = base_to_shared_map.get(norm_code, [])

        # 3. 7일 및 30일 총 출고량 합산
        m_sales_7d = c_sales_7d + other_sales_7d
        m_sales_30d = c_sales_30d + other_sales_30d

        # 4. 채널별 가중 일평균 출고량 산출 (최근 7일 60% : 30일 40% 노이즈 보정)
        c_daily_7 = c_sales_7d / 7.0
        c_daily_30 = c_sales_30d / 30.0 if c_sales_30d > 0 else c_daily_7
        if c_sales_7d > 0 and c_sales_30d > 0:
            coupang_daily = round((c_daily_7 * 0.6) + (c_daily_30 * 0.4), 2)
        elif c_sales_7d > 0:
            coupang_daily = round(c_daily_7, 2)
        else:
            coupang_daily = round(c_daily_30, 2)

        o_daily_7 = other_sales_7d / 7.0
        o_daily_30 = other_sales_30d / 30.0 if other_sales_30d > 0 else o_daily_7
        if other_sales_7d > 0 and other_sales_30d > 0:
            other_daily = round((o_daily_7 * 0.6) + (o_daily_30 * 0.4), 2)
        elif other_sales_7d > 0:
            other_daily = round(o_daily_7, 2)
        else:
            other_daily = round(o_daily_30, 2)

        daily_sales = round(coupang_daily + other_daily, 2)

        total_gaemi_stock += g_total
        total_coupang_stock += c_physical_total
        total_physical_stock += master_physical_total

        total_coupang_7d += c_sales_7d
        total_coupang_30d += c_sales_30d
        total_other_7d += other_sales_7d
        total_other_30d += other_sales_30d
        total_sales_7d += m_sales_7d
        total_sales_30d += m_sales_30d

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
                elif nearest_days_left <= eff_exp_warn:
                    expiry_risk = True
                    expiry_note = f"기한 잔여 {nearest_days_left}일 주의"
            else:
                if nearest_days_left <= eff_exp_warn and nearest_lot_stock > 0:
                    expiry_risk = True
                    expiry_risk_qty = nearest_lot_stock
                    expiry_note = f"잔여 {nearest_days_left}일 (판매 정체)"

        # 6. 결품 방지 및 발주 의사결정 액션(Action) 판정 트리 (품목별/관리자 설정 리드타임 반영)
        is_urgent = False
        is_order_prepare = False
        is_coupang_transfer = False
        is_exp_warn = False
        is_safe = False

        if master_physical_total == 0:
            status = "🔴 결품 (0개)"
            status_color = "red"
            action_cta = "🚨 결품 즉시 발주"
            action_cta_class = "bg-rose-600 hover:bg-rose-700 text-white"
            risk_score = 1000
            is_urgent = True
        elif total_runway < eff_lead_time:
            status = f"🔴 긴급 발주 (D-{int(total_runway)}일)"
            status_color = "red"
            action_cta = "🚨 수입 발주 기안" if product_type == "수입" else "🚨 사입 긴급 발주"
            action_cta_class = "bg-rose-600 hover:bg-rose-700 text-white"
            risk_score = (900 if product_type == "수입" else 850) - total_runway
            is_urgent = True
        elif g_total >= 10 and (coupang_runway <= 14 or (c_physical_total == 0 and c_sales_7d > 0)):
            status = f"🔵 쿠팡 채우기 (쿠팡 D-{int(coupang_runway)}일)"
            status_color = "blue"
            action_cta = "📦 쿠팡 입고 요청"
            action_cta_class = "bg-blue-600 hover:bg-blue-700 text-white"
            risk_score = 700 - coupang_runway
            is_coupang_transfer = True
        elif total_runway <= eff_prepare_limit:
            status = f"🟡 발주 준비 (D-{int(total_runway)}일)" if product_type == "수입" else f"🟡 사입 주의 (D-{int(total_runway)}일)"
            status_color = "amber"
            action_cta = "📝 발주서 작성" if product_type == "수입" else "📝 사입 발주 검토"
            action_cta_class = "bg-amber-500 hover:bg-amber-600 text-white"
            risk_score = (500 if product_type == "수입" else 450) - total_runway
            is_order_prepare = True
        elif expiry_risk:
            status = f"⏳ 기한 주의 ({expiry_note})"
            status_color = "purple"
            action_cta = "🏷️ 임박 특가/프로모션"
            action_cta_class = "bg-purple-600 hover:bg-purple-700 text-white"
            risk_score = 350
            is_exp_warn = True
        else:
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
            "is_active": m.get("is_active", True),
            "lead_time_days": custom_lt,
            "safety_buffer_days": custom_buf,
            "expiry_warn_days": custom_exp_warn,
            "effective_lead_time": eff_lead_time,
            "effective_prepare_limit": eff_prepare_limit,
            "effective_expiry_warn": eff_exp_warn,
            "in_transit_qty": in_transit_qty,
            "expected_arrival_date": expected_arrival_date,
            "lot_sync_note": lot_sync_note,
            "is_shared": False,
            "has_b2b_set": (norm_code in base_to_shared_map),
            "b2b_linked_codes": b2b_linked_codes,
            "b2b_shared_sales": b2b_shared_sales,
            "category": m.get("category", "기타"),
            "sales_codes": sales_codes,
            "has_custom_sales_codes": has_custom_sales_codes,
            "gaemi_mappings": copy.deepcopy(m.get("gaemi_mappings") or []),
            "gaemi_codes": m.get("gaemi_codes", []),
            "gaemi_filter": m.get("gaemi_filter", ""),
            "gaemi_total": g_total,
            "gaemi_details": g_details,
            "gaemi_lots": gaemi_lots,
            "csv_gaemi_lots": csv_gaemi_lots,
            "manual_lots": copy.deepcopy(m.get("manual_lots") or []),
            "has_manual_lots": has_manual_lots,
            "has_unassigned_stock": has_unassigned_stock,
            "has_admin_expiry": has_admin_expiry,
            "default_expiry_date": normalize_date_str(m.get("default_expiry_date", "")),
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
            "coupang_sales_30d": c_sales_30d,
            "other_sales_7d": other_sales_7d,
            "other_sales_30d": other_sales_30d,
            "total_sales_7d": m_sales_7d,
            "total_sales_30d": m_sales_30d,
            "daily_sales": daily_sales,
            "coupang_daily": coupang_daily,
            "other_daily": other_daily,
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
    legacy_leadtime_count = sum(1 for m in master_summaries if m["product_type"] == "수입" and m["total_runway"] <= m["effective_prepare_limit"])

    return jsonify({
        "masters": master_summaries,
        "stats": {
            "total_masters": len(master_summaries),
            "inactive_count": inactive_count,
            "total_gaemi_stock": total_gaemi_stock,
            "total_coupang_stock": total_coupang_stock,
            "total_physical_stock": total_physical_stock,
            "total_coupang_7d": total_coupang_7d,
            "total_coupang_30d": total_coupang_30d,
            "total_other_7d": total_other_7d,
            "total_other_30d": total_other_30d,
            "total_sales_7d": total_sales_7d,
            "total_sales_30d": total_sales_30d,
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
            "data_uploaded_at": last_updated,
            "upload_timestamps": upload_timestamps,
            "has_gaemi_data": len(app_state["gaemi_inventory"]) > 0,
            "has_coupang_data": len(app_state["coupang_inventory"]) > 0,
            "has_sales_data": len(app_state["sales_inventory"]) > 0
        },
        "settings": {
            "default_import_lead_time": default_import_lt,
            "default_import_buffer": default_import_buf,
            "default_purchase_lead_time": default_purchase_lt,
            "default_purchase_buffer": default_purchase_buf,
            "default_expiry_warn_days": default_exp_warn
        },
        "drive_sync": copy.deepcopy(drive_sync_state),
        "logs": logs,
        "public_url": public_url
    })

def apply_fifo_deduction_on_gaemi_upload(old_gaemi, new_gaemi):
    """
    개미창고 CSV 재업로드 시, 관리자가 수동 분할한 소비기한 로트(manual_lots)가 있는 품목에 대해
    이전 CSV 재고 대비 감소한 출고 수량만큼 가장 빠른 소비기한 로트부터 선입선출(FIFO) 자동 차감
    """
    now_label = now_kst().strftime("%m-%d %H:%M")
    deducted_products = []
    for m in app_state.get("master_products", []):
        manual_lots = m.get("manual_lots")
        if not manual_lots:
            continue
        old_items = get_matching_gaemi_items(m, old_gaemi)
        new_items = get_matching_gaemi_items(m, new_gaemi)
        old_total = sum(int(item.get("stock", 0)) for _, item in old_items)
        new_total = sum(int(item.get("stock", 0)) for _, item in new_items)

        if not new_items and new_total == 0:
            continue

        shipped = max(0, old_total - new_total) if old_items else 0
        current_manual_sum = sum(max(0, int(l.get("qty", 0))) for l in manual_lots)
        excess = max(0, current_manual_sum - new_total)
        to_deduct = max(shipped, excess)

        if to_deduct > 0:
            indexed = list(enumerate(manual_lots))
            indexed.sort(key=lambda pair: (normalize_date_str(pair[1].get("expiry_date", "")) or "9999-12-31", pair[0]))
            remaining_deduct = to_deduct
            actually_deducted = 0
            for _, lot in indexed:
                if remaining_deduct <= 0:
                    break
                q = max(0, int(lot.get("qty", 0)))
                if q <= 0:
                    continue
                take = min(q, remaining_deduct)
                lot["qty"] = q - take
                remaining_deduct -= take
                actually_deducted += take
            if actually_deducted > 0:
                m["lot_sync_note"] = f"새 CSV 출고분 {actually_deducted}개 선입선출(FIFO) 자동 차감 ({now_label})"
                deducted_products.append(f"{m.get('seller_code')}(-{actually_deducted})")
        elif new_total > current_manual_sum:
            diff = new_total - current_manual_sum
            m["lot_sync_note"] = f"신규 입고 +{diff}개 감지됨 (소비기한 확인 권장 · {now_label})"
    return deducted_products

@app.route("/api/upload-file-data", methods=["POST"])
def upload_file_data():
    """
    파싱된 2차원 배열 데이터(개미창고, 쿠팡, 쿠팡외 매출) 반영.
    관리자 마스터 정보(상품명/구분/유통기한/리드타임/메모)는 보존되고 재고·출고 숫자만 최신 CSV로 병합됨.
    """
    body = request.get_json() or {}
    source = body.get("source") # 'gaemi', 'coupang', 'sales'
    rows = body.get("rows", [])

    if source == "gaemi":
        parsed = parse_gaemi_rows(rows)
        if not parsed:
            return jsonify({"success": False, "message": "개미창고 파일 형식(헤더: 상품코드, 재고량 등)을 인식하지 못했습니다."}), 400
        uploaded_at = now_kst().strftime("%Y-%m-%d %H:%M")
        with data_lock:
            old_gaemi = copy.deepcopy(app_state["gaemi_inventory"])
            deducted = apply_fifo_deduction_on_gaemi_upload(old_gaemi, parsed)
            app_state["gaemi_inventory"] = parsed
            app_state["data_uploaded_at"] = uploaded_at
            app_state["last_updated"] = uploaded_at
            app_state.setdefault("upload_timestamps", {})["gaemi"] = uploaded_at
        sync_res = save_state(sync_drive=True, wait_drive=False)
        total_items = sum(len(v) for v in parsed.values())
        fifo_msg = f" · FIFO 차감: {', '.join(deducted)}" if deducted else ""
        add_log(f"📦 [개미창고(3PL)] {len(parsed)}개 상품코드(세부 {total_items}건) 수량 갱신 완료 (관리자 소비기한/기준 유지{fifo_msg})")
        return jsonify({
            "success": True,
            "message": f"개미창고 {total_items}개 품목 재고가 반영되었습니다. (관리자 설정 보존)",
            "uploaded_at": uploaded_at,
            "fifo_deducted": deducted,
            "drive_sync": sync_res
        })

    elif source == "coupang":
        parsed = parse_coupang_rows(rows)
        if not parsed:
            return jsonify({"success": False, "message": "쿠팡 윙 파일 형식(헤더: 옵션 ID, 판매가능재고 등)을 인식하지 못했습니다."}), 400
        uploaded_at = now_kst().strftime("%Y-%m-%d %H:%M")
        with data_lock:
            app_state["coupang_inventory"].update(parsed)
            app_state["data_uploaded_at"] = uploaded_at
            app_state["last_updated"] = uploaded_at
            app_state.setdefault("upload_timestamps", {})["coupang"] = uploaded_at
        sync_res = save_state(sync_drive=True, wait_drive=False)
        add_log(f"🚀 [쿠팡 윙] {len(parsed)}개 옵션 ID의 실시간 재고 및 판매량 데이터가 업데이트되었습니다!")
        return jsonify({
            "success": True,
            "message": f"쿠팡 윙 {len(parsed)}개 옵션 데이터가 반영되었습니다.",
            "uploaded_at": uploaded_at,
            "drive_sync": sync_res
        })

    elif source == "sales":
        parsed = parse_sales_rows(rows)
        if not parsed:
            return jsonify({"success": False, "message": "매출정보 파일 형식(헤더: 판매자관리코드, 주문수량 등)을 인식하지 못했습니다."}), 400
        uploaded_at = now_kst().strftime("%Y-%m-%d %H:%M")
        with data_lock:
            app_state["sales_inventory"] = parsed
            app_state["data_uploaded_at"] = uploaded_at
            app_state["last_updated"] = uploaded_at
            app_state.setdefault("upload_timestamps", {})["sales"] = uploaded_at
        sync_res = save_state(sync_drive=True, wait_drive=False)
        total_qty = sum(v["total_7d"] for v in parsed.values())
        add_log(f"📈 [쿠팡외 통합 매출] {len(parsed)}개 상품코드(총 {total_qty}개 주문 출고) 데이터가 반영되었습니다!")
        return jsonify({
            "success": True,
            "message": f"쿠팡외 매출 {len(parsed)}개 품목({total_qty}개 출고)이 반영되었습니다.",
            "uploaded_at": uploaded_at,
            "drive_sync": sync_res
        })

    return jsonify({"success": False, "message": "유효하지 않은 데이터 소스입니다."}), 400

@app.route("/api/load-sample", methods=["POST"])
@require_admin
def load_sample():
    """원클릭 샘플 데이터 로드 (관리자 PIN 인증 필요)"""
    loaded = []
    loaded_sources = []

    gaemi_sample = os.path.join(BASE_DIR, "개미창고_샘플.csv")
    if os.path.exists(gaemi_sample):
        try:
            with open(gaemi_sample, encoding="utf-8") as f:
                rows = list(csv.reader(f))
                parsed = parse_gaemi_rows(rows)
                if parsed:
                    with data_lock:
                        old_gaemi = copy.deepcopy(app_state["gaemi_inventory"])
                        apply_fifo_deduction_on_gaemi_upload(old_gaemi, parsed)
                        app_state["gaemi_inventory"] = parsed
                    loaded.append("개미창고")
                    loaded_sources.append("gaemi")
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
                    loaded_sources.append("coupang")
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
                    loaded_sources.append("sales")
        except Exception as e:
            add_log(f"매출정보 샘플 로드 오류: {e}")

    if loaded_sources:
        uploaded_at = now_kst().strftime("%Y-%m-%d %H:%M")
        with data_lock:
            app_state["data_uploaded_at"] = uploaded_at
            app_state["last_updated"] = uploaded_at
            ts_map = app_state.setdefault("upload_timestamps", {})
            for src in loaded_sources:
                ts_map[src] = uploaded_at

    sync_res = save_state(sync_drive=True, wait_drive=False)
    add_log(f"✨ [샘플 데이터 로드 완료] {' / '.join(loaded)} 데이터가 반영되었습니다!")
    return jsonify({"success": True, "message": f"{', '.join(loaded)} 샘플 데이터 로드 완료", "drive_sync": sync_res})

@app.route("/api/clear-logs", methods=["POST"])
def clear_logs():
    with data_lock:
        app_state["logs"] = [f"[{now_kst().strftime('%H:%M:%S')}] 콘솔 로그가 초기화되었습니다."]
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
        "쿠팡(실물) 재고", "쿠팡 런웨이(일)", 
        "7일 출고량", "30일 출고량", "일평균 출고량", 
        "발주 권고 상태", "추천 조치"
    ])

    for m in masters:
        cw.writerow([
            m["seller_code"], m.get("product_type", "사입"), m["name"],
            m["master_physical_total"], m["total_runway_str"],
            m["gaemi_total"], m["gaemi_runway_str"], m.get("nearest_expiry_date") or "-",
            m["coupang_physical_total"], m["coupang_runway_str"],
            m["total_sales_7d"], m.get("total_sales_30d", 0), m["daily_sales"],
            m["status"], m["action_cta"]
        ])

    output = si.getvalue().encode('utf-8-sig')
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=iljeomodo_inventory_action_report.csv"}
    )

# -------------------------------------------------------------
# 관리자 모드 인증 & 데이터 편집 REST API
# -------------------------------------------------------------
@app.route("/api/admin/verify-pin", methods=["POST"])
def admin_verify_pin():
    body = request.get_json() or {}
    pin_input = str(body.get("pin", "")).strip()
    effective_pin, pin_source = get_effective_admin_pin()

    if not pin_input or not hmac.compare_digest(pin_input, effective_pin):
        return jsonify({"success": False, "message": "PIN 번호가 일치하지 않습니다."}), 401

    token, ttl = issue_admin_token()
    add_log("🔓 [관리자 모드] PIN 인증 성공 — 관리자 편집 모드가 활성화되었습니다.")
    return jsonify({
        "success": True,
        "token": token,
        "expires_in": ttl,
        "pin_source": pin_source,
        "drive_sync": copy.deepcopy(drive_sync_state)
    })

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    global _admin_revoked_before
    _admin_revoked_before = max(_admin_revoked_before, _admin_last_iat_ms, int(time.time() * 1000))
    add_log("🔒 [관리자 모드] 관리자 편집 모드가 잠금 처리되었습니다.")
    return jsonify({"success": True})

@app.route("/api/admin/session", methods=["GET"])
def admin_session_status():
    token = request.headers.get("X-Admin-Token", "").strip()
    is_auth = verify_admin_token(token)
    _, pin_source = get_effective_admin_pin()
    return jsonify({
        "authenticated": is_auth,
        "pin_source": pin_source if is_auth else "",
        "drive_sync": copy.deepcopy(drive_sync_state)
    })

@app.route("/api/admin/config", methods=["GET"])
@require_admin
def admin_get_config():
    _, pin_source = get_effective_admin_pin()
    gas_code = ""
    if os.path.exists(GAS_SCRIPT_FILE):
        try:
            with open(GAS_SCRIPT_FILE, 'r', encoding='utf-8') as f:
                gas_code = f.read()
        except Exception:
            pass

    with data_lock:
        masters = copy.deepcopy(app_state["master_products"])
        b2b = copy.deepcopy(get_active_b2b_mappings())
        lot_overrides = copy.deepcopy(app_state.get("lot_expiry_overrides", {}))
        settings = copy.deepcopy(app_state.get("settings", DEFAULT_SETTINGS))
        settings_public = {
            "default_import_lead_time": settings.get("default_import_lead_time", 110),
            "default_import_buffer": settings.get("default_import_buffer", 20),
            "default_purchase_lead_time": settings.get("default_purchase_lead_time", 7),
            "default_purchase_buffer": settings.get("default_purchase_buffer", 7),
            "default_expiry_warn_days": settings.get("default_expiry_warn_days", 120)
        }
        gaemi = copy.deepcopy(app_state.get("gaemi_inventory", {}))
        coupang = copy.deepcopy(app_state.get("coupang_inventory", {}))
        raw_sales = app_state.get("sales_inventory", {})
        sales_summary_map = {}
        for sc, sdata in raw_sales.items():
            orders = sdata.get("orders") or []
            sample_names = []
            malls = []
            for o in orders:
                nm = str(o.get("name", "")).strip()
                ml = str(o.get("mall", "")).strip()
                if nm and nm not in sample_names:
                    sample_names.append(nm)
                if ml and ml not in malls:
                    malls.append(ml)
                if len(sample_names) >= 2 and len(malls) >= 2:
                    break
            sales_summary_map[sc] = {
                "total_7d": int(sdata.get("total_7d", 0) or 0),
                "total_30d": int(sdata.get("total_30d", sdata.get("total_7d", 0)) or 0),
                "orders_count": len(orders),
                "sample_names": sample_names,
                "malls": malls
            }

    return jsonify({
        "success": True,
        "master_products": masters,
        "b2b_mappings": b2b,
        "lot_expiry_overrides": lot_overrides,
        "settings": settings_public,
        "gaemi_inventory": gaemi,
        "coupang_inventory": coupang,
        "sales_inventory": sales_summary_map,
        "pin_source": pin_source,
        "drive_sync": copy.deepcopy(drive_sync_state),
        "gas_script": gas_code
    })

def _parse_optional_positive_int(val):
    if val in (None, "", "null"):
        return None
    try:
        n = int(float(val))
        return n if n >= 0 else None
    except Exception:
        return None

def _parse_sales_codes_input(raw_val):
    if raw_val is None:
        return None
    if isinstance(raw_val, str):
        parts = [c.strip().upper() for c in raw_val.split(",") if c.strip()]
    elif isinstance(raw_val, list):
        parts = [str(c).strip().upper() for c in raw_val if str(c).strip()]
    else:
        parts = []
    seen = set()
    res = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res

def _parse_gaemi_mappings_input(raw_val):
    if not isinstance(raw_val, list):
        return []
    seen = set()
    res = []
    for item in raw_val:
        if not isinstance(item, dict):
            continue
        g_code = str(item.get("gaemi_code") or item.get("code") or "").strip()
        opt_name = str(item.get("option_name", "")).strip()
        if not g_code:
            continue
        sig = (g_code.upper(), opt_name)
        if sig in seen:
            continue
        seen.add(sig)
        res.append({"gaemi_code": g_code, "option_name": opt_name})
    return res

def _parse_coupang_options_input(raw_val):
    if not isinstance(raw_val, list):
        return []
    seen = set()
    clean_options = []
    for opt in raw_val:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("id", "")).strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        bundle = max(1, _parse_optional_positive_int(opt.get("bundle")) or 1)
        oname = str(opt.get("name", f"{bundle}개")).strip() or f"{bundle}개"
        clean_options.append({"id": oid, "bundle": bundle, "name": oname})
    return clean_options

@app.route("/api/admin/product/save", methods=["POST"])
@require_admin
def admin_save_product():
    """단일 마스터 상품 생성/수정 + 소비기한 오버라이드/분할 로트 + 코드매핑 + (선택) 임시 수량 보정"""
    body = request.get_json() or {}
    seller_code = str(body.get("seller_code", "")).strip()
    orig_id = str(body.get("id", "")).strip()
    orig_seller_code = str(body.get("original_seller_code", seller_code)).strip()
    name = str(body.get("name", "")).strip()

    if not seller_code or not name:
        return jsonify({"success": False, "message": "판매자관리코드와 상품명은 필수 입력 항목입니다."}), 400

    product_type = str(body.get("product_type", "사입")).strip()
    if product_type not in ("수입", "사입"):
        product_type = "사입"
    category = str(body.get("category", "기타")).strip() or "기타"
    note = str(body.get("note", "")).strip()
    is_active = bool(body.get("is_active", True))

    lead_time_days = _parse_optional_positive_int(body.get("lead_time_days"))
    safety_buffer_days = _parse_optional_positive_int(body.get("safety_buffer_days"))
    expiry_warn_days = _parse_optional_positive_int(body.get("expiry_warn_days"))
    in_transit_qty = max(0, _parse_optional_positive_int(body.get("in_transit_qty")) or 0)
    expected_arrival_date = normalize_date_str(body.get("expected_arrival_date", ""))
    default_expiry_date = normalize_date_str(body.get("default_expiry_date", ""))

    # 개미창고 코드 파싱 (배열 또는 쉼표 문자열 지원)
    raw_g_codes = body.get("gaemi_codes", [])
    if isinstance(raw_g_codes, str):
        gaemi_codes = [c.strip() for c in raw_g_codes.split(",") if c.strip()]
    elif isinstance(raw_g_codes, list):
        gaemi_codes = [str(c).strip() for c in raw_g_codes if str(c).strip()]
    else:
        gaemi_codes = []
    gaemi_filter = str(body.get("gaemi_filter", "")).strip()

    has_sales_codes = ("sales_codes" in body)
    parsed_sales_codes = _parse_sales_codes_input(body.get("sales_codes")) if has_sales_codes else None
    has_gaemi_mappings = ("gaemi_mappings" in body)
    clean_gaemi_mappings = _parse_gaemi_mappings_input(body.get("gaemi_mappings")) if has_gaemi_mappings else []

    # 쿠팡 옵션 파싱
    clean_options = _parse_coupang_options_input(body.get("options") or [])

    # 수동 분할 소비기한 로트 파싱
    clean_manual_lots = []
    for lot in (body.get("manual_lots") or []):
        exp = normalize_date_str(lot.get("expiry_date", ""))
        label = str(lot.get("label", "")).strip() or "로트"
        qty = max(0, _parse_optional_positive_int(lot.get("qty")) or 0)
        lot_id = str(lot.get("lot_id") or f"L_{uuid.uuid4().hex[:8]}").strip()
        if not exp and qty == 0 and not label:
            continue
        clean_manual_lots.append({
            "lot_id": lot_id,
            "label": label,
            "expiry_date": exp,
            "qty": qty
        })

    is_new = bool(body.get("is_new", False))
    norm_new_code = seller_code.upper()
    norm_orig_code = orig_seller_code.upper()

    with data_lock:
        masters = app_state["master_products"]
        target_idx = -1

        if not is_new:
            for idx, item in enumerate(masters):
                item_id = str(item.get("id", "")).strip()
                item_code = str(item.get("seller_code", "")).strip().upper()
                if (orig_id and item_id == orig_id) or (norm_orig_code and item_code == norm_orig_code):
                    target_idx = idx
                    break

        # 코드 중복 체크
        for idx, item in enumerate(masters):
            if idx == target_idx:
                continue
            if str(item.get("seller_code", "")).strip().upper() == norm_new_code:
                return jsonify({"success": False, "message": f"관리코드 '{seller_code}'는 이미 다른 품목에 등록되어 있습니다."}), 400

        if target_idx == -1:
            # 신규 단품 추가
            new_id = orig_id or f"P_{seller_code}"
            existing_ids = {str(x.get("id", "")) for x in masters}
            if new_id in existing_ids:
                new_id = f"P_{seller_code}_{uuid.uuid4().hex[:4]}"
            eff_g_codes = list(dict.fromkeys([r["gaemi_code"] for r in clean_gaemi_mappings])) if clean_gaemi_mappings else gaemi_codes
            eff_g_filter = "" if clean_gaemi_mappings else gaemi_filter
            record = {
                "id": new_id,
                "seller_code": seller_code,
                "name": name,
                "product_type": product_type,
                "note": note,
                "category": category,
                "is_active": is_active,
                "lead_time_days": lead_time_days,
                "safety_buffer_days": safety_buffer_days,
                "expiry_warn_days": expiry_warn_days,
                "in_transit_qty": in_transit_qty,
                "expected_arrival_date": expected_arrival_date,
                "default_expiry_date": default_expiry_date,
                "gaemi_codes": eff_g_codes,
                "gaemi_filter": eff_g_filter,
                "options": clean_options,
                "manual_lots": clean_manual_lots,
                "lot_sync_note": ""
            }
            if clean_gaemi_mappings:
                record["gaemi_mappings"] = clean_gaemi_mappings
            if has_sales_codes and parsed_sales_codes is not None:
                record["sales_codes"] = parsed_sales_codes
            masters.append(record)
            action_label = "신규 등록"
        else:
            record = masters[target_idx]
            old_code_upper = str(record.get("seller_code", "")).strip().upper()
            record["seller_code"] = seller_code
            record["name"] = name
            if "product_type" in body:
                record["product_type"] = product_type
            if "category" in body:
                record["category"] = category
            if "note" in body:
                record["note"] = note
            if "is_active" in body:
                record["is_active"] = is_active
            if "lead_time_days" in body:
                record["lead_time_days"] = lead_time_days
            if "safety_buffer_days" in body:
                record["safety_buffer_days"] = safety_buffer_days
            if "expiry_warn_days" in body:
                record["expiry_warn_days"] = expiry_warn_days
            if "in_transit_qty" in body:
                record["in_transit_qty"] = in_transit_qty
            if "expected_arrival_date" in body:
                record["expected_arrival_date"] = expected_arrival_date
            if "default_expiry_date" in body:
                record["default_expiry_date"] = default_expiry_date
            if has_sales_codes:
                if parsed_sales_codes is None:
                    record.pop("sales_codes", None)
                else:
                    record["sales_codes"] = parsed_sales_codes
            if has_gaemi_mappings:
                record["gaemi_mappings"] = clean_gaemi_mappings
                if len(clean_gaemi_mappings) > 0:
                    record["gaemi_codes"] = list(dict.fromkeys([r["gaemi_code"] for r in clean_gaemi_mappings]))
                    record["gaemi_filter"] = ""
                else:
                    if "gaemi_codes" in body:
                        record["gaemi_codes"] = gaemi_codes
                    if "gaemi_filter" in body:
                        record["gaemi_filter"] = gaemi_filter
            else:
                if "gaemi_codes" in body:
                    record["gaemi_codes"] = gaemi_codes
                if "gaemi_filter" in body:
                    record["gaemi_filter"] = gaemi_filter
            if "options" in body:
                record["options"] = clean_options
            if "manual_lots" in body:
                record["manual_lots"] = clean_manual_lots
                if len(clean_manual_lots) == 0:
                    record["lot_sync_note"] = ""

            # 관리코드 변경 시 b2b_mappings 역참조 자동 갱신
            if old_code_upper and old_code_upper != norm_new_code:
                for _, binfo in get_active_b2b_mappings().items():
                    if str(binfo.get("base_code", "")).strip().upper() == old_code_upper:
                        binfo["base_code"] = norm_new_code
            action_label = "수정"

        # CSV 개미 로트별 소비기한 오버라이드 저장
        csv_lot_expiries = body.get("csv_lot_expiries") or {}
        if isinstance(csv_lot_expiries, dict):
            overrides = app_state.setdefault("lot_expiry_overrides", {})
            for lot_key, exp_val in csv_lot_expiries.items():
                k = str(lot_key).strip()
                if not k:
                    continue
                v = str(exp_val or "").strip()
                if v == "__RESTORE__":
                    overrides.pop(k, None)
                elif v == "__CLEAR__":
                    overrides[k] = "__CLEAR__"
                elif v == "":
                    overrides.pop(k, None)
                else:
                    norm_d = normalize_date_str(v)
                    if norm_d:
                        overrides[k] = norm_d

        # (선택) 임시 수동 재고 보정 — 다음 CSV 업로드 전까지 반영됨
        temp_gaemi_stock_map = body.get("temp_gaemi_stocks")
        if isinstance(temp_gaemi_stock_map, dict) and temp_gaemi_stock_map:
            gaemi_inv = app_state["gaemi_inventory"]
            for g_code, item in get_matching_gaemi_items(record, gaemi_inv):
                ek, _ = make_gaemi_lot_keys(g_code, item)
                if ek in temp_gaemi_stock_map and temp_gaemi_stock_map[ek] not in (None, ""):
                    item["stock"] = max(0, int(temp_gaemi_stock_map[ek]))

        temp_coupang_stock_map = body.get("temp_coupang_stocks")
        if isinstance(temp_coupang_stock_map, dict) and temp_coupang_stock_map:
            coupang_inv = app_state["coupang_inventory"]
            for oid, st_val in temp_coupang_stock_map.items():
                if st_val in (None, ""):
                    continue
                oid_s = str(oid).strip()
                if oid_s in coupang_inv:
                    coupang_inv[oid_s]["stock"] = max(0, int(st_val))
                    coupang_inv[oid_s]["updated_at"] = now_kst().strftime("%m-%d %H:%M")
                else:
                    coupang_inv[oid_s] = {
                        "product_name": name,
                        "option_name": "수동 등록",
                        "stock": max(0, int(st_val)),
                        "inbound": 0,
                        "sales_7d": 0,
                        "sales_30d": 0,
                        "doc": 0,
                        "updated_at": now_kst().strftime("%m-%d %H:%M")
                    }

        saved_copy = copy.deepcopy(record)

    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log(f"✏️ [관리자 {action_label}] {seller_code} ({name}) 기준 정보가 업데이트되었습니다.")
    return jsonify({
        "success": True,
        "message": f"[{seller_code}] {name} {action_label} 완료!",
        "product": saved_copy,
        "drive_sync": sync_res
    })

@app.route("/api/admin/product/mappings/save", methods=["POST"])
@require_admin
def admin_save_product_mappings():
    """마스터 상품의 3채널 코드 매핑(쿠팡외 매출 상품관리코드, 개미창고 상품코드+옵션명, 쿠팡 옵션 ID)만 수동 저장"""
    body = request.get_json() or {}
    prod_id = str(body.get("id", "")).strip()
    seller_code_req = str(body.get("seller_code", "")).strip().upper()

    with data_lock:
        masters = app_state["master_products"]
        target = None
        for m in masters:
            m_id = str(m.get("id", "")).strip()
            m_code = str(m.get("seller_code", "")).strip().upper()
            if (prod_id and m_id == prod_id) or (seller_code_req and m_code == seller_code_req):
                target = m
                break
        if target is None:
            return jsonify({"success": False, "message": "대상 마스터 상품을 찾을 수 없습니다."}), 404

        if "sales_codes" in body:
            parsed_sc = _parse_sales_codes_input(body.get("sales_codes"))
            if parsed_sc is None:
                target.pop("sales_codes", None)
            else:
                target["sales_codes"] = parsed_sc

        if "gaemi_mappings" in body:
            clean_gm = _parse_gaemi_mappings_input(body.get("gaemi_mappings"))
            target["gaemi_mappings"] = clean_gm
            if len(clean_gm) > 0:
                target["gaemi_codes"] = list(dict.fromkeys([r["gaemi_code"] for r in clean_gm]))
                target["gaemi_filter"] = ""
            elif body.get("clear_legacy_gaemi"):
                target["gaemi_codes"] = []
                target["gaemi_filter"] = ""
        elif "gaemi_codes" in body or "gaemi_filter" in body:
            if "gaemi_codes" in body:
                raw_gc = body.get("gaemi_codes") or []
                if isinstance(raw_gc, str):
                    target["gaemi_codes"] = [c.strip() for c in raw_gc.split(",") if c.strip()]
                elif isinstance(raw_gc, list):
                    target["gaemi_codes"] = [str(c).strip() for c in raw_gc if str(c).strip()]
            if "gaemi_filter" in body:
                target["gaemi_filter"] = str(body.get("gaemi_filter", "")).strip()

        if "options" in body:
            target["options"] = _parse_coupang_options_input(body.get("options") or [])

        saved_copy = copy.deepcopy(target)
        t_code = target.get("seller_code", "")
        t_name = target.get("name", "")

    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log(f"🔗 [관리자 코드매핑] {t_code} ({t_name}) 채널별 코드 매핑이 저장되었습니다.")
    return jsonify({
        "success": True,
        "message": f"[{t_code}] {t_name} 코드 매핑 저장 완료!",
        "product": saved_copy,
        "drive_sync": sync_res
    })

@app.route("/api/admin/product/delete", methods=["POST"])
@require_admin
def admin_delete_product():
    """마스터 상품 숨김(비활성화) 토글 또는 완전 삭제"""
    body = request.get_json() or {}
    seller_code = str(body.get("seller_code", "")).strip().upper()
    prod_id = str(body.get("id", "")).strip()
    mode = str(body.get("mode", "hide")).strip() # 'hide' | 'restore' | 'hard'

    with data_lock:
        masters = app_state["master_products"]
        idx_found = -1
        for i, m in enumerate(masters):
            if (prod_id and m.get("id") == prod_id) or (seller_code and str(m.get("seller_code", "")).strip().upper() == seller_code):
                idx_found = i
                break
        if idx_found == -1:
            return jsonify({"success": False, "message": "대상 상품을 찾을 수 없습니다."}), 404

        target = masters[idx_found]
        t_name = target.get("name", seller_code)
        if mode == "hard":
            masters.pop(idx_found)
            msg = f"[{target.get('seller_code')}] {t_name} 영구 삭제 완료"
        elif mode == "restore":
            target["is_active"] = True
            msg = f"[{target.get('seller_code')}] {t_name} 대시보드 노출 복원 완료"
        else:
            target["is_active"] = False
            msg = f"[{target.get('seller_code')}] {t_name} 대시보드 숨김 처리 완료"

    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log(f"🗂️ [관리자] {msg}")
    return jsonify({"success": True, "message": msg, "drive_sync": sync_res})

@app.route("/api/admin/products/bulk-save", methods=["POST"])
@require_admin
def admin_bulk_save_products():
    """관리자 센터 일괄 편집(상품명/구분/리드타임/입고예정/메모/소비기한) 저장"""
    body = request.get_json() or {}
    items = body.get("products") or []
    lot_updates = body.get("lot_expiry_overrides") or {}

    updated_count = 0
    with data_lock:
        masters = app_state["master_products"]
        by_id = {str(m.get("id", "")): m for m in masters}
        by_code = {str(m.get("seller_code", "")).strip().upper(): m for m in masters}

        for row in items:
            m = by_id.get(str(row.get("id", ""))) or by_code.get(str(row.get("seller_code", "")).strip().upper())
            if not m:
                continue
            if "name" in row and str(row["name"]).strip():
                m["name"] = str(row["name"]).strip()
            if "product_type" in row and row["product_type"] in ("수입", "사입"):
                m["product_type"] = row["product_type"]
            if "category" in row and str(row["category"]).strip():
                m["category"] = str(row["category"]).strip()
            if "lead_time_days" in row:
                m["lead_time_days"] = _parse_optional_positive_int(row["lead_time_days"])
            if "safety_buffer_days" in row:
                m["safety_buffer_days"] = _parse_optional_positive_int(row["safety_buffer_days"])
            if "in_transit_qty" in row:
                m["in_transit_qty"] = max(0, _parse_optional_positive_int(row["in_transit_qty"]) or 0)
            if "expected_arrival_date" in row:
                m["expected_arrival_date"] = normalize_date_str(row["expected_arrival_date"])
            if "default_expiry_date" in row:
                m["default_expiry_date"] = normalize_date_str(row["default_expiry_date"])
            if "note" in row:
                m["note"] = str(row["note"] or "").strip()
            if "is_active" in row:
                m["is_active"] = bool(row["is_active"])
            updated_count += 1

        if isinstance(lot_updates, dict):
            overrides = app_state.setdefault("lot_expiry_overrides", {})
            for k, v in lot_updates.items():
                key = str(k).strip()
                val = str(v or "").strip()
                if not key:
                    continue
                if val == "__RESTORE__" or val == "":
                    overrides.pop(key, None)
                elif val == "__CLEAR__":
                    overrides[key] = "__CLEAR__"
                else:
                    nd = normalize_date_str(val)
                    if nd:
                        overrides[key] = nd

    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log(f"📊 [관리자 일괄 저장] {updated_count}개 품목 및 소비기한 기준이 저장되었습니다.")
    return jsonify({
        "success": True,
        "message": f"{updated_count}개 품목 일괄 저장 완료!",
        "drive_sync": sync_res
    })

@app.route("/api/admin/b2b-mappings/save", methods=["POST"])
@require_admin
def admin_save_b2b_mappings():
    """B2B/세트(S코드) 연동 매핑 저장"""
    body = request.get_json() or {}
    mappings_list = body.get("mappings")
    if not isinstance(mappings_list, list):
        return jsonify({"success": False, "message": "매핑 목록 형식이 올바르지 않습니다."}), 400

    new_map = {}
    for row in mappings_list:
        s_code = str(row.get("s_code", "")).strip().upper()
        base_code = str(row.get("base_code", "")).strip().upper()
        mult = max(1, _parse_optional_positive_int(row.get("multiplier")) or 1)
        desc = str(row.get("desc", "")).strip()
        if not s_code or not base_code:
            continue
        new_map[s_code] = {
            "base_code": base_code,
            "multiplier": mult,
            "desc": desc or f"{base_code} {mult}개 세트"
        }

    with data_lock:
        app_state["b2b_mappings"] = new_map

    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log(f"🔗 [관리자 B2B 매핑] {len(new_map)}개 세트(S코드) 연동 규칙이 업데이트되었습니다.")
    return jsonify({
        "success": True,
        "message": f"{len(new_map)}개 B2B/세트 매핑 저장 완료!",
        "b2b_mappings": new_map,
        "drive_sync": sync_res
    })

@app.route("/api/admin/settings/save", methods=["POST"])
@require_admin
def admin_save_settings():
    """전역 발주/소비기한 기본 기준 및 커스텀 PIN 저장"""
    body = request.get_json() or {}
    new_pin = str(body.get("new_pin", "")).strip()

    with data_lock:
        s = app_state.setdefault("settings", copy.deepcopy(DEFAULT_SETTINGS))
        for key, default_val in [
            ("default_import_lead_time", 110),
            ("default_import_buffer", 20),
            ("default_purchase_lead_time", 7),
            ("default_purchase_buffer", 7),
            ("default_expiry_warn_days", 120),
        ]:
            if key in body:
                v = _parse_optional_positive_int(body[key])
                if v is not None:
                    s[key] = max(1, v)

        pin_msg = ""
        if new_pin:
            if os.environ.get("ADMIN_PIN", "").strip():
                pin_msg = " (단, Render 환경변수 ADMIN_PIN이 설정되어 있어 환경변수 PIN이 우선 적용됩니다)"
            elif len(new_pin) < 4:
                return jsonify({"success": False, "message": "PIN 번호는 4자리 이상으로 설정해주세요."}), 400
            else:
                s["admin_pin_custom"] = new_pin
                pin_msg = " 및 관리자 PIN 변경 완료"

    new_token = ""
    if new_pin and not os.environ.get("ADMIN_PIN", "").strip():
        new_token, _ = issue_admin_token()

    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log(f"⚙️ [관리자 설정] 전역 발주/소비기한 기준{pin_msg} 저장 완료")
    return jsonify({
        "success": True,
        "message": f"전역 설정이 저장되었습니다.{pin_msg}",
        "new_token": new_token,
        "drive_sync": sync_res
    })

@app.route("/api/admin/drive/sync-now", methods=["POST"])
@require_admin
def admin_drive_sync_now():
    cfg = get_gas_config()
    if not cfg["configured"]:
        return jsonify({
            "success": False,
            "message": "Render 환경변수(GOOGLE_APPS_SCRIPT_URL, GOOGLE_APPS_SCRIPT_TOKEN)가 아직 등록되지 않았습니다.",
            "drive_sync": copy.deepcopy(drive_sync_state)
        }), 400
    res = sync_to_drive(wait=True)
    if res.get("status") == "synced":
        add_log("☁️ [Google Drive 수동 백업] 현재 전체 데이터가 Google Drive에 즉시 저장되었습니다.")
        return jsonify({"success": True, "message": "Google Drive 즉시 백업 완료!", "drive_sync": res})
    return jsonify({"success": False, "message": f"동기화 실패: {res.get('last_error')}", "drive_sync": res}), 500

@app.route("/api/admin/drive/pull-now", methods=["POST"])
@require_admin
def admin_drive_pull_now():
    cfg = get_gas_config()
    if not cfg["configured"]:
        return jsonify({
            "success": False,
            "message": "Render 환경변수(GOOGLE_APPS_SCRIPT_URL, GOOGLE_APPS_SCRIPT_TOKEN)가 아직 등록되지 않았습니다.",
            "drive_sync": copy.deepcopy(drive_sync_state)
        }), 400
    res = pull_from_drive()
    if res.get("success") and res.get("exists"):
        add_log("⬇️ [Google Drive 복원] Google Drive의 최신 데이터로 서버 상태를 복원했습니다.")
        return jsonify({"success": True, "message": "Google Drive 최신 데이터 불러오기 완료!", "drive_sync": copy.deepcopy(drive_sync_state)})
    elif res.get("success") and not res.get("exists"):
        return jsonify({"success": False, "message": "Google Drive에 아직 저장된 백업 파일이 없습니다. 먼저 [Drive에 즉시 저장]을 실행하세요.", "drive_sync": copy.deepcopy(drive_sync_state)}), 404
    return jsonify({"success": False, "message": f"불러오기 실패: {res.get('error')}", "drive_sync": copy.deepcopy(drive_sync_state)}), 500

@app.route("/api/admin/backup/export", methods=["GET"])
@require_admin
def admin_backup_export():
    snap = build_state_snapshot()
    # 보안상 custom PIN 해시/원문은 제외하고 싶다면 settings에서 제외할지 여부 -> 백업 복원 호환성을 위해 포함하거나 그대로 백업
    payload = json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"iljeomodo_backup_{now_kst().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )

@app.route("/api/admin/backup/import", methods=["POST"])
@require_admin
def admin_backup_import():
    body = request.get_json() or {}
    backup_data = body.get("data", body)
    if not isinstance(backup_data, dict) or "master_products" not in backup_data:
        return jsonify({"success": False, "message": "올바른 일쩜오도 백업 JSON 형식이 아닙니다. ('master_products' 키 누락)"}), 400

    _apply_loaded_state(backup_data)
    sync_res = save_state(sync_drive=True, wait_drive=True)
    add_log("📥 [관리자 백업 복원] JSON 백업 파일로부터 전체 데이터를 성공적으로 복원했습니다.")
    return jsonify({
        "success": True,
        "message": f"{len(app_state['master_products'])}개 마스터 상품 포함 전체 백업이 복원되었습니다.",
        "drive_sync": sync_res
    })

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

    <!-- 상단 헤더 (일쩜오도 BI 로고, 실시간 재고 업데이트 일시, 관리자 & 액션 버튼) -->
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
                    <div class="flex flex-wrap items-center gap-2 mt-0.5">
                        <p id="headerSubtitleText" class="text-[11px] text-slate-400">수입 리드타임 110일 발주 경보 · 창고 간 불균형 해소 · 소비기한 선입선출(FIFO) 관리</p>
                        <span class="text-slate-600 hidden sm:inline">|</span>
                        <span class="inline-flex items-center text-[11px] text-emerald-400 font-semibold bg-emerald-950/60 border border-emerald-500/30 px-2 py-0.5 rounded-md" title="데이터 파일(개미창고·쿠팡·매출) 최종 업로드 시간 (한국 시간 KST)">
                            <i class="fa-regular fa-clock mr-1 text-emerald-400"></i>
                            <span>재고 업데이트: </span>
                            <span id="headerLastUpdated" class="ml-1 font-mono text-white">확인 중...</span>
                        </span>
                        <span id="driveSyncBadge" class="inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-md border bg-slate-800 text-slate-300 border-slate-700" title="Google Drive 영구 저장소 연동 상태">
                            <i class="fa-solid fa-cloud mr-1"></i>
                            <span id="driveSyncBadgeText">로컬 저장 모드</span>
                        </span>
                    </div>
                </div>
            </div>
            
            <div class="flex flex-wrap items-center gap-1.5 sm:gap-2">
                <button onclick="toggleFullScreen()" id="fullscreenBtn" class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm" title="전체화면으로 시원하게 보기">
                    <i class="fa-solid fa-expand" id="fullscreenIcon"></i>
                    <span id="fullscreenText">전체화면</span>
                </button>
                <button onclick="openInNewTab()" class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm" title="새 탭에서 100% 꽉 차게 열기">
                    <i class="fa-solid fa-arrow-up-right-from-square"></i>
                    <span class="hidden sm:inline">새 탭으로 열기</span>
                </button>

                <!-- 관리자 잠금 상태 버튼 -->
                <button id="btnAdminUnlock" onclick="openPinModal()" class="px-3 py-1.5 bg-amber-500 hover:bg-amber-600 text-slate-950 rounded-lg text-xs font-extrabold transition flex items-center space-x-1.5 shadow-sm" title="PIN 인증 후 상품명, 유통기한, 리드타임, B2B 매핑을 수정합니다">
                    <i class="fa-solid fa-lock"></i>
                    <span>관리자 모드</span>
                </button>

                <!-- 관리자 인증 완료 시 표시되는 툴바 -->
                <div id="adminToolbarBtns" class="hidden flex items-center gap-1.5">
                    <button onclick="openCreateProductModal()" class="px-2.5 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-bold transition flex items-center space-x-1 shadow-sm">
                        <i class="fa-solid fa-plus"></i>
                        <span>단품 추가</span>
                    </button>
                    <button onclick="openAdminCenter('mapping')" class="px-2.5 py-1.5 bg-sky-500 hover:bg-sky-400 text-slate-950 rounded-lg text-xs font-extrabold transition flex items-center space-x-1 shadow-sm" title="쿠팡외 매출코드 · 개미 상품코드+옵션명 · 쿠팡 옵션ID 수동 매핑">
                        <i class="fa-solid fa-link"></i>
                        <span>코드 매핑</span>
                    </button>
                    <button onclick="openAdminCenter('bulk')" class="px-2.5 py-1.5 bg-amber-400 hover:bg-amber-300 text-slate-950 rounded-lg text-xs font-extrabold transition flex items-center space-x-1 shadow-sm">
                        <i class="fa-solid fa-sliders"></i>
                        <span>관리자 센터</span>
                    </button>
                    <button onclick="lockAdminMode()" class="px-2.5 py-1.5 bg-rose-600 hover:bg-rose-700 text-white rounded-lg text-xs font-bold transition flex items-center space-x-1 shadow-sm" title="관리자 편집 권한 잠금">
                        <i class="fa-solid fa-lock-open"></i>
                        <span>잠금</span>
                    </button>
                </div>

                <a href="/api/export-master-csv" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-xs font-bold transition flex items-center space-x-1.5 shadow-sm">
                    <i class="fa-solid fa-file-excel"></i>
                    <span>액션 리포트 다운로드</span>
                </a>
            </div>
        </div>
    </header>

    <!-- 관리자 모드 활성 배너 -->
    <div id="adminModeBanner" class="hidden bg-amber-50 border-b border-amber-300 text-amber-950 px-4 py-2 text-xs">
        <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center space-x-2">
                <span class="px-2 py-0.5 rounded bg-amber-500 text-slate-950 font-extrabold text-[10px]">관리자 편집 모드 ON</span>
                <span class="font-medium">각 행의 <strong>[🔗 매핑]</strong>·<strong>[✏️ 수정]</strong> 버튼으로 채널별 코드 매핑·상품명·소비기한·입고예정을 편집할 수 있습니다.</span>
            </div>
            <div class="flex items-center space-x-2">
                <button onclick="openAdminCenter('mapping')" class="px-2 py-1 bg-indigo-50 hover:bg-indigo-100 border border-indigo-300 rounded font-bold text-[11px] text-indigo-900 transition">
                    🔗 상품 코드 매핑
                </button>
                <button onclick="openAdminCenter('expiry')" class="px-2 py-1 bg-white hover:bg-amber-100 border border-amber-300 rounded font-bold text-[11px] text-amber-900 transition">
                    ⏳ 소비기한 일괄입력
                </button>
                <button onclick="openAdminCenter('drive')" class="px-2 py-1 bg-white hover:bg-amber-100 border border-amber-300 rounded font-bold text-[11px] text-amber-900 transition">
                    ☁️ Drive/백업 관리
                </button>
            </div>
        </div>
    </div>

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
            
            <!-- 🔴 긴급 발주 요망 (통합 D-Day < 리드타임) -->
            <div onclick="setStatusFilter('urgent')" class="bg-rose-50/70 rounded-xl shadow-sm border-2 border-rose-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-rose-100/80 transition relative overflow-hidden group">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-rose-900 flex items-center">
                        <i class="fa-solid fa-triangle-exclamation text-rose-600 mr-1.5 animate-bounce"></i>긴급 발주 요망
                    </span>
                    <span id="kpiBadgeUrgent" class="text-[9px] bg-rose-200 text-rose-900 px-1.5 py-0.5 rounded font-bold">110일 미만/결품</span>
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

            <!-- 🟡 발주 준비 (리드타임 ~ 리드타임+버퍼) -->
            <div onclick="setStatusFilter('prepare')" class="bg-amber-50/70 rounded-xl shadow-sm border-2 border-amber-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-amber-100/80 transition relative overflow-hidden group">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-amber-900 flex items-center">
                        <i class="fa-solid fa-clipboard-list text-amber-600 mr-1.5"></i>발주 준비
                    </span>
                    <span id="kpiBadgePrepare" class="text-[9px] bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded font-bold">110~130일</span>
                </div>
                <div class="flex items-baseline justify-between mt-2">
                    <div>
                        <span id="statOrderPrepare" class="text-3xl font-black text-amber-700">0</span>
                        <span class="text-xs text-amber-600 font-bold ml-1">개 품목</span>
                    </div>
                    <i class="fa-solid fa-cart-flatbed text-amber-400 text-2xl group-hover:scale-110 transition"></i>
                </div>
                <div class="mt-1 flex items-center justify-between text-[10px] text-amber-700 font-semibold border-t border-amber-200/60 pt-1">
                    <span>안전재고 구간 (기안 준비)</span>
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

            <!-- ⏳ 소비기한 주의 (기준일 미만 or 미소진 위험) -->
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
                    <span id="kpiSubExpiry">잔여 120일 미만 or 미소진 위험</span>
                    <span class="text-purple-900 group-hover:underline">클릭 시 필터</span>
                </div>
            </div>

            <!-- 🟢 정상 안전 -->
            <div onclick="setStatusFilter('safe')" class="bg-emerald-50/70 rounded-xl shadow-sm border-2 border-emerald-300 p-3.5 flex flex-col justify-between cursor-pointer hover:bg-emerald-100/80 transition relative overflow-hidden group col-span-2 sm:col-span-1">
                <div class="flex items-center justify-between">
                    <span class="text-[11px] font-bold text-emerald-900 flex items-center">
                        <i class="fa-solid fa-circle-check text-emerald-600 mr-1.5"></i>정상 안전
                    </span>
                    <span id="kpiBadgeSafe" class="text-[9px] bg-emerald-200 text-emerald-900 px-1.5 py-0.5 rounded font-bold">130일 이상</span>
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
                    <span class="font-bold text-slate-900">단품 품목:</span>
                    <span id="statTotalMasters" class="font-bold text-indigo-700">38개</span>
                    <span id="statMasterSubtext" class="text-slate-400 text-[11px]">(수입 11 / 사입 27)</span>
                </div>
                <span class="text-slate-300">|</span>
                <div class="flex items-center space-x-1.5">
                    <span class="font-bold text-slate-900">총 가용재고:</span>
                    <span id="statTotalStock" class="font-bold text-emerald-700 text-sm">0개</span>
                    <span class="text-slate-400 text-[11px]">(개미 <span id="statGaemiStock" class="font-semibold text-amber-700">0</span> + 쿠팡실물 <span id="statCoupangStock" class="font-semibold text-blue-700">0</span>)</span>
                </div>
                <span class="text-slate-300">|</span>
                <div class="flex items-center space-x-1.5">
                    <span class="font-bold text-slate-900">총 출고량:</span>
                    <span id="statTotalSales7d" class="font-bold text-indigo-700 text-sm">7일 0개</span>
                    <span class="text-slate-400 text-[11px]">/ 30일: <span id="statTotalSales30d" class="font-semibold text-slate-700">0</span>개</span>
                    <span id="statSalesSubtext" class="text-slate-400 text-[11px]">(쿠팡 0 + 외 0)</span>
                </div>
            </div>
            <div class="text-[11px] text-slate-500 flex items-center space-x-2">
                <span class="inline-flex items-center text-indigo-800 bg-indigo-50 border border-indigo-200 px-2 py-0.5 rounded font-mono text-[10px]">
                    <i class="fa-solid fa-scale-balanced mr-1 text-indigo-600"></i>7일(60%)+30일(40%) 가중 일평균 런웨이
                </span>
                <span class="inline-flex items-center text-slate-600 bg-slate-100 px-2 py-0.5 rounded font-mono border border-slate-200">
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
                        <h2 class="font-bold text-slate-900 text-sm">일쩜오도 단품 재고 & 발주 의사결정 보드</h2>
                        <span id="filteredCountText" class="text-xs text-slate-400 font-normal">| 총 38개 단품</span>
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
                        <button onclick="setCategoryFilter('all')" class="category-pill active px-2.5 py-1 rounded-md text-[11px] font-semibold bg-slate-800 text-white">전체 (<span id="catCountAll">38</span>)</button>
                        <button onclick="setCategoryFilter('수입')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-purple-100 text-purple-900 border border-purple-200 hover:bg-purple-200">🚢 수입제품 (<span id="catCountImport">11</span>)</button>
                        <button onclick="setCategoryFilter('사입')" class="category-pill px-2.5 py-1 rounded-md text-[11px] font-semibold bg-blue-100 text-blue-900 border border-blue-200 hover:bg-blue-200">📦 사입제품 (<span id="catCountPurchase">27</span>)</button>
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

    <!-- 카테고리 자동완성 -->
    <datalist id="categorySuggestions">
        <option value="오일/식용유"></option>
        <option value="파스타"></option>
        <option value="식초/애사비"></option>
        <option value="발사믹"></option>
        <option value="쿠키/스낵"></option>
        <option value="소스/드레싱"></option>
        <option value="잼/스프레드"></option>
        <option value="기타"></option>
    </datalist>

    <!-- 1. 관리자 PIN 인증 모달 -->
    <div id="pinModal" class="hidden fixed inset-0 z-[100] bg-slate-900/70 backdrop-blur-xs flex items-center justify-center p-4">
        <div class="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-sm overflow-hidden">
            <div class="bg-slate-900 text-white px-5 py-3.5 flex items-center justify-between">
                <div class="flex items-center space-x-2">
                    <i class="fa-solid fa-shield-halved text-amber-400"></i>
                    <h3 class="font-bold text-sm">관리자 모드 PIN 인증</h3>
                </div>
                <button onclick="closePinModal()" class="text-slate-400 hover:text-white text-sm">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>
            <form onsubmit="submitPinVerification(event)" class="p-5 space-y-4">
                <p class="text-xs text-slate-600 leading-relaxed">
                    대시보드 조회 및 CSV 업로드는 누구나 사용할 수 있습니다.<br>
                    <strong>상품 정보 · 소비기한 · 리드타임 · B2B 매핑</strong>을 수정하려면 관리자 PIN 번호를 입력하세요.
                </p>
                <div>
                    <label for="pinInput" class="block text-xs font-bold text-slate-700 mb-1.5">관리자 PIN 번호</label>
                    <input type="password" id="pinInput" inputmode="numeric" autocomplete="current-password" placeholder="PIN 입력 후 Enter" class="w-full px-3.5 py-2.5 border border-slate-300 rounded-xl text-sm font-mono tracking-widest focus:outline-none focus:ring-2 focus:ring-amber-500">
                </div>
                <div id="pinErrorMsg" class="hidden text-xs text-rose-600 font-semibold bg-rose-50 border border-rose-200 rounded-lg px-3 py-2"></div>
                <div class="flex items-center justify-end space-x-2 pt-1">
                    <button type="button" onclick="closePinModal()" class="px-3.5 py-2 rounded-lg text-xs font-bold bg-slate-100 hover:bg-slate-200 text-slate-700 transition">취소</button>
                    <button type="submit" id="pinSubmitBtn" class="px-4 py-2 rounded-lg text-xs font-extrabold bg-amber-500 hover:bg-amber-600 text-slate-950 transition shadow-sm flex items-center space-x-1.5">
                        <i class="fa-solid fa-unlock-keyhole"></i>
                        <span>잠금 해제</span>
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- 2. 단품 마스터 생성 / 수정 모달 -->
    <div id="editProductModal" class="hidden fixed inset-0 z-[100] bg-slate-900/75 backdrop-blur-xs flex items-center justify-center p-3 sm:p-4">
        <div class="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-3xl flex flex-col max-h-[92vh] overflow-hidden">
            <div class="bg-slate-900 text-white px-5 py-3.5 flex items-center justify-between shrink-0">
                <div class="flex items-center space-x-2">
                    <i class="fa-solid fa-pen-to-square text-amber-400"></i>
                    <h3 id="ep_modal_title" class="font-bold text-sm sm:text-base">단품 마스터 정보 수정</h3>
                </div>
                <button onclick="closeEditProductModal()" class="text-slate-400 hover:text-white text-base">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>

            <div class="p-4 sm:p-5 overflow-y-auto space-y-5 text-xs">
                <input type="hidden" id="ep_is_new" value="0">
                <input type="hidden" id="ep_id" value="">
                <input type="hidden" id="ep_orig_seller_code" value="">

                <!-- 섹션 1: 기본 마스터 정보 -->
                <div class="bg-slate-50 rounded-xl border border-slate-200 p-4 space-y-3">
                    <div class="flex items-center justify-between border-b border-slate-200 pb-2">
                        <span class="font-extrabold text-slate-800 text-xs flex items-center space-x-1.5">
                            <i class="fa-solid fa-tag text-indigo-600"></i>
                            <span>1. 기본 마스터 기준 (CSV 재업로드 시에도 고정 유지됨)</span>
                        </span>
                        <label class="inline-flex items-center space-x-1.5 cursor-pointer select-none">
                            <input type="checkbox" id="ep_is_active" checked class="rounded border-slate-300 text-emerald-600 focus:ring-emerald-500">
                            <span class="font-bold text-slate-700 text-[11px]">대시보드 노출</span>
                        </label>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        <div>
                            <label class="block font-bold text-slate-700 mb-1">판매자관리코드 <span class="text-rose-500">*</span></label>
                            <input type="text" id="ep_seller_code" placeholder="예: 20DC02" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        </div>
                        <div class="sm:col-span-2">
                            <label class="block font-bold text-slate-700 mb-1">마스터 상품명 <span class="text-rose-500">*</span></label>
                            <input type="text" id="ep_name" placeholder="예: 데체코 엑스트라버진 올리브오일 500ml" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        </div>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        <div>
                            <label class="block font-bold text-slate-700 mb-1">구분</label>
                            <select id="ep_product_type" class="w-full px-3 py-2 border border-slate-300 rounded-lg bg-white font-semibold focus:outline-none focus:ring-2 focus:ring-indigo-500">
                                <option value="수입">🚢 수입 (장기 리드타임)</option>
                                <option value="사입">📦 사입 (국내/단기)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block font-bold text-slate-700 mb-1">카테고리</label>
                            <input type="text" id="ep_category" list="categorySuggestions" placeholder="예: 오일/식용유" class="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        </div>
                        <div>
                            <label class="block font-bold text-slate-700 mb-1">참고사항 / 운영 메모</label>
                            <input type="text" id="ep_note" placeholder="발주·입고·선입선출 특이사항 메모" class="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        </div>
                    </div>
                </div>

                <!-- 섹션 2: 발주 기준 & 입고 예정 -->
                <div class="bg-blue-50/40 rounded-xl border border-blue-200 p-4 space-y-3">
                    <div class="font-extrabold text-blue-950 text-xs flex items-center space-x-1.5 border-b border-blue-200 pb-2">
                        <i class="fa-solid fa-ship text-blue-600"></i>
                        <span>2. 품목별 발주 기준 & 입고 예정 (비워두면 전역 기본값 자동 적용)</span>
                    </div>
                    <div class="grid grid-cols-2 sm:grid-cols-5 gap-2.5">
                        <div>
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">개별 리드타임(일)</label>
                            <input type="number" min="0" id="ep_lead_time_days" placeholder="기본값" class="w-full px-2.5 py-1.5 border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">
                        </div>
                        <div>
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">안전재고 버퍼(일)</label>
                            <input type="number" min="0" id="ep_safety_buffer_days" placeholder="기본값" class="w-full px-2.5 py-1.5 border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">
                        </div>
                        <div>
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">소비기한 주의(일)</label>
                            <input type="number" min="0" id="ep_expiry_warn_days" placeholder="기본값" class="w-full px-2.5 py-1.5 border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">
                        </div>
                        <div>
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">입고 예정 수량(개)</label>
                            <input type="number" min="0" id="ep_in_transit_qty" value="0" class="w-full px-2.5 py-1.5 border border-slate-300 rounded-lg font-mono font-bold text-blue-800 focus:outline-none focus:ring-2 focus:ring-blue-500">
                        </div>
                        <div class="col-span-2 sm:col-span-1">
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">입고 예정일</label>
                            <input type="date" id="ep_expected_arrival_date" class="w-full px-2 py-1.5 border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">
                        </div>
                    </div>
                </div>

                <!-- 섹션 3: 개미창고 소비기한 & 수동 다중 로트(FIFO) 관리 -->
                <div class="bg-amber-50/40 rounded-xl border border-amber-200 p-4 space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-amber-200 pb-2">
                        <span class="font-extrabold text-amber-950 text-xs flex items-center space-x-1.5">
                            <i class="fa-regular fa-calendar-check text-amber-600"></i>
                            <span>3. 상품별 소비기한 관리 & 선입선출(FIFO) 로트 분할</span>
                        </span>
                        <span id="ep_gaemi_summary_bar" class="text-[11px] font-bold text-amber-900 bg-amber-100 px-2 py-0.5 rounded"></span>
                    </div>

                    <div id="ep_lot_sync_note" class="hidden text-[11px] bg-indigo-50 text-indigo-900 border border-indigo-200 px-3 py-2 rounded-lg font-semibold"></div>

                    <!-- 3-A. CSV 개미창고 로트별 소비기한 오버라이드 -->
                    <div class="space-y-1.5">
                        <div class="text-[11px] font-bold text-slate-700">개미창고 CSV 로트별 소비기한 지정/수정 (새 CSV 업로드 후에도 유지됨)</div>
                        <div id="ep_csv_lots_container" class="space-y-1.5">
                            <!-- JS 동적 렌더링 -->
                        </div>
                    </div>

                    <!-- 3-B. 수동 다중 소비기한 로트 분할 (FIFO 자동 차감) -->
                    <div class="pt-2 border-t border-amber-200/70 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <div>
                                <div class="text-[11px] font-bold text-slate-800">수동 소비기한 로트 분할 (FIFO 선입선출 자동 차감)</div>
                                <div class="text-[10px] text-slate-500">예: 개미창고 CSV는 1줄(1,000개)인데 실제 소비기한이 2개로 나뉜 경우 직접 분할 등록하면, 이후 CSV 재업로드 시 감소 수량을 가장 빠른 기한부터 자동 차감합니다.</div>
                            </div>
                            <button type="button" onclick="addManualLotRow()" class="px-2.5 py-1 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-[11px] font-bold transition shrink-0">
                                + 로트 분할 추가
                            </button>
                        </div>
                        <div id="ep_manual_lots_container" class="space-y-1.5">
                            <!-- JS 동적 렌더링 -->
                        </div>
                        <div id="ep_manual_lots_sum_bar" class="hidden text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border"></div>
                    </div>

                    <!-- 3-C. 대표/기본 소비기한 -->
                    <div class="pt-2 border-t border-amber-200/70 flex flex-wrap items-center gap-3">
                        <div class="flex-1 min-w-[200px]">
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">기본 소비기한 (개미 CSV 기한이 미기재일 때 대체 사용)</label>
                            <input type="date" id="ep_default_expiry_date" class="px-3 py-1.5 border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-amber-500">
                            <button type="button" onclick="document.getElementById('ep_default_expiry_date').value=''" class="ml-1.5 px-2 py-1 bg-slate-100 hover:bg-slate-200 text-slate-600 rounded text-[10px] font-semibold">지우기</button>
                        </div>
                    </div>
                </div>

                <!-- 섹션 4: 창고 코드 & 채널별 코드 매핑 -->
                <div class="bg-slate-50 rounded-xl border border-slate-200 p-4 space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 pb-2">
                        <span class="font-extrabold text-slate-800 text-xs flex items-center space-x-1.5">
                            <i class="fa-solid fa-cubes text-slate-600"></i>
                            <span>4. 채널별 코드 매핑 (쿠팡외 매출코드 · 개미 상품코드+옵션명 · 쿠팡 옵션 ID)</span>
                        </span>
                        <button type="button" id="ep_btn_open_mapping" onclick="openCodeMappingFromEditModal()" class="px-2.5 py-1 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-[10px] font-bold transition shadow-xs">
                            🔗 3채널 코드 매핑 전용창 열기
                        </button>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-700 mb-1 text-[11px]">① 쿠팡외 매출정보 상품관리코드 (비워두면 판매자관리코드 자동 사용, 여러 개면 쉼표 구분)</label>
                        <input type="text" id="ep_sales_codes" placeholder="기본값: 판매자관리코드와 동일 (추가/변경 시 쉼표로 구분 입력)" class="w-full px-3 py-1.5 border border-slate-300 rounded-lg font-mono uppercase focus:outline-none focus:ring-2 focus:ring-indigo-500">
                    </div>
                    <div id="ep_gaemi_mappings_box" class="hidden bg-amber-50 border border-amber-200 rounded-lg p-2.5 space-y-1.5">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <span class="text-[11px] font-bold text-amber-900">② 개미창고 정확 매핑 (상품코드 + 옵션명) 사용 중</span>
                            <button type="button" onclick="clearEpGaemiMappingsOverride()" class="px-2 py-0.5 bg-white hover:bg-rose-50 border border-amber-300 text-rose-700 rounded text-[10px] font-bold">구방식(코드+키워드)으로 전환</button>
                        </div>
                        <div id="ep_gaemi_mappings_badges" class="flex flex-wrap gap-1"></div>
                    </div>
                    <div id="ep_gaemi_legacy_fields" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div>
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">② 개미창고 상품코드 (여러 개면 쉼표 구분)</label>
                            <input type="text" id="ep_gaemi_codes" placeholder="예: PJ1489503" class="w-full px-3 py-1.5 border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        </div>
                        <div>
                            <label class="block font-bold text-slate-700 mb-1 text-[11px]">개미 옵션 필터 키워드 (선택 · 전용창에서 정확 매핑 권장)</label>
                            <input type="text" id="ep_gaemi_filter" placeholder="특정 옵션명 포함 시만 매칭할 경우 입력" class="w-full px-3 py-1.5 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        </div>
                    </div>
                    <div class="space-y-1.5">
                        <div class="flex items-center justify-between">
                            <span class="font-bold text-slate-700 text-[11px]">③ 쿠팡 재고건전성 옵션 ID & 번들(개입수) 연결</span>
                            <button type="button" onclick="addCoupangOptionRow()" class="px-2 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-[10px] font-bold">
                                + 쿠팡 옵션 추가
                            </button>
                        </div>
                        <div id="ep_options_container" class="space-y-1.5">
                            <!-- JS 동적 렌더링 -->
                        </div>
                    </div>
                </div>

                <!-- 섹션 5: (접이식) 임시 재고 수동 보정 -->
                <details id="ep_temp_stocks_details" class="bg-rose-50/40 rounded-xl border border-rose-200 p-3">
                    <summary class="cursor-pointer font-bold text-rose-900 text-xs select-none flex items-center space-x-1.5">
                        <i class="fa-solid fa-wrench text-rose-600"></i>
                        <span>5. (선택) 긴급 실사 재고 수동 임시 보정 — 다음 CSV 업로드 전까지만 유지</span>
                    </summary>
                    <div class="mt-3 space-y-2">
                        <p class="text-[11px] text-rose-700">재고 수량은 원칙적으로 CSV 업로드를 기준으로 갱신됩니다. 값을 비워두면 수량은 변경되지 않습니다.</p>
                        <div id="ep_temp_stocks_container" class="space-y-2"></div>
                    </div>
                </details>
            </div>

            <div class="bg-slate-50 border-t border-slate-200 px-5 py-3 flex flex-wrap items-center justify-between gap-2 shrink-0">
                <div class="flex items-center space-x-2">
                    <button type="button" id="ep_btn_toggle_hide" onclick="toggleHideCurrentProduct()" class="px-3 py-1.5 rounded-lg text-xs font-bold border border-slate-300 bg-white hover:bg-slate-100 text-slate-700 transition">
                        대시보드 숨김
                    </button>
                    <button type="button" id="ep_btn_hard_delete" onclick="deleteCurrentProductHard()" class="px-3 py-1.5 rounded-lg text-xs font-bold border border-rose-300 bg-rose-50 hover:bg-rose-100 text-rose-700 transition">
                        영구 삭제
                    </button>
                </div>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="closeEditProductModal()" class="px-4 py-1.5 rounded-lg text-xs font-bold bg-slate-200 hover:bg-slate-300 text-slate-800 transition">취소</button>
                    <button type="button" onclick="saveProductModal()" class="px-5 py-1.5 rounded-lg text-xs font-extrabold bg-emerald-600 hover:bg-emerald-700 text-white transition shadow-sm flex items-center space-x-1.5">
                        <i class="fa-solid fa-floppy-disk"></i>
                        <span>저장 및 영구 반영</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- 3. 관리자 통합 관리 센터 모달 -->
    <div id="adminCenterModal" class="hidden fixed inset-0 z-[100] bg-slate-900/75 backdrop-blur-xs flex items-center justify-center p-2 sm:p-4">
        <div class="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-6xl flex flex-col max-h-[92vh] overflow-hidden">
            <div class="bg-slate-900 text-white px-5 py-3.5 flex items-center justify-between shrink-0">
                <div class="flex items-center space-x-2">
                    <i class="fa-solid fa-sliders text-amber-400"></i>
                    <h3 class="font-bold text-sm sm:text-base">일쩜오도 관리자 통합 센터 (Admin Control Center)</h3>
                </div>
                <button onclick="closeAdminCenter()" class="text-slate-400 hover:text-white text-base">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>

            <!-- 탭 네비게이션 -->
            <div class="bg-slate-100 border-b border-slate-200 px-4 pt-2 flex flex-wrap gap-1.5 shrink-0 text-xs">
                <button data-tab="bulk" onclick="switchAdminTab('bulk')" class="ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-white text-slate-900">📋 전체 품목 일괄 편집</button>
                <button data-tab="mapping" onclick="switchAdminTab('mapping')" class="ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-slate-200/60 text-slate-600 hover:bg-white">🔗 상품 코드 매핑</button>
                <button data-tab="expiry" onclick="switchAdminTab('expiry')" class="ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-slate-200/60 text-slate-600 hover:bg-white">⏳ 소비기한 일괄 입력</button>
                <button data-tab="b2b" onclick="switchAdminTab('b2b')" class="ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-slate-200/60 text-slate-600 hover:bg-white">📦 B2B/세트(S코드) 매핑</button>
                <button data-tab="settings" onclick="switchAdminTab('settings')" class="ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-slate-200/60 text-slate-600 hover:bg-white">⚙️ 전역 기준 & PIN 변경</button>
                <button data-tab="drive" onclick="switchAdminTab('drive')" class="ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-slate-200/60 text-slate-600 hover:bg-white">☁️ Google Drive 연동 · 백업</button>
            </div>

            <!-- 탭 컨텐츠 -->
            <div class="p-4 sm:p-5 overflow-y-auto flex-1 text-xs space-y-4">

                <!-- TAB 1: 전체 품목 일괄 편집 -->
                <div id="acTab_bulk" class="space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                        <div class="flex flex-wrap items-center gap-2">
                            <input type="text" id="acBulkSearch" oninput="renderAdminBulkTable()" placeholder="상품명 또는 관리코드 검색..." class="px-3 py-1.5 border border-slate-300 rounded-lg w-56 focus:outline-none focus:ring-1 focus:ring-indigo-500">
                            <label class="inline-flex items-center space-x-1.5 cursor-pointer bg-slate-100 px-2.5 py-1.5 rounded-lg border border-slate-200">
                                <input type="checkbox" id="acBulkShowInactive" onchange="renderAdminBulkTable()" checked>
                                <span class="font-semibold text-slate-700">숨김 품목 포함 보기</span>
                            </label>
                        </div>
                        <button onclick="saveAdminBulkProducts()" class="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-extrabold shadow-sm flex items-center space-x-1.5">
                            <i class="fa-solid fa-floppy-disk"></i>
                            <span>표 변경사항 일괄 저장</span>
                        </button>
                    </div>
                    <div class="overflow-x-auto border border-slate-200 rounded-xl">
                        <table class="w-full text-left border-collapse text-xs">
                            <thead class="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                                <tr>
                                    <th class="p-2 w-20">코드</th>
                                    <th class="p-2 min-w-[200px]">마스터 상품명</th>
                                    <th class="p-2 w-20">구분</th>
                                    <th class="p-2 w-28">카테고리</th>
                                    <th class="p-2 w-20">리드타임(일)</th>
                                    <th class="p-2 w-20">입고예정수</th>
                                    <th class="p-2 w-32">입고예정일</th>
                                    <th class="p-2 w-32">기본소비기한</th>
                                    <th class="p-2 w-14 text-center">노출</th>
                                    <th class="p-2 min-w-[140px]">메모</th>
                                </tr>
                            </thead>
                            <tbody id="acBulkTbody" class="divide-y divide-slate-100"></tbody>
                        </table>
                    </div>
                </div>

                <!-- TAB 2: 상품 3채널 코드 매핑 수동 지정 -->
                <div id="acTab_mapping" class="hidden space-y-4">
                    <div id="acMapToast" class="hidden px-3 py-2 rounded-lg text-xs font-bold bg-emerald-50 border border-emerald-300 text-emerald-900"></div>

                    <!-- 상단: 업로드된 CSV 미매핑/전체 코드 탐색기 -->
                    <details open id="acMapExplorerDetails" class="bg-sky-50/50 border border-sky-200 rounded-xl p-3 space-y-2.5">
                        <summary class="cursor-pointer font-extrabold text-sky-950 text-xs select-none flex flex-wrap items-center justify-between gap-2">
                            <span class="flex items-center space-x-1.5">
                                <i class="fa-solid fa-magnifying-glass-chart text-sky-600"></i>
                                <span>업로드된 CSV 미매핑 / 전체 코드 탐색기 (클릭하여 접기/펼치기)</span>
                            </span>
                            <span id="acMapCsvCounts" class="text-[11px] font-semibold text-sky-900"></span>
                        </summary>
                        <div class="pt-2 border-t border-sky-200/70 space-y-2.5">
                            <div class="flex flex-wrap items-center justify-between gap-2">
                                <div class="flex flex-wrap items-center gap-2">
                                    <select id="acMapCsvChannel" onchange="renderAdminMappingExplorer()" class="px-2.5 py-1.5 border border-sky-300 rounded-lg bg-white font-bold text-sky-950 text-[11px]">
                                        <option value="all">전체 (3개 채널 CSV)</option>
                                        <option value="sales">① 쿠팡외 매출정보 (상품관리코드)</option>
                                        <option value="gaemi">② 개미창고 (상품코드 + 옵션명)</option>
                                        <option value="coupang">③ 쿠팡 재고건전성 (쿠팡 옵션 ID)</option>
                                    </select>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer bg-white px-2.5 py-1.5 rounded-lg border border-sky-200 text-[11px]">
                                        <input type="checkbox" id="acMapCsvOnlyUnmapped" onchange="renderAdminMappingExplorer()" checked>
                                        <span class="font-bold text-slate-700">미매핑 코드만 보기</span>
                                    </label>
                                    <input type="text" id="acMapCsvSearch" oninput="renderAdminMappingExplorer()" placeholder="CSV 코드, 옵션명, 상품명 검색..." class="px-3 py-1.5 border border-slate-300 rounded-lg bg-white w-56 text-[11px]">
                                </div>
                                <span class="text-[10px] text-slate-500">* B2B S코드는 'B2B/세트' 탭 규칙으로 별도 판정됩니다.</span>
                            </div>
                            <div class="max-h-60 overflow-y-auto bg-white border border-sky-200 rounded-lg">
                                <table class="w-full text-left border-collapse text-[11px]">
                                    <thead class="bg-slate-50 text-slate-600 font-bold border-b border-slate-200 sticky top-0">
                                        <tr>
                                            <th class="p-2 w-24">채널</th>
                                            <th class="p-2 min-w-[190px]">CSV 코드 / 옵션명</th>
                                            <th class="p-2">CSV 품목 정보 · 수량/매출</th>
                                            <th class="p-2 w-28">연결 상태</th>
                                            <th class="p-2 min-w-[260px]">빠른 마스터 연결 / 신규등록</th>
                                        </tr>
                                    </thead>
                                    <tbody id="acMapCsvTbody" class="divide-y divide-slate-100"></tbody>
                                </table>
                            </div>
                        </div>
                    </details>

                    <!-- 하단: 마스터 단품별 3채널 코드 매핑 현황표 -->
                    <div class="space-y-2.5">
                        <div class="flex flex-wrap items-center justify-between gap-2 bg-indigo-50/70 border border-indigo-200 rounded-xl p-3">
                            <div class="flex flex-wrap items-center gap-2">
                                <input type="text" id="acMapMasterSearch" oninput="renderAdminMappingMasterTable()" placeholder="마스터 상품명 또는 코드 검색..." class="px-3 py-1.5 border border-slate-300 rounded-lg bg-white w-52">
                                <select id="acMapMasterFilter" onchange="renderAdminMappingMasterTable()" class="px-2.5 py-1.5 border border-indigo-300 rounded-lg bg-white font-bold text-indigo-950">
                                    <option value="all">전체 단품 보기</option>
                                    <option value="missing">매핑 누락/0건 확인 필요만</option>
                                    <option value="custom">수동 매핑 지정 완료만</option>
                                </select>
                                <label class="inline-flex items-center space-x-1.5 cursor-pointer bg-white px-2.5 py-1.5 rounded-lg border border-indigo-200">
                                    <input type="checkbox" id="acMapMasterShowInactive" onchange="renderAdminMappingMasterTable()">
                                    <span class="font-semibold text-slate-700">숨김 품목 포함</span>
                                </label>
                            </div>
                            <span class="text-[11px] text-indigo-900 font-semibold">상품명·소비기한 등 다른 정보는 그대로 두고 <strong>코드 매핑만 단독 수정</strong>할 수 있습니다.</span>
                        </div>
                        <div class="overflow-x-auto border border-slate-200 rounded-xl">
                            <table class="w-full text-left border-collapse text-xs">
                                <thead class="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                                    <tr>
                                        <th class="p-2.5 min-w-[180px]">마스터 상품</th>
                                        <th class="p-2.5 min-w-[170px]">① 쿠팡외 매출<br><span class="text-[10px] font-normal text-slate-400">상품관리코드</span></th>
                                        <th class="p-2.5 min-w-[230px]">② 개미창고 상품목록<br><span class="text-[10px] font-normal text-slate-400">상품코드 + 옵션명</span></th>
                                        <th class="p-2.5 min-w-[210px]">③ 쿠팡 재고건전성<br><span class="text-[10px] font-normal text-slate-400">쿠팡 옵션 ID</span></th>
                                        <th class="p-2.5 w-24 text-center">매핑 관리</th>
                                    </tr>
                                </thead>
                                <tbody id="acMapMasterTbody" class="divide-y divide-slate-100"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- TAB 2: 소비기한 일괄 입력 -->
                <div id="acTab_expiry" class="hidden space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 bg-purple-50 border border-purple-200 rounded-xl p-3">
                        <div class="flex flex-wrap items-center gap-2">
                            <select id="acExpiryFilterMode" onchange="renderAdminExpiryTable()" class="px-3 py-1.5 border border-purple-300 rounded-lg bg-white font-bold text-purple-950">
                                <option value="missing">소비기한 미등록 품목만</option>
                                <option value="soon">소비기한 180일 이내 임박만</option>
                                <option value="all">전체 단품 보기</option>
                            </select>
                            <input type="text" id="acExpirySearch" oninput="renderAdminExpiryTable()" placeholder="상품명 또는 코드 검색..." class="px-3 py-1.5 border border-slate-300 rounded-lg bg-white w-52">
                        </div>
                        <button onclick="saveAdminExpiryBulk()" class="px-4 py-1.5 bg-purple-600 hover:bg-purple-700 text-white rounded-lg font-extrabold shadow-sm flex items-center space-x-1.5">
                            <i class="fa-regular fa-calendar-check"></i>
                            <span>소비기한 일괄 저장</span>
                        </button>
                    </div>
                    <p class="text-[11px] text-slate-600">
                        * 개미 CSV 로트가 여러 개인 경우 각 로트별 소비기한을 바로 수정하거나, 여러 개 로트로 쪼개야 할 때는 우측 <strong>[상세 분할]</strong> 버튼을 눌러 로트 분할 모달을 여세요.
                    </p>
                    <div class="overflow-x-auto border border-slate-200 rounded-xl">
                        <table class="w-full text-left border-collapse text-xs">
                            <thead class="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                                <tr>
                                    <th class="p-2.5 w-20">코드</th>
                                    <th class="p-2.5 min-w-[200px]">상품명 / 개미 재고</th>
                                    <th class="p-2.5">로트 / 옵션 현황</th>
                                    <th class="p-2.5 w-44">소비기한 입력/수정</th>
                                    <th class="p-2.5 w-24 text-center">상세</th>
                                </tr>
                            </thead>
                            <tbody id="acExpiryTbody" class="divide-y divide-slate-100"></tbody>
                        </table>
                    </div>
                </div>

                <!-- TAB 3: B2B/세트(S코드) 매핑 -->
                <div id="acTab_b2b" class="hidden space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 bg-indigo-50 border border-indigo-200 rounded-xl p-3">
                        <div class="text-indigo-950 font-semibold">
                            S코드(B2B/세트 상품)는 메인 보드에서 숨겨지고, 해당 S코드 주문이 들어오면 아래 지정한 <strong>단품 관리코드</strong> 출고량에 배수만큼 자동 합산됩니다.
                        </div>
                        <div class="flex items-center space-x-2">
                            <button onclick="addAdminB2bRow()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg font-bold">+ S코드 매핑 추가</button>
                            <button onclick="saveAdminB2bMappings()" class="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-extrabold">매핑 저장</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto border border-slate-200 rounded-xl">
                        <table class="w-full text-left border-collapse text-xs">
                            <thead class="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                                <tr>
                                    <th class="p-2.5 w-32">세트/B2B 코드 (S코드)</th>
                                    <th class="p-2.5 w-36">연동 단품 코드</th>
                                    <th class="p-2.5 w-24">차감 배수(xN)</th>
                                    <th class="p-2.5">설명</th>
                                    <th class="p-2.5 w-16 text-center">삭제</th>
                                </tr>
                            </thead>
                            <tbody id="acB2bTbody" class="divide-y divide-slate-100"></tbody>
                        </table>
                    </div>
                </div>

                <!-- TAB 4: 전역 기준 & PIN 변경 -->
                <div id="acTab_settings" class="hidden space-y-4">
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div class="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
                            <h4 class="font-extrabold text-slate-800 text-sm border-b border-slate-200 pb-2">전역 발주 / 소비기한 판정 기준 (일)</h4>
                            <div class="grid grid-cols-2 gap-3">
                                <div>
                                    <label class="block font-bold text-slate-700 mb-1">수입 기본 리드타임(일)</label>
                                    <input type="number" min="1" id="acSetImportLt" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono font-bold">
                                </div>
                                <div>
                                    <label class="block font-bold text-slate-700 mb-1">수입 안전재고 버퍼(일)</label>
                                    <input type="number" min="0" id="acSetImportBuf" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono font-bold">
                                </div>
                                <div>
                                    <label class="block font-bold text-slate-700 mb-1">사입 기본 리드타임(일)</label>
                                    <input type="number" min="1" id="acSetPurchaseLt" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono font-bold">
                                </div>
                                <div>
                                    <label class="block font-bold text-slate-700 mb-1">사입 안전재고 버퍼(일)</label>
                                    <input type="number" min="0" id="acSetPurchaseBuf" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono font-bold">
                                </div>
                                <div class="col-span-2">
                                    <label class="block font-bold text-slate-700 mb-1">소비기한 임박 경고 기준(일)</label>
                                    <input type="number" min="1" id="acSetExpWarn" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono font-bold">
                                </div>
                            </div>
                        </div>

                        <div class="bg-amber-50/50 border border-amber-200 rounded-xl p-4 space-y-3">
                            <h4 class="font-extrabold text-amber-950 text-sm border-b border-amber-200 pb-2">관리자 PIN 보안 설정</h4>
                            <div id="acSetPinSourceInfo" class="text-[11px] bg-white border border-amber-200 rounded-lg p-2.5 text-slate-700"></div>
                            <div>
                                <label class="block font-bold text-slate-700 mb-1">새 관리자 PIN 변경 (선택, 4자리 이상)</label>
                                <input type="password" id="acSetNewPin" inputmode="numeric" placeholder="변경 시에만 입력하세요" class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono">
                                <p class="text-[10px] text-slate-500 mt-1">* Render 환경변수 <code>ADMIN_PIN</code>이 설정되어 있으면 환경변수 PIN이 최우선 적용됩니다.</p>
                            </div>
                        </div>
                    </div>
                    <div class="flex justify-end">
                        <button onclick="saveAdminSettings()" class="px-5 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-extrabold shadow-sm">
                            전역 기준 & PIN 저장
                        </button>
                    </div>
                </div>

                <!-- TAB 5: Google Drive 영구 연동 · JSON 백업 · 테스트 도구 -->
                <div id="acTab_drive" class="hidden space-y-4">
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <!-- Google Drive 실시간 연동 제어 -->
                        <div class="bg-emerald-50/50 border border-emerald-200 rounded-xl p-4 space-y-3">
                            <div class="flex items-center justify-between border-b border-emerald-200 pb-2">
                                <h4 class="font-extrabold text-emerald-950 text-sm flex items-center space-x-1.5">
                                    <i class="fa-brands fa-google-drive text-emerald-600"></i>
                                    <span>Google Drive 영구 저장소 상태</span>
                                </h4>
                            </div>
                            <div id="acDriveStatusBox" class="text-xs bg-white border border-emerald-200 rounded-lg p-3 space-y-1"></div>
                            <div class="flex flex-wrap gap-2">
                                <button onclick="syncDriveNow()" class="px-3.5 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-bold shadow-sm">
                                    ☁️ Drive에 즉시 저장
                                </button>
                                <button onclick="pullDriveNow()" class="px-3.5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-bold shadow-sm">
                                    ⬇️ Drive에서 즉시 복원
                                </button>
                            </div>
                        </div>

                        <!-- 수동 JSON 백업/복원 & 테스트 샘플 로드 -->
                        <div class="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
                            <h4 class="font-extrabold text-slate-800 text-sm border-b border-slate-200 pb-2">로컬 JSON 백업 파일 & 시스템 도구</h4>
                            <div class="flex flex-wrap gap-2">
                                <button onclick="downloadBackupJson()" class="px-3.5 py-2 bg-slate-800 hover:bg-slate-900 text-white rounded-lg font-bold">
                                    📥 전체 데이터 JSON 다운로드
                                </button>
                                <button onclick="document.getElementById('acBackupFileInput').click()" class="px-3.5 py-2 bg-white hover:bg-slate-100 border border-slate-300 text-slate-800 rounded-lg font-bold">
                                    📤 JSON 백업 파일 복원
                                </button>
                                <input type="file" id="acBackupFileInput" accept=".json" class="hidden" onchange="importBackupJsonFile(this)">
                            </div>
                            <div class="pt-3 border-t border-slate-200 flex items-center justify-between gap-2">
                                <div class="text-[11px] text-slate-500">
                                    <strong class="text-indigo-700">[테스트용] 샘플 CSV 로드</strong>: 실데이터가 샘플 CSV로 덮어씌워집니다.
                                </div>
                                <button onclick="loadSampleData()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg font-bold shrink-0">
                                    ✨ 샘플 데이터 즉시 로드
                                </button>
                            </div>
                        </div>
                    </div>

                    <!-- Google Apps Script 설치 안내 & 원클릭 복사 -->
                    <div class="bg-slate-900 text-slate-100 rounded-xl p-4 space-y-2.5">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <h4 class="font-bold text-xs sm:text-sm text-emerald-300">Google Workspace 드라이브 24시간 영구 연동 스크립트 (1회 설치)</h4>
                            <button onclick="copyGasScript()" class="px-3 py-1 bg-emerald-500 hover:bg-emerald-600 text-slate-950 rounded-lg text-xs font-extrabold">
                                📋 Apps Script (.gs) 코드 복사
                            </button>
                        </div>
                        <ol class="list-decimal list-inside text-[11px] text-slate-300 space-y-1 leading-relaxed">
                            <li>Google Drive(<code>script.google.com</code>)에서 <strong>새 프로젝트</strong> 생성 후 아래 코드를 붙여넣고 <code>API_TOKEN</code>을 임의의 비밀번호로 바꿉니다.</li>
                            <li><strong>배포 → 새 배포 → 웹 앱</strong> 선택 (실행 주체: <strong>나(Me)</strong> / 액세스 권한: <strong>모든 사용자(Anyone)</strong>) 후 생성된 <strong>웹 앱 URL(/exec)</strong>을 복사합니다.</li>
                            <li>Render 대시보드 → <strong>Environment</strong>에 <code>GOOGLE_APPS_SCRIPT_URL</code>, <code>GOOGLE_APPS_SCRIPT_TOKEN</code>, <code>ADMIN_PIN</code>을 저장하면 서버가 재시작되어도 관리자 수정사항이 영구 보존됩니다.</li>
                        </ol>
                        <pre id="acGasCodePreview" class="bg-slate-950 text-emerald-300 p-3 rounded-lg text-[10px] font-mono max-h-48 overflow-auto border border-slate-800"></pre>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- 4. 상품별 3채널 코드 매핑 수동 지정 전용 모달 (z-[110]) -->
    <div id="codeMappingModal" class="hidden fixed inset-0 z-[110] bg-slate-900/80 backdrop-blur-xs flex items-center justify-center p-2 sm:p-4">
        <div class="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-5xl flex flex-col max-h-[92vh] overflow-hidden">
            <div class="bg-slate-900 text-white px-5 py-3.5 flex items-center justify-between shrink-0">
                <div class="flex items-center space-x-2">
                    <i class="fa-solid fa-link text-sky-400"></i>
                    <h3 id="cm_modal_title" class="font-bold text-sm sm:text-base">3채널 코드 매핑 수동 지정</h3>
                </div>
                <button onclick="closeCodeMappingModal()" class="text-slate-400 hover:text-white text-base">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>

            <div class="p-4 sm:p-5 overflow-y-auto flex-1 text-xs space-y-5">
                <div class="bg-sky-50 border border-sky-200 rounded-xl px-3.5 py-2.5 text-[11px] text-sky-950 font-semibold flex flex-wrap items-center justify-between gap-2">
                    <span>💡 상품명·소비기한·리드타임 등은 변경하지 않고 <strong>① 쿠팡외 매출(상품관리코드) · ② 개미창고(상품코드+옵션명) · ③ 쿠팡(쿠팡 옵션 ID)</strong> 코드 매핑만 저장합니다.</span>
                    <span id="cm_target_badge" class="font-mono bg-white border border-sky-300 px-2 py-0.5 rounded text-sky-900"></span>
                </div>

                <!-- ① 쿠팡외 매출정보: 상품관리코드 -->
                <div class="bg-indigo-50/40 border border-indigo-200 rounded-xl p-4 space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-indigo-200 pb-2">
                        <div>
                            <div class="font-extrabold text-indigo-950 text-xs flex items-center space-x-1.5">
                                <span class="px-1.5 py-0.5 rounded bg-indigo-600 text-white text-[10px]">①</span>
                                <span>쿠팡외 매출정보 — 상품관리코드 매핑</span>
                            </div>
                            <div id="cm_sales_status_text" class="text-[11px] text-indigo-800 mt-0.5"></div>
                        </div>
                        <div class="flex items-center space-x-1.5">
                            <button type="button" onclick="resetCmSalesDefault()" class="px-2.5 py-1 bg-white hover:bg-indigo-100 border border-indigo-300 text-indigo-900 rounded-lg text-[11px] font-bold">대표코드로 기본 복원</button>
                            <button type="button" onclick="clearCmSalesAll()" class="px-2.5 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 rounded-lg text-[11px] font-bold">모두 비우기</button>
                        </div>
                    </div>

                    <div id="cm_sales_chips" class="flex flex-wrap gap-1.5 min-h-[28px] items-center"></div>

                    <div class="flex flex-wrap items-center gap-2">
                        <input type="text" id="cm_sales_add_input" onkeydown="if(event.key==='Enter'){event.preventDefault();addCmSalesManual();}" placeholder="직접 입력 (예: 20DC02 또는 스마트스토어 상품관리코드, 쉼표 구분 가능)" class="flex-1 min-w-[220px] px-3 py-1.5 border border-slate-300 rounded-lg bg-white font-mono uppercase focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        <button type="button" onclick="addCmSalesManual()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg font-bold">+ 직접 추가</button>
                    </div>

                    <div class="bg-white border border-indigo-200 rounded-lg p-2.5 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <span class="font-bold text-slate-700 text-[11px]">업로드된 쿠팡외 매출 CSV에서 선택</span>
                            <input type="text" id="cm_sales_csv_search" oninput="renderCmSalesCsvList()" placeholder="매출 코드, 상품명, 쇼핑몰 검색..." class="px-2.5 py-1 border border-slate-300 rounded-lg text-[11px] w-56">
                        </div>
                        <div id="cm_sales_csv_list" class="max-h-44 overflow-y-auto divide-y divide-slate-100"></div>
                    </div>
                </div>

                <!-- ② 개미창고 상품목록: 상품코드 + 옵션명 -->
                <div class="bg-amber-50/40 border border-amber-200 rounded-xl p-4 space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-amber-200 pb-2">
                        <div>
                            <div class="font-extrabold text-amber-950 text-xs flex items-center space-x-1.5">
                                <span class="px-1.5 py-0.5 rounded bg-amber-600 text-white text-[10px]">②</span>
                                <span>개미창고 상품목록 — 상품코드 + 옵션명 매핑</span>
                            </div>
                            <div class="text-[11px] text-amber-800 mt-0.5">같은 상품코드 안에 여러 옵션명이 섞여 있어도 <strong>상품코드 + 옵션명 정확 일치</strong>로 분리 매핑됩니다. (동일 옵션명의 다중 등록일 로트 자동 합산)</div>
                        </div>
                        <button type="button" onclick="clearCmGaemiAll()" class="px-2.5 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 rounded-lg text-[11px] font-bold">매핑 모두 비우기</button>
                    </div>

                    <div id="cm_gaemi_legacy_banner" class="hidden rounded-lg border px-3 py-2 text-[11px] space-y-1.5"></div>

                    <div id="cm_gaemi_chips" class="flex flex-wrap gap-1.5 min-h-[28px] items-center"></div>

                    <div class="grid grid-cols-1 sm:grid-cols-12 gap-2 items-center">
                        <input type="text" id="cm_gaemi_code_input" placeholder="개미 상품코드 (예: PJ1491144)" class="sm:col-span-4 px-3 py-1.5 border border-slate-300 rounded-lg bg-white font-mono focus:outline-none focus:ring-2 focus:ring-amber-500">
                        <input type="text" id="cm_gaemi_opt_input" onkeydown="if(event.key==='Enter'){event.preventDefault();addCmGaemiManual();}" placeholder="옵션명 (예: 정제 1L · 빈칸 또는 * 입력 시 해당 코드 전체 옵션)" class="sm:col-span-6 px-3 py-1.5 border border-slate-300 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-amber-500">
                        <button type="button" onclick="addCmGaemiManual()" class="sm:col-span-2 px-3 py-1.5 bg-amber-600 hover:bg-amber-700 text-white rounded-lg font-bold">+ 직접 추가</button>
                    </div>

                    <div class="bg-white border border-amber-200 rounded-lg p-2.5 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <span class="font-bold text-slate-700 text-[11px]">업로드된 개미창고 CSV (상품코드 + 옵션명 그룹)에서 선택</span>
                            <input type="text" id="cm_gaemi_csv_search" oninput="renderCmGaemiCsvList()" placeholder="개미 상품코드, 옵션명, 상품명 검색..." class="px-2.5 py-1 border border-slate-300 rounded-lg text-[11px] w-56">
                        </div>
                        <div id="cm_gaemi_csv_list" class="max-h-52 overflow-y-auto divide-y divide-slate-100"></div>
                    </div>
                </div>

                <!-- ③ 쿠팡 재고건전성: 쿠팡 옵션 ID -->
                <div class="bg-blue-50/40 border border-blue-200 rounded-xl p-4 space-y-3">
                    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-blue-200 pb-2">
                        <div>
                            <div class="font-extrabold text-blue-950 text-xs flex items-center space-x-1.5">
                                <span class="px-1.5 py-0.5 rounded bg-blue-600 text-white text-[10px]">③</span>
                                <span>쿠팡 재고건전성 — 쿠팡 옵션 ID & 번들(개입수) 매핑</span>
                            </div>
                            <div class="text-[11px] text-blue-800 mt-0.5">연결된 각 옵션 ID의 세트 재고·7일 출고량에 개입(배수)을 곱해 단품 실물 재고로 환산합니다.</div>
                        </div>
                        <button type="button" onclick="clearCmCoupangAll()" class="px-2.5 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 rounded-lg text-[11px] font-bold">모두 비우기</button>
                    </div>

                    <div id="cm_coupang_rows" class="space-y-1.5"></div>

                    <div class="grid grid-cols-1 sm:grid-cols-12 gap-2 items-center">
                        <input type="text" id="cm_coupang_id_input" placeholder="쿠팡 옵션 ID (예: 95844803290)" class="sm:col-span-4 px-3 py-1.5 border border-slate-300 rounded-lg bg-white font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <div class="sm:col-span-2 flex items-center space-x-1">
                            <input type="number" min="1" id="cm_coupang_bundle_input" value="1" class="w-full px-2.5 py-1.5 border border-slate-300 rounded-lg bg-white font-mono text-right focus:outline-none focus:ring-2 focus:ring-blue-500">
                            <span class="text-[11px] text-slate-600 shrink-0">개입</span>
                        </div>
                        <input type="text" id="cm_coupang_name_input" onkeydown="if(event.key==='Enter'){event.preventDefault();addCmCoupangManual();}" placeholder="옵션명 (예: 2개 500ml)" class="sm:col-span-4 px-3 py-1.5 border border-slate-300 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <button type="button" onclick="addCmCoupangManual()" class="sm:col-span-2 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-bold">+ 직접 추가</button>
                    </div>

                    <div class="bg-white border border-blue-200 rounded-lg p-2.5 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <span class="font-bold text-slate-700 text-[11px]">업로드된 쿠팡 재고건전성 CSV에서 선택 (개입수 자동 추정)</span>
                            <input type="text" id="cm_coupang_csv_search" oninput="renderCmCoupangCsvList()" placeholder="옵션 ID, 옵션명, 쿠팡 상품명 검색..." class="px-2.5 py-1 border border-slate-300 rounded-lg text-[11px] w-56">
                        </div>
                        <div id="cm_coupang_csv_list" class="max-h-48 overflow-y-auto divide-y divide-slate-100"></div>
                    </div>
                </div>
            </div>

            <div class="bg-slate-50 border-t border-slate-200 px-5 py-3 flex items-center justify-end space-x-2 shrink-0">
                <button type="button" onclick="closeCodeMappingModal()" class="px-4 py-1.5 rounded-lg text-xs font-bold bg-slate-200 hover:bg-slate-300 text-slate-800 transition">취소</button>
                <button type="button" onclick="saveCodeMappingModal()" class="px-5 py-1.5 rounded-lg text-xs font-extrabold bg-indigo-600 hover:bg-indigo-700 text-white transition shadow-sm flex items-center space-x-1.5">
                    <i class="fa-solid fa-link"></i>
                    <span>코드 매핑 저장 및 반영</span>
                </button>
            </div>
        </div>
    </div>

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
            'total_sales': { id: 'total_sales', label: '출고량 (7일/30일)', thClass: 'text-right w-28 font-black bg-indigo-50/60 text-indigo-950', tdClass: 'text-right font-mono font-bold text-indigo-900 bg-indigo-50/20' },
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

        const ADMIN_TOKEN_KEY = 'iljeomodo_admin_token';
        let allMasterData = [];
        let publicTunnelUrl = null;
        let expandAllOptions = false;
        let expandedRowIds = new Set();
        let activeCategory = 'all';
        let activeStatus = 'all';
        let adminToken = '';
        try { adminToken = sessionStorage.getItem(ADMIN_TOKEN_KEY) || ''; } catch (e) {}
        let isAdminMode = false;
        let adminConfigCache = null;
        let globalSettings = {
            default_import_lead_time: 110,
            default_import_buffer: 20,
            default_purchase_lead_time: 7,
            default_purchase_buffer: 7,
            default_expiry_warn_days: 120
        };

        function isAnyModalOpen() {
            const m1 = document.getElementById('pinModal');
            const m2 = document.getElementById('editProductModal');
            const m3 = document.getElementById('adminCenterModal');
            const m4 = document.getElementById('codeMappingModal');
            return (m1 && !m1.classList.contains('hidden')) ||
                   (m2 && !m2.classList.contains('hidden')) ||
                   (m3 && !m3.classList.contains('hidden')) ||
                   (m4 && !m4.classList.contains('hidden'));
        }

        function updateAdminUI() {
            const btnUnlock = document.getElementById('btnAdminUnlock');
            const toolbar = document.getElementById('adminToolbarBtns');
            const banner = document.getElementById('adminModeBanner');
            if (isAdminMode) {
                if (btnUnlock) btnUnlock.classList.add('hidden');
                if (toolbar) toolbar.classList.remove('hidden');
                if (banner) banner.classList.remove('hidden');
            } else {
                if (btnUnlock) btnUnlock.classList.remove('hidden');
                if (toolbar) toolbar.classList.add('hidden');
                if (banner) banner.classList.add('hidden');
            }
            if (allMasterData.length > 0) renderMasterTable();
        }

        async function checkAdminSession() {
            if (!adminToken) {
                isAdminMode = false;
                updateAdminUI();
                return false;
            }
            try {
                const res = await fetch('/api/admin/session', {
                    headers: { 'X-Admin-Token': adminToken }
                });
                const json = await res.json();
                if (json.authenticated) {
                    isAdminMode = true;
                } else {
                    isAdminMode = false;
                    adminToken = '';
                    try { sessionStorage.removeItem(ADMIN_TOKEN_KEY); } catch (e) {}
                }
            } catch (e) {
                isAdminMode = false;
            }
            updateAdminUI();
            return isAdminMode;
        }

        function openPinModal() {
            const modal = document.getElementById('pinModal');
            const input = document.getElementById('pinInput');
            const err = document.getElementById('pinErrorMsg');
            err.classList.add('hidden');
            err.innerText = '';
            input.value = '';
            modal.classList.remove('hidden');
            setTimeout(() => input.focus(), 50);
        }

        function closePinModal() {
            document.getElementById('pinModal').classList.add('hidden');
        }

        async function submitPinVerification(e) {
            if (e) e.preventDefault();
            const pin = (document.getElementById('pinInput').value || '').trim();
            const errEl = document.getElementById('pinErrorMsg');
            if (!pin) {
                errEl.innerText = 'PIN 번호를 입력해주세요.';
                errEl.classList.remove('hidden');
                return;
            }
            try {
                const res = await fetch('/api/admin/verify-pin', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pin })
                });
                const json = await res.json();
                if (res.ok && json.success) {
                    adminToken = json.token;
                    try { sessionStorage.setItem(ADMIN_TOKEN_KEY, adminToken); } catch (ex) {}
                    isAdminMode = true;
                    closePinModal();
                    updateAdminUI();
                    fetchSummary();
                } else {
                    errEl.innerText = json.message || 'PIN 인증 실패';
                    errEl.classList.remove('hidden');
                    document.getElementById('pinInput').select();
                }
            } catch (err) {
                errEl.innerText = '네트워크 오류: ' + err.message;
                errEl.classList.remove('hidden');
            }
        }

        async function lockAdminMode() {
            try {
                await fetch('/api/admin/logout', { method: 'POST' });
            } catch (e) {}
            adminToken = '';
            isAdminMode = false;
            adminConfigCache = null;
            try { sessionStorage.removeItem(ADMIN_TOKEN_KEY); } catch (e) {}
            closeCodeMappingModal();
            closeEditProductModal();
            closeAdminCenter();
            updateAdminUI();
            fetchSummary();
        }

        async function adminFetch(url, options = {}) {
            const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
            if (adminToken) headers['X-Admin-Token'] = adminToken;
            const res = await fetch(url, Object.assign({}, options, { headers }));
            if (res.status === 401) {
                isAdminMode = false;
                adminToken = '';
                try { sessionStorage.removeItem(ADMIN_TOKEN_KEY); } catch (e) {}
                updateAdminUI();
                alert('관리자 세션이 만료되었거나 인증이 필요합니다. 다시 PIN을 입력해주세요.');
                openPinModal();
                throw new Error('ADMIN_AUTH_REQUIRED');
            }
            return res;
        }

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
            if (!isAdminMode) {
                openPinModal();
                return;
            }
            if (!confirm("현재 재고 수량을 테스트용 샘플 CSV 데이터로 덮어쓰시겠습니까? (관리자 마스터 설정은 유지됩니다)")) return;
            try {
                const res = await adminFetch('/api/load-sample', { method: 'POST' });
                const json = await res.json();
                if (json.success) {
                    alert("✨ 개미창고(소비기한 포함), 쿠팡윙, 타채널 매출 샘플 데이터가 성공적으로 로드되었습니다!");
                    fetchSummary();
                } else {
                    alert("로드 실패: " + json.message);
                }
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert("샘플 로드 오류: " + err.message);
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
            if (!expandAllOptions) expandedRowIds.clear();
            document.getElementById('expandBtnText').innerText = expandAllOptions ? "소비기한 접기" : "소비기한 세부내역";
            renderMasterTable();
        }

        function toggleSingleOption(prodId) {
            const row = document.getElementById(`subrow_${prodId}`);
            if (!row) return;
            const willOpen = row.classList.contains('hidden');
            row.classList.toggle('hidden');
            if (willOpen) expandedRowIds.add(prodId);
            else expandedRowIds.delete(prodId);
        }

        function setCategoryFilter(cat) {
            activeCategory = cat;
            document.querySelectorAll('#categoryPills .category-pill').forEach(btn => {
                btn.classList.remove('bg-slate-800', 'text-white');
                btn.classList.add('bg-slate-100', 'text-slate-600');
            });
            const btn = (typeof event !== 'undefined' && event && event.target)
                ? event.target.closest('.category-pill')
                : null;
            if (btn) {
                btn.classList.remove('bg-slate-100', 'text-slate-600');
                btn.classList.add('bg-slate-800', 'text-white');
            }
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

        function updateDriveSyncBadge(ds) {
            const badge = document.getElementById('driveSyncBadge');
            const text = document.getElementById('driveSyncBadgeText');
            if (!badge || !text) return;
            const info = ds || {};
            if (!info.configured) {
                badge.className = "inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-md border bg-slate-800 text-slate-300 border-slate-700";
                text.innerText = "로컬 저장 모드 (Drive 미연동)";
            } else if (info.status === 'synced') {
                badge.className = "inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-md border bg-emerald-950/70 text-emerald-300 border-emerald-500/40";
                text.innerText = "Google Drive 동기화 완료";
            } else if (info.status === 'syncing') {
                badge.className = "inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-md border bg-amber-950/70 text-amber-300 border-amber-500/40";
                text.innerText = "Google Drive 동기화 중...";
            } else if (info.status === 'error') {
                badge.className = "inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-md border bg-rose-950/70 text-rose-300 border-rose-500/40";
                text.innerText = "Drive 동기화 오류";
                badge.title = info.last_error || "Google Drive 연결 오류";
            } else {
                badge.className = "inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-md border bg-blue-950/70 text-blue-300 border-blue-500/40";
                text.innerText = "Google Drive 연결됨";
            }
        }

        // 데이터 주기적 갱신
        async function fetchSummary() {
            try {
                const res = await fetch('/api/summary');
                const data = await res.json();
                allMasterData = data.masters || [];
                if (data.settings) {
                    globalSettings = data.settings;
                    const impLt = globalSettings.default_import_lead_time || 110;
                    const impBuf = globalSettings.default_import_buffer || 20;
                    const expWarn = globalSettings.default_expiry_warn_days || 120;
                    const prepLim = impLt + impBuf;
                    const elSub = document.getElementById('headerSubtitleText');
                    if (elSub) elSub.innerText = `수입 리드타임 ${impLt}일 발주 경보 · 창고 간 불균형 해소 · 소비기한 선입선출(FIFO) 관리`;
                    const elU = document.getElementById('kpiBadgeUrgent');
                    if (elU) elU.innerText = `${impLt}일 미만/결품`;
                    const elP = document.getElementById('kpiBadgePrepare');
                    if (elP) elP.innerText = `${impLt}~${prepLim}일`;
                    const elE = document.getElementById('kpiSubExpiry');
                    if (elE) elE.innerText = `잔여 ${expWarn}일 미만 or 미소진 위험`;
                    const elS = document.getElementById('kpiBadgeSafe');
                    if (elS) elS.innerText = `${prepLim}일 이상`;
                }
                updateDriveSyncBadge(data.drive_sync);

                // 1. 헤더 재고 업데이트 일시 표기 (데이터 파일 업로드 시간 · KST)
                const headerLastUpdatedEl = document.getElementById('headerLastUpdated');
                if (headerLastUpdatedEl) {
                    headerLastUpdatedEl.innerText = data.stats.last_updated
                        ? `${data.stats.last_updated} (KST)`
                        : '파일 업로드 대기 중';
                }

                // 2. 5대 Action KPI 통계 갱신
                document.getElementById('statUrgentOrders').innerText = (data.stats.urgent_order_count || 0);
                document.getElementById('statOrderPrepare').innerText = (data.stats.order_prepare_count || 0);
                document.getElementById('statCoupangTransfer').innerText = (data.stats.coupang_transfer_count || 0);
                document.getElementById('statExpiryWarning').innerText = (data.stats.expiry_warning_count || 0);
                document.getElementById('statSafeStock').innerText = (data.stats.safe_count || 0);

                // 3. 재고 요약 바 및 카테고리별 단품 수 갱신
                const totalMasters = data.stats.total_masters || allMasterData.length;
                const importCount = allMasterData.filter(m => m.product_type === '수입').length;
                const purchaseCount = allMasterData.filter(m => m.product_type === '사입').length;

                document.getElementById('statTotalMasters').innerText = totalMasters + '개';
                if (document.getElementById('statMasterSubtext')) {
                    const inactiveTxt = data.stats.inactive_count > 0 ? ` / 숨김 ${data.stats.inactive_count}` : '';
                    document.getElementById('statMasterSubtext').innerText = `(수입 ${importCount} / 사입 ${purchaseCount}${inactiveTxt})`;
                }
                if (document.getElementById('catCountAll')) document.getElementById('catCountAll').innerText = totalMasters;
                if (document.getElementById('catCountImport')) document.getElementById('catCountImport').innerText = importCount;
                if (document.getElementById('catCountPurchase')) document.getElementById('catCountPurchase').innerText = purchaseCount;

                document.getElementById('statGaemiStock').innerText = data.stats.total_gaemi_stock.toLocaleString() + '개';
                document.getElementById('statCoupangStock').innerText = data.stats.total_coupang_stock.toLocaleString() + '개';
                document.getElementById('statTotalStock').innerText = data.stats.total_physical_stock.toLocaleString() + '개';
                document.getElementById('statTotalSales7d').innerText = `7일 ${data.stats.total_sales_7d.toLocaleString()}개`;
                if (document.getElementById('statTotalSales30d')) {
                    document.getElementById('statTotalSales30d').innerText = (data.stats.total_sales_30d || 0).toLocaleString();
                }
                const c7 = data.stats.total_coupang_7d || 0;
                const c30 = data.stats.total_coupang_30d || 0;
                const o7 = data.stats.total_other_7d || 0;
                const o30 = data.stats.total_other_30d || 0;
                document.getElementById('statSalesSubtext').innerText = `(쿠팡 7일 ${c7} / 30일 ${c30} + 타채널 7일 ${o7} / 30일 ${o30})`;

                // 4. 하단 소스 상태 배지
                const ut = data.stats.upload_timestamps || {};
                if (data.stats.has_gaemi_data) {
                    document.getElementById('gaemiStatusText').innerText = "✅ 개미창고 소비기한 & 재고 로드됨" + (ut.gaemi ? ` (${ut.gaemi})` : "");
                    document.getElementById('gaemiStatusText').className = "text-[10px] text-amber-700 font-semibold";
                }
                if (data.stats.has_coupang_data) {
                    document.getElementById('coupangStatusText').innerText = "✅ 쿠팡 윙 재고 로드됨" + (ut.coupang ? ` (${ut.coupang})` : "");
                    document.getElementById('coupangStatusText').className = "text-[10px] text-blue-700 font-semibold";
                }
                if (data.stats.has_sales_data) {
                    document.getElementById('salesStatusText').innerText = "✅ 쿠팡외 통합 매출 로드됨" + (ut.sales ? ` (${ut.sales})` : "");
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

                if (!isAnyModalOpen()) {
                    renderMasterTable();
                }
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
                    } else if (!(m.category || '').includes(activeCategory)) {
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

            document.getElementById('filteredCountText').innerText = `| ${filtered.length}개 단품 표시 (총 ${allMasterData.length}개)`;

            if (filtered.length === 0) {
                tbody.innerHTML = `<tr><td colspan="${currentColumnOrder.length}" class="py-12 text-center text-slate-400">선택하신 조건에 일치하는 품목이 없습니다.</td></tr>`;
                tfoot.innerHTML = "";
                return;
            }

            // 합계 계산 (모두 단품이므로 실물 중복 없이 계산)
            let sumTotalStock = 0;
            let sumGaemiStock = 0;
            let sumCoupangStock = 0;
            let sumCoupangSales = 0;
            let sumCoupangSales30d = 0;
            let sumOtherSales = 0;
            let sumOtherSales30d = 0;
            let sumTotalSales = 0;
            let sumTotalSales30d = 0;
            let sumDailySales = 0;

            let html = "";
            filtered.forEach((m, idx) => {
                sumTotalStock += m.master_physical_total;
                sumGaemiStock += m.gaemi_total;
                sumCoupangStock += m.coupang_physical_total;
                sumCoupangSales += m.coupang_sales_7d;
                sumCoupangSales30d += (m.coupang_sales_30d || 0);
                sumOtherSales += m.other_sales_7d;
                sumOtherSales30d += (m.other_sales_30d || 0);
                sumTotalSales += m.total_sales_7d;
                sumTotalSales30d += (m.total_sales_30d || 0);
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

                let b2bBadge = (m.has_b2b_set && m.b2b_linked_codes && m.b2b_linked_codes.length > 0)
                    ? `<span class="px-1 py-0.2 rounded text-[9px] font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200 ml-1" title="B2B/세트 출고 연동: ${escapeHtml(m.b2b_linked_codes.map(b => b.s_code + ' (x' + b.multiplier + ')').join(', '))}">B2B연동</span>`
                    : '';

                // 개미 소비기한 미니 태그
                let expBadgeHtml = "";
                if (m.nearest_expiry_date || m.has_unassigned_stock) {
                    let tags = "";
                    if (m.nearest_expiry_date) {
                        let dDayColor = "bg-emerald-50 text-emerald-700 border-emerald-200";
                        if (m.nearest_days_left <= 90 || m.expiry_risk) {
                            dDayColor = "bg-rose-100 text-rose-800 border-rose-300 font-bold";
                        } else if (m.nearest_days_left <= 180) {
                            dDayColor = "bg-amber-100 text-amber-800 border-amber-300 font-semibold";
                        }
                        const expShort = m.nearest_expiry_date.length >= 10 ? m.nearest_expiry_date.slice(2) : m.nearest_expiry_date;
                        const adminMark = m.has_admin_expiry ? '✏️ ' : '';
                        tags += `
                            <span class="inline-flex items-center text-[9px] px-1.5 py-0.2 rounded border ${dDayColor}" title="최단 소비기한: ${escapeHtml(m.nearest_expiry_date)}">
                                <i class="fa-regular fa-clock mr-1 text-[8px]"></i>${adminMark}${escapeHtml(expShort)} (D-${m.nearest_days_left})
                            </span>
                        `;
                    }
                    if (m.is_mixed_lots) {
                        tags += `<span class="text-[9px] bg-slate-100 text-slate-600 border border-slate-200 px-1 rounded font-mono" title="${m.lot_count}개 로트/기한 혼합">${m.lot_count}개 기한</span>`;
                    }
                    if (m.has_unassigned_stock) {
                        tags += `<span class="text-[9px] bg-rose-100 text-rose-800 border border-rose-300 px-1 rounded font-bold" title="신규 입고분의 소비기한 지정 필요">⚠️ 기한미지정</span>`;
                    }
                    expBadgeHtml = `<div class="mt-0.5 flex flex-wrap justify-end gap-1">${tags}</div>`;
                }

                // 쿠팡 런웨이 경고
                let coupangRunwayHtml = m.coupang_runway_str;
                if (m.coupang_runway <= 7 && m.coupang_physical_total > 0) {
                    coupangRunwayHtml = `<span class="text-rose-600 font-bold animate-pulse">🚨 ${m.coupang_runway_str}</span>`;
                }

                // 통합 런웨이 프로그레스 바 시각화 (품목별/전역 기준 반영)
                const effLt = m.effective_lead_time || 110;
                const effPrep = m.effective_prepare_limit || 130;
                let runwayBarColor = "bg-emerald-500";
                if (m.total_runway < effLt) runwayBarColor = "bg-rose-500";
                else if (m.total_runway <= effPrep) runwayBarColor = "bg-amber-500";
                const barBase = Math.max(180, effPrep + 30);
                const barPercent = Math.min(100, Math.max(8, (m.total_runway / barBase) * 100));

                const inTransitHtml = (m.in_transit_qty && m.in_transit_qty > 0)
                    ? `<div class="text-[9px] font-bold text-blue-700 mt-0.5" title="예상 입고일: ${escapeHtml(m.expected_arrival_date || '미정')}">🚢 입고예정 +${m.in_transit_qty.toLocaleString()}${m.expected_arrival_date ? ' (' + escapeHtml(m.expected_arrival_date.slice(5)) + ')' : ''}</div>`
                    : '';

                const isSubExpanded = expandAllOptions || expandedRowIds.has(m.id);
                const activeGaemiLots = (m.gaemi_lots || []).filter(lot => (lot.stock || 0) > 0);

                html += `
                    <tr draggable="true" data-row-idx="${allMasterData.indexOf(m)}" class="hover:bg-slate-50 transition cursor-pointer select-none" onclick="toggleSingleOption('${escapeHtml(m.id)}')">
                `;

                currentColumnOrder.forEach(colId => {
                    const col = COLUMN_DEFS[colId];
                    if (colId === 'no') {
                        html += `<td class="${col.tdClass}"><i class="fa-solid fa-grip-vertical text-slate-300 mr-1 text-[9px]"></i>${idx + 1}</td>`;
                    } else if (colId === 'status') {
                        html += `<td class="${col.tdClass}"><span class="inline-block px-2 py-0.5 rounded-full text-[10px] ${badgeClass}">${escapeHtml(m.status)}</span></td>`;
                    } else if (colId === 'type') {
                        html += `<td class="${col.tdClass}">${ptypeBadge}</td>`;
                    } else if (colId === 'code') {
                        html += `<td class="${col.tdClass}">${escapeHtml(m.seller_code)}${b2bBadge}</td>`;
                    } else if (colId === 'name') {
                        const customLtBadge = (m.lead_time_days !== null && m.lead_time_days !== undefined)
                            ? `<span class="text-[9px] bg-blue-50 text-blue-700 border border-blue-200 px-1 rounded font-mono ml-1" title="품목 개별 리드타임">LT ${m.lead_time_days}일</span>`
                            : '';
                        html += `
                            <td class="${col.tdClass}">
                                <div class="font-bold text-slate-800 flex items-center space-x-1">
                                    <i class="fa-solid fa-angle-right text-[10px] text-slate-400"></i>
                                    <span>${escapeHtml(m.name)}</span>
                                    ${customLtBadge}
                                </div>
                                <div class="text-[10px] text-slate-400 mt-0.5 truncate max-w-xs">
                                    ${escapeHtml(m.category)} | ${escapeHtml(m.gaemi_details)}
                                </div>
                            </td>
                        `;
                    } else if (colId === 'total_stock') {
                        html += `
                            <td class="${col.tdClass}">
                                <div>${m.master_physical_total.toLocaleString()}</div>
                                ${inTransitHtml}
                            </td>
                        `;
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
                        const s7 = (m.total_sales_7d || 0).toLocaleString();
                        const s30 = (m.total_sales_30d || 0).toLocaleString();
                        html += `
                            <td class="${col.tdClass}">
                                <div class="font-bold font-mono text-indigo-950">${s7}개 <span class="text-slate-400 font-normal text-[10px]">/ 30일 ${s30}개</span></div>
                                <div class="text-[9px] text-slate-500 font-normal mt-0.5">가중 일평균 ${m.daily_sales}개</div>
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
                        const mapBtn = isAdminMode
                            ? `<button type="button" data-prod-id="${escapeHtml(m.id)}" onclick="event.stopPropagation(); openCodeMappingModal(this.dataset.prodId)" class="ml-1 px-2 py-1 rounded-md text-[10px] font-extrabold bg-indigo-100 hover:bg-indigo-200 text-indigo-900 border border-indigo-300 shadow-xs transition" title="3채널 코드 매핑 수동 지정">🔗 매핑</button>`
                            : '';
                        const editBtn = isAdminMode
                            ? `<button type="button" data-prod-id="${escapeHtml(m.id)}" onclick="event.stopPropagation(); openEditProductModal(this.dataset.prodId)" class="ml-1 px-2 py-1 rounded-md text-[10px] font-extrabold bg-amber-400 hover:bg-amber-500 text-slate-950 shadow-xs transition" title="관리자 수정">✏️ 수정</button>`
                            : '';
                        html += `
                            <td class="${col.tdClass}">
                                <div class="inline-flex items-center justify-center flex-wrap gap-1">
                                    <button type="button" class="px-2.5 py-1 rounded-md text-[10px] font-bold transition shadow-xs ${m.action_cta_class}">
                                        ${escapeHtml(m.action_cta)}
                                    </button>
                                    ${mapBtn}
                                    ${editBtn}
                                </div>
                            </td>
                        `;
                    }
                });

                html += `</tr>`;

                // 세부 확장 행 (소비기한 로트 목록, 선입선출 우선순위, 소진 시뮬레이션 및 쿠팡 번들 내역)
                html += `
                    <tr id="subrow_${escapeHtml(m.id)}" class="${isSubExpanded ? '' : 'hidden'} bg-slate-50 border-b border-slate-200">
                        <td colspan="${currentColumnOrder.length}" class="py-3 px-5">
                            <div class="space-y-3">

                                ${isAdminMode ? `
                                    <div class="flex flex-wrap items-center justify-between gap-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-[11px]">
                                        <span class="font-bold text-amber-950">⚙️ 관리자 빠른 작업 — 기준 리드타임: ${effLt}일 (안전버퍼 +${effPrep - effLt}일) · 기한경고: ${m.effective_expiry_warn || 120}일${m.in_transit_qty > 0 ? ` · 입고예정 +${m.in_transit_qty}개 (${escapeHtml(m.expected_arrival_date || '예정일 미정')})` : ''}</span>
                                        <div class="flex items-center gap-1.5">
                                            <button type="button" data-prod-id="${escapeHtml(m.id)}" onclick="openCodeMappingModal(this.dataset.prodId)" class="px-3 py-1 bg-indigo-600 hover:bg-indigo-700 text-white rounded-md font-extrabold shadow-xs">
                                                🔗 코드 매핑만 지정
                                            </button>
                                            <button type="button" data-prod-id="${escapeHtml(m.id)}" onclick="openEditProductModal(this.dataset.prodId)" class="px-3 py-1 bg-amber-500 hover:bg-amber-600 text-slate-950 rounded-md font-extrabold shadow-xs">
                                                ✏️ 이 상품 기준정보 · 소비기한 수정
                                            </button>
                                        </div>
                                    </div>
                                ` : (m.in_transit_qty > 0 ? `
                                    <div class="bg-blue-50 border border-blue-200 rounded-lg px-3 py-2 text-[11px] text-blue-900 font-semibold">
                                        🚢 입고 예정(선적/발주중): <strong>+${m.in_transit_qty.toLocaleString()}개</strong>${m.expected_arrival_date ? ` (예상 입고일: <strong>${escapeHtml(m.expected_arrival_date)}</strong>)` : ''}
                                    </div>
                                ` : '')}

                                ${m.lot_sync_note ? `
                                    <div class="bg-indigo-50 border border-indigo-200 rounded-lg px-3 py-1.5 text-[11px] text-indigo-900 font-semibold">
                                        🔄 <strong>로트 자동 동기화 메모:</strong> ${escapeHtml(m.lot_sync_note)}
                                    </div>
                                ` : ''}
                                
                                <!-- 1. 개미창고 소비기한 & 로트별 선입선출(FIFO) 현황판 -->
                                ${activeGaemiLots.length > 0 ? `
                                    <div class="bg-white rounded-lg border border-amber-200 p-3 shadow-xs">
                                        <div class="flex items-center justify-between mb-2">
                                            <div class="flex items-center space-x-1.5 text-xs font-bold text-amber-900">
                                                <i class="fa-solid fa-clock-rotate-left text-amber-600"></i>
                                                <span>개미창고 옵션별 소비기한 및 선입선출(FIFO) 출고 관리</span>
                                            </div>
                                            <span class="text-[10px] bg-amber-100 text-amber-900 font-semibold px-2 py-0.5 rounded">
                                                총 ${activeGaemiLots.length}개 로트/옵션
                                            </span>
                                        </div>

                                        <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2 text-xs">
                                            ${activeGaemiLots.map((lot) => {
                                                const isNearest = (lot.expiry_date && lot.expiry_date === m.nearest_expiry_date);
                                                let fifoBadge = '<span class="text-[9px] bg-slate-100 text-slate-500 px-1.5 py-0.2 rounded font-medium">출고 대기</span>';
                                                let cardBorder = 'border-slate-200 bg-slate-50/50';

                                                if (lot.source === 'unassigned') {
                                                    fifoBadge = '<span class="text-[9px] bg-rose-100 text-rose-800 font-bold px-2 py-0.5 rounded-full border border-rose-200">⚠️ 기한 지정 필요</span>';
                                                    cardBorder = 'border-rose-300 bg-rose-50/40';
                                                } else if (isNearest && lot.stock > 0) {
                                                    fifoBadge = '<span class="text-[9px] bg-rose-100 text-rose-800 font-bold px-2 py-0.5 rounded-full border border-rose-200 animate-pulse">🥇 1순위 우선 출고</span>';
                                                    cardBorder = 'border-rose-300 bg-rose-50/40 ring-1 ring-rose-200';
                                                }

                                                const expText = lot.expiry_date ? `${lot.expiry_date} (D-${lot.days_left}일)` : '미기재/로트명';
                                                const adminLotBadge = lot.is_admin_expiry ? ' <span class="text-[9px] bg-amber-100 text-amber-900 px-1 rounded font-bold">관리자</span>' : '';

                                                return `
                                                    <div class="p-2.5 rounded-lg border ${cardBorder} flex justify-between items-center">
                                                        <div class="min-w-0 pr-2">
                                                            <div class="font-bold text-slate-800 truncate">${escapeHtml(lot.option_name)}${adminLotBadge}</div>
                                                            <div class="text-[10px] text-slate-500 font-mono mt-0.5 flex items-center">
                                                                <i class="fa-regular fa-calendar-check mr-1 text-slate-400"></i>
                                                                <span>${escapeHtml(expText)}</span>
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
                                                        <span>7일(60%)+30일(40%) 가중 일평균 <strong>${m.daily_sales}개</strong> 판매 기준, 최단 소비기한(<strong>${escapeHtml(m.nearest_expiry_date)}</strong>, D-${m.nearest_days_left}일)까지 <strong>${m.projected_sales_to_exp}개</strong> 소진 예상 (로트 재고 ${m.nearest_lot_stock}개)</span>
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

                                <!-- 2. B2B/세트(S코드) 출고 연동 안내 바 -->
                                ${m.has_b2b_set && m.b2b_linked_codes && m.b2b_linked_codes.length > 0 ? `
                                    <div class="bg-indigo-50/70 rounded-lg border border-indigo-200 p-2.5 text-[11px] text-indigo-900 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2">
                                        <div class="flex items-center space-x-1.5">
                                            <i class="fa-solid fa-boxes-stacked text-indigo-600"></i>
                                            <span class="font-bold">B2B/세트(S코드) 출고 연동:</span>
                                            <span>이 단품은 <strong>${escapeHtml(m.b2b_linked_codes.map(b => `${b.s_code} (${b.desc})`).join(', '))}</strong> 주문 시 개미창고 실물 재고에서 차감되며, 출고량(7일/30일) 및 런웨이에 자동 환산 합산되어 있습니다.</span>
                                        </div>
                                        ${m.b2b_shared_sales && m.b2b_shared_sales.length > 0 ? `
                                            <span class="text-[10px] bg-indigo-100 text-indigo-800 font-semibold px-2 py-0.5 rounded border border-indigo-200 whitespace-nowrap">
                                                B2B 출고량: 7일 ${m.b2b_shared_sales.reduce((acc, cur) => acc + cur.sales_7d, 0)}개 / 30일 ${m.b2b_shared_sales.reduce((acc, cur) => acc + cur.sales_30d, 0)}개
                                            </span>
                                        ` : `
                                            <span class="text-[10px] text-slate-400 font-normal whitespace-nowrap">최근 S코드 주문 없음</span>
                                        `}
                                    </div>
                                ` : ''}

                                ${m.note ? `
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
                                                        <span class="font-bold text-slate-800">${escapeHtml(opt.name)}</span>
                                                        <span class="text-slate-400 font-mono block text-[10px]">${escapeHtml(opt.id)} (${opt.bundle}개입)</span>
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
                                        <div class="text-[11px] font-bold text-indigo-700 mb-1 flex items-center justify-between">
                                            <div class="flex items-center space-x-1">
                                                <i class="fa-solid fa-receipt"></i>
                                                <span>쿠팡외 타채널 출고: 7일 <strong>${m.other_sales_7d}개</strong> / 30일 <strong>${m.other_sales_30d || m.other_sales_7d}개</strong></span>
                                            </div>
                                            <span class="text-[10px] text-slate-400 font-normal">최근 주문 5건 표시</span>
                                        </div>
                                        <div class="flex flex-wrap gap-1.5 text-[10px]">
                                            ${m.recent_orders.map(ord => `
                                                <span class="bg-indigo-50 border border-indigo-100 px-2 py-0.5 rounded text-slate-700">
                                                    <span class="font-semibold text-indigo-800">${escapeHtml(ord.mall || '스마트스토어')}</span>: ${ord.qty}개 (${escapeHtml(ord.date)})
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
                    footHtml += `<td class="py-2.5 px-3 text-left font-black text-slate-900">총 ${filtered.length}개 단품 (실물 합계)</td>`;
                } else if (colId === 'total_stock') {
                    footHtml += `<td class="py-2.5 px-3 text-right font-black text-emerald-800 text-xs bg-emerald-100/60" title="단품 실물 기준 합계">${sumTotalStock.toLocaleString()}</td>`;
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
                    footHtml += `
                        <td class="py-2.5 px-3 text-right font-black text-indigo-950 text-xs bg-indigo-100/60">
                            <div>7일 ${sumTotalSales.toLocaleString()}개</div>
                            <div class="text-[10px] text-slate-500 font-normal">30일 ${sumTotalSales30d.toLocaleString()}개</div>
                        </td>
                    `;
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
            return String(text ?? '').replace(/[&<>"']/g, function(m) {
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

        // -------------------------------------------------------------
        // 단품 마스터 생성 / 수정 모달 로직
        // -------------------------------------------------------------
        let currentEditGaemiCsvTotal = 0;
        let epSalesOrigState = '';
        let epSalesExplicitEmpty = false;
        let epGaemiMappingsState = [];
        let epGaemiClearedExplicit = false;

        async function ensureAdminConfig() {
            const res = await adminFetch('/api/admin/config', { method: 'GET' });
            const json = await res.json();
            if (json.success) adminConfigCache = json;
            return adminConfigCache;
        }

        function computeCsvLotsFromCache(item, cfg) {
            const gaemi = (cfg && cfg.gaemi_inventory) || {};
            const overrides = (cfg && cfg.lot_expiry_overrides) || {};
            const lots = [];

            const pushLot = (actualCode, gItem) => {
                const optName = gItem.option_name || '';
                const reg = (gItem.reg_date || '').trim();
                const exactKey = reg ? `${actualCode}||${optName.trim()}||${reg}` : `${actualCode}||${optName.trim()}`;
                const optKey = `${actualCode}||${optName.trim()}`;
                const rawExp = (gItem.expiry_date || '').trim();
                const ov = overrides[exactKey] !== undefined ? overrides[exactKey] : overrides[optKey];
                let effExp = rawExp;
                let isAdmin = false;
                if (ov === '__CLEAR__') { effExp = ''; isAdmin = true; }
                else if (ov) { effExp = ov; isAdmin = true; }
                lots.push({
                    gaemi_code: actualCode,
                    lot_key: exactKey,
                    option_name: optName,
                    stock: parseInt(gItem.stock || 0, 10),
                    reg_date: reg,
                    expiry_date: effExp,
                    raw_expiry_date: rawExp,
                    is_admin_expiry: isAdmin
                });
            };

            const gMappings = item.gaemi_mappings;
            if (Array.isArray(gMappings) && gMappings.length > 0) {
                const upperCodeMap = {};
                Object.keys(gaemi).forEach(k => {
                    upperCodeMap[String(k).trim().toUpperCase()] = { actualCode: k, rows: gaemi[k] || [] };
                });
                const seenRefs = new Set();
                gMappings.forEach(rule => {
                    if (!rule || typeof rule !== 'object') return;
                    const reqCode = String(rule.gaemi_code || rule.code || '').trim();
                    const reqOpt = String(rule.option_name || '').trim();
                    if (!reqCode) return;
                    let actualCode = reqCode;
                    let matched = gaemi[reqCode];
                    if (!matched) {
                        const found = upperCodeMap[reqCode.toUpperCase()];
                        if (found) {
                            actualCode = found.actualCode;
                            matched = found.rows;
                        }
                    }
                    if (!matched) return;
                    matched.forEach((gItem, idx) => {
                        const itemOpt = String(gItem.option_name || '').trim();
                        if (reqOpt === '' || reqOpt === '*' || itemOpt === reqOpt) {
                            const ref = `${actualCode}::${idx}`;
                            if (!seenRefs.has(ref)) {
                                seenRefs.add(ref);
                                pushLot(actualCode, gItem);
                            }
                        }
                    });
                });
                return lots;
            }

            const optFilter = (item.gaemi_filter || '').trim();
            const gCodes = item.gaemi_codes || [];
            const seenLegacy = new Set();
            gCodes.forEach(gCode => {
                const gC = String(gCode || '').trim();
                if (!gC || seenLegacy.has(gC)) return;
                seenLegacy.add(gC);
                (gaemi[gC] || []).forEach(gItem => {
                    const optName = gItem.option_name || '';
                    if (optFilter && !optName.includes(optFilter)) return;
                    pushLot(gC, gItem);
                });
            });
            return lots;
        }

        function renderEpGaemiMappingSection() {
            const box = document.getElementById('ep_gaemi_mappings_box');
            const badges = document.getElementById('ep_gaemi_mappings_badges');
            const legacyFields = document.getElementById('ep_gaemi_legacy_fields');
            if (!box || !badges || !legacyFields) return;
            if (Array.isArray(epGaemiMappingsState) && epGaemiMappingsState.length > 0) {
                box.classList.remove('hidden');
                legacyFields.classList.add('hidden');
                badges.innerHTML = epGaemiMappingsState.map(r => `
                    <span class="inline-flex items-center px-2 py-0.5 rounded bg-white border border-amber-300 text-amber-950 font-mono text-[11px] font-bold">
                        ${escapeHtml(r.gaemi_code)} · ${escapeHtml(r.option_name || '* (전체 옵션)')}
                    </span>
                `).join('');
            } else {
                box.classList.add('hidden');
                legacyFields.classList.remove('hidden');
                badges.innerHTML = '';
            }
        }

        function clearEpGaemiMappingsOverride() {
            epGaemiMappingsState = [];
            epGaemiClearedExplicit = true;
            renderEpGaemiMappingSection();
        }

        function openCreateProductModal(prefill = null) {
            if (!isAdminMode) { openPinModal(); return; }
            document.getElementById('ep_modal_title').innerText = '신규 단품 마스터 등록';
            document.getElementById('ep_is_new').value = '1';
            document.getElementById('ep_id').value = '';
            document.getElementById('ep_orig_seller_code').value = '';
            document.getElementById('ep_seller_code').value = (prefill && prefill.seller_code) ? prefill.seller_code : '';
            document.getElementById('ep_name').value = (prefill && prefill.name) ? prefill.name : '';
            document.getElementById('ep_product_type').value = (prefill && prefill.product_type) ? prefill.product_type : '사입';
            document.getElementById('ep_category').value = (prefill && prefill.category) ? prefill.category : '기타';
            document.getElementById('ep_is_active').checked = true;
            document.getElementById('ep_note').value = '';
            document.getElementById('ep_lead_time_days').value = '';
            document.getElementById('ep_safety_buffer_days').value = '';
            document.getElementById('ep_expiry_warn_days').value = '';
            document.getElementById('ep_in_transit_qty').value = 0;
            document.getElementById('ep_expected_arrival_date').value = '';
            document.getElementById('ep_default_expiry_date').value = '';

            epSalesExplicitEmpty = false;
            epSalesOrigState = '';
            const epSalesEl = document.getElementById('ep_sales_codes');
            if (prefill && Array.isArray(prefill.sales_codes) && prefill.sales_codes.length > 0) {
                epSalesEl.value = prefill.sales_codes.join(', ');
            } else {
                epSalesEl.value = '';
            }
            epSalesEl.placeholder = '기본값: 판매자관리코드와 동일 (추가/변경 시 쉼표로 구분 입력)';

            epGaemiMappingsState = (prefill && Array.isArray(prefill.gaemi_mappings)) ? JSON.parse(JSON.stringify(prefill.gaemi_mappings)) : [];
            epGaemiClearedExplicit = false;
            document.getElementById('ep_gaemi_codes').value = (prefill && prefill.gaemi_codes) ? prefill.gaemi_codes : '';
            document.getElementById('ep_gaemi_filter').value = '';
            renderEpGaemiMappingSection();

            document.getElementById('ep_lot_sync_note').classList.add('hidden');

            currentEditGaemiCsvTotal = 0;
            document.getElementById('ep_gaemi_summary_bar').innerText = '개미 CSV 재고: 0개 (신규)';
            renderEpCsvLots([]);
            document.getElementById('ep_manual_lots_container').innerHTML = '';
            updateManualLotsSum();
            const optContainer = document.getElementById('ep_options_container');
            optContainer.innerHTML = '';
            if (prefill && Array.isArray(prefill.options)) {
                prefill.options.forEach(o => addCoupangOptionRow(o));
            }
            renderEpTempStocks([], []);
            document.getElementById('ep_temp_stocks_details').open = false;

            document.getElementById('ep_btn_open_mapping').classList.add('hidden');
            document.getElementById('ep_btn_toggle_hide').classList.add('hidden');
            document.getElementById('ep_btn_hard_delete').classList.add('hidden');
            document.getElementById('editProductModal').classList.remove('hidden');
        }

        async function openEditProductModal(prodId) {
            if (!isAdminMode) { openPinModal(); return; }
            let item = allMasterData.find(m => m.id === prodId || m.seller_code === prodId);
            if (!item && (!adminConfigCache || !adminConfigCache.master_products)) {
                try { await ensureAdminConfig(); } catch (e) { return; }
            }
            let rawCfgItem = null;
            if (adminConfigCache && adminConfigCache.master_products) {
                rawCfgItem = adminConfigCache.master_products.find(m => m.id === prodId || m.seller_code === prodId);
            }
            if (!item && rawCfgItem) {
                const csvLots = computeCsvLotsFromCache(rawCfgItem, adminConfigCache);
                const gTotal = csvLots.reduce((a, b) => a + (b.stock || 0), 0);
                item = Object.assign({}, rawCfgItem, {
                    has_custom_sales_codes: Array.isArray(rawCfgItem.sales_codes),
                    csv_gaemi_lots: csvLots,
                    gaemi_total: gTotal,
                    options: (rawCfgItem.options || []).map(o => ({
                        id: o.id, bundle: o.bundle, name: o.name,
                        coupang_stock: ((adminConfigCache.coupang_inventory || {})[o.id] || {}).stock || 0
                    }))
                });
            }
            if (!item) {
                alert('대상 상품 정보를 찾을 수 없습니다.');
                return;
            }

            document.getElementById('ep_modal_title').innerText = `[${item.seller_code}] ${item.name} — 기준정보 수정`;
            document.getElementById('ep_is_new').value = '0';
            document.getElementById('ep_id').value = item.id || '';
            document.getElementById('ep_orig_seller_code').value = item.seller_code || '';
            document.getElementById('ep_seller_code').value = item.seller_code || '';
            document.getElementById('ep_name').value = item.name || '';
            document.getElementById('ep_product_type').value = item.product_type === '수입' ? '수입' : '사입';
            document.getElementById('ep_category').value = item.category || '기타';
            document.getElementById('ep_is_active').checked = (item.is_active !== false);
            document.getElementById('ep_note').value = item.note || '';
            document.getElementById('ep_lead_time_days').value = (item.lead_time_days !== null && item.lead_time_days !== undefined) ? item.lead_time_days : '';
            document.getElementById('ep_safety_buffer_days').value = (item.safety_buffer_days !== null && item.safety_buffer_days !== undefined) ? item.safety_buffer_days : '';
            document.getElementById('ep_expiry_warn_days').value = (item.expiry_warn_days !== null && item.expiry_warn_days !== undefined) ? item.expiry_warn_days : '';
            document.getElementById('ep_in_transit_qty').value = item.in_transit_qty || 0;
            document.getElementById('ep_expected_arrival_date').value = item.expected_arrival_date || '';
            document.getElementById('ep_default_expiry_date').value = item.default_expiry_date || '';

            const epSalesEl = document.getElementById('ep_sales_codes');
            if (item.has_custom_sales_codes) {
                const scList = item.sales_codes || [];
                if (scList.length === 0) {
                    epSalesExplicitEmpty = true;
                    epSalesOrigState = '__EXPLICIT_EMPTY__';
                    epSalesEl.value = '';
                    epSalesEl.placeholder = '⚠️ 현재 매출 연동 비활성(0건) — 코드 입력 시 재연동';
                } else {
                    epSalesExplicitEmpty = false;
                    epSalesEl.value = scList.join(', ');
                    epSalesOrigState = epSalesEl.value.trim();
                    epSalesEl.placeholder = '기본값: 판매자관리코드와 동일';
                }
            } else {
                epSalesExplicitEmpty = false;
                epSalesOrigState = '';
                epSalesEl.value = '';
                epSalesEl.placeholder = `기본값: 판매자관리코드 (${item.seller_code || ''}) 자동 연동`;
            }

            epGaemiMappingsState = Array.isArray(item.gaemi_mappings) ? JSON.parse(JSON.stringify(item.gaemi_mappings)) : [];
            epGaemiClearedExplicit = false;
            document.getElementById('ep_gaemi_codes').value = (item.gaemi_codes || []).join(', ');
            document.getElementById('ep_gaemi_filter').value = item.gaemi_filter || '';
            renderEpGaemiMappingSection();

            const syncNoteEl = document.getElementById('ep_lot_sync_note');
            if (item.lot_sync_note) {
                syncNoteEl.innerText = '🔄 ' + item.lot_sync_note;
                syncNoteEl.classList.remove('hidden');
            } else {
                syncNoteEl.classList.add('hidden');
            }

            const csvLots = item.csv_gaemi_lots || [];
            currentEditGaemiCsvTotal = csvLots.reduce((a, b) => a + (b.stock || 0), 0);
            document.getElementById('ep_gaemi_summary_bar').innerText = `개미 CSV 실물 합계: ${currentEditGaemiCsvTotal.toLocaleString()}개 (현재 화면 총합 ${ (item.gaemi_total || 0).toLocaleString() }개)`;

            renderEpCsvLots(csvLots);

            const manualContainer = document.getElementById('ep_manual_lots_container');
            manualContainer.innerHTML = '';
            (item.manual_lots || []).forEach(lot => addManualLotRow(lot));
            updateManualLotsSum();

            const optContainer = document.getElementById('ep_options_container');
            optContainer.innerHTML = '';
            (item.options || []).forEach(opt => addCoupangOptionRow(opt));

            renderEpTempStocks(csvLots, item.options || []);
            document.getElementById('ep_temp_stocks_details').open = false;

            document.getElementById('ep_btn_open_mapping').classList.remove('hidden');
            const btnHide = document.getElementById('ep_btn_toggle_hide');
            const btnDel = document.getElementById('ep_btn_hard_delete');
            btnHide.classList.remove('hidden');
            btnDel.classList.remove('hidden');
            btnHide.innerText = (item.is_active === false) ? '대시보드 노출 복원' : '대시보드 숨김';
            btnHide.dataset.isActive = (item.is_active === false) ? '0' : '1';

            document.getElementById('editProductModal').classList.remove('hidden');
        }

        function openCodeMappingFromEditModal() {
            const prodId = document.getElementById('ep_id').value.trim() || document.getElementById('ep_orig_seller_code').value.trim();
            if (prodId) openCodeMappingModal(prodId);
        }

        function syncEditModalFromSavedProduct(savedProd) {
            if (!savedProd) return;
            const editModal = document.getElementById('editProductModal');
            if (!editModal || editModal.classList.contains('hidden')) return;
            const curId = document.getElementById('ep_id').value.trim();
            const curCode = document.getElementById('ep_orig_seller_code').value.trim().toUpperCase();
            if ((curId && savedProd.id !== curId) && (curCode && String(savedProd.seller_code || '').toUpperCase() !== curCode)) return;

            const epSalesEl = document.getElementById('ep_sales_codes');
            if (Array.isArray(savedProd.sales_codes)) {
                if (savedProd.sales_codes.length === 0) {
                    epSalesExplicitEmpty = true;
                    epSalesOrigState = '__EXPLICIT_EMPTY__';
                    epSalesEl.value = '';
                    epSalesEl.placeholder = '⚠️ 현재 매출 연동 비활성(0건) — 코드 입력 시 재연동';
                } else {
                    epSalesExplicitEmpty = false;
                    epSalesEl.value = savedProd.sales_codes.join(', ');
                    epSalesOrigState = epSalesEl.value.trim();
                    epSalesEl.placeholder = '기본값: 판매자관리코드와 동일';
                }
            } else {
                epSalesExplicitEmpty = false;
                epSalesOrigState = '';
                epSalesEl.value = '';
                epSalesEl.placeholder = `기본값: 판매자관리코드 (${savedProd.seller_code || ''}) 자동 연동`;
            }

            epGaemiMappingsState = Array.isArray(savedProd.gaemi_mappings) ? JSON.parse(JSON.stringify(savedProd.gaemi_mappings)) : [];
            epGaemiClearedExplicit = false;
            document.getElementById('ep_gaemi_codes').value = (savedProd.gaemi_codes || []).join(', ');
            document.getElementById('ep_gaemi_filter').value = savedProd.gaemi_filter || '';
            renderEpGaemiMappingSection();

            const optContainer = document.getElementById('ep_options_container');
            optContainer.innerHTML = '';
            (savedProd.options || []).forEach(opt => addCoupangOptionRow(opt));

            if (adminConfigCache) {
                const csvLots = computeCsvLotsFromCache(savedProd, adminConfigCache);
                currentEditGaemiCsvTotal = csvLots.reduce((a, b) => a + (b.stock || 0), 0);
                document.getElementById('ep_gaemi_summary_bar').innerText = `개미 CSV 실물 합계: ${currentEditGaemiCsvTotal.toLocaleString()}개`;
                renderEpCsvLots(csvLots);
                renderEpTempStocks(csvLots, savedProd.options || []);
            }
        }

        function closeEditProductModal() {
            const modal = document.getElementById('editProductModal');
            if (modal) modal.classList.add('hidden');
        }

        function renderEpCsvLots(csvLots) {
            const box = document.getElementById('ep_csv_lots_container');
            if (!csvLots || csvLots.length === 0) {
                box.innerHTML = '<div class="text-[11px] text-slate-400 bg-white border border-slate-200 rounded-lg p-2.5">연결된 개미창고 CSV 로트가 없습니다. (개미 상품코드 매핑 또는 아래 기본 소비기한을 사용하세요)</div>';
                return;
            }
            const activeLots = csvLots.filter(lot => (lot.stock || 0) > 0);
            const emptyLots = csvLots.filter(lot => (lot.stock || 0) <= 0);
            const renderLotCard = (lot, muted = false) => {
                const initMode = (lot.is_admin_expiry && !lot.expiry_date) ? '__CLEAR__' : '';
                const rawHint = lot.raw_expiry_date ? `CSV원본: ${escapeHtml(lot.raw_expiry_date)}` : 'CSV원본: 미기재';
                return `
                    <div class="flex flex-wrap items-center justify-between gap-2 bg-white border ${muted ? 'border-slate-200 opacity-80' : 'border-amber-200'} rounded-lg p-2">
                        <div class="min-w-0 flex-1">
                            <div class="font-bold text-slate-800 truncate">${escapeHtml(lot.option_name || '옵션명 없음')} <span class="${muted ? 'text-slate-500' : 'text-amber-700'} font-mono">(${(lot.stock || 0).toLocaleString()}개)</span></div>
                            <div class="text-[10px] text-slate-400 font-mono">${escapeHtml(lot.gaemi_code)} · ${lot.reg_date ? '등록 ' + escapeHtml(lot.reg_date) + ' · ' : ''}${rawHint}</div>
                        </div>
                        <div class="flex items-center space-x-1.5 shrink-0">
                            <input type="date" class="ep-csv-lot-input px-2 py-1 border border-slate-300 rounded text-[11px] font-mono" data-lot-key="${escapeHtml(lot.lot_key)}" data-raw-expiry="${escapeHtml(lot.raw_expiry_date || '')}" data-was-admin="${lot.is_admin_expiry ? '1' : '0'}" data-mode="${initMode}" value="${escapeHtml(lot.expiry_date || '')}" oninput="this.dataset.mode=''">
                            <button type="button" onclick="restoreCsvLotInput(this)" class="px-2 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded text-[10px] font-bold" title="CSV 원본 소비기한으로 복원">원복</button>
                            <button type="button" onclick="clearCsvLotInput(this)" class="px-2 py-1 bg-rose-50 hover:bg-rose-100 text-rose-700 rounded text-[10px] font-bold" title="이 로트 소비기한 표시 제거">제거</button>
                        </div>
                    </div>
                `;
            };
            let html = '';
            if (activeLots.length > 0) {
                html += activeLots.map(lot => renderLotCard(lot, false)).join('');
            } else {
                html += '<div class="text-[11px] text-slate-400 bg-white border border-slate-200 rounded-lg p-2.5">현재 개미창고 보유 재고(1개 이상) 로트가 없습니다. 아래 기본 소비기한을 사용하세요.</div>';
            }
            if (emptyLots.length > 0) {
                html += `
                    <details class="pt-1">
                        <summary class="cursor-pointer text-[10px] font-semibold text-slate-500 hover:text-slate-700 select-none">
                            소진 완료(재고 0개) 과거 입고 이력 ${emptyLots.length}건 보기
                        </summary>
                        <div class="mt-1.5 space-y-1.5">${emptyLots.map(lot => renderLotCard(lot, true)).join('')}</div>
                    </details>
                `;
            }
            box.innerHTML = html;
        }

        function restoreCsvLotInput(btn) {
            const inp = btn.parentElement.querySelector('.ep-csv-lot-input');
            if (!inp) return;
            inp.dataset.mode = '__RESTORE__';
            inp.value = inp.dataset.rawExpiry || '';
        }

        function clearCsvLotInput(btn) {
            const inp = btn.parentElement.querySelector('.ep-csv-lot-input');
            if (!inp) return;
            inp.dataset.mode = '__CLEAR__';
            inp.value = '';
        }

        function addManualLotRow(lot = {}) {
            const container = document.getElementById('ep_manual_lots_container');
            const row = document.createElement('div');
            row.className = 'ep-manual-lot-row flex flex-wrap items-center gap-2 bg-white border border-amber-300 rounded-lg p-2';
            row.innerHTML = `
                <input type="hidden" class="ep-ml-id" value="${escapeHtml(lot.lot_id || '')}">
                <input type="text" class="ep-ml-label flex-1 min-w-[120px] px-2.5 py-1 border border-slate-300 rounded text-[11px]" placeholder="로트명 (예: 1차 입고분)" value="${escapeHtml(lot.label || '')}">
                <input type="date" class="ep-ml-expiry px-2.5 py-1 border border-slate-300 rounded text-[11px] font-mono" value="${escapeHtml(lot.expiry_date || '')}">
                <div class="flex items-center space-x-1">
                    <input type="number" min="0" class="ep-ml-qty w-24 px-2.5 py-1 border border-slate-300 rounded text-[11px] font-mono font-bold text-right" placeholder="수량" value="${lot.qty !== undefined ? lot.qty : 0}" oninput="updateManualLotsSum()">
                    <span class="text-[11px] text-slate-500">개</span>
                </div>
                <button type="button" onclick="this.closest('.ep-manual-lot-row').remove(); updateManualLotsSum();" class="px-2 py-1 bg-rose-50 hover:bg-rose-100 text-rose-700 rounded text-[10px] font-bold">삭제</button>
            `;
            container.appendChild(row);
            updateManualLotsSum();
        }

        function updateManualLotsSum() {
            const rows = document.querySelectorAll('#ep_manual_lots_container .ep-manual-lot-row');
            const bar = document.getElementById('ep_manual_lots_sum_bar');
            if (rows.length === 0) {
                bar.classList.add('hidden');
                return;
            }
            let sum = 0;
            rows.forEach(r => {
                sum += Math.max(0, parseInt(r.querySelector('.ep-ml-qty').value || '0', 10) || 0);
            });
            bar.classList.remove('hidden');
            if (currentEditGaemiCsvTotal === 0) {
                bar.className = 'text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border bg-blue-50 border-blue-200 text-blue-900';
                bar.innerText = `수동 로트 합계: ${sum.toLocaleString()}개 (CSV 업로드 시 이 수량 기준으로 선입선출 FIFO 자동 차감됩니다)`;
            } else if (sum === currentEditGaemiCsvTotal) {
                bar.className = 'text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border bg-emerald-50 border-emerald-200 text-emerald-900';
                bar.innerText = `✅ 개미 CSV 재고(${currentEditGaemiCsvTotal.toLocaleString()}개)와 수동 분할 합계(${sum.toLocaleString()}개)가 정확히 일치합니다.`;
            } else if (sum < currentEditGaemiCsvTotal) {
                const diff = currentEditGaemiCsvTotal - sum;
                bar.className = 'text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border bg-amber-50 border-amber-300 text-amber-950';
                bar.innerText = `⚠️ 개미 CSV ${currentEditGaemiCsvTotal.toLocaleString()}개 중 ${sum.toLocaleString()}개만 로트 배정됨 → 나머지 ${diff.toLocaleString()}개는 '미지정 재고'로 경고 표시됩니다.`;
            } else {
                bar.className = 'text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border bg-rose-50 border-rose-200 text-rose-900';
                bar.innerText = `ℹ️ 수동 로트 합계(${sum.toLocaleString()}개)가 현재 개미 CSV(${currentEditGaemiCsvTotal.toLocaleString()}개)를 초과합니다. 차기 CSV 업로드 시 초과분이 FIFO로 자동 차감됩니다.`;
            }
        }

        function addCoupangOptionRow(opt = {}) {
            const container = document.getElementById('ep_options_container');
            const row = document.createElement('div');
            row.className = 'ep-opt-row flex flex-wrap items-center gap-2 bg-white border border-slate-200 rounded-lg p-2';
            row.innerHTML = `
                <input type="text" class="ep-opt-id w-36 px-2.5 py-1 border border-slate-300 rounded font-mono text-[11px]" placeholder="쿠팡 옵션 ID" value="${escapeHtml(opt.id || '')}">
                <div class="flex items-center space-x-1">
                    <input type="number" min="1" class="ep-opt-bundle w-16 px-2 py-1 border border-slate-300 rounded font-mono text-[11px] text-right" value="${opt.bundle || 1}">
                    <span class="text-[11px] text-slate-500">개입</span>
                </div>
                <input type="text" class="ep-opt-name flex-1 min-w-[100px] px-2.5 py-1 border border-slate-300 rounded text-[11px]" placeholder="옵션명 (예: 2개)" value="${escapeHtml(opt.name || '')}">
                <button type="button" onclick="this.closest('.ep-opt-row').remove()" class="px-2 py-1 bg-rose-50 hover:bg-rose-100 text-rose-700 rounded text-[10px] font-bold">삭제</button>
            `;
            container.appendChild(row);
        }

        function renderEpTempStocks(csvLots, options) {
            const box = document.getElementById('ep_temp_stocks_container');
            let html = '';
            if (csvLots && csvLots.length > 0) {
                html += `<div class="text-[11px] font-bold text-amber-900">개미창고(3PL) 임시 재고 보정</div>`;
                html += csvLots.map(l => `
                    <div class="flex items-center justify-between gap-2 bg-white border border-rose-200 rounded px-2.5 py-1.5">
                        <span class="text-[11px] text-slate-700 truncate">${escapeHtml(l.option_name || l.gaemi_code)} (현재 ${l.stock}개)</span>
                        <input type="number" min="0" class="ep-temp-gaemi w-24 px-2 py-1 border border-slate-300 rounded font-mono text-[11px] text-right" data-lot-key="${escapeHtml(l.lot_key)}" placeholder="${l.stock}">
                    </div>
                `).join('');
            }
            if (options && options.length > 0) {
                html += `<div class="text-[11px] font-bold text-blue-900 mt-2">쿠팡 창고(세트 기준) 임시 재고 보정</div>`;
                html += options.map(o => `
                    <div class="flex items-center justify-between gap-2 bg-white border border-rose-200 rounded px-2.5 py-1.5">
                        <span class="text-[11px] text-slate-700 truncate">${escapeHtml(o.name)} [${escapeHtml(o.id)}] (현재 ${o.coupang_stock || 0}세트)</span>
                        <input type="number" min="0" class="ep-temp-coupang w-24 px-2 py-1 border border-slate-300 rounded font-mono text-[11px] text-right" data-opt-id="${escapeHtml(o.id)}" placeholder="${o.coupang_stock || 0}">
                    </div>
                `).join('');
            }
            if (!html) {
                html = '<div class="text-[11px] text-slate-400">보정 가능한 창고 로트 또는 쿠팡 옵션 ID가 없습니다. 먼저 위 4번에서 개미/쿠팡 코드를 연결하세요.</div>';
            }
            box.innerHTML = html;
        }

        async function saveProductModal() {
            const sellerCode = document.getElementById('ep_seller_code').value.trim();
            const name = document.getElementById('ep_name').value.trim();
            if (!sellerCode || !name) {
                alert('판매자관리코드와 마스터 상품명은 필수입니다.');
                return;
            }

            const options = [];
            document.querySelectorAll('#ep_options_container .ep-opt-row').forEach(r => {
                const id = r.querySelector('.ep-opt-id').value.trim();
                const bundle = parseInt(r.querySelector('.ep-opt-bundle').value || '1', 10) || 1;
                const oname = r.querySelector('.ep-opt-name').value.trim() || `${bundle}개`;
                if (id) options.push({ id, bundle, name: oname });
            });

            const manualLots = [];
            document.querySelectorAll('#ep_manual_lots_container .ep-manual-lot-row').forEach(r => {
                const lotId = r.querySelector('.ep-ml-id').value.trim();
                const label = r.querySelector('.ep-ml-label').value.trim() || '로트';
                const exp = r.querySelector('.ep-ml-expiry').value.trim();
                const qty = parseInt(r.querySelector('.ep-ml-qty').value || '0', 10) || 0;
                if (exp || qty > 0) manualLots.push({ lot_id: lotId, label, expiry_date: exp, qty });
            });

            const csvLotExpiries = {};
            document.querySelectorAll('#ep_csv_lots_container .ep-csv-lot-input').forEach(inp => {
                const k = inp.dataset.lotKey;
                const mode = inp.dataset.mode || '';
                const wasAdmin = inp.dataset.wasAdmin === '1';
                const rawExp = (inp.dataset.rawExpiry || '').trim();
                const curVal = inp.value.trim();
                if (!k) return;
                if (mode === '__RESTORE__') csvLotExpiries[k] = '__RESTORE__';
                else if (mode === '__CLEAR__') csvLotExpiries[k] = '__CLEAR__';
                else if (wasAdmin || curVal !== rawExp) csvLotExpiries[k] = curVal;
            });

            const tempGaemiStocks = {};
            document.querySelectorAll('#ep_temp_stocks_container .ep-temp-gaemi').forEach(inp => {
                if (inp.value !== '') tempGaemiStocks[inp.dataset.lotKey] = parseInt(inp.value, 10);
            });
            const tempCoupangStocks = {};
            document.querySelectorAll('#ep_temp_stocks_container .ep-temp-coupang').forEach(inp => {
                if (inp.value !== '') tempCoupangStocks[inp.dataset.optId] = parseInt(inp.value, 10);
            });

            const isNewFlag = document.getElementById('ep_is_new').value === '1';
            const scCur = (document.getElementById('ep_sales_codes').value || '').trim();

            const payload = {
                is_new: isNewFlag,
                id: document.getElementById('ep_id').value.trim(),
                original_seller_code: document.getElementById('ep_orig_seller_code').value.trim(),
                seller_code: sellerCode,
                name: name,
                product_type: document.getElementById('ep_product_type').value,
                category: document.getElementById('ep_category').value.trim() || '기타',
                is_active: document.getElementById('ep_is_active').checked,
                note: document.getElementById('ep_note').value.trim(),
                lead_time_days: document.getElementById('ep_lead_time_days').value.trim(),
                safety_buffer_days: document.getElementById('ep_safety_buffer_days').value.trim(),
                expiry_warn_days: document.getElementById('ep_expiry_warn_days').value.trim(),
                in_transit_qty: parseInt(document.getElementById('ep_in_transit_qty').value || '0', 10) || 0,
                expected_arrival_date: document.getElementById('ep_expected_arrival_date').value.trim(),
                default_expiry_date: document.getElementById('ep_default_expiry_date').value.trim(),
                gaemi_codes: document.getElementById('ep_gaemi_codes').value.trim(),
                gaemi_filter: document.getElementById('ep_gaemi_filter').value.trim(),
                options: options,
                manual_lots: manualLots,
                csv_lot_expiries: csvLotExpiries,
                temp_gaemi_stocks: tempGaemiStocks,
                temp_coupang_stocks: tempCoupangStocks
            };

            if (isNewFlag) {
                if (scCur) payload.sales_codes = scCur;
            } else if (epSalesExplicitEmpty) {
                if (scCur) payload.sales_codes = scCur;
            } else if (scCur !== epSalesOrigState) {
                payload.sales_codes = scCur ? scCur : null;
            }

            if (Array.isArray(epGaemiMappingsState) && epGaemiMappingsState.length > 0) {
                payload.gaemi_mappings = epGaemiMappingsState;
            } else if (epGaemiClearedExplicit) {
                payload.gaemi_mappings = [];
            }

            try {
                const res = await adminFetch('/api/admin/product/save', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '저장 실패');
                    return;
                }
                closeEditProductModal();
                await fetchSummary();
                if (adminConfigCache || !document.getElementById('adminCenterModal').classList.contains('hidden')) {
                    await ensureAdminConfig();
                    if (!document.getElementById('adminCenterModal').classList.contains('hidden')) {
                        renderAdminCenterTabs();
                    }
                }
                alert('✅ ' + json.message);
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('저장 오류: ' + err.message);
            }
        }

        async function toggleHideCurrentProduct() {
            const btn = document.getElementById('ep_btn_toggle_hide');
            const isCurrentlyActive = btn.dataset.isActive === '1';
            const mode = isCurrentlyActive ? 'hide' : 'restore';
            const msg = isCurrentlyActive
                ? '이 상품을 메인 대시보드에서 숨기시겠습니까? (관리자 센터에서 언제든 복원 가능)'
                : '이 상품을 메인 대시보드에 다시 노출하시겠습니까?';
            if (!confirm(msg)) return;
            await executeDeleteProduct(mode);
        }

        async function deleteCurrentProductHard() {
            const code = document.getElementById('ep_seller_code').value.trim();
            if (!confirm(`[${code}] 상품을 마스터 목록에서 영구 삭제하시겠습니까?\\n이 작업은 되돌릴 수 없습니다.`)) return;
            await executeDeleteProduct('hard');
        }

        async function executeDeleteProduct(mode) {
            try {
                const res = await adminFetch('/api/admin/product/delete', {
                    method: 'POST',
                    body: JSON.stringify({
                        id: document.getElementById('ep_id').value.trim(),
                        seller_code: document.getElementById('ep_orig_seller_code').value.trim(),
                        mode: mode
                    })
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '처리 실패');
                    return;
                }
                closeEditProductModal();
                await fetchSummary();
                if (!document.getElementById('adminCenterModal').classList.contains('hidden')) {
                    await ensureAdminConfig();
                    renderAdminCenterTabs();
                }
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('처리 오류: ' + err.message);
            }
        }

        // -------------------------------------------------------------
        // 관리자 통합 센터 (6개 탭) 로직
        // -------------------------------------------------------------
        let activeAdminTab = 'bulk';

        async function openAdminCenter(tabName = 'bulk') {
            if (!isAdminMode) { openPinModal(); return; }
            document.getElementById('adminCenterModal').classList.remove('hidden');
            switchAdminTab(tabName);
            try {
                await ensureAdminConfig();
                renderAdminCenterTabs();
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('관리자 설정 불러오기 실패: ' + err.message);
            }
        }

        function closeAdminCenter() {
            const m = document.getElementById('adminCenterModal');
            if (m) m.classList.add('hidden');
            fetchSummary();
        }

        function switchAdminTab(tabName) {
            activeAdminTab = tabName;
            ['bulk', 'mapping', 'expiry', 'b2b', 'settings', 'drive'].forEach(t => {
                const pane = document.getElementById(`acTab_${t}`);
                if (pane) pane.classList.toggle('hidden', t !== tabName);
            });
            document.querySelectorAll('.ac-tab-btn').forEach(btn => {
                const isActive = btn.dataset.tab === tabName;
                btn.className = isActive
                    ? 'ac-tab-btn px-3.5 py-2 rounded-t-lg font-extrabold border border-b-0 bg-white text-slate-900 shadow-xs'
                    : 'ac-tab-btn px-3.5 py-2 rounded-t-lg font-bold border border-b-0 bg-slate-200/60 text-slate-600 hover:bg-white';
            });
        }

        function renderAdminCenterTabs() {
            if (!adminConfigCache) return;
            renderAdminBulkTable();
            renderAdminMappingTab();
            renderAdminExpiryTable();
            renderAdminB2bTable();
            renderAdminSettingsTab();
            renderAdminDriveTab();
        }

        function renderAdminBulkTable() {
            if (!adminConfigCache) return;
            const tbody = document.getElementById('acBulkTbody');
            const q = (document.getElementById('acBulkSearch').value || '').toLowerCase().trim();
            const showInactive = document.getElementById('acBulkShowInactive').checked;
            const items = (adminConfigCache.master_products || []).filter(m => {
                if (!showInactive && m.is_active === false) return false;
                if (q) {
                    const mName = (m.name || '').toLowerCase();
                    const mCode = (m.seller_code || '').toLowerCase();
                    if (!mName.includes(q) && !mCode.includes(q)) return false;
                }
                return true;
            });

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" class="p-6 text-center text-slate-400">표시할 품목이 없습니다.</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(m => {
                const isAct = m.is_active !== false;
                return `
                    <tr class="ac-bulk-row ${isAct ? 'hover:bg-slate-50' : 'bg-slate-100/70 opacity-75'}" data-id="${escapeHtml(m.id || '')}" data-seller-code="${escapeHtml(m.seller_code)}">
                        <td class="p-1.5 font-mono font-bold text-slate-700">${escapeHtml(m.seller_code)}</td>
                        <td class="p-1.5"><input type="text" class="ac-b-name w-full px-2 py-1 border border-slate-300 rounded font-semibold" value="${escapeHtml(m.name || '')}"></td>
                        <td class="p-1.5">
                            <select class="ac-b-type w-full px-1.5 py-1 border border-slate-300 rounded bg-white">
                                <option value="수입" ${m.product_type === '수입' ? 'selected' : ''}>수입</option>
                                <option value="사입" ${m.product_type !== '수입' ? 'selected' : ''}>사입</option>
                            </select>
                        </td>
                        <td class="p-1.5"><input type="text" list="categorySuggestions" class="ac-b-cat w-full px-2 py-1 border border-slate-300 rounded" value="${escapeHtml(m.category || '')}"></td>
                        <td class="p-1.5"><input type="number" min="0" class="ac-b-lt w-full px-1.5 py-1 border border-slate-300 rounded font-mono text-right" placeholder="기본" value="${(m.lead_time_days !== null && m.lead_time_days !== undefined) ? m.lead_time_days : ''}"></td>
                        <td class="p-1.5"><input type="number" min="0" class="ac-b-transit w-full px-1.5 py-1 border border-slate-300 rounded font-mono text-right" value="${m.in_transit_qty || 0}"></td>
                        <td class="p-1.5"><input type="date" class="ac-b-arr w-full px-1.5 py-1 border border-slate-300 rounded font-mono text-[11px]" value="${escapeHtml(m.expected_arrival_date || '')}"></td>
                        <td class="p-1.5"><input type="date" class="ac-b-defexp w-full px-1.5 py-1 border border-slate-300 rounded font-mono text-[11px]" value="${escapeHtml(m.default_expiry_date || '')}"></td>
                        <td class="p-1.5 text-center"><input type="checkbox" class="ac-b-active" ${isAct ? 'checked' : ''}></td>
                        <td class="p-1.5"><input type="text" class="ac-b-note w-full px-2 py-1 border border-slate-300 rounded" value="${escapeHtml(m.note || '')}"></td>
                    </tr>
                `;
            }).join('');
        }

        async function saveAdminBulkProducts() {
            const products = [];
            document.querySelectorAll('#acBulkTbody .ac-bulk-row').forEach(tr => {
                products.push({
                    id: tr.dataset.id,
                    seller_code: tr.dataset.sellerCode,
                    name: tr.querySelector('.ac-b-name').value.trim(),
                    product_type: tr.querySelector('.ac-b-type').value,
                    category: tr.querySelector('.ac-b-cat').value.trim(),
                    lead_time_days: tr.querySelector('.ac-b-lt').value.trim(),
                    in_transit_qty: parseInt(tr.querySelector('.ac-b-transit').value || '0', 10) || 0,
                    expected_arrival_date: tr.querySelector('.ac-b-arr').value.trim(),
                    default_expiry_date: tr.querySelector('.ac-b-defexp').value.trim(),
                    is_active: tr.querySelector('.ac-b-active').checked,
                    note: tr.querySelector('.ac-b-note').value.trim()
                });
            });
            try {
                const res = await adminFetch('/api/admin/products/bulk-save', {
                    method: 'POST',
                    body: JSON.stringify({ products })
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '일괄 저장 실패');
                    return;
                }
                await ensureAdminConfig();
                await fetchSummary();
                renderAdminCenterTabs();
                alert('✅ ' + json.message);
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('일괄 저장 오류: ' + err.message);
            }
        }

        function renderAdminExpiryTable() {
            if (!adminConfigCache) return;
            const tbody = document.getElementById('acExpiryTbody');
            const mode = document.getElementById('acExpiryFilterMode').value;
            const q = (document.getElementById('acExpirySearch').value || '').toLowerCase().trim();
            const b2bMap = adminConfigCache.b2b_mappings || {};

            const enriched = (adminConfigCache.master_products || [])
                .filter(m => {
                    const c = (m.seller_code || '').toUpperCase();
                    return m.is_active !== false && !c.startsWith('S') && !b2bMap[c];
                })
                .map(m => {
                    const sItem = allMasterData.find(x => x.id === m.id || x.seller_code === m.seller_code) || {};
                    const rawCsvLots = sItem.csv_gaemi_lots || computeCsvLotsFromCache(m, adminConfigCache);
                    const activeCsvLots = rawCsvLots.filter(l => (l.stock || 0) > 0);
                    const manLots = m.manual_lots || [];
                    const activeManLots = manLots.filter(l => (l.qty || 0) > 0);
                    const hasManualLots = manLots.length > 0;
                    const gTotal = sItem.gaemi_total !== undefined ? sItem.gaemi_total : activeCsvLots.reduce((a, b) => a + (b.stock || 0), 0);
                    const nearestExp = sItem.nearest_expiry_date || m.default_expiry_date || '';
                    const nearestDays = sItem.nearest_days_left;
                    const hasUnassigned = Boolean(sItem.has_unassigned_stock);
                    return { m, rawCsvLots, activeCsvLots, manLots, activeManLots, hasManualLots, gTotal, nearestExp, nearestDays, hasUnassigned };
                })
                .filter(({ m, activeCsvLots, activeManLots, hasManualLots, nearestExp, nearestDays, hasUnassigned }) => {
                    if (q) {
                        const mn = (m.name || '').toLowerCase();
                        const mc = (m.seller_code || '').toLowerCase();
                        if (!mn.includes(q) && !mc.includes(q)) return false;
                    }
                    if (mode === 'missing') {
                        if (hasManualLots) {
                            return !nearestExp || activeManLots.some(l => !l.expiry_date) || hasUnassigned;
                        }
                        if (activeCsvLots.length > 0) {
                            return activeCsvLots.some(l => !l.expiry_date);
                        }
                        return !nearestExp;
                    } else if (mode === 'soon') {
                        return nearestDays !== null && nearestDays !== undefined && nearestDays <= 180;
                    }
                    return true;
                });

            if (enriched.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="p-6 text-center text-slate-400">조건에 해당하는 품목이 없습니다.</td></tr>';
                return;
            }

            tbody.innerHTML = enriched.map(({ m, rawCsvLots, activeCsvLots, activeManLots, hasManualLots, gTotal, nearestExp, nearestDays }) => {
                let lotSummaryHtml = '';
                let inputColHtml = '';
                if (!hasManualLots && activeCsvLots.length > 0) {
                    const dupOptNames = {};
                    activeCsvLots.forEach(l => {
                        const k = String(l.option_name || '옵션명 없음').trim();
                        dupOptNames[k] = (dupOptNames[k] || 0) + 1;
                    });
                    lotSummaryHtml = activeCsvLots.map(l => {
                        const optTxt = String(l.option_name || '옵션명 없음').trim();
                        const regHint = (dupOptNames[optTxt] > 1 && l.reg_date)
                            ? ` <span class="text-[10px] text-slate-400 font-mono">(등록 ${escapeHtml(String(l.reg_date).slice(0, 10))})</span>`
                            : '';
                        return `<div class="text-[11px] text-slate-600 truncate">• ${escapeHtml(optTxt)} (${(l.stock || 0).toLocaleString()}개)${regHint}</div>`;
                    }).join('');
                    inputColHtml = activeCsvLots.map(l => `
                        <div class="mb-1 flex items-center gap-1">
                            <input type="date" class="ac-exp-lot-input w-full px-2 py-1 border border-slate-300 rounded font-mono text-[11px]" data-lot-key="${escapeHtml(l.lot_key)}" data-orig="${escapeHtml(l.expiry_date || '')}" value="${escapeHtml(l.expiry_date || '')}">
                        </div>
                    `).join('');
                } else if (hasManualLots) {
                    lotSummaryHtml = activeManLots.length > 0
                        ? activeManLots.map(l => `<div class="text-[11px] text-amber-800">• ${escapeHtml(l.label)}: ${(l.qty || 0).toLocaleString()}개 (${escapeHtml(l.expiry_date || '미지정')})</div>`).join('')
                        : '<span class="text-[11px] text-slate-400">수동 로트 보유 재고 0개</span>';
                    inputColHtml = '<div class="text-[11px] text-amber-800 font-semibold py-1">우측 [상세 분할]에서 로트별 수정</div>';
                } else {
                    lotSummaryHtml = rawCsvLots.length > 0
                        ? '<span class="text-[11px] text-slate-400">개미 보유 재고 0개 (기본 소비기한 적용)</span>'
                        : '<span class="text-[11px] text-slate-400">개미 CSV 로트 없음 (기본 소비기한 적용)</span>';
                    inputColHtml = `
                        <input type="date" class="ac-exp-default-input w-full px-2 py-1 border border-slate-300 rounded font-mono text-[11px]" data-id="${escapeHtml(m.id || '')}" data-seller-code="${escapeHtml(m.seller_code)}" data-orig="${escapeHtml(m.default_expiry_date || '')}" value="${escapeHtml(m.default_expiry_date || '')}">
                    `;
                }
                const dBadge = (nearestDays !== null && nearestDays !== undefined)
                    ? `<span class="text-[10px] font-mono ${nearestDays <= 120 ? 'text-rose-700 font-bold' : 'text-slate-500'}">(D-${nearestDays}일)</span>`
                    : `<span class="text-[10px] text-rose-600 font-bold">미등록</span>`;

                return `
                    <tr class="hover:bg-slate-50">
                        <td class="p-2 font-mono font-bold text-slate-700 align-top">${escapeHtml(m.seller_code)}</td>
                        <td class="p-2 align-top">
                            <div class="font-bold text-slate-800">${escapeHtml(m.name)}</div>
                            <div class="text-[10px] text-slate-500 mt-0.5">개미 재고: ${gTotal.toLocaleString()}개 · ${dBadge}</div>
                        </td>
                        <td class="p-2 align-top">${lotSummaryHtml}</td>
                        <td class="p-2 align-top">${inputColHtml}</td>
                        <td class="p-2 text-center align-top">
                            <button type="button" data-prod-id="${escapeHtml(m.id || m.seller_code)}" onclick="openEditProductModal(this.dataset.prodId)" class="px-2.5 py-1 bg-amber-100 hover:bg-amber-200 text-amber-950 rounded font-bold text-[11px]">상세 분할</button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        async function saveAdminExpiryBulk() {
            const products = [];
            const lotOverrides = {};

            document.querySelectorAll('#acExpiryTbody .ac-exp-default-input').forEach(inp => {
                const v = inp.value.trim();
                const orig = inp.dataset.orig || '';
                if (v !== orig) {
                    products.push({
                        id: inp.dataset.id,
                        seller_code: inp.dataset.sellerCode,
                        default_expiry_date: v
                    });
                }
            });

            document.querySelectorAll('#acExpiryTbody .ac-exp-lot-input').forEach(inp => {
                const v = inp.value.trim();
                const orig = inp.dataset.orig || '';
                const key = inp.dataset.lotKey;
                if (key && v !== orig) {
                    lotOverrides[key] = v ? v : '__CLEAR__';
                }
            });

            if (products.length === 0 && Object.keys(lotOverrides).length === 0) {
                alert('변경된 소비기한 항목이 없습니다.');
                return;
            }

            try {
                const res = await adminFetch('/api/admin/products/bulk-save', {
                    method: 'POST',
                    body: JSON.stringify({ products, lot_expiry_overrides: lotOverrides })
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '소비기한 일괄 저장 실패');
                    return;
                }
                await ensureAdminConfig();
                await fetchSummary();
                renderAdminCenterTabs();
                alert('✅ 소비기한 일괄 반영 완료!');
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('소비기한 저장 오류: ' + err.message);
            }
        }

        function renderAdminB2bTable() {
            if (!adminConfigCache) return;
            const tbody = document.getElementById('acB2bTbody');
            tbody.innerHTML = '';
            const map = adminConfigCache.b2b_mappings || {};
            const entries = Object.entries(map);
            if (entries.length === 0) {
                addAdminB2bRow();
            } else {
                entries.forEach(([sCode, info]) => {
                    addAdminB2bRow({
                        s_code: sCode,
                        base_code: info.base_code || '',
                        multiplier: info.multiplier || 1,
                        desc: info.desc || ''
                    });
                });
            }
        }

        function addAdminB2bRow(row = {}) {
            const tbody = document.getElementById('acB2bTbody');
            const tr = document.createElement('tr');
            tr.className = 'ac-b2b-row hover:bg-slate-50';
            tr.innerHTML = `
                <td class="p-2"><input type="text" class="ac-b2b-s w-full px-2 py-1 border border-slate-300 rounded font-mono font-bold uppercase" placeholder="예: S100005" value="${escapeHtml(row.s_code || '')}"></td>
                <td class="p-2"><input type="text" class="ac-b2b-base w-full px-2 py-1 border border-slate-300 rounded font-mono font-bold uppercase" placeholder="예: 10GL02" value="${escapeHtml(row.base_code || '')}"></td>
                <td class="p-2"><input type="number" min="1" class="ac-b2b-mult w-full px-2 py-1 border border-slate-300 rounded font-mono text-right font-bold" value="${row.multiplier || 1}"></td>
                <td class="p-2"><input type="text" class="ac-b2b-desc w-full px-2 py-1 border border-slate-300 rounded" placeholder="설명" value="${escapeHtml(row.desc || '')}"></td>
                <td class="p-2 text-center"><button type="button" onclick="this.closest('tr').remove()" class="px-2 py-1 bg-rose-50 hover:bg-rose-100 text-rose-700 rounded font-bold">삭제</button></td>
            `;
            tbody.appendChild(tr);
        }

        async function saveAdminB2bMappings() {
            const mappings = [];
            document.querySelectorAll('#acB2bTbody .ac-b2b-row').forEach(tr => {
                const s_code = tr.querySelector('.ac-b2b-s').value.trim().toUpperCase();
                const base_code = tr.querySelector('.ac-b2b-base').value.trim().toUpperCase();
                const multiplier = parseInt(tr.querySelector('.ac-b2b-mult').value || '1', 10) || 1;
                const desc = tr.querySelector('.ac-b2b-desc').value.trim();
                if (s_code && base_code) mappings.push({ s_code, base_code, multiplier, desc });
            });
            try {
                const res = await adminFetch('/api/admin/b2b-mappings/save', {
                    method: 'POST',
                    body: JSON.stringify({ mappings })
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || 'B2B 매핑 저장 실패');
                    return;
                }
                await ensureAdminConfig();
                await fetchSummary();
                renderAdminCenterTabs();
                alert('✅ ' + json.message);
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('B2B 매핑 오류: ' + err.message);
            }
        }

        function renderAdminSettingsTab() {
            if (!adminConfigCache) return;
            const s = adminConfigCache.settings || {};
            document.getElementById('acSetImportLt').value = s.default_import_lead_time ?? 110;
            document.getElementById('acSetImportBuf').value = s.default_import_buffer ?? 20;
            document.getElementById('acSetPurchaseLt').value = s.default_purchase_lead_time ?? 7;
            document.getElementById('acSetPurchaseBuf').value = s.default_purchase_buffer ?? 7;
            document.getElementById('acSetExpWarn').value = s.default_expiry_warn_days ?? 120;
            document.getElementById('acSetNewPin').value = '';

            const src = adminConfigCache.pin_source || 'default';
            const infoEl = document.getElementById('acSetPinSourceInfo');
            if (src === 'env') {
                infoEl.innerHTML = '🔒 현재 PIN 적용 방식: <strong>Render 환경변수 (ADMIN_PIN)</strong> 적용 중입니다. (환경변수 변경은 Render 대시보드에서 가능)';
            } else if (src === 'custom') {
                infoEl.innerHTML = '🔑 현재 PIN 적용 방식: <strong>관리자 커스텀 PIN</strong>이 저장되어 있습니다.';
            } else {
                infoEl.innerHTML = 'ℹ️ 현재 <strong>초기 기본 PIN</strong> 상태입니다. 보안을 위해 아래에서 새 PIN을 지정하거나 Render 환경변수 <code>ADMIN_PIN</code>을 설정하는 것을 권장합니다.';
            }
        }

        async function saveAdminSettings() {
            const payload = {
                default_import_lead_time: parseInt(document.getElementById('acSetImportLt').value || '110', 10),
                default_import_buffer: parseInt(document.getElementById('acSetImportBuf').value || '20', 10),
                default_purchase_lead_time: parseInt(document.getElementById('acSetPurchaseLt').value || '7', 10),
                default_purchase_buffer: parseInt(document.getElementById('acSetPurchaseBuf').value || '7', 10),
                default_expiry_warn_days: parseInt(document.getElementById('acSetExpWarn').value || '120', 10),
                new_pin: document.getElementById('acSetNewPin').value.trim()
            };
            try {
                const res = await adminFetch('/api/admin/settings/save', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '설정 저장 실패');
                    return;
                }
                if (json.new_token) {
                    adminToken = json.new_token;
                    try { sessionStorage.setItem(ADMIN_TOKEN_KEY, adminToken); } catch (e) {}
                }
                await ensureAdminConfig();
                await fetchSummary();
                renderAdminCenterTabs();
                alert('✅ ' + json.message);
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('설정 저장 오류: ' + err.message);
            }
        }

        function renderAdminDriveTab() {
            if (!adminConfigCache) return;
            const ds = adminConfigCache.drive_sync || {};
            const box = document.getElementById('acDriveStatusBox');
            const configuredLabel = ds.configured
                ? '<span class="text-emerald-700 font-bold">✅ 환경변수 연결됨</span>'
                : '<span class="text-amber-700 font-bold">⚠️ 미설정 (현재 Render 로컬 캐시만 사용)</span>';
            box.innerHTML = `
                <div><strong>연동 상태:</strong> ${configuredLabel} (${escapeHtml(ds.status || '-')})</div>
                <div><strong>마지막 Drive 백업:</strong> <span class="font-mono">${escapeHtml(ds.last_synced_at || '기록 없음')}</span></div>
                <div><strong>마지막 Drive 복원:</strong> <span class="font-mono">${escapeHtml(ds.last_loaded_at || '기록 없음')}</span></div>
                ${ds.file_id ? `<div><strong>Drive 파일 ID:</strong> <span class="font-mono text-[11px]">${escapeHtml(ds.file_id)}</span></div>` : ''}
                ${ds.last_error ? `<div class="text-rose-600 font-bold"><strong>최근 오류:</strong> ${escapeHtml(ds.last_error)}</div>` : ''}
            `;
            document.getElementById('acGasCodePreview').textContent = adminConfigCache.gas_script || '// google_apps_script_bridge.gs 파일 내용을 불러올 수 없습니다.';
        }

        async function syncDriveNow() {
            try {
                const res = await adminFetch('/api/admin/drive/sync-now', { method: 'POST' });
                const json = await res.json();
                await ensureAdminConfig();
                renderAdminDriveTab();
                fetchSummary();
                alert(json.success ? ('✅ ' + json.message) : ('⚠️ ' + json.message));
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('Drive 저장 오류: ' + err.message);
            }
        }

        async function pullDriveNow() {
            if (!confirm('Google Drive에 저장된 최신 백업으로 현재 서버의 모든 데이터를 덮어쓰고 복원하시겠습니까?')) return;
            try {
                const res = await adminFetch('/api/admin/drive/pull-now', { method: 'POST' });
                const json = await res.json();
                await ensureAdminConfig();
                renderAdminCenterTabs();
                await fetchSummary();
                alert(json.success ? ('✅ ' + json.message) : ('⚠️ ' + json.message));
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('Drive 복원 오류: ' + err.message);
            }
        }

        async function downloadBackupJson() {
            try {
                const res = await adminFetch('/api/admin/backup/export', { method: 'GET' });
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `iljeomodo_backup_${new Date().toISOString().slice(0, 10)}.json`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('백업 다운로드 오류: ' + err.message);
            }
        }

        function importBackupJsonFile(inputEl) {
            const file = inputEl.files && inputEl.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = async function(e) {
                try {
                    const parsed = JSON.parse(e.target.result);
                    if (!confirm(`'${file.name}' 백업 파일로 전체 마스터/재고 데이터를 복원하시겠습니까?`)) return;
                    const res = await adminFetch('/api/admin/backup/import', {
                        method: 'POST',
                        body: JSON.stringify({ data: parsed })
                    });
                    const json = await res.json();
                    if (!res.ok || !json.success) {
                        alert(json.message || '백업 복원 실패');
                        return;
                    }
                    await ensureAdminConfig();
                    renderAdminCenterTabs();
                    await fetchSummary();
                    alert('✅ ' + json.message);
                } catch (err) {
                    if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('JSON 백업 파일 파싱/복원 오류: ' + err.message);
                } finally {
                    inputEl.value = '';
                }
            };
            reader.readAsText(file, 'utf-8');
        }

        function copyGasScript() {
            const code = (adminConfigCache && adminConfigCache.gas_script) || document.getElementById('acGasCodePreview').textContent;
            if (!code) {
                alert('복사할 스크립트 내용이 없습니다.');
                return;
            }
            const fallbackCopy = () => {
                const ta = document.createElement('textarea');
                ta.value = code;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                const ok = document.execCommand('copy');
                ta.remove();
                if (ok) alert('Google Apps Script (.gs) 전체 코드가 클립보드에 복사되었습니다!');
                else alert('자동 복사가 차단되었습니다. 아래 코드 영역을 직접 선택해 복사해주세요.');
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(code).then(() => {
                    alert('Google Apps Script (.gs) 전체 코드가 클립보드에 복사되었습니다!');
                }).catch(fallbackCopy);
            } else {
                fallbackCopy();
            }
        }

        // -------------------------------------------------------------
        // 상품 3채널 코드 매핑 수동 지정 (탐색기 + 마스터 현황표 + 전용 모달)
        // -------------------------------------------------------------
        let acMapExplorerRows = [];
        let cmState = {
            id: '',
            seller_code: '',
            name: '',
            sales_codes: [],
            sales_is_custom: false,
            gaemi_mappings: [],
            clear_legacy_gaemi: false,
            legacy_gaemi_codes: [],
            legacy_gaemi_filter: '',
            options: []
        };
        let cmSalesCsvItems = [];
        let cmGaemiCsvItems = [];
        let cmCoupangCsvItems = [];

        function isMasterSCodeRecord(m, cfg) {
            const c = String((m && m.seller_code) || '').trim().toUpperCase();
            const b2b = (cfg && cfg.b2b_mappings) || {};
            return c.startsWith('S') || Boolean(b2b[c]);
        }

        function guessCoupangBundle(text) {
            const s = String(text || '');
            const m1 = s.match(/(\\d+)\\s*개(?!입)/);
            if (m1) return Math.max(1, parseInt(m1[1], 10) || 1);
            const m2 = s.match(/(\\d+)\\s*(?:팩|입|병|봉|통|박스)/);
            if (m2) return Math.max(1, parseInt(m2[1], 10) || 1);
            return 1;
        }

        function getGroupedGaemiEntries(cfg) {
            const gaemiInv = (cfg && cfg.gaemi_inventory) || {};
            const map = new Map();
            Object.entries(gaemiInv).forEach(([gCode, rows]) => {
                (rows || []).forEach(r => {
                    const optName = String(r.option_name || '').trim();
                    const key = `${String(gCode).trim().toUpperCase()}||${optName}`;
                    if (!map.has(key)) {
                        map.set(key, {
                            key,
                            gaemi_code: String(gCode).trim(),
                            option_name: optName,
                            product_name: String(r.product_name || '').trim(),
                            stock: 0,
                            lot_count: 0
                        });
                    }
                    const entry = map.get(key);
                    const rowStock = parseInt(r.stock || 0, 10) || 0;
                    entry.stock += rowStock;
                    if (rowStock > 0) entry.lot_count += 1;
                    if (!entry.product_name && r.product_name) {
                        entry.product_name = String(r.product_name).trim();
                    }
                });
            });
            return Array.from(map.values()).sort((a, b) => {
                const cCmp = a.gaemi_code.localeCompare(b.gaemi_code);
                return cCmp !== 0 ? cCmp : a.option_name.localeCompare(b.option_name);
            });
        }

        function getMatchedGaemiEntriesForMaster(m, groupedEntries) {
            const gMappings = m.gaemi_mappings;
            if (Array.isArray(gMappings) && gMappings.length > 0) {
                const matched = [];
                const seenKeys = new Set();
                gMappings.forEach(rule => {
                    if (!rule || typeof rule !== 'object') return;
                    const reqCode = String(rule.gaemi_code || rule.code || '').trim().toUpperCase();
                    const reqOpt = String(rule.option_name || '').trim();
                    if (!reqCode) return;
                    groupedEntries.forEach(entry => {
                        if (entry.gaemi_code.toUpperCase() !== reqCode) return;
                        if (reqOpt === '' || reqOpt === '*' || entry.option_name === reqOpt) {
                            if (!seenKeys.has(entry.key)) {
                                seenKeys.add(entry.key);
                                matched.push(entry);
                            }
                        }
                    });
                });
                return { matched, usedLegacy: false };
            }

            const legacyCodes = (m.gaemi_codes || []).map(c => String(c || '').trim()).filter(Boolean);
            const optFilter = String(m.gaemi_filter || '').trim();
            const matched = [];
            const seenKeys = new Set();
            legacyCodes.forEach(lc => {
                groupedEntries.forEach(entry => {
                    if (entry.gaemi_code !== lc) return;
                    if (optFilter && !entry.option_name.includes(optFilter)) return;
                    if (!seenKeys.has(entry.key)) {
                        seenKeys.add(entry.key);
                        matched.push(entry);
                    }
                });
            });
            return { matched, usedLegacy: legacyCodes.length > 0 };
        }

        function buildMappingOwnershipIndex(cfg) {
            const masters = (cfg && cfg.master_products) || [];
            const b2bMap = (cfg && cfg.b2b_mappings) || {};
            const groupedGaemi = getGroupedGaemiEntries(cfg);

            const salesOwners = {};
            const gaemiOwners = {};
            const coupangOwners = {};
            const b2bSalesOwners = {};

            Object.entries(b2bMap).forEach(([sCode, info]) => {
                const sc = String(sCode || '').trim().toUpperCase();
                if (!sc) return;
                b2bSalesOwners[sc] = String((info && info.base_code) || '').trim().toUpperCase();
            });

            masters.forEach(m => {
                if (m.is_active === false || isMasterSCodeRecord(m, cfg)) return;
                const ownerInfo = {
                    id: m.id || '',
                    seller_code: String(m.seller_code || '').trim(),
                    name: String(m.name || '').trim()
                };
                const normCode = ownerInfo.seller_code.toUpperCase();

                const scList = Array.isArray(m.sales_codes)
                    ? m.sales_codes
                    : (normCode ? [normCode] : []);
                scList.forEach(rawSc => {
                    const sc = String(rawSc || '').trim().toUpperCase();
                    if (!sc) return;
                    if (!salesOwners[sc]) salesOwners[sc] = [];
                    if (!salesOwners[sc].some(o => o.seller_code === ownerInfo.seller_code)) {
                        salesOwners[sc].push(ownerInfo);
                    }
                });

                const { matched } = getMatchedGaemiEntriesForMaster(m, groupedGaemi);
                matched.forEach(entry => {
                    if (!gaemiOwners[entry.key]) gaemiOwners[entry.key] = [];
                    if (!gaemiOwners[entry.key].some(o => o.seller_code === ownerInfo.seller_code)) {
                        gaemiOwners[entry.key].push(ownerInfo);
                    }
                });

                (m.options || []).forEach(opt => {
                    const oid = String(opt.id || '').trim();
                    if (!oid) return;
                    if (!coupangOwners[oid]) coupangOwners[oid] = [];
                    if (!coupangOwners[oid].some(o => o.seller_code === ownerInfo.seller_code)) {
                        coupangOwners[oid].push(ownerInfo);
                    }
                });
            });

            return { groupedGaemi, salesOwners, b2bSalesOwners, gaemiOwners, coupangOwners };
        }

        function showAcMapToast(msg, isError = false) {
            const el = document.getElementById('acMapToast');
            if (!el) return;
            el.className = isError
                ? 'px-3 py-2 rounded-lg text-xs font-bold bg-rose-50 border border-rose-300 text-rose-900'
                : 'px-3 py-2 rounded-lg text-xs font-bold bg-emerald-50 border border-emerald-300 text-emerald-900';
            el.innerText = msg;
            el.classList.remove('hidden');
            clearTimeout(window._acMapToastTimer);
            window._acMapToastTimer = setTimeout(() => {
                el.classList.add('hidden');
            }, 3500);
        }

        function renderAdminMappingTab() {
            if (!adminConfigCache) return;
            renderAdminMappingExplorer();
            renderAdminMappingMasterTable();
        }

        function renderAdminMappingExplorer() {
            if (!adminConfigCache) return;
            const cfg = adminConfigCache;
            const idxData = buildMappingOwnershipIndex(cfg);
            const salesInv = cfg.sales_inventory || {};
            const coupangInv = cfg.coupang_inventory || {};

            const allCsvItems = [];
            let salesTotal = 0, salesUnmapped = 0;
            Object.entries(salesInv).sort((a, b) => a[0].localeCompare(b[0])).forEach(([code, sdata]) => {
                const upCode = String(code || '').trim().toUpperCase();
                if (!upCode) return;
                salesTotal += 1;
                const owners = idxData.salesOwners[upCode] || [];
                const b2bBase = idxData.b2bSalesOwners[upCode] || '';
                const isMapped = owners.length > 0 || Boolean(b2bBase) || upCode.startsWith('S');
                if (!isMapped) salesUnmapped += 1;
                allCsvItems.push({
                    channel: 'sales',
                    code: upCode,
                    sampleName: (sdata.sample_names || [])[0] || '',
                    malls: (sdata.malls || []).join(', '),
                    total_7d: sdata.total_7d || 0,
                    total_30d: sdata.total_30d || 0,
                    owners,
                    b2bBase,
                    isMapped,
                    searchBlob: `${upCode} ${(sdata.sample_names || []).join(' ')} ${(sdata.malls || []).join(' ')}`.toLowerCase()
                });
            });

            let gaemiTotal = 0, gaemiUnmapped = 0;
            idxData.groupedGaemi.forEach(entry => {
                gaemiTotal += 1;
                const owners = idxData.gaemiOwners[entry.key] || [];
                const isMapped = owners.length > 0;
                if (!isMapped) gaemiUnmapped += 1;
                allCsvItems.push({
                    channel: 'gaemi',
                    gaemi_code: entry.gaemi_code,
                    option_name: entry.option_name,
                    product_name: entry.product_name,
                    stock: entry.stock,
                    lot_count: entry.lot_count,
                    owners,
                    isMapped,
                    searchBlob: `${entry.gaemi_code} ${entry.option_name} ${entry.product_name}`.toLowerCase()
                });
            });

            let coupangTotal = 0, coupangUnmapped = 0;
            Object.entries(coupangInv).sort((a, b) => a[0].localeCompare(b[0])).forEach(([oid, cdata]) => {
                const oidStr = String(oid || '').trim();
                if (!oidStr) return;
                coupangTotal += 1;
                const owners = idxData.coupangOwners[oidStr] || [];
                const isMapped = owners.length > 0;
                if (!isMapped) coupangUnmapped += 1;
                const optLabel = String(cdata.option_name || '').trim();
                const prodName = String(cdata.product_name || '').trim();
                allCsvItems.push({
                    channel: 'coupang',
                    option_id: oidStr,
                    option_name: optLabel,
                    product_name: prodName,
                    stock: parseInt(cdata.stock || 0, 10) || 0,
                    sales_7d: parseInt(cdata.sales_7d || 0, 10) || 0,
                    guessedBundle: guessCoupangBundle(optLabel || prodName),
                    owners,
                    isMapped,
                    searchBlob: `${oidStr} ${optLabel} ${prodName}`.toLowerCase()
                });
            });

            const countsEl = document.getElementById('acMapCsvCounts');
            if (countsEl) {
                countsEl.innerText = `매출코드 ${salesTotal}건 (미매핑 ${salesUnmapped}) · 개미 ${gaemiTotal}건 (미매핑 ${gaemiUnmapped}) · 쿠팡 ${coupangTotal}건 (미매핑 ${coupangUnmapped})`;
            }

            const chFilter = (document.getElementById('acMapCsvChannel') || {}).value || 'all';
            const onlyUnmapped = Boolean((document.getElementById('acMapCsvOnlyUnmapped') || {}).checked);
            const q = String((document.getElementById('acMapCsvSearch') || {}).value || '').toLowerCase().trim();

            acMapExplorerRows = allCsvItems.filter(row => {
                if (chFilter !== 'all' && row.channel !== chFilter) return false;
                if (onlyUnmapped && row.isMapped) return false;
                if (q && !row.searchBlob.includes(q)) return false;
                return true;
            });

            const tbody = document.getElementById('acMapCsvTbody');
            if (!tbody) return;
            if (acMapExplorerRows.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="p-4 text-center text-slate-400">표시할 CSV 코드가 없습니다. (모든 코드가 매핑되었거나 CSV가 아직 업로드되지 않았습니다)</td></tr>';
                return;
            }

            const activeMasters = (cfg.master_products || [])
                .filter(m => m.is_active !== false && !isMasterSCodeRecord(m, cfg))
                .slice()
                .sort((a, b) => String(a.seller_code || '').localeCompare(String(b.seller_code || '')));
            const masterOptsHtml = '<option value="">-- 대상 상품 선택 --</option>' + activeMasters.map(m =>
                `<option value="${escapeHtml(m.id || m.seller_code)}">[${escapeHtml(m.seller_code)}] ${escapeHtml(m.name)}</option>`
            ).join('');

            const MAX_ROWS = 200;
            const visibleRows = acMapExplorerRows.slice(0, MAX_ROWS);
            let html = visibleRows.map((row, idx) => {
                let chBadge = '';
                let codeCell = '';
                let infoCell = '';
                if (row.channel === 'sales') {
                    chBadge = '<span class="px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-800 font-bold text-[10px]">매출코드</span>';
                    codeCell = `<span class="font-mono font-bold text-indigo-950">${escapeHtml(row.code)}</span>`;
                    infoCell = `<div class="truncate max-w-[260px] text-slate-700" title="${escapeHtml(row.sampleName)}">${escapeHtml(row.sampleName || '상품명 미기재')}</div><div class="text-[10px] text-slate-400">7일 ${row.total_7d}개 · 30일 ${row.total_30d}개${row.malls ? ' · ' + escapeHtml(row.malls) : ''}</div>`;
                } else if (row.channel === 'gaemi') {
                    chBadge = '<span class="px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 font-bold text-[10px]">개미창고</span>';
                    codeCell = `<div class="font-mono font-bold text-amber-950">${escapeHtml(row.gaemi_code)}</div><div class="text-[11px] font-semibold text-slate-800">${escapeHtml(row.option_name || '(옵션명 없음)')}</div>`;
                    infoCell = `<div class="truncate max-w-[260px] text-slate-700" title="${escapeHtml(row.product_name)}">${escapeHtml(row.product_name || '상품명 미기재')}</div><div class="text-[10px] text-slate-500 font-mono">재고 ${row.stock.toLocaleString()}개${row.lot_count > 1 ? ` (${row.lot_count}개 로트 합계)` : ''}</div>`;
                } else {
                    chBadge = '<span class="px-1.5 py-0.5 rounded bg-blue-100 text-blue-900 font-bold text-[10px]">쿠팡옵션</span>';
                    codeCell = `<div class="font-mono font-bold text-blue-950">${escapeHtml(row.option_id)}</div><div class="text-[10px] text-slate-600">${escapeHtml(row.option_name || '')}</div>`;
                    infoCell = `<div class="truncate max-w-[260px] text-slate-700" title="${escapeHtml(row.product_name)}">${escapeHtml(row.product_name || '상품명 미기재')}</div><div class="text-[10px] text-slate-500 font-mono">재고 ${row.stock}세트 · 7일출고 ${row.sales_7d}세트 (추천 ${row.guessedBundle}개입)</div>`;
                }

                let statusBadge = '';
                if (row.b2bBase) {
                    statusBadge = `<span class="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-800 border border-indigo-200 font-bold text-[10px]">B2B→${escapeHtml(row.b2bBase)}</span>`;
                } else if (row.owners && row.owners.length > 0) {
                    statusBadge = row.owners.map(o => `<span class="inline-block px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-800 border border-emerald-200 font-mono text-[10px] mr-0.5">${escapeHtml(o.seller_code)}</span>`).join('');
                } else if (row.channel === 'sales' && row.code.startsWith('S')) {
                    statusBadge = '<span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-semibold text-[10px]">S코드</span>';
                } else {
                    statusBadge = '<span class="px-1.5 py-0.5 rounded bg-rose-100 text-rose-800 border border-rose-200 font-bold text-[10px]">미매핑</span>';
                }

                const bundleInputHtml = row.channel === 'coupang'
                    ? `<input type="number" min="1" value="${row.guessedBundle}" class="ac-map-exp-bundle w-12 px-1 py-1 border border-slate-300 rounded font-mono text-[10px] text-right" title="개입(배수)">`
                    : '';

                return `
                    <tr class="hover:bg-slate-50" data-exp-idx="${idx}">
                        <td class="p-2 align-top">${chBadge}</td>
                        <td class="p-2 align-top">${codeCell}</td>
                        <td class="p-2 align-top">${infoCell}</td>
                        <td class="p-2 align-top">${statusBadge}</td>
                        <td class="p-2 align-top">
                            <div class="flex flex-wrap items-center gap-1">
                                <select class="ac-map-exp-target flex-1 min-w-[140px] max-w-[200px] px-1.5 py-1 border border-slate-300 rounded bg-white text-[10px]">${masterOptsHtml}</select>
                                ${bundleInputHtml}
                                <button type="button" onclick="quickLinkCsvRow(${idx}, this)" class="px-2 py-1 bg-indigo-600 hover:bg-indigo-700 text-white rounded font-bold text-[10px] shrink-0">연결</button>
                                <button type="button" onclick="quickCreateFromCsvRow(${idx})" class="px-2 py-1 bg-slate-100 hover:bg-slate-200 border border-slate-300 text-slate-700 rounded font-bold text-[10px] shrink-0" title="이 코드로 신규 단품 마스터 생성">+ 신규</button>
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');

            if (acMapExplorerRows.length > MAX_ROWS) {
                html += `<tr><td colspan="5" class="p-2 text-center text-[10px] text-slate-500 bg-slate-50">상위 ${MAX_ROWS}건만 표시 중입니다 (전체 ${acMapExplorerRows.length}건 — 검색어로 좁혀보세요).</td></tr>`;
            }
            tbody.innerHTML = html;
        }

        async function quickLinkCsvRow(idx, btnEl) {
            const row = acMapExplorerRows[idx];
            if (!row) return;
            const tr = btnEl.closest('tr');
            const selectEl = tr ? tr.querySelector('.ac-map-exp-target') : null;
            const targetId = selectEl ? selectEl.value : '';
            if (!targetId) {
                alert('먼저 연결할 마스터 상품을 선택하세요.');
                return;
            }
            const target = (adminConfigCache.master_products || []).find(m => m.id === targetId || m.seller_code === targetId);
            if (!target) {
                alert('선택한 마스터 상품을 찾을 수 없습니다.');
                return;
            }

            const payload = { id: target.id, seller_code: target.seller_code };
            if (row.channel === 'sales') {
                let curCodes = Array.isArray(target.sales_codes)
                    ? target.sales_codes.map(c => String(c).trim().toUpperCase()).filter(Boolean)
                    : (target.seller_code ? [String(target.seller_code).trim().toUpperCase()] : []);
                if (!curCodes.includes(row.code)) curCodes.push(row.code);
                payload.sales_codes = curCodes;
            } else if (row.channel === 'gaemi') {
                const curRules = Array.isArray(target.gaemi_mappings)
                    ? target.gaemi_mappings.map(r => ({ gaemi_code: String(r.gaemi_code || '').trim(), option_name: String(r.option_name || '').trim() })).filter(r => r.gaemi_code)
                    : [];
                if (curRules.length === 0 && Array.isArray(target.gaemi_codes) && target.gaemi_codes.length > 0) {
                    const legacyRes = getMatchedGaemiEntriesForMaster(target, getGroupedGaemiEntries(adminConfigCache));
                    legacyRes.matched.forEach(entry => {
                        if (!curRules.some(r => r.gaemi_code.toUpperCase() === entry.gaemi_code.toUpperCase() && r.option_name === entry.option_name)) {
                            curRules.push({ gaemi_code: entry.gaemi_code, option_name: entry.option_name });
                        }
                    });
                }
                const dup = curRules.some(r => r.gaemi_code.toUpperCase() === row.gaemi_code.toUpperCase() && r.option_name === row.option_name);
                if (!dup) curRules.push({ gaemi_code: row.gaemi_code, option_name: row.option_name });
                payload.gaemi_mappings = curRules;
            } else if (row.channel === 'coupang') {
                const bundleEl = tr ? tr.querySelector('.ac-map-exp-bundle') : null;
                const bundleVal = Math.max(1, parseInt((bundleEl && bundleEl.value) || row.guessedBundle || '1', 10) || 1);
                const curOpts = (target.options || []).map(o => ({ id: String(o.id || '').trim(), bundle: o.bundle || 1, name: o.name || '' })).filter(o => o.id);
                if (!curOpts.some(o => o.id === row.option_id)) {
                    curOpts.push({ id: row.option_id, bundle: bundleVal, name: row.option_name || `${bundleVal}개` });
                }
                payload.options = curOpts;
            }

            try {
                const res = await adminFetch('/api/admin/product/mappings/save', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '연결 실패');
                    return;
                }
                await ensureAdminConfig();
                renderAdminCenterTabs();
                await fetchSummary();
                showAcMapToast(`✅ [${target.seller_code}] ${target.name}에 코드가 연결되었습니다.`);
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('연결 오류: ' + err.message);
            }
        }

        function quickCreateFromCsvRow(idx) {
            const row = acMapExplorerRows[idx];
            if (!row) return;
            const prefill = {};
            if (row.channel === 'sales') {
                prefill.seller_code = row.code;
                prefill.name = row.sampleName || '';
                prefill.sales_codes = [row.code];
            } else if (row.channel === 'gaemi') {
                prefill.name = row.product_name || '';
                prefill.gaemi_mappings = [{ gaemi_code: row.gaemi_code, option_name: row.option_name }];
                prefill.gaemi_codes = row.gaemi_code;
            } else if (row.channel === 'coupang') {
                prefill.name = row.product_name || '';
                prefill.options = [{ id: row.option_id, bundle: row.guessedBundle || 1, name: row.option_name || `${row.guessedBundle || 1}개` }];
            }
            openCreateProductModal(prefill);
        }

        function renderAdminMappingMasterTable() {
            if (!adminConfigCache) return;
            const cfg = adminConfigCache;
            const tbody = document.getElementById('acMapMasterTbody');
            if (!tbody) return;

            const q = String((document.getElementById('acMapMasterSearch') || {}).value || '').toLowerCase().trim();
            const mode = (document.getElementById('acMapMasterFilter') || {}).value || 'all';
            const showInactive = Boolean((document.getElementById('acMapMasterShowInactive') || {}).checked);

            const salesInv = cfg.sales_inventory || {};
            const coupangInv = cfg.coupang_inventory || {};
            const groupedGaemi = getGroupedGaemiEntries(cfg);
            const hasGaemiCsv = groupedGaemi.length > 0;

            const rows = (cfg.master_products || []).filter(m => {
                if (isMasterSCodeRecord(m, cfg)) return false;
                if (!showInactive && m.is_active === false) return false;
                if (q) {
                    const mn = String(m.name || '').toLowerCase();
                    const mc = String(m.seller_code || '').toLowerCase();
                    const gs = (m.gaemi_mappings || []).map(r => `${r.gaemi_code} ${r.option_name}`).join(' ').toLowerCase();
                    const sc = (m.sales_codes || []).join(' ').toLowerCase();
                    if (!mn.includes(q) && !mc.includes(q) && !gs.includes(q) && !sc.includes(q)) return false;
                }
                return true;
            }).map(m => {
                const normCode = String(m.seller_code || '').trim().toUpperCase();
                const hasCustomSales = Array.isArray(m.sales_codes);
                const effSalesCodes = hasCustomSales ? m.sales_codes : (normCode ? [normCode] : []);
                const gInfo = getMatchedGaemiEntriesForMaster(m, groupedGaemi);
                const opts = m.options || [];

                const hasMissing = effSalesCodes.length === 0 || (Array.isArray(m.gaemi_mappings) ? m.gaemi_mappings.length === 0 && (m.gaemi_codes || []).length === 0 : (m.gaemi_codes || []).length === 0) || (hasGaemiCsv && gInfo.matched.length === 0) || opts.length === 0;
                const hasCustom = hasCustomSales || (Array.isArray(m.gaemi_mappings) && m.gaemi_mappings.length > 0);
                return { m, effSalesCodes, hasCustomSales, gInfo, opts, hasMissing, hasCustom };
            }).filter(r => {
                if (mode === 'missing') return r.hasMissing;
                if (mode === 'custom') return r.hasCustom;
                return true;
            });

            if (rows.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="p-6 text-center text-slate-400">조건에 맞는 마스터 상품이 없습니다.</td></tr>';
                return;
            }

            tbody.innerHTML = rows.map(({ m, effSalesCodes, hasCustomSales, gInfo, opts }) => {
                const isAct = m.is_active !== false;

                let salesHtml = '';
                if (effSalesCodes.length === 0) {
                    salesHtml = '<span class="px-1.5 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200 text-[10px] font-bold">매출 연동 해제 (0건)</span>';
                } else {
                    salesHtml = effSalesCodes.map(sc => {
                        const up = String(sc).trim().toUpperCase();
                        const hit = salesInv[up];
                        const hitTxt = hit ? ` <span class="text-indigo-600">(7일 ${hit.total_7d})</span>` : '';
                        const cls = hasCustomSales
                            ? 'bg-indigo-50 text-indigo-900 border-indigo-200'
                            : 'bg-slate-50 text-slate-700 border-slate-200';
                        const tag = hasCustomSales ? '' : '<span class="text-[9px] text-slate-400 mr-0.5">기본</span>';
                        return `<span class="inline-block px-1.5 py-0.5 rounded border font-mono text-[10px] mr-1 mb-1 ${cls}">${tag}${escapeHtml(up)}${hitTxt}</span>`;
                    }).join('');
                }

                let gaemiHtml = '';
                const gMaps = Array.isArray(m.gaemi_mappings) ? m.gaemi_mappings : [];
                if (gMaps.length > 0) {
                    gaemiHtml = gMaps.map(rule => {
                        const rc = String(rule.gaemi_code || '').trim();
                        const ro = String(rule.option_name || '').trim();
                        const sumStock = groupedGaemi
                            .filter(e => e.gaemi_code.toUpperCase() === rc.toUpperCase() && (ro === '' || ro === '*' || e.option_name === ro))
                            .reduce((a, b) => a + b.stock, 0);
                        return `<span class="inline-block px-1.5 py-0.5 rounded bg-amber-50 text-amber-950 border border-amber-300 font-mono text-[10px] mr-1 mb-1"><strong>${escapeHtml(rc)}</strong> · ${escapeHtml(ro || '* 전체')} <span class="text-amber-700">(${sumStock.toLocaleString()}개)</span></span>`;
                    }).join('');
                } else if ((m.gaemi_codes || []).length > 0) {
                    const legText = `${(m.gaemi_codes || []).join(', ')}${m.gaemi_filter ? ` (키워드: ${m.gaemi_filter})` : ''}`;
                    const legStock = gInfo.matched.reduce((a, b) => a + b.stock, 0);
                    const warnZero = (hasGaemiCsv && gInfo.matched.length === 0)
                        ? '<span class="block text-[10px] text-rose-600 font-bold mt-0.5">⚠️ CSV 0건 일치 (옵션 재지정 필요)</span>'
                        : `<span class="block text-[10px] text-slate-500 mt-0.5">매칭 ${gInfo.matched.length}건 · ${legStock.toLocaleString()}개</span>`;
                    gaemiHtml = `<span class="inline-block px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200 font-mono text-[10px]">[구방식] ${escapeHtml(legText)}</span>${warnZero}`;
                } else {
                    gaemiHtml = '<span class="px-1.5 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200 text-[10px] font-bold">미지정</span>';
                }

                let coupangHtml = '';
                if (opts.length === 0) {
                    coupangHtml = '<span class="px-1.5 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200 text-[10px] font-bold">미지정</span>';
                } else {
                    coupangHtml = opts.map(o => {
                        const cd = coupangInv[String(o.id || '').trim()];
                        const stTxt = cd ? ` · ${cd.stock}세트` : '';
                        return `<span class="inline-block px-1.5 py-0.5 rounded bg-blue-50 text-blue-900 border border-blue-200 font-mono text-[10px] mr-1 mb-1">${escapeHtml(o.id)} <span class="text-blue-700">(${o.bundle || 1}개입${stTxt})</span></span>`;
                    }).join('');
                }

                return `
                    <tr class="${isAct ? 'hover:bg-slate-50' : 'bg-slate-100/70 opacity-75'}">
                        <td class="p-2.5 align-top">
                            <div class="font-mono font-bold text-slate-800">${escapeHtml(m.seller_code)}${!isAct ? ' <span class="text-[9px] bg-slate-200 text-slate-600 px-1 rounded">숨김</span>' : ''}</div>
                            <div class="font-semibold text-slate-700 mt-0.5">${escapeHtml(m.name)}</div>
                        </td>
                        <td class="p-2.5 align-top">${salesHtml}</td>
                        <td class="p-2.5 align-top">${gaemiHtml}</td>
                        <td class="p-2.5 align-top">${coupangHtml}</td>
                        <td class="p-2.5 text-center align-top">
                            <button type="button" data-prod-id="${escapeHtml(m.id || m.seller_code)}" onclick="openCodeMappingModal(this.dataset.prodId)" class="px-2.5 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg font-bold text-[11px] shadow-xs whitespace-nowrap">
                                🔗 매핑 지정
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        async function openCodeMappingModal(prodId) {
            if (!isAdminMode) { openPinModal(); return; }
            try {
                await ensureAdminConfig();
            } catch (e) {
                return;
            }
            const cfg = adminConfigCache || {};
            const target = (cfg.master_products || []).find(m => m.id === prodId || m.seller_code === prodId);
            if (!target) {
                alert('대상 마스터 상품을 찾을 수 없습니다.');
                return;
            }

            const normSeller = String(target.seller_code || '').trim().toUpperCase();
            const hasCustomSales = Array.isArray(target.sales_codes);

            cmState = {
                id: target.id || '',
                seller_code: target.seller_code || '',
                name: target.name || '',
                sales_codes: hasCustomSales
                    ? target.sales_codes.map(c => String(c).trim().toUpperCase()).filter(Boolean)
                    : (normSeller ? [normSeller] : []),
                sales_is_custom: hasCustomSales,
                gaemi_mappings: Array.isArray(target.gaemi_mappings)
                    ? target.gaemi_mappings.map(r => ({
                        gaemi_code: String(r.gaemi_code || r.code || '').trim(),
                        option_name: String(r.option_name || '').trim()
                    })).filter(r => r.gaemi_code)
                    : [],
                clear_legacy_gaemi: false,
                legacy_gaemi_codes: Array.isArray(target.gaemi_codes) ? target.gaemi_codes.slice() : [],
                legacy_gaemi_filter: String(target.gaemi_filter || '').trim(),
                options: Array.isArray(target.options)
                    ? target.options.map(o => ({
                        id: String(o.id || '').trim(),
                        bundle: Math.max(1, parseInt(o.bundle || 1, 10) || 1),
                        name: String(o.name || '').trim()
                    })).filter(o => o.id)
                    : []
            };

            document.getElementById('cm_modal_title').innerText = `[${cmState.seller_code}] ${cmState.name} — 3채널 코드 매핑`;
            document.getElementById('cm_target_badge').innerText = `대표코드: ${cmState.seller_code}`;
            document.getElementById('cm_sales_add_input').value = '';
            document.getElementById('cm_sales_csv_search').value = '';
            document.getElementById('cm_gaemi_code_input').value = '';
            document.getElementById('cm_gaemi_opt_input').value = '';
            document.getElementById('cm_gaemi_csv_search').value = '';
            document.getElementById('cm_coupang_id_input').value = '';
            document.getElementById('cm_coupang_bundle_input').value = '1';
            document.getElementById('cm_coupang_name_input').value = '';
            document.getElementById('cm_coupang_csv_search').value = '';

            renderCodeMappingModalBody();
            document.getElementById('codeMappingModal').classList.remove('hidden');
        }

        function closeCodeMappingModal() {
            const m = document.getElementById('codeMappingModal');
            if (m) m.classList.add('hidden');
        }

        function renderCodeMappingModalBody() {
            renderCmSalesChips();
            renderCmSalesCsvList();
            renderCmGaemiSection();
            renderCmCoupangRows();
            renderCmCoupangCsvList();
        }

        // --- ① 쿠팡외 매출 (상품관리코드) ---
        function renderCmSalesChips() {
            const box = document.getElementById('cm_sales_chips');
            const stEl = document.getElementById('cm_sales_status_text');
            const salesInv = (adminConfigCache && adminConfigCache.sales_inventory) || {};

            if (!cmState.sales_is_custom) {
                stEl.innerText = `현재 기본값 모드: 대표코드(${cmState.seller_code}) 매출을 자동 연동합니다.`;
            } else if (cmState.sales_codes.length === 0) {
                stEl.innerText = '⚠️ 매출 코드가 모두 비워져 있습니다 (쿠팡외 매출 출고량 0으로 집계됨).';
            } else {
                stEl.innerText = `수동 지정됨: ${cmState.sales_codes.length}개 상품관리코드 매출을 합산합니다.`;
            }

            if (cmState.sales_codes.length === 0) {
                box.innerHTML = '<span class="text-[11px] text-rose-700 font-bold bg-rose-50 border border-rose-200 px-2.5 py-1 rounded-lg">연동된 매출 코드 없음 (0건)</span>';
                return;
            }

            box.innerHTML = cmState.sales_codes.map((sc, idx) => {
                const hit = salesInv[sc];
                const statTxt = hit ? ` · 7일 ${hit.total_7d} / 30일 ${hit.total_30d}` : ' · CSV 미매칭';
                const defBadge = (!cmState.sales_is_custom && sc === String(cmState.seller_code).toUpperCase())
                    ? '<span class="text-[9px] bg-indigo-100 text-indigo-800 px-1 rounded mr-1">기본</span>'
                    : '';
                return `
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-white border border-indigo-300 text-indigo-950 font-mono text-[11px] font-bold shadow-xs">
                        ${defBadge}${escapeHtml(sc)}<span class="font-sans font-normal text-[10px] text-slate-500">${statTxt}</span>
                        <button type="button" onclick="removeCmSalesCode(${idx})" class="ml-1 text-rose-500 hover:text-rose-700 font-black" title="제거">&times;</button>
                    </span>
                `;
            }).join('');
        }

        function resetCmSalesDefault() {
            const up = String(cmState.seller_code || '').trim().toUpperCase();
            cmState.sales_is_custom = false;
            cmState.sales_codes = up ? [up] : [];
            renderCmSalesChips();
            renderCmSalesCsvList();
        }

        function clearCmSalesAll() {
            cmState.sales_is_custom = true;
            cmState.sales_codes = [];
            renderCmSalesChips();
            renderCmSalesCsvList();
        }

        function removeCmSalesCode(idx) {
            cmState.sales_is_custom = true;
            cmState.sales_codes.splice(idx, 1);
            renderCmSalesChips();
            renderCmSalesCsvList();
        }

        function addCmSalesManual() {
            const inp = document.getElementById('cm_sales_add_input');
            const parts = String(inp.value || '').split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
            if (parts.length === 0) return;
            cmState.sales_is_custom = true;
            parts.forEach(p => {
                if (!cmState.sales_codes.includes(p)) cmState.sales_codes.push(p);
            });
            inp.value = '';
            renderCmSalesChips();
            renderCmSalesCsvList();
        }

        function renderCmSalesCsvList() {
            const box = document.getElementById('cm_sales_csv_list');
            const cfg = adminConfigCache || {};
            const salesInv = cfg.sales_inventory || {};
            const entries = Object.entries(salesInv);
            if (entries.length === 0) {
                cmSalesCsvItems = [];
                box.innerHTML = '<div class="py-3 text-center text-[11px] text-slate-400">업로드된 쿠팡외 매출 CSV가 없습니다. 위 입력란에서 직접 코드를 추가할 수 있습니다.</div>';
                return;
            }
            const idxData = buildMappingOwnershipIndex(cfg);
            const q = String((document.getElementById('cm_sales_csv_search') || {}).value || '').toLowerCase().trim();

            cmSalesCsvItems = entries
                .map(([code, sdata]) => {
                    const up = String(code || '').trim().toUpperCase();
                    const sNames = (sdata.sample_names || []).join(', ');
                    const malls = (sdata.malls || []).join(', ');
                    return {
                        code: up,
                        total_7d: sdata.total_7d || 0,
                        total_30d: sdata.total_30d || 0,
                        sampleNames: sNames,
                        malls,
                        owners: (idxData.salesOwners[up] || []).filter(o => o.seller_code !== cmState.seller_code),
                        b2bBase: idxData.b2bSalesOwners[up] || '',
                        searchBlob: `${up} ${sNames} ${malls}`.toLowerCase()
                    };
                })
                .filter(it => !q || it.searchBlob.includes(q))
                .sort((a, b) => {
                    const aSel = cmState.sales_codes.includes(a.code) ? 0 : 1;
                    const bSel = cmState.sales_codes.includes(b.code) ? 0 : 1;
                    return aSel !== bSel ? aSel - bSel : a.code.localeCompare(b.code);
                });

            if (cmSalesCsvItems.length === 0) {
                box.innerHTML = '<div class="py-3 text-center text-[11px] text-slate-400">검색어와 일치하는 매출 코드가 없습니다.</div>';
                return;
            }

            box.innerHTML = cmSalesCsvItems.slice(0, 120).map((it, idx) => {
                const isSelected = cmState.sales_codes.includes(it.code);
                let ownerBadge = '<span class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 text-[10px]">미매핑</span>';
                if (isSelected) {
                    ownerBadge = '<span class="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-900 border border-emerald-300 font-bold text-[10px]">연결됨</span>';
                } else if (it.b2bBase) {
                    ownerBadge = `<span class="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-800 text-[10px]">B2B→${escapeHtml(it.b2bBase)}</span>`;
                } else if (it.owners.length > 0) {
                    ownerBadge = `<span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px]">타상품: ${escapeHtml(it.owners.map(o => o.seller_code).join(', '))}</span>`;
                }
                const btnHtml = isSelected
                    ? `<button type="button" onclick="toggleCmSalesCsvItem(${idx})" class="px-2 py-0.5 rounded bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-200 font-bold text-[10px]">해제</button>`
                    : `<button type="button" onclick="toggleCmSalesCsvItem(${idx})" class="px-2 py-0.5 rounded bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-[10px]">+ 연결</button>`;
                return `
                    <div class="py-1.5 px-1 flex items-center justify-between gap-2 hover:bg-slate-50">
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5">
                                <span class="font-mono font-bold text-slate-900">${escapeHtml(it.code)}</span>
                                ${ownerBadge}
                                <span class="text-[10px] text-slate-500 font-mono">7일 ${it.total_7d} / 30일 ${it.total_30d}</span>
                            </div>
                            <div class="text-[10px] text-slate-500 truncate">${escapeHtml(it.sampleNames || '상품명 미기재')}${it.malls ? ' · ' + escapeHtml(it.malls) : ''}</div>
                        </div>
                        <div class="shrink-0">${btnHtml}</div>
                    </div>
                `;
            }).join('');
        }

        function toggleCmSalesCsvItem(idx) {
            const it = cmSalesCsvItems[idx];
            if (!it) return;
            cmState.sales_is_custom = true;
            const pos = cmState.sales_codes.indexOf(it.code);
            if (pos >= 0) cmState.sales_codes.splice(pos, 1);
            else cmState.sales_codes.push(it.code);
            renderCmSalesChips();
            renderCmSalesCsvList();
        }

        // --- ② 개미창고 (상품코드 + 옵션명) ---
        function renderCmGaemiSection() {
            renderCmGaemiLegacyBanner();
            renderCmGaemiChips();
            renderCmGaemiCsvList();
        }

        function renderCmGaemiLegacyBanner() {
            const banner = document.getElementById('cm_gaemi_legacy_banner');
            if (!banner) return;
            const showLegacy = cmState.gaemi_mappings.length === 0 && !cmState.clear_legacy_gaemi && cmState.legacy_gaemi_codes.length > 0;
            if (!showLegacy) {
                banner.classList.add('hidden');
                return;
            }
            const cfg = adminConfigCache || {};
            const grouped = getGroupedGaemiEntries(cfg);
            const legRes = getMatchedGaemiEntriesForMaster({
                gaemi_codes: cmState.legacy_gaemi_codes,
                gaemi_filter: cmState.legacy_gaemi_filter
            }, grouped);
            const legTxt = `${cmState.legacy_gaemi_codes.join(', ')}${cmState.legacy_gaemi_filter ? ` (키워드: '${cmState.legacy_gaemi_filter}')` : ''}`;
            const hasCsv = grouped.length > 0;
            banner.classList.remove('hidden');

            if (hasCsv && legRes.matched.length === 0) {
                banner.className = 'rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-[11px] text-rose-900 space-y-1.5';
                banner.innerHTML = `
                    <div class="flex flex-wrap items-center justify-between gap-2">
                        <span>⚠️ 현재 구방식 규칙 <strong>[${escapeHtml(legTxt)}]</strong>이 업로드된 개미 CSV와 <strong>0건 일치</strong>합니다. 아래 목록에서 정확한 '상품코드 + 옵션명'을 선택하세요.</span>
                        <button type="button" onclick="clearLegacyGaemiInCm()" class="px-2 py-0.5 bg-white border border-rose-300 text-rose-700 rounded font-bold text-[10px]">구방식 규칙 비우기</button>
                    </div>
                `;
            } else {
                banner.className = 'rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-950 space-y-1.5';
                banner.innerHTML = `
                    <div class="flex flex-wrap items-center justify-between gap-2">
                        <span>ℹ️ 현재 구방식 규칙 <strong>[${escapeHtml(legTxt)}]</strong> 사용 중 (CSV 일치 ${legRes.matched.length}건).</span>
                        <div class="flex items-center gap-1.5">
                            ${legRes.matched.length > 0 ? `<button type="button" onclick="importLegacyGaemiMatchesToCm()" class="px-2 py-0.5 bg-amber-600 hover:bg-amber-700 text-white rounded font-bold text-[10px]">✨ 현재 일치 ${legRes.matched.length}건 정확 매핑으로 가져오기</button>` : ''}
                            <button type="button" onclick="clearLegacyGaemiInCm()" class="px-2 py-0.5 bg-white border border-amber-300 text-slate-700 rounded font-bold text-[10px]">구방식 규칙 비우기</button>
                        </div>
                    </div>
                `;
            }
        }

        function importLegacyGaemiMatchesToCm() {
            const grouped = getGroupedGaemiEntries(adminConfigCache || {});
            const legRes = getMatchedGaemiEntriesForMaster({
                gaemi_codes: cmState.legacy_gaemi_codes,
                gaemi_filter: cmState.legacy_gaemi_filter
            }, grouped);
            legRes.matched.forEach(entry => {
                const exists = cmState.gaemi_mappings.some(r =>
                    r.gaemi_code.toUpperCase() === entry.gaemi_code.toUpperCase() && r.option_name === entry.option_name
                );
                if (!exists) {
                    cmState.gaemi_mappings.push({ gaemi_code: entry.gaemi_code, option_name: entry.option_name });
                }
            });
            renderCmGaemiSection();
        }

        function clearLegacyGaemiInCm() {
            cmState.clear_legacy_gaemi = true;
            cmState.legacy_gaemi_codes = [];
            cmState.legacy_gaemi_filter = '';
            renderCmGaemiSection();
        }

        function renderCmGaemiChips() {
            const box = document.getElementById('cm_gaemi_chips');
            const grouped = getGroupedGaemiEntries(adminConfigCache || {});
            if (cmState.gaemi_mappings.length === 0) {
                box.innerHTML = '<span class="text-[11px] text-slate-500">지정된 정확 매핑(상품코드+옵션명)이 없습니다. 아래에서 추가하거나 CSV 목록에서 선택하세요.</span>';
                return;
            }
            box.innerHTML = cmState.gaemi_mappings.map((rule, idx) => {
                const rc = rule.gaemi_code;
                const ro = rule.option_name;
                const matches = grouped.filter(e =>
                    e.gaemi_code.toUpperCase() === rc.toUpperCase() && (ro === '' || ro === '*' || e.option_name === ro)
                );
                const sumStock = matches.reduce((a, b) => a + b.stock, 0);
                const optLabel = ro ? ro : '* (해당 코드 전체 옵션)';
                const stockLabel = grouped.length > 0 ? ` · ${sumStock.toLocaleString()}개` : '';
                return `
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-white border border-amber-300 text-amber-950 text-[11px] font-bold shadow-xs">
                        <span class="font-mono">${escapeHtml(rc)}</span>
                        <span class="text-slate-400">|</span>
                        <span>${escapeHtml(optLabel)}</span>
                        <span class="font-mono font-normal text-[10px] text-amber-700">${stockLabel}</span>
                        <button type="button" onclick="removeCmGaemiRule(${idx})" class="ml-1 text-rose-500 hover:text-rose-700 font-black" title="제거">&times;</button>
                    </span>
                `;
            }).join('');
        }

        function clearCmGaemiAll() {
            cmState.gaemi_mappings = [];
            cmState.clear_legacy_gaemi = true;
            renderCmGaemiSection();
        }

        function removeCmGaemiRule(idx) {
            cmState.gaemi_mappings.splice(idx, 1);
            if (cmState.gaemi_mappings.length === 0) {
                cmState.clear_legacy_gaemi = true;
            }
            renderCmGaemiSection();
        }

        function addCmGaemiManual() {
            const codeEl = document.getElementById('cm_gaemi_code_input');
            const optEl = document.getElementById('cm_gaemi_opt_input');
            const gCode = String(codeEl.value || '').trim();
            const optName = String(optEl.value || '').trim();
            if (!gCode) {
                alert('개미 상품코드를 입력해주세요.');
                codeEl.focus();
                return;
            }
            const exists = cmState.gaemi_mappings.some(r =>
                r.gaemi_code.toUpperCase() === gCode.toUpperCase() && r.option_name === optName
            );
            if (!exists) {
                cmState.gaemi_mappings.push({ gaemi_code: gCode, option_name: optName });
            }
            cmState.clear_legacy_gaemi = false;
            codeEl.value = '';
            optEl.value = '';
            renderCmGaemiSection();
        }

        function renderCmGaemiCsvList() {
            const box = document.getElementById('cm_gaemi_csv_list');
            const cfg = adminConfigCache || {};
            const grouped = getGroupedGaemiEntries(cfg);
            if (grouped.length === 0) {
                cmGaemiCsvItems = [];
                box.innerHTML = '<div class="py-3 text-center text-[11px] text-slate-400">업로드된 개미창고 CSV가 없습니다. 위 입력란에서 직접 추가할 수 있습니다.</div>';
                return;
            }
            const idxData = buildMappingOwnershipIndex(cfg);
            const q = String((document.getElementById('cm_gaemi_csv_search') || {}).value || '').toLowerCase().trim();

            const isSelectedEntry = (entry) => cmState.gaemi_mappings.some(r =>
                r.gaemi_code.toUpperCase() === entry.gaemi_code.toUpperCase() &&
                (r.option_name === entry.option_name || r.option_name === '' || r.option_name === '*')
            );

            cmGaemiCsvItems = grouped
                .map(entry => ({
                    gaemi_code: entry.gaemi_code,
                    option_name: entry.option_name,
                    product_name: entry.product_name,
                    stock: entry.stock,
                    lot_count: entry.lot_count,
                    owners: (idxData.gaemiOwners[entry.key] || []).filter(o => o.seller_code !== cmState.seller_code),
                    searchBlob: `${entry.gaemi_code} ${entry.option_name} ${entry.product_name}`.toLowerCase()
                }))
                .filter(it => !q || it.searchBlob.includes(q))
                .sort((a, b) => {
                    const aSel = isSelectedEntry(a) ? 0 : 1;
                    const bSel = isSelectedEntry(b) ? 0 : 1;
                    if (aSel !== bSel) return aSel - bSel;
                    const cCmp = a.gaemi_code.localeCompare(b.gaemi_code);
                    return cCmp !== 0 ? cCmp : a.option_name.localeCompare(b.option_name);
                });

            if (cmGaemiCsvItems.length === 0) {
                box.innerHTML = '<div class="py-3 text-center text-[11px] text-slate-400">검색어와 일치하는 개미창고 품목이 없습니다.</div>';
                return;
            }

            box.innerHTML = cmGaemiCsvItems.slice(0, 120).map((it, idx) => {
                const exactIdx = cmState.gaemi_mappings.findIndex(r =>
                    r.gaemi_code.toUpperCase() === it.gaemi_code.toUpperCase() && r.option_name === it.option_name
                );
                const wildMatch = exactIdx < 0 && isSelectedEntry(it);
                let ownerBadge = '<span class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 text-[10px]">미매핑</span>';
                if (exactIdx >= 0 || wildMatch) {
                    ownerBadge = '<span class="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-900 border border-emerald-300 font-bold text-[10px]">연결됨</span>';
                } else if (it.owners.length > 0) {
                    ownerBadge = `<span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px]">타상품: ${escapeHtml(it.owners.map(o => o.seller_code).join(', '))}</span>`;
                }
                const btnHtml = (exactIdx >= 0)
                    ? `<button type="button" onclick="toggleCmGaemiCsvItem(${idx})" class="px-2 py-0.5 rounded bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-200 font-bold text-[10px]">해제</button>`
                    : (wildMatch
                        ? '<span class="text-[10px] text-emerald-700 font-semibold">전체규칙 포함</span>'
                        : `<button type="button" onclick="toggleCmGaemiCsvItem(${idx})" class="px-2 py-0.5 rounded bg-amber-600 hover:bg-amber-700 text-white font-bold text-[10px]">+ 연결</button>`);
                return `
                    <div class="py-1.5 px-1 flex items-center justify-between gap-2 hover:bg-slate-50">
                        <div class="min-w-0 flex-1">
                            <div class="flex flex-wrap items-center gap-1.5">
                                <span class="font-mono font-bold text-slate-900">${escapeHtml(it.gaemi_code)}</span>
                                <span class="font-bold text-amber-950 bg-amber-100/70 px-1.5 rounded text-[10px]">${escapeHtml(it.option_name || '(옵션명 없음)')}</span>
                                ${ownerBadge}
                                <span class="text-[10px] text-slate-600 font-mono">재고 ${it.stock.toLocaleString()}개${it.lot_count > 1 ? ` (${it.lot_count}로트)` : ''}</span>
                            </div>
                            <div class="text-[10px] text-slate-500 truncate">${escapeHtml(it.product_name || '상품명 미기재')}</div>
                        </div>
                        <div class="shrink-0">${btnHtml}</div>
                    </div>
                `;
            }).join('');
        }

        function toggleCmGaemiCsvItem(idx) {
            const it = cmGaemiCsvItems[idx];
            if (!it) return;
            const pos = cmState.gaemi_mappings.findIndex(r =>
                r.gaemi_code.toUpperCase() === it.gaemi_code.toUpperCase() && r.option_name === it.option_name
            );
            if (pos >= 0) {
                cmState.gaemi_mappings.splice(pos, 1);
                if (cmState.gaemi_mappings.length === 0) cmState.clear_legacy_gaemi = true;
            } else {
                cmState.gaemi_mappings.push({ gaemi_code: it.gaemi_code, option_name: it.option_name });
                cmState.clear_legacy_gaemi = false;
            }
            renderCmGaemiSection();
        }

        // --- ③ 쿠팡 재고건전성 (쿠팡 옵션 ID) ---
        function renderCmCoupangRows() {
            const box = document.getElementById('cm_coupang_rows');
            const coupangInv = (adminConfigCache && adminConfigCache.coupang_inventory) || {};
            if (cmState.options.length === 0) {
                box.innerHTML = '<div class="text-[11px] text-slate-500 bg-white border border-blue-200 rounded-lg p-2.5">연결된 쿠팡 옵션 ID가 없습니다. 아래에서 직접 입력하거나 CSV 목록에서 추가하세요.</div>';
                return;
            }
            box.innerHTML = cmState.options.map((opt, idx) => {
                const cd = coupangInv[opt.id];
                const csvInfo = cd
                    ? `현재 ${cd.stock || 0}세트 · 7일출고 ${cd.sales_7d || 0}세트`
                    : 'CSV 미등록';
                return `
                    <div class="flex flex-wrap items-center gap-2 bg-white border border-blue-200 rounded-lg p-2">
                        <span class="font-mono font-bold text-blue-950 w-32 truncate" title="${escapeHtml(opt.id)}">${escapeHtml(opt.id)}</span>
                        <div class="flex items-center space-x-1">
                            <input type="number" min="1" value="${opt.bundle || 1}" oninput="cmState.options[${idx}].bundle = Math.max(1, parseInt(this.value || '1', 10) || 1)" class="w-16 px-2 py-1 border border-slate-300 rounded font-mono text-[11px] text-right font-bold">
                            <span class="text-[11px] text-slate-500">개입</span>
                        </div>
                        <input type="text" value="${escapeHtml(opt.name || '')}" oninput="cmState.options[${idx}].name = this.value" placeholder="옵션명" class="flex-1 min-w-[120px] px-2.5 py-1 border border-slate-300 rounded text-[11px]">
                        <span class="text-[10px] font-mono text-slate-500 shrink-0">${csvInfo}</span>
                        <button type="button" onclick="removeCmCoupangRow(${idx})" class="px-2 py-1 bg-rose-50 hover:bg-rose-100 text-rose-700 rounded text-[10px] font-bold">삭제</button>
                    </div>
                `;
            }).join('');
        }

        function clearCmCoupangAll() {
            cmState.options = [];
            renderCmCoupangRows();
            renderCmCoupangCsvList();
        }

        function removeCmCoupangRow(idx) {
            cmState.options.splice(idx, 1);
            renderCmCoupangRows();
            renderCmCoupangCsvList();
        }

        function addCmCoupangManual() {
            const idEl = document.getElementById('cm_coupang_id_input');
            const bEl = document.getElementById('cm_coupang_bundle_input');
            const nameEl = document.getElementById('cm_coupang_name_input');
            const oid = String(idEl.value || '').trim();
            if (!oid) {
                alert('쿠팡 옵션 ID를 입력해주세요.');
                idEl.focus();
                return;
            }
            const bundle = Math.max(1, parseInt(bEl.value || '1', 10) || 1);
            const oname = String(nameEl.value || '').trim() || `${bundle}개`;
            if (!cmState.options.some(o => o.id === oid)) {
                cmState.options.push({ id: oid, bundle, name: oname });
            }
            idEl.value = '';
            bEl.value = '1';
            nameEl.value = '';
            renderCmCoupangRows();
            renderCmCoupangCsvList();
        }

        function renderCmCoupangCsvList() {
            const box = document.getElementById('cm_coupang_csv_list');
            const cfg = adminConfigCache || {};
            const coupangInv = cfg.coupang_inventory || {};
            const entries = Object.entries(coupangInv);
            if (entries.length === 0) {
                cmCoupangCsvItems = [];
                box.innerHTML = '<div class="py-3 text-center text-[11px] text-slate-400">업로드된 쿠팡 재고건전성 CSV가 없습니다. 위 입력란에서 직접 추가할 수 있습니다.</div>';
                return;
            }
            const idxData = buildMappingOwnershipIndex(cfg);
            const q = String((document.getElementById('cm_coupang_csv_search') || {}).value || '').toLowerCase().trim();

            cmCoupangCsvItems = entries
                .map(([oid, cdata]) => {
                    const oidStr = String(oid || '').trim();
                    const optLabel = String(cdata.option_name || '').trim();
                    const prodName = String(cdata.product_name || '').trim();
                    return {
                        id: oidStr,
                        option_name: optLabel,
                        product_name: prodName,
                        stock: parseInt(cdata.stock || 0, 10) || 0,
                        sales_7d: parseInt(cdata.sales_7d || 0, 10) || 0,
                        guessedBundle: guessCoupangBundle(optLabel || prodName),
                        owners: (idxData.coupangOwners[oidStr] || []).filter(o => o.seller_code !== cmState.seller_code),
                        searchBlob: `${oidStr} ${optLabel} ${prodName}`.toLowerCase()
                    };
                })
                .filter(it => !q || it.searchBlob.includes(q))
                .sort((a, b) => {
                    const aSel = cmState.options.some(o => o.id === a.id) ? 0 : 1;
                    const bSel = cmState.options.some(o => o.id === b.id) ? 0 : 1;
                    return aSel !== bSel ? aSel - bSel : a.id.localeCompare(b.id);
                });

            if (cmCoupangCsvItems.length === 0) {
                box.innerHTML = '<div class="py-3 text-center text-[11px] text-slate-400">검색어와 일치하는 쿠팡 옵션이 없습니다.</div>';
                return;
            }

            box.innerHTML = cmCoupangCsvItems.slice(0, 120).map((it, idx) => {
                const isSelected = cmState.options.some(o => o.id === it.id);
                let ownerBadge = '<span class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 text-[10px]">미매핑</span>';
                if (isSelected) {
                    ownerBadge = '<span class="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-900 border border-emerald-300 font-bold text-[10px]">연결됨</span>';
                } else if (it.owners.length > 0) {
                    ownerBadge = `<span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px]">타상품: ${escapeHtml(it.owners.map(o => o.seller_code).join(', '))}</span>`;
                }
                const btnHtml = isSelected
                    ? `<button type="button" onclick="toggleCmCoupangCsvItem(${idx})" class="px-2 py-0.5 rounded bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-200 font-bold text-[10px]">해제</button>`
                    : `<button type="button" onclick="toggleCmCoupangCsvItem(${idx})" class="px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-700 text-white font-bold text-[10px]">+ 연결 (${it.guessedBundle}개입)</button>`;
                return `
                    <div class="py-1.5 px-1 flex items-center justify-between gap-2 hover:bg-slate-50">
                        <div class="min-w-0 flex-1">
                            <div class="flex flex-wrap items-center gap-1.5">
                                <span class="font-mono font-bold text-slate-900">${escapeHtml(it.id)}</span>
                                <span class="font-bold text-blue-950 bg-blue-100/70 px-1.5 rounded text-[10px]">${escapeHtml(it.option_name || '옵션명 없음')}</span>
                                ${ownerBadge}
                                <span class="text-[10px] text-slate-500 font-mono">재고 ${it.stock}세트 · 7일출고 ${it.sales_7d}</span>
                            </div>
                            <div class="text-[10px] text-slate-500 truncate">${escapeHtml(it.product_name || '상품명 미기재')}</div>
                        </div>
                        <div class="shrink-0">${btnHtml}</div>
                    </div>
                `;
            }).join('');
        }

        function toggleCmCoupangCsvItem(idx) {
            const it = cmCoupangCsvItems[idx];
            if (!it) return;
            const pos = cmState.options.findIndex(o => o.id === it.id);
            if (pos >= 0) {
                cmState.options.splice(pos, 1);
            } else {
                cmState.options.push({
                    id: it.id,
                    bundle: it.guessedBundle || 1,
                    name: it.option_name || `${it.guessedBundle || 1}개`
                });
            }
            renderCmCoupangRows();
            renderCmCoupangCsvList();
        }

        async function saveCodeMappingModal() {
            const payload = {
                id: cmState.id,
                seller_code: cmState.seller_code,
                sales_codes: cmState.sales_is_custom ? cmState.sales_codes : null,
                gaemi_mappings: cmState.gaemi_mappings,
                clear_legacy_gaemi: cmState.clear_legacy_gaemi,
                options: cmState.options.filter(o => o.id).map(o => ({
                    id: o.id,
                    bundle: Math.max(1, parseInt(o.bundle || 1, 10) || 1),
                    name: String(o.name || '').trim() || `${o.bundle || 1}개`
                }))
            };

            try {
                const res = await adminFetch('/api/admin/product/mappings/save', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });
                const json = await res.json();
                if (!res.ok || !json.success) {
                    alert(json.message || '코드 매핑 저장 실패');
                    return;
                }
                closeCodeMappingModal();
                await ensureAdminConfig();
                await fetchSummary();
                syncEditModalFromSavedProduct(json.product);
                const centerOpen = !document.getElementById('adminCenterModal').classList.contains('hidden');
                if (centerOpen) {
                    renderAdminCenterTabs();
                    if (activeAdminTab === 'mapping') {
                        showAcMapToast('✅ ' + json.message);
                    }
                } else {
                    alert('✅ ' + json.message);
                }
            } catch (err) {
                if (err.message !== 'ADMIN_AUTH_REQUIRED') alert('코드 매핑 저장 오류: ' + err.message);
            }
        }

        // 초기화
        renderTableHeader();
        checkAdminSession();
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
