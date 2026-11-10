import os
import sys

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import unittest
import json
import csv
import copy
import shutil
import tempfile
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
import app as app_module
from app import app


class FakeHttpResponse:
    def __init__(self, obj):
        self._data = json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeOpener:
    def __init__(self, store_box, fail_mode=False):
        self.store_box = store_box
        self.fail_mode = fail_mode

    def open(self, req, timeout=15):
        if self.fail_mode:
            raise RuntimeError("Simulated Drive Network Error")
        body = json.loads(req.data.decode("utf-8"))
        action = body.get("action")
        if action == "save":
            self.store_box["saved_payload"] = copy.deepcopy(body.get("payload"))
            self.store_box["save_calls"] += 1
            return FakeHttpResponse({"success": True, "fileId": "fake-drive-file-123"})
        elif action == "load":
            self.store_box["load_calls"] += 1
            if self.store_box.get("remote_data") is not None:
                return FakeHttpResponse({
                    "success": True,
                    "exists": True,
                    "fileId": "fake-drive-file-123",
                    "data": copy.deepcopy(self.store_box["remote_data"])
                })
            return FakeHttpResponse({"success": True, "exists": False})
        elif action == "ping":
            return FakeHttpResponse({"success": True, "status": "ok"})
        return FakeHttpResponse({"success": False, "error": "Unknown action"})


class TestIljeomodoAdminMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data_file = app_module.DATA_FILE
        cls._orig_state = copy.deepcopy(app_module.app_state)
        cls._orig_drive_state = copy.deepcopy(app_module.drive_sync_state)
        cls._orig_env_pin = os.environ.pop("ADMIN_PIN", None)
        cls._orig_gas_url = os.environ.pop("GOOGLE_APPS_SCRIPT_URL", None)
        cls._orig_gas_tok = os.environ.pop("GOOGLE_APPS_SCRIPT_TOKEN", None)

        cls._tmpdir = tempfile.mkdtemp(prefix="iljeomodo_test_admin_")
        cls._tmp_data_file = os.path.join(cls._tmpdir, "inventory_master_store.json")
        if os.path.exists(cls._orig_data_file):
            shutil.copy2(cls._orig_data_file, cls._tmp_data_file)
        app_module.DATA_FILE = cls._tmp_data_file

    @classmethod
    def tearDownClass(cls):
        app_module.DATA_FILE = cls._orig_data_file
        with app_module.data_lock:
            app_module.app_state.clear()
            app_module.app_state.update(copy.deepcopy(cls._orig_state))
        app_module.drive_sync_state.clear()
        app_module.drive_sync_state.update(copy.deepcopy(cls._orig_drive_state))
        for k, v in [
            ("ADMIN_PIN", cls._orig_env_pin),
            ("GOOGLE_APPS_SCRIPT_URL", cls._orig_gas_url),
            ("GOOGLE_APPS_SCRIPT_TOKEN", cls._orig_gas_tok),
        ]:
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        os.environ.pop("ADMIN_PIN", None)
        os.environ.pop("GOOGLE_APPS_SCRIPT_URL", None)
        os.environ.pop("GOOGLE_APPS_SCRIPT_TOKEN", None)
        with app_module.data_lock:
            app_module.app_state.clear()
            app_module.app_state.update(copy.deepcopy(self._orig_state))
            app_module.app_state["settings"]["admin_pin_custom"] = ""
        app_module._admin_revoked_before = 0
        app_module.drive_sync_state.clear()
        app_module.drive_sync_state.update(copy.deepcopy(self._orig_drive_state))
        app_module.drive_sync_state["configured"] = False
        app_module.drive_sync_state["status"] = "disabled"
        self.client = app.test_client()

    def _login(self, pin="1500"):
        res = self.client.post("/api/admin/verify-pin", json={"pin": pin})
        self.assertEqual(res.status_code, 200)
        token = json.loads(res.data)["token"]
        return {"X-Admin-Token": token}

    def test_01_pin_auth_and_protection(self):
        """PIN 인증, 보호된 엔드포인트 401 차단, 로그아웃 즉시 무효화, 환경변수 PIN 우선순위 검증"""
        # 1. 잘못된 PIN
        bad = self.client.post("/api/admin/verify-pin", json={"pin": "9999"})
        self.assertEqual(bad.status_code, 401)

        # 2. 토큰 없이 관리자 엔드포인트 접근 차단
        for ep, method in [
            ("/api/admin/config", "GET"),
            ("/api/admin/product/save", "POST"),
            ("/api/admin/product/mappings/save", "POST"),
            ("/api/admin/product/delete", "POST"),
            ("/api/admin/products/bulk-save", "POST"),
            ("/api/admin/b2b-mappings/save", "POST"),
            ("/api/admin/settings/save", "POST"),
            ("/api/admin/drive/sync-now", "POST"),
            ("/api/admin/drive/pull-now", "POST"),
            ("/api/admin/backup/export", "GET"),
            ("/api/admin/backup/import", "POST"),
            ("/api/load-sample", "POST"),
        ]:
            res = self.client.open(ep, method=method)
            self.assertEqual(res.status_code, 401, f"{ep} ({method}) must require admin auth")

        # 3. 기본 PIN 1500 인증 및 세션 확인
        headers = self._login("1500")
        sess = json.loads(self.client.get("/api/admin/session", headers=headers).data)
        self.assertTrue(sess["authenticated"])

        # 4. 로그아웃 시 기존 토큰 즉시 무효화 확인
        self.client.post("/api/admin/logout")
        sess_after_logout = json.loads(self.client.get("/api/admin/session", headers=headers).data)
        self.assertFalse(sess_after_logout["authenticated"])
        self.assertEqual(self.client.get("/api/admin/config", headers=headers).status_code, 401)

        # 5. 커스텀 PIN 변경 후 이전 토큰 무효화 및 새 토큰 유효 검증
        headers2 = self._login("1500")
        set_res = self.client.post(
            "/api/admin/settings/save",
            headers=headers2,
            json={"new_pin": "2468"}
        )
        self.assertEqual(set_res.status_code, 200)
        new_token = json.loads(set_res.data)["new_token"]
        self.assertTrue(bool(new_token))

        # 이전 PIN(1500) 거절 + 새 PIN(2468) 성공 + 이전 토큰 거절
        self.assertEqual(self.client.post("/api/admin/verify-pin", json={"pin": "1500"}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/config", headers=headers2).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/config", headers={"X-Admin-Token": new_token}).status_code, 200)

        # 5. Render 환경변수 ADMIN_PIN 최우선 오버라이드 검증
        os.environ["ADMIN_PIN"] = "7788"
        self.assertEqual(self.client.post("/api/admin/verify-pin", json={"pin": "2468"}).status_code, 401)
        env_res = self.client.post("/api/admin/verify-pin", json={"pin": "7788"})
        self.assertEqual(env_res.status_code, 200)
        self.assertEqual(json.loads(env_res.data)["pin_source"], "env")
        os.environ.pop("ADMIN_PIN", None)
        print("OK: [Admin Test 1] PIN 인증, 401 보호, 커스텀 PIN 변경 및 ADMIN_PIN 환경변수 우선순위 검증 완료")

    def test_02_admin_edit_preserved_across_csv_uploads(self):
        """관리자가 수정한 상품명/구분/리드타임/입고예정/메모가 CSV 재업로드 후에도 그대로 고정 유지되고 재고수량만 갱신되는지 검증"""
        headers = self._login("1500")

        # 1. 관리자 수정: 20DC02 상품명, 메모, 입고예정, 리드타임 커스텀 수정
        save_res = self.client.post("/api/admin/product/save", headers=headers, json={
            "seller_code": "20DC02",
            "name": "[관리자확정] 데체코 EV 올리브오일 500ml",
            "product_type": "수입",
            "category": "프리미엄오일",
            "note": "이탈리아 4분기 선적 완료",
            "lead_time_days": 95,
            "safety_buffer_days": 15,
            "in_transit_qty": 480,
            "expected_arrival_date": "2026-12-20",
            "gaemi_codes": ["PJ1489516"],
            "options": [
                {"id": "95844803290", "bundle": 1, "name": "1개"}
            ]
        })
        self.assertEqual(save_res.status_code, 200)

        # 2. 개미창고 / 쿠팡 / 매출 CSV 재업로드
        for src, fname in [
            ("gaemi", "개미창고_샘플.csv"),
            ("coupang", "쿠팡_샘플.csv"),
            ("sales", "매출정보_샘플.csv"),
        ]:
            with open(fname, encoding="utf-8") as f:
                rows = list(csv.reader(f))
            up_res = self.client.post("/api/upload-file-data", json={"source": src, "rows": rows})
            self.assertEqual(up_res.status_code, 200)

        # 3. 검증: 재고는 새 CSV 값으로 반영되지만, 관리자 기준정보(이름/입고예정/리드타임/메모)는 유지됨
        summary = json.loads(self.client.get("/api/summary").data)
        item = next(m for m in summary["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(item["name"], "[관리자확정] 데체코 EV 올리브오일 500ml")
        self.assertEqual(item["category"], "프리미엄오일")
        self.assertEqual(item["note"], "이탈리아 4분기 선적 완료")
        self.assertEqual(item["effective_lead_time"], 95)
        self.assertEqual(item["effective_prepare_limit"], 110)
        self.assertEqual(item["in_transit_qty"], 480)
        self.assertEqual(item["expected_arrival_date"], "2026-12-20")
        # 쿠팡_샘플.csv 내 95844803290 재고 = 468개 반영 확인
        self.assertEqual(item["coupang_physical_total"], 468)

        # 4. 부분 업데이트 호출 시 미전달 필드(note/in_transit_qty/lead_time/options) 보존 검증
        partial_res = self.client.post("/api/admin/product/save", headers=headers, json={
            "seller_code": "20DC02",
            "name": "[부분수정] 데체코 EV 올리브오일 500ml",
            "default_expiry_date": "2027-09-09"
        })
        self.assertEqual(partial_res.status_code, 200)
        summary_p = json.loads(self.client.get("/api/summary").data)
        item_p = next(m for m in summary_p["masters"] if m["seller_code"] == "20DC02")
        item_gl02 = next(m for m in summary_p["masters"] if m["seller_code"] == "10GL02")
        self.assertEqual(item_p["name"], "[부분수정] 데체코 EV 올리브오일 500ml")
        self.assertEqual(item_p["note"], "이탈리아 4분기 선적 완료")
        self.assertEqual(item_p["in_transit_qty"], 480)
        self.assertEqual(item_p["coupang_physical_total"], 468)
        self.assertEqual(item_p["default_expiry_date"], "2027-09-09")
        self.assertEqual(item_gl02["default_expiry_date"], "")
        print("OK: [Admin Test 2] 관리자 중심 데이터 우선순위(상품명/리드타임/입고예정 보존 + CSV 수량 병합 + 부분수정 격리) 검증 완료")

    def test_03_create_hide_restore_and_hard_delete_product(self):
        """신규 단품 등록, 대시보드 숨김/복원, 영구 삭제 동작 검증"""
        headers = self._login("1500")

        # 1. 신규 단품 추가
        add_res = self.client.post("/api/admin/product/save", headers=headers, json={
            "is_new": True,
            "seller_code": "NEW99",
            "name": "테스트 신규 올리브오일",
            "product_type": "사입",
            "category": "오일/식용유",
            "default_expiry_date": "2027-08-31",
            "in_transit_qty": 100
        })
        self.assertEqual(add_res.status_code, 200)
        s1 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s1["stats"]["total_masters"], 39)
        self.assertTrue(any(m["seller_code"] == "NEW99" for m in s1["masters"]))

        # 2. 대시보드 숨김 처리
        hide_res = self.client.post("/api/admin/product/delete", headers=headers, json={
            "seller_code": "NEW99",
            "mode": "hide"
        })
        self.assertEqual(hide_res.status_code, 200)
        s2 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s2["stats"]["total_masters"], 38)
        self.assertEqual(s2["stats"]["inactive_count"], 1)
        self.assertFalse(any(m["seller_code"] == "NEW99" for m in s2["masters"]))

        # 관리자 config에서는 여전히 숨김 상태로 조회 가능
        cfg = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        new_rec = next(m for m in cfg["master_products"] if m["seller_code"] == "NEW99")
        self.assertFalse(new_rec["is_active"])

        # 3. 노출 복원
        rest_res = self.client.post("/api/admin/product/delete", headers=headers, json={
            "seller_code": "NEW99",
            "mode": "restore"
        })
        self.assertEqual(rest_res.status_code, 200)
        s3 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s3["stats"]["total_masters"], 39)

        # 4. 영구 삭제
        hard_res = self.client.post("/api/admin/product/delete", headers=headers, json={
            "seller_code": "NEW99",
            "mode": "hard"
        })
        self.assertEqual(hard_res.status_code, 200)
        s4 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s4["stats"]["total_masters"], 38)
        print("OK: [Admin Test 3] 신규 단품 등록, 숨김/복원, 영구 삭제 검증 완료")

    def test_04_duplicate_lot_key_expiry_override_and_preserve(self):
        """동일 옵션명('500ml') 다중 로트(PJ1489503 / 20EO02) 개별 소비기한 지정 및 CSV 재업로드 후 유지 검증"""
        headers = self._login("1500")
        with open("개미창고_샘플.csv", encoding="utf-8") as f:
            gaemi_rows = list(csv.reader(f))
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})

        summary_before = json.loads(self.client.get("/api/summary").data)
        item_eo02 = next(m for m in summary_before["masters"] if m["seller_code"] == "20EO02")
        lots = item_eo02["csv_gaemi_lots"]
        self.assertGreaterEqual(len(lots), 2)
        lot_keys = [l["lot_key"] for l in lots]
        self.assertEqual(len(set(lot_keys)), len(lot_keys), "중복 옵션명 로트키가 서로 구별되어야 함")

        # stock > 0 인 로트와 stock == 0 인 로트 구별 후 각각 다른 관리자 소비기한 직접 지정
        active_lot = next(l for l in lots if l["stock"] > 0)
        empty_lot = next(l for l in lots if l["stock"] == 0)

        self.client.post("/api/admin/product/save", headers=headers, json={
            "id": item_eo02["id"],
            "seller_code": "20EO02",
            "name": item_eo02["name"],
            "product_type": item_eo02["product_type"],
            "category": item_eo02["category"],
            "gaemi_codes": item_eo02["gaemi_codes"],
            "options": [{"id": o["id"], "bundle": o["bundle"], "name": o["name"]} for o in item_eo02["options"]],
            "csv_lot_expiries": {
                active_lot["lot_key"]: "2027-02-01",
                empty_lot["lot_key"]: "2028-01-15"
            }
        })

        # 동일한 개미 CSV 재업로드 후에도 관리자 지정 소비기한 유지 확인 (실물 재고가 있는 active_lot 기준)
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})
        summary_after = json.loads(self.client.get("/api/summary").data)
        after_eo02 = next(m for m in summary_after["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(after_eo02["nearest_expiry_date"], "2027-02-01")
        self.assertTrue(after_eo02["has_admin_expiry"])
        lot_exp_map = {l["lot_key"]: l["expiry_date"] for l in after_eo02["csv_gaemi_lots"]}
        self.assertEqual(lot_exp_map[active_lot["lot_key"]], "2027-02-01")
        self.assertEqual(lot_exp_map[empty_lot["lot_key"]], "2028-01-15")

        # __CLEAR__ 및 __RESTORE__ 동작 검증 (20DC02 -> CSV 원본 기한 2027-03-28 제거 후 복원)
        item_dc02 = next(m for m in summary_after["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(item_dc02["csv_gaemi_lots"][0]["gaemi_code"], "PJ1489516")
        self.assertEqual(item_dc02["manual_lots"], [])
        dc_lot_key = item_dc02["csv_gaemi_lots"][0]["lot_key"]
        self.client.post("/api/admin/product/save", headers=headers, json={
            "id": item_dc02["id"], "seller_code": "20DC02", "name": item_dc02["name"],
            "product_type": item_dc02["product_type"], "category": item_dc02["category"],
            "gaemi_codes": item_dc02["gaemi_codes"],
            "options": [{"id": o["id"], "bundle": o["bundle"], "name": o["name"]} for o in item_dc02["options"]],
            "csv_lot_expiries": {dc_lot_key: "__CLEAR__"}
        })
        s_cleared = next(m for m in json.loads(self.client.get("/api/summary").data)["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(s_cleared["nearest_expiry_date"], "")

        self.client.post("/api/admin/product/save", headers=headers, json={
            "id": item_dc02["id"], "seller_code": "20DC02", "name": item_dc02["name"],
            "product_type": item_dc02["product_type"], "category": item_dc02["category"],
            "gaemi_codes": item_dc02["gaemi_codes"],
            "options": [{"id": o["id"], "bundle": o["bundle"], "name": o["name"]} for o in item_dc02["options"]],
            "csv_lot_expiries": {dc_lot_key: "__RESTORE__"}
        })
        s_restored = next(m for m in json.loads(self.client.get("/api/summary").data)["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(s_restored["nearest_expiry_date"], "2027-03-28")
        print("OK: [Admin Test 4] 동일 옵션명 다중 로트 개별 소비기한 오버라이드(__CLEAR__/__RESTORE__ 포함) 및 CSV 재업로드 보존 검증 완료")

    def test_05_manual_lots_fifo_deduction_and_unassigned_stock(self):
        """수동 분할 소비기한(manual_lots) 등록 후 CSV 재고 감소 시 FIFO 자동 차감 & 재고 증가 시 미지정 재고 감지 검증"""
        headers = self._login("1500")

        # 1. 초기 개미창고 CSV 업로드 (20DC02 -> PJ1489516 재고 1000개)
        rows_1000 = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            ["1", "PJ1489516", "데체코 500ml", "단일", "입고완료", "1000", "0", "1000", "2026-10-01 10:00:00"]
        ]
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": rows_1000})

        # 2. 관리자 수동 소비기한 2개 로트로 분할: 1차분 400개(2026-12-01), 2차분 600개(2027-06-30)
        s0 = json.loads(self.client.get("/api/summary").data)
        item = next(m for m in s0["masters"] if m["seller_code"] == "20DC02")
        save_res = self.client.post("/api/admin/product/save", headers=headers, json={
            "id": item["id"],
            "seller_code": "20DC02",
            "name": item["name"],
            "product_type": item["product_type"],
            "category": item["category"],
            "gaemi_codes": item["gaemi_codes"],
            "options": [{"id": o["id"], "bundle": o["bundle"], "name": o["name"]} for o in item["options"]],
            "manual_lots": [
                {"lot_id": "L1", "label": "1차분(선입)", "expiry_date": "2026-12-01", "qty": 400},
                {"lot_id": "L2", "label": "2차분(신규)", "expiry_date": "2027-06-30", "qty": 600},
            ]
        })
        self.assertEqual(save_res.status_code, 200)

        # 3. 새 개미 CSV 업로드: 재고 1000 -> 700 (300개 출고됨)
        rows_700 = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            ["1", "PJ1489516", "데체코 500ml", "단일", "입고완료", "1000", "300", "700", "2026-10-01 10:00:00"]
        ]
        up_dec = json.loads(self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": rows_700}).data)
        self.assertTrue(up_dec["success"])

        s1 = json.loads(self.client.get("/api/summary").data)
        after_dec = next(m for m in s1["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(after_dec["gaemi_total"], 700)
        lot_map = {l["lot_id"]: l["stock"] for l in after_dec["gaemi_lots"]}
        self.assertEqual(lot_map["L1"], 100, "가장 빠른 소비기한 L1에서 300개가 먼저 차감되어야 함")
        self.assertEqual(lot_map["L2"], 600, "L2는 그대로 600개 유지되어야 함")
        self.assertIn("300개", after_dec["lot_sync_note"])

        # 4. 새 개미 CSV 업로드: 재고 700 -> 950 (250개 신규 입고됨 -> 미지정 재고 감지)
        rows_950 = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            ["1", "PJ1489516", "데체코 500ml", "단일", "입고완료", "1250", "300", "950", "2026-10-01 10:00:00"]
        ]
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": rows_950})
        s2 = json.loads(self.client.get("/api/summary").data)
        after_inc = next(m for m in s2["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(after_inc["gaemi_total"], 950)
        self.assertTrue(after_inc["has_unassigned_stock"])
        lot_map2 = {l["lot_id"]: l["stock"] for l in after_inc["gaemi_lots"]}
        self.assertEqual(lot_map2["__unassigned__"], 250)
        print("OK: [Admin Test 5] 수동 분할 로트 FIFO 자동 차감(-300개 -> L1:100) 및 입고 증가분 미지정 재고(+250개) 감지 완료")

    def test_06_dynamic_b2b_mappings_and_bulk_save(self):
        """관리자 B2B/S코드 동적 매핑 추가 및 일괄 저장 반영 검증"""
        headers = self._login("1500")

        # 1. 동적 B2B 매핑 S99999 -> 20DC02 (x5) 등록
        b2b_res = self.client.post("/api/admin/b2b-mappings/save", headers=headers, json={
            "mappings": [
                {"s_code": "S99999", "base_code": "20DC02", "multiplier": 5, "desc": "데체코 5병 세트"}
            ]
        })
        self.assertEqual(b2b_res.status_code, 200)

        # 2. 오늘 날짜로 S99999 주문 3건 업로드
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sales_rows = [
            ["주문일", "묶음번호", "쇼핑몰", "온라인상품명", "주문수량", "판매자관리코드"],
            [now_str, "B001", "스마트스토어", "데체코 5병세트", 3, "S99999"]
        ]
        self.client.post("/api/upload-file-data", json={"source": "sales", "rows": sales_rows})

        summary = json.loads(self.client.get("/api/summary").data)
        item_dc02 = next(m for m in summary["masters"] if m["seller_code"] == "20DC02")
        self.assertGreaterEqual(item_dc02["other_sales_7d"], 15)  # 3 * 5 = 15
        self.assertTrue(any(b["s_code"] == "S99999" and b["sales_7d"] == 15 for b in item_dc02["b2b_shared_sales"]))

        # 3. Bulk save 검증
        bulk_res = self.client.post("/api/admin/products/bulk-save", headers=headers, json={
            "products": [
                {"seller_code": "20DC02", "note": "일괄 저장 테스트 메모", "in_transit_qty": 222}
            ]
        })
        self.assertEqual(bulk_res.status_code, 200)
        summary2 = json.loads(self.client.get("/api/summary").data)
        item_dc02_2 = next(m for m in summary2["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(item_dc02_2["note"], "일괄 저장 테스트 메모")
        self.assertEqual(item_dc02_2["in_transit_qty"], 222)
        print("OK: [Admin Test 6] 동적 B2B S코드 매핑 환산 합산 및 대량 일괄 저장 검증 완료")

    def test_07_drive_bridge_mock_and_json_backup_restore(self):
        """Google Apps Script 브리지 동기화/복원(Mock) 및 JSON 백업 내보내기/복원 검증"""
        headers = self._login("1500")

        # 1. JSON 백업 export 검증 (38개 단품 + 6개 S코드 = 44개 master_products 포함)
        exp_res = self.client.get("/api/admin/backup/export", headers=headers)
        self.assertEqual(exp_res.status_code, 200)
        backup_json = json.loads(exp_res.data)
        self.assertEqual(backup_json["schema_version"], 2)
        self.assertEqual(len(backup_json["master_products"]), len(app_module.app_state["master_products"]))
        self.assertEqual(len(backup_json["master_products"]), 44)

        # 2. Google Apps Script 브리지 sync-now & pull-now & 부팅 load_state Mock 테스트
        os.environ["GOOGLE_APPS_SCRIPT_URL"] = "https://script.google.com/macros/s/fake-id/exec"
        os.environ["GOOGLE_APPS_SCRIPT_TOKEN"] = "fake-secret-token"
        store_box = {"save_calls": 0, "load_calls": 0, "saved_payload": None, "remote_data": None}

        with patch("urllib.request.build_opener", side_effect=lambda *a, **kw: FakeOpener(store_box)):
            # Sync now
            sync_res = self.client.post("/api/admin/drive/sync-now", headers=headers)
            self.assertEqual(sync_res.status_code, 200)
            sync_data = json.loads(sync_res.data)
            self.assertTrue(sync_data["success"])
            self.assertEqual(store_box["save_calls"], 1)
            self.assertEqual(sync_data["drive_sync"]["status"], "synced")

            # 원격 Drive 데이터를 수정된 스냅샷으로 준비 후 Pull now 테스트
            remote_snap = copy.deepcopy(backup_json)
            remote_snap["master_products"][0]["note"] = "Google Drive에서 복원된 메모!"
            store_box["remote_data"] = remote_snap

            pull_res = self.client.post("/api/admin/drive/pull-now", headers=headers)
            self.assertEqual(pull_res.status_code, 200)
            self.assertEqual(app_module.app_state["master_products"][0]["note"], "Google Drive에서 복원된 메모!")

            # 서버 재시작 부팅(load_state) 시에도 Drive 자동 복원 검증
            with app_module.data_lock:
                app_module.app_state["master_products"][0]["note"] = "재부팅 전 날아갈 로컬 임시 메모"
            remote_snap["master_products"][0]["note"] = "부팅 시 Drive 자동 복원 성공!"
            app_module.load_state()
            self.assertEqual(app_module.app_state["master_products"][0]["note"], "부팅 시 Drive 자동 복원 성공!")

        # 3. JSON backup import로 원래 메모 복원 검증
        imp_res = self.client.post("/api/admin/backup/import", headers=headers, json={"data": backup_json})
        self.assertEqual(imp_res.status_code, 200)
        self.assertNotEqual(app_module.app_state["master_products"][0].get("note", ""), "부팅 시 Drive 자동 복원 성공!")
        print("OK: [Admin Test 7] Google Drive 브리지 동기화/복원/부팅복원(Mock) 및 JSON 백업 내보내기/복원 검증 완료")

    def test_08_code_mapping_gaemi_exact_option_and_fifo(self):
        """개미창고 상품코드+옵션명 수동 정확 매핑(부분일치 배제, 다중 로트 합산, 대소문자/와일드카드, FIFO 차감) 검증"""
        headers = self._login("1500")
        with open("개미창고_샘플.csv", encoding="utf-8") as f:
            gaemi_rows = list(csv.reader(f))
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})

        # 1. PJ1491144 '정제 1L' 단독 매핑 -> '정제 1L-1'(1080개)가 섞이지 않고 정확히 982개만 매칭되어야 함
        r1 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EK02",
            "gaemi_mappings": [
                {"gaemi_code": "PJ1491144", "option_name": "정제 1L"}
            ]
        })
        self.assertEqual(r1.status_code, 200)
        s1 = json.loads(self.client.get("/api/summary").data)
        m_ek02 = next(m for m in s1["masters"] if m["seller_code"] == "20EK02")
        self.assertEqual(m_ek02["gaemi_total"], 982)
        self.assertEqual(len(m_ek02["csv_gaemi_lots"]), 1)
        self.assertEqual(m_ek02["csv_gaemi_lots"][0]["option_name"], "정제 1L")

        # 2. PJ1489544 '허니 500ml' 단독 매핑 vs '허니 크랜베리 500ml' 분리 확인
        r2 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20DC02",
            "gaemi_mappings": [
                {"gaemi_code": "PJ1489544", "option_name": "허니 500ml"}
            ]
        })
        self.assertEqual(r2.status_code, 200)
        s2 = json.loads(self.client.get("/api/summary").data)
        m_dc02 = next(m for m in s2["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(m_dc02["gaemi_total"], 16)
        self.assertEqual(len(m_dc02["csv_gaemi_lots"]), 1)

        # 3. 다중 등록일 로트 (PJ1489503 + '500ml' 2건) 대소문자 무시 매핑 및 수동 FIFO 자동 차감 연계
        r3 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EO02",
            "gaemi_mappings": [
                {"gaemi_code": "pj1489503", "option_name": "500ml"}
            ]
        })
        self.assertEqual(r3.status_code, 200)
        s3 = json.loads(self.client.get("/api/summary").data)
        m_eo02 = next(m for m in s3["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(m_eo02["gaemi_total"], 1208)
        self.assertEqual(len(m_eo02["csv_gaemi_lots"]), 2)

        # 수동 로트(L1:500, L2:708) 등록 후 새 개미 CSV(1208 -> 908) 업로드 시 L1 FIFO -300 차감 확인
        self.client.post("/api/admin/product/save", headers=headers, json={
            "id": m_eo02["id"],
            "seller_code": "20EO02",
            "name": m_eo02["name"],
            "manual_lots": [
                {"lot_id": "ML1", "label": "선입분", "expiry_date": "2026-12-01", "qty": 500},
                {"lot_id": "ML2", "label": "후입분", "expiry_date": "2027-08-01", "qty": 708}
            ]
        })
        rows_reduced = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            ["1", "PJ1489503", "엘레올로지 500ml", "500ml", "입고완료", "2160", "1252", "908", "2026-09-15 16:31:50"]
        ]
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": rows_reduced})
        s4 = json.loads(self.client.get("/api/summary").data)
        m_eo02_after = next(m for m in s4["masters"] if m["seller_code"] == "20EO02")
        lmap = {l["lot_id"]: l["stock"] for l in m_eo02_after["gaemi_lots"]}
        self.assertEqual(lmap["ML1"], 200)
        self.assertEqual(lmap["ML2"], 708)

        # 4. 와일드카드 '*' 매핑: PJ1491144 전체 옵션(982+1080+0+1788 = 3850)
        r4 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EK02",
            "gaemi_mappings": [
                {"gaemi_code": "PJ1491144", "option_name": "*"}
            ]
        })
        self.assertEqual(r4.status_code, 200)
        # Re-upload full gaemi CSV first since rows_reduced replaced gaemi_inventory
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})
        s5 = json.loads(self.client.get("/api/summary").data)
        m_ek02_wild = next(m for m in s5["masters"] if m["seller_code"] == "20EK02")
        self.assertEqual(m_ek02_wild["gaemi_total"], 3850)
        print("OK: [Admin Test 8] 개미창고 상품코드+옵션명 정확 매핑(부분일치 배제, 다중로트, FIFO 차감, 와일드카드) 검증 완료")

    def test_09_code_mapping_sales_codes_and_header_variants(self):
        """쿠팡외 매출정보 상품관리코드 수동 다중매핑, '상품관리코드' 헤더 인식, 빈 목록/복원, B2B S코드 비중복 검증"""
        headers = self._login("1500")
        now_day1 = (datetime.now()).strftime("%Y-%m-%d 10:00:00")
        now_day2 = (datetime.now()).strftime("%Y-%m-%d 09:00:00")

        # '상품관리코드' 헤더명 변형 지원 확인
        sales_rows = [
            ["주문일", "묶음번호", "쇼핑몰", "온라인상품명", "주문수량", "상품관리코드"],
            [now_day2, "B10", "스마트스토어", "데체코 올리브오일(구)", 4, "ALT_DC02_A"],
            [now_day1, "B11", "자사몰", "데체코 올리브오일(신)", 6, "alt_dc02_b"],
            [now_day1, "B12", "카카오", "데체코 기본코드", 10, "20DC02"],
            [now_day1, "B13", "스마트스토어", "B2B 세트", 2, "S100005"]
        ]
        up_res = self.client.post("/api/upload-file-data", json={"source": "sales", "rows": sales_rows})
        self.assertEqual(up_res.status_code, 200)

        # 1. 커스텀 sales_codes = ['ALT_DC02_A', 'ALT_DC02_B'] (기본 20DC02 제외)
        r1 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20DC02",
            "sales_codes": ["alt_dc02_a", "ALT_DC02_B"]
        })
        self.assertEqual(r1.status_code, 200)
        s1 = json.loads(self.client.get("/api/summary").data)
        m1 = next(m for m in s1["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(m1["sales_codes"], ["ALT_DC02_A", "ALT_DC02_B"])
        self.assertTrue(m1["has_custom_sales_codes"])
        self.assertEqual(m1["other_sales_7d"], 10)  # 4 + 6 (20DC02 10개 미포함)
        self.assertEqual(m1["recent_orders"][0]["date"], now_day1)

        # 2. 명시적 빈 목록([]) 설정 시 매출 0건
        r2 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20DC02",
            "sales_codes": []
        })
        self.assertEqual(r2.status_code, 200)
        s2 = json.loads(self.client.get("/api/summary").data)
        m2 = next(m for m in s2["masters"] if m["seller_code"] == "20DC02")
        self.assertEqual(m2["sales_codes"], [])
        self.assertTrue(m2["has_custom_sales_codes"])
        self.assertEqual(m2["other_sales_7d"], 0)

        # 3. 기본 복원(sales_codes: None) 시 대표코드 20DC02 자동 복원
        r3 = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20DC02",
            "sales_codes": None
        })
        self.assertEqual(r3.status_code, 200)
        s3 = json.loads(self.client.get("/api/summary").data)
        m3 = next(m for m in s3["masters"] if m["seller_code"] == "20DC02")
        self.assertFalse(m3["has_custom_sales_codes"])
        self.assertEqual(m3["sales_codes"], ["20DC02"])
        self.assertEqual(m3["other_sales_7d"], 10)

        # 4. B2B 매핑 S100005(-> 10GL02, x12)가 이미 있는 상태에서 10GL02의 sales_codes에 S100005를 넣어도 중복 합산되지 않음
        self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "10GL02",
            "sales_codes": ["10GL02", "S100005"]
        })
        s4 = json.loads(self.client.get("/api/summary").data)
        m_gl02 = next(m for m in s4["masters"] if m["seller_code"] == "10GL02")
        self.assertEqual(m_gl02["other_sales_7d"], 24)  # S100005 2 * 12 = 24 (not 26!)
        print("OK: [Admin Test 9] 쿠팡외 매출 상품관리코드 다중매핑/해제/기본복원/헤더인식/B2B 비중복 검증 완료")

    def test_10_mappings_save_isolates_other_fields_and_config_sales_inventory(self):
        """코드매핑 전용 저장 시 상품명·리드타임·입고예정·수동로트 보존, 구방식 초기화, admin/config sales_inventory 요약 확인"""
        headers = self._login("1500")

        # 1. 기준정보 선저장
        self.client.post("/api/admin/product/save", headers=headers, json={
            "seller_code": "20TZ03",
            "name": "[고정명] 테라찌노 유기농 250ml",
            "product_type": "수입",
            "note": "유지되어야 할 메모",
            "lead_time_days": 88,
            "in_transit_qty": 321,
            "expected_arrival_date": "2026-12-31",
            "gaemi_codes": ["PJ1489272"],
            "gaemi_filter": "250",
            "manual_lots": [
                {"lot_id": "TZ_L1", "label": "1차", "expiry_date": "2027-05-01", "qty": 50}
            ]
        })

        # 2. 코드매핑만 수정 (쿠팡 옵션 + 매출코드 + 개미 매핑)
        res = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20TZ03",
            "sales_codes": ["20TZ03", "TZ03_EVENT"],
            "gaemi_mappings": [
                {"gaemi_code": "PJ1489272", "option_name": "라노 500ml"}
            ],
            "options": [
                {"id": "OPT_TZ_250_1", "bundle": 2, "name": "2개 세트"}
            ]
        })
        self.assertEqual(res.status_code, 200)
        saved = json.loads(res.data)["product"]
        self.assertEqual(saved["name"], "[고정명] 테라찌노 유기농 250ml")
        self.assertEqual(saved["note"], "유지되어야 할 메모")
        self.assertEqual(saved["lead_time_days"], 88)
        self.assertEqual(saved["in_transit_qty"], 321)
        self.assertEqual(saved["expected_arrival_date"], "2026-12-31")
        self.assertEqual(len(saved["manual_lots"]), 1)
        self.assertEqual(saved["sales_codes"], ["20TZ03", "TZ03_EVENT"])
        self.assertEqual(saved["gaemi_mappings"], [{"gaemi_code": "PJ1489272", "option_name": "라노 500ml"}])
        self.assertEqual(saved["gaemi_filter"], "")
        self.assertEqual(saved["options"], [{"id": "OPT_TZ_250_1", "bundle": 2, "name": "2개 세트"}])

        # 3. clear_legacy_gaemi: True 로 개미 매핑 완전 비우기 시 구방식 Fallback 부활 안 함 확인
        with open("개미창고_샘플.csv", encoding="utf-8") as f:
            self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": list(csv.reader(f))})
        res_clear = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20TZ03",
            "gaemi_mappings": [],
            "clear_legacy_gaemi": True
        })
        self.assertEqual(res_clear.status_code, 200)
        cleared_prod = json.loads(res_clear.data)["product"]
        self.assertEqual(cleared_prod["gaemi_codes"], [])
        self.assertEqual(cleared_prod["gaemi_filter"], "")
        self.assertEqual(cleared_prod["gaemi_mappings"], [])

        # 4. /api/admin/config 에 sales_inventory 요약 필드 반환 검증
        with open("매출정보_샘플.csv", encoding="utf-8") as f:
            self.client.post("/api/upload-file-data", json={"source": "sales", "rows": list(csv.reader(f))})
        cfg = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        self.assertIn("sales_inventory", cfg)
        self.assertGreater(len(cfg["sales_inventory"]), 0)
        first_sc = next(iter(cfg["sales_inventory"].values()))
        for key in ("total_7d", "total_30d", "orders_count", "sample_names", "malls"):
            self.assertIn(key, first_sc)
        print("OK: [Admin Test 10] 코드매핑 단독저장 격리(기타 기준 보존), 구방식 초기화, config sales_inventory 요약 검증 완료")

    def test_11_upload_timestamp_kst_and_non_upload_isolation(self):
        """재고 업데이트 일시는 KST(+09:00) 데이터 파일 업로드 시각만 반영되고 서버부팅·관리자수정은 이를 덮어쓰지 않음"""
        headers = self._login("1500")

        # 1. KST 시간대(UTC+9) 검증
        kst_now = app_module.now_kst()
        self.assertEqual(kst_now.utcoffset(), timedelta(hours=9))

        # 2. 구 스키마 백업(data_uploaded_at 없음, 레거시 last_updated: '2026-11-10 01:31') 로드 시 가짜 시각 승계 금지
        app_module._apply_loaded_state({
            "master_products": copy.deepcopy(app_module.app_state["master_products"]),
            "last_updated": "2026-11-10 01:31",
            "saved_at": "2026-11-10 01:31:00"
        })
        s0 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s0["stats"]["last_updated"], "")
        self.assertEqual(s0["stats"]["upload_timestamps"], {})

        # 3. 01:31 UTC = 10:31 KST 상황에서 개미창고 CSV 업로드 시 한국 시간 '2026-11-10 10:31' 기록 확인
        fixed_kst_upload1 = datetime(2026, 11, 10, 10, 31, 0, tzinfo=timezone(timedelta(hours=9)))
        with open("개미창고_샘플.csv", encoding="utf-8") as f:
            gaemi_rows = list(csv.reader(f))
        with patch.object(app_module, "now_kst", return_value=fixed_kst_upload1):
            r_up1 = self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})
        self.assertEqual(r_up1.status_code, 200)
        self.assertEqual(json.loads(r_up1.data)["uploaded_at"], "2026-11-10 10:31")

        s1 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s1["stats"]["last_updated"], "2026-11-10 10:31")
        self.assertEqual(s1["stats"]["upload_timestamps"].get("gaemi"), "2026-11-10 10:31")

        # 4. 다른 날/다른 시각에 관리자 수정, 코드 매핑 저장, 일괄 저장, B2B 저장, 재부팅(load_state)이 발생해도 last_updated 불변!
        later_kst = datetime(2026, 11, 11, 16, 45, 0, tzinfo=timezone(timedelta(hours=9)))
        with patch.object(app_module, "now_kst", return_value=later_kst):
            self.client.post("/api/admin/product/save", headers=headers, json={
                "seller_code": "20DC02",
                "name": "시간불변 검증 상품",
                "note": "관리자 수정은 업로드 일시를 변경하면 안 됨"
            })
            self.client.post("/api/admin/product/mappings/save", headers=headers, json={
                "seller_code": "20DC02",
                "sales_codes": ["20DC02"]
            })
            self.client.post("/api/admin/products/bulk-save", headers=headers, json={
                "products": [{"seller_code": "20DC02", "in_transit_qty": 10}]
            })
            self.client.post("/api/admin/b2b-mappings/save", headers=headers, json={
                "mappings": [{"s_code": "S100005", "base_code": "10GL02", "multiplier": 12, "desc": "B2B"}]
            })
            self.client.post("/api/admin/product/delete", headers=headers, json={
                "seller_code": "20DC02", "mode": "hide"
            })
            self.client.post("/api/admin/product/delete", headers=headers, json={
                "seller_code": "20DC02", "mode": "restore"
            })
            # 잘못된 CSV 업로드(400)도 기존 업로드 시각 유지
            bad_up = self.client.post("/api/upload-file-data", json={"source": "coupang", "rows": [["invalid"]]})
            self.assertEqual(bad_up.status_code, 400)
            # 로컬 스냅샷으로부터 서버 재시작(load_state) 시뮬레이션
            app_module.load_state()

        s2 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s2["stats"]["last_updated"], "2026-11-10 10:31")
        self.assertEqual(s2["stats"]["upload_timestamps"], {"gaemi": "2026-11-10 10:31"})

        # 5. 이후 쿠팡 CSV 업로드 시 새 업로드 시각(KST)으로만 갱신되고 채널별 업로드 시각 유지
        fixed_kst_upload2 = datetime(2026, 11, 12, 9, 5, 0, tzinfo=timezone(timedelta(hours=9)))
        with open("쿠팡_샘플.csv", encoding="utf-8") as f:
            coupang_rows = list(csv.reader(f))
        with patch.object(app_module, "now_kst", return_value=fixed_kst_upload2):
            r_up2 = self.client.post("/api/upload-file-data", json={"source": "coupang", "rows": coupang_rows})
        self.assertEqual(r_up2.status_code, 200)
        s3 = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s3["stats"]["last_updated"], "2026-11-12 09:05")
        self.assertEqual(s3["stats"]["upload_timestamps"]["gaemi"], "2026-11-10 10:31")
        self.assertEqual(s3["stats"]["upload_timestamps"]["coupang"], "2026-11-12 09:05")
        print("OK: [Admin Test 11] KST(UTC+9) 파일 업로드 전용 타임스탬프 및 관리자수정/재부팅 격리 검증 완료")

    def test_12_zero_stock_gaemi_lot_excluded_from_expiry_ui_and_summary(self):
        """개미창고 재고 0개 소진 이력(동일 옵션 PJ1489503·500ml)이 소비기한 표시 및 최단 기한 산출에 중복 노출되지 않음을 검증"""
        headers = self._login("1500")
        with open("개미창고_샘플.csv", encoding="utf-8") as f:
            gaemi_rows = list(csv.reader(f))
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})

        # 20EO02에 PJ1489503 + '500ml' 단일 매핑 지정
        res_map = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EO02",
            "gaemi_mappings": [{"gaemi_code": "PJ1489503", "option_name": "500ml"}]
        })
        self.assertEqual(res_map.status_code, 200)

        # 1. 요약 API에서 gaemi_lots는 실재고(>0) 로트 1개만 노출되고, gaemi_details에도 0개 이력이 섞이지 않아야 함
        s1 = json.loads(self.client.get("/api/summary").data)
        eo02 = next(m for m in s1["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(eo02["gaemi_total"], 1208)
        self.assertEqual(len(eo02["csv_gaemi_lots"]), 2)  # raw CSV 로트 감사/매핑용 보존
        self.assertEqual(len(eo02["gaemi_lots"]), 1, "소비기한/FIFO 표시에 재고 0개 로트가 노출되면 안 됨")
        self.assertEqual(eo02["gaemi_lots"][0]["stock"], 1208)
        self.assertEqual(eo02["gaemi_details"], "500ml: 1208개")
        self.assertFalse(eo02["is_mixed_lots"])
        self.assertEqual(eo02["lot_count"], 1)

        # 2. 소진 완료(0개) 과거 로트에 소비기한이 있어도, 보유 재고(>0) 로트의 소비기한이 미지정이면 0개 로트를 최단 소비기한으로 오인하면 안 됨
        empty_lot = next(l for l in eo02["csv_gaemi_lots"] if l["stock"] == 0)
        active_lot = next(l for l in eo02["csv_gaemi_lots"] if l["stock"] > 0)
        self.client.post("/api/admin/products/bulk-save", headers=headers, json={
            "products": [],
            "lot_expiry_overrides": {
                empty_lot["lot_key"]: "2026-01-01"
            }
        })
        s2 = json.loads(self.client.get("/api/summary").data)
        eo02_2 = next(m for m in s2["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(eo02_2["nearest_expiry_date"], "")
        self.assertFalse(eo02_2["has_admin_expiry"])

        # 3. 실재고 로트(1208개)에 2027-10-28 입력 시 해당 기한만 정확히 반영
        self.client.post("/api/admin/products/bulk-save", headers=headers, json={
            "products": [],
            "lot_expiry_overrides": {
                active_lot["lot_key"]: "2027-10-28"
            }
        })
        s3 = json.loads(self.client.get("/api/summary").data)
        eo02_3 = next(m for m in s3["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(eo02_3["nearest_expiry_date"], "2027-10-28")
        self.assertEqual(eo02_3["nearest_lot_stock"], 1208)
        self.assertEqual(len(eo02_3["gaemi_lots"]), 1)

        # 4. 전량 소진 품목(보유 재고 0개)은 gaemi_lots=[] 및 '보유 재고 0개'로 표시
        rows_zero = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            ["1", "PJ1489503", "엘레올로지 500ml", "500ml", "입고완료", "528", "528", "0", "2026-02-25 11:24:56"]
        ]
        self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": rows_zero})
        s4 = json.loads(self.client.get("/api/summary").data)
        eo02_4 = next(m for m in s4["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(eo02_4["gaemi_lots"], [])
        self.assertEqual(eo02_4["gaemi_details"], "보유 재고 0개")
        self.assertEqual(eo02_4["nearest_expiry_date"], "")

        # 5. 프론트엔드 소비기한 일괄 입력·상세모달·코드매핑 집계 필터링 로직 포함 확인
        html = self.client.get("/").data.decode("utf-8")
        self.assertIn("activeCsvLots = rawCsvLots.filter(l => (l.stock || 0) > 0)", html)
        self.assertIn("소진 완료(재고 0개) 과거 입고 이력", html)
        self.assertIn("if (rowStock > 0) entry.lot_count += 1", html)
        print("OK: [Admin Test 12] 개미창고 재고 0개 로트 소비기한 중복 노출/오탐 제거 검증 완료")


if __name__ == '__main__':
    unittest.main()
