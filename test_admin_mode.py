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
        app_module._cancel_boot_recovery()
        app_module.DATA_FILE = cls._orig_data_file
        with app_module.data_lock:
            app_module.app_state.clear()
            app_module.app_state.update(copy.deepcopy(cls._orig_state))
            app_module._reset_blocked_flags_locked()
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
        app_module._cancel_boot_recovery()
        os.environ.pop("ADMIN_PIN", None)
        os.environ.pop("GOOGLE_APPS_SCRIPT_URL", None)
        os.environ.pop("GOOGLE_APPS_SCRIPT_TOKEN", None)
        with app_module.data_lock:
            app_module.app_state.clear()
            app_module.app_state.update(copy.deepcopy(self._orig_state))
            app_module.app_state["settings"]["admin_pin_custom"] = ""
            app_module._reset_blocked_flags_locked()
        app_module._admin_revoked_before = 0
        app_module.drive_sync_state.clear()
        app_module.drive_sync_state.update(copy.deepcopy(self._orig_drive_state))
        app_module.drive_sync_state["configured"] = False
        app_module.drive_sync_state["status"] = "disabled"
        app_module.drive_sync_state["sync_blocked"] = False
        app_module.drive_sync_state["boot_restore_failed"] = False
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

    def test_13_no_sales_alarm_column_resizer_and_compact_subrow(self):
        """소진무(재고 보유 · 판매 0) 알람 추천 조치, 열 너비 조정 UI/JS, 컴팩트 sticky 세부행 레이아웃 검증"""
        headers = self._login("1500")

        # 1. 재고·매출 초기화 후 3개 케이스 구성:
        #    (A) 20EO02: 개미 재고 1208개, 판매 0건, 먼 소비기한(2028-12-31) -> 🟠 판매 정체 (소진무) / 🔔 미판매 점검 알람
        #    (B) 20EK02: 개미 재고 982개, 판매 0건, 임박 소비기한(30일 남음) -> ⏳ 기한 주의 유지 + is_no_sales=True
        #    (C) 20DC02: 개미 400개 + 쿠팡 실물 60개(쿠팡 판매 0), 쿠팡외 일평균 1개(7일 7개) -> 정상 안전 + is_coupang_no_sales=True
        #    (D) 10GL04: 재고 0개, 판매 0건 -> 🔴 결품 (0개), is_no_sales=False
        far_exp = (datetime.now() + timedelta(days=600)).strftime("%Y-%m-%d")
        near_exp = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        with app_module.data_lock:
            app_module.app_state["gaemi_inventory"] = {
                "PJ1489503": [
                    {
                        "lot_id": "PJ1489503::500ml::01",
                        "gaemi_code": "PJ1489503",
                        "option_name": "500ml",
                        "stock": 1208,
                        "expiry_date": far_exp,
                        "reg_date": "2026-10-01",
                    }
                ],
                "PJ1491144": [
                    {
                        "lot_id": "PJ1491144::정제 1L::01",
                        "gaemi_code": "PJ1491144",
                        "option_name": "정제 1L",
                        "stock": 982,
                        "expiry_date": near_exp,
                        "reg_date": "2026-10-01",
                    }
                ],
                "PJ_DC02_TEST": [
                    {
                        "lot_id": "PJ_DC02_TEST::500ml::01",
                        "gaemi_code": "PJ_DC02_TEST",
                        "option_name": "500ml",
                        "stock": 400,
                        "expiry_date": far_exp,
                        "reg_date": "2026-10-01",
                    }
                ],
            }
            app_module.app_state["coupang_inventory"] = {
                "OPT_DC02_COUPANG_ONLY": {
                    "stock": 60,
                    "sales_7d": 0,
                    "sales_30d": 0,
                    "option_name": "1개",
                    "product_name": "데체코 올리브오일",
                }
            }
            app_module.app_state["sales_inventory"] = {
                "20DC02": {
                    "total_7d": 7,
                    "total_30d": 30,
                    "orders": [{"date": "2026-11-09 10:00:00", "qty": 1, "mall": "스마트스토어", "name": "데체코"}],
                }
            }
            app_module.app_state["lot_expiry_overrides"] = {}

        # 매핑 지정: 20EO02, 20EK02, 20DC02
        self.assertEqual(self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EO02",
            "gaemi_mappings": [{"gaemi_code": "PJ1489503", "option_name": "500ml"}],
            "sales_codes": [],
            "options": [],
        }).status_code, 200)
        self.assertEqual(self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EK02",
            "gaemi_mappings": [{"gaemi_code": "PJ1491144", "option_name": "정제 1L"}],
            "sales_codes": [],
            "options": [],
        }).status_code, 200)
        self.assertEqual(self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20DC02",
            "gaemi_mappings": [{"gaemi_code": "PJ_DC02_TEST", "option_name": "500ml"}],
            "sales_codes": ["20DC02"],
            "options": [{"id": "OPT_DC02_COUPANG_ONLY", "bundle": 1, "name": "1개"}],
        }).status_code, 200)

        res = self.client.get("/api/summary")
        self.assertEqual(res.status_code, 200)
        summary = json.loads(res.data)
        stats = summary["stats"]
        masters_by_code = {m["seller_code"]: m for m in summary["masters"]}

        # (A) 20EO02: 소진무 판매 정체 알람 판정 확인
        eo02 = masters_by_code["20EO02"]
        self.assertEqual(eo02["master_physical_total"], 1208)
        self.assertEqual(eo02["daily_sales"], 0)
        self.assertEqual(eo02["total_runway_str"], "소진무")
        self.assertEqual(eo02["status"], "🟠 판매 정체 (소진무)")
        self.assertEqual(eo02["status_color"], "orange")
        self.assertEqual(eo02["action_cta"], "🔔 미판매 점검 알람")
        self.assertTrue(eo02["is_no_sales"])
        self.assertGreaterEqual(stats["no_sales_count"], 1)

        # (B) 20EK02: 임박 소비기한 우선 -> purple 유지 + is_no_sales True
        ek02 = masters_by_code["20EK02"]
        self.assertEqual(ek02["status_color"], "purple")
        self.assertIn("기한 주의", ek02["status"])
        self.assertTrue(ek02["is_no_sales"])

        # (C) 20DC02: 타채널 판매로 전체는 정상 안전(D-460일), 쿠팡만 실물 보유+미판매 -> is_coupang_no_sales=True
        dc02 = masters_by_code["20DC02"]
        self.assertEqual(dc02["status_color"], "green")
        self.assertIn("정상 안전", dc02["status"])
        self.assertFalse(dc02["is_no_sales"])
        self.assertEqual(dc02["coupang_runway_str"], "소진무")
        self.assertTrue(dc02["is_coupang_no_sales"])

        # (D) 10GL04: 재고 0개 -> 결품 (orange 아님)
        gl04 = masters_by_code["10GL04"]
        self.assertEqual(gl04["master_physical_total"], 0)
        self.assertEqual(gl04["status_color"], "red")
        self.assertFalse(gl04["is_no_sales"])

        # (E) risk_score 정렬 우선순위 확인: purple(350) > orange(250~299) > green(<100)
        self.assertGreater(ek02["risk_score"], eo02["risk_score"])
        self.assertGreater(eo02["risk_score"], dc02["risk_score"])

        # 7대 상태 상호배타 합계 == total_masters 확인
        kpi_sum = (
            stats["urgent_order_count"]
            + stats["order_prepare_count"]
            + stats["coupang_transfer_count"]
            + stats["expiry_warning_count"]
            + stats["in_transit_count"]
            + stats["no_sales_count"]
            + stats["safe_count"]
        )
        self.assertEqual(kpi_sum, stats["total_masters"])

        # CSV 다운로드에도 판매 정체(소진무) 및 미판매 점검 알람 반영 확인
        csv_res = self.client.get("/api/export-master-csv")
        self.assertEqual(csv_res.status_code, 200)
        csv_text = csv_res.data.decode("utf-8-sig")
        self.assertIn("🟠 판매 정체 (소진무)", csv_text)
        self.assertIn("🔔 미판매 점검 알람", csv_text)

        # 2. 프론트엔드 HTML/JS 검증 (소진무 KPI/필터, 열 너비 조정, 컴팩트 sticky 세부행 레이아웃, PIN 미노출)
        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        for marker in (
            'id="statNoSales"',
            "setStatusFilter('no_sales')",
            "판매 정체 (소진무)",
            'id="masterTableColGroup"',
            'id="colWidthAdjustPanel"',
            "col-resizer",
            "startColumnResize",
            "iljeomodo_col_widths_v1",
            "applyColWidthPreset",
            "resetSingleColWidth",
            "syncSubrowViewportWidth",
            "ResizeObserver",
            "subrow-sticky-shell",
            "--subrow-view-w",
            "680px",
            "쿠팡 윙 옵션별 번들 · 실물 재고",
            "판매 정체(소진무) 알람:",
        ):
            self.assertIn(marker, html, f"Missing UI/JS marker: {marker}")

        print("OK: [Admin Test 13] 소진무 알람 판정/우선순위/CSV, 열 너비 조정, 컴팩트 sticky 세부행 UI 검증 완료")

    def test_14_in_transit_arrival_replaces_urgent_order_prompts(self):
        """입고 예정일/수량 등록 품목이 '긴급 발주/즉시 발주' 대신 '입고 예정/입고 대기/추가 검토' 표현으로 전환되는지 검증"""
        headers = self._login("1500")
        fixed_today = datetime(2026, 11, 11, 11, 30, 0, tzinfo=timezone(timedelta(hours=9)))
        far_exp = "2028-12-31"

        with app_module.data_lock:
            app_module.app_state["gaemi_inventory"] = {
                # 10GL02: 현재 40개 (일판매 10 -> D-4일) + 11-18(+4320) -> 입고전 소진주의
                "PJ_GL02_T": [{"lot_id": "GL02::1", "gaemi_code": "PJ_GL02_T", "option_name": "기본", "stock": 40, "expiry_date": far_exp, "reg_date": "2026-10-01"}],
                # 10GL04: 현재 250개 (일판매 10 -> D-25일) + 11-18(+2880) -> 입고 예정 (합산 313일)
                "PJ_GL04_T": [{"lot_id": "GL04::1", "gaemi_code": "PJ_GL04_T", "option_name": "기본", "stock": 250, "expiry_date": far_exp, "reg_date": "2026-10-01"}],
                # 10GL05: 현재 100개 (일판매 10 -> D-10일) + 11-18(+500) -> 입고포함 D-60일 (< 110) -> 추가 발주 검토
                "PJ_GL05_T": [{"lot_id": "GL05::1", "gaemi_code": "PJ_GL05_T", "option_name": "기본", "stock": 100, "expiry_date": far_exp, "reg_date": "2026-10-01"}],
                # 20TZ01: 현재 200개 (일판매 10 -> D-20일) + 11-18(+1000) -> 입고포함 D-120일 (110~130) -> 입고 예정 · 추가준비
                "PJ_TZ01_T": [{"lot_id": "TZ01::1", "gaemi_code": "PJ_TZ01_T", "option_name": "기본", "stock": 200, "expiry_date": far_exp, "reg_date": "2026-10-01"}],
                # 20TZ03: 현재 250개 (일판매 10 -> D-25일), 입고예정 0 -> 기존 긴급 발주 유지 비교군
                "PJ_TZ03_T": [{"lot_id": "TZ03::1", "gaemi_code": "PJ_TZ03_T", "option_name": "기본", "stock": 250, "expiry_date": far_exp, "reg_date": "2026-10-01"}],
                # 20EO02: 현재 200개 (일판매 10 -> D-20일) + 날짜미정(+2000) -> 날짜 없음 graceful fallback
                "PJ_EO02_T": [{"lot_id": "EO02::1", "gaemi_code": "PJ_EO02_T", "option_name": "기본", "stock": 200, "expiry_date": far_exp, "reg_date": "2026-10-01"}],
            }
            app_module.app_state["coupang_inventory"] = {}
            app_module.app_state["sales_inventory"] = {
                code: {"total_7d": 70, "total_30d": 300, "orders": [{"date": "2026-11-10 10:00:00", "qty": 10, "mall": "스마트스토어", "name": code}]}
                for code in ("10GL02", "10GL04", "10GL05", "20TZ01", "20TZ03", "20EO02")
            }
            app_module.app_state["lot_expiry_overrides"] = {}

        # 매핑 & 입고 예정 등록
        test_configs = [
            ("10GL06", [], 2880, "2026-11-18", "수입"),  # 현재 0개(품절) + 11-18(+2880)
            ("10GL02", [{"gaemi_code": "PJ_GL02_T", "option_name": "기본"}], 4320, "2026-11-18", "수입"),
            ("10GL04", [{"gaemi_code": "PJ_GL04_T", "option_name": "기본"}], 2880, "2026-11-18", "수입"),
            ("10GL05", [{"gaemi_code": "PJ_GL05_T", "option_name": "기본"}], 500, "2026-11-18", "수입"),
            ("20TZ01", [{"gaemi_code": "PJ_TZ01_T", "option_name": "기본"}], 1000, "2026-11-18", "수입"),
            ("20TZ03", [{"gaemi_code": "PJ_TZ03_T", "option_name": "기본"}], 0, "", "수입"),
            ("20EO02", [{"gaemi_code": "PJ_EO02_T", "option_name": "기본"}], 2000, "", "수입"),
        ]
        bulk_products = []
        for code, gmaps, transit, arr_date, ptype in test_configs:
            self.assertEqual(self.client.post("/api/admin/product/mappings/save", headers=headers, json={
                "seller_code": code,
                "gaemi_mappings": gmaps,
                "sales_codes": [code],
                "options": [],
            }).status_code, 200)
            bulk_products.append({
                "seller_code": code,
                "product_type": ptype,
                "in_transit_qty": transit,
                "expected_arrival_date": arr_date,
                "lead_time_days": None,
                "safety_buffer_days": None,
            })
        self.assertEqual(self.client.post("/api/admin/products/bulk-save", headers=headers, json={
            "products": bulk_products
        }).status_code, 200)

        with patch.object(app_module, "now_kst", return_value=fixed_today):
            res = self.client.get("/api/summary")
        self.assertEqual(res.status_code, 200)
        summary = json.loads(res.data)
        by_code = {m["seller_code"]: m for m in summary["masters"]}
        stats = summary["stats"]

        # 1. 10GL06 (품절 + 11-18 입고예정): 결품 즉시 발주 대신 입고 대기
        gl06 = by_code["10GL06"]
        self.assertEqual(gl06["status"], "🚢 입고 대기 (품절 · 11-18)")
        self.assertEqual(gl06["status_color"], "teal")
        self.assertEqual(gl06["action_cta"], "🚚 11-18 입고 확인")
        self.assertTrue(gl06["is_in_transit_covered"])
        self.assertNotIn("결품 즉시 발주", gl06["action_cta"])

        # 2. 10GL02 (D-4일 < 7일 후 입고): 입고 전 소진주의 + 점검
        gl02 = by_code["10GL02"]
        self.assertEqual(gl02["status"], "🚚 입고 전 소진주의 (11-18)")
        self.assertEqual(gl02["status_color"], "teal")
        self.assertEqual(gl02["action_cta"], "🚚 11-18 입고 점검")
        self.assertTrue(gl02["pre_arrival_gap"])
        self.assertGreater(gl02["effective_runway"], 130)

        # 3. 10GL04 (D-25일 + 11-18(+2880)): 긴급 발주 대신 입고 예정/입고 대기
        gl04 = by_code["10GL04"]
        self.assertEqual(gl04["status"], "🚢 입고 예정 (11-18)")
        self.assertEqual(gl04["status_color"], "teal")
        self.assertEqual(gl04["action_cta"], "📦 11-18 입고 대기")
        self.assertFalse(gl04["pre_arrival_gap"])
        self.assertEqual(gl04["effective_total_with_transit"], 3130)

        # 4. 10GL05 (입고 포함해도 D-60일 < 110일): 기존 '긴급 발주'가 아니라 '추가 발주 검토'
        gl05 = by_code["10GL05"]
        self.assertIn("추가 발주 검토", gl05["status"])
        self.assertIn("입고포함 D-60일", gl05["status"])
        self.assertEqual(gl05["status_color"], "red")
        self.assertEqual(gl05["action_cta"], "📋 추가 발주 검토")

        # 5. 20TZ01 (입고 포함 D-120일, 110~130 버퍼 구간): 입고 예정 · 추가준비
        tz01 = by_code["20TZ01"]
        self.assertEqual(tz01["status"], "🚢 입고 예정 · 추가준비 (11-18)")
        self.assertEqual(tz01["status_color"], "teal")
        self.assertEqual(tz01["action_cta"], "📋 추가 발주 확인")

        # 6. 20EO02 (입고예정일 없이 수량만 입력): '+2,000개' fallback
        eo02 = by_code["20EO02"]
        self.assertEqual(eo02["status"], "🚢 입고 예정 (+2,000개)")
        self.assertEqual(eo02["status_color"], "teal")
        self.assertEqual(eo02["action_cta"], "📦 입고 일정 확인")

        # 7. 20TZ03 (입고예정 미등록 대조군): 기존 긴급 발주 유지
        tz03 = by_code["20TZ03"]
        self.assertIn("긴급 발주", tz03["status"])
        self.assertEqual(tz03["status_color"], "red")
        self.assertEqual(tz03["action_cta"], "🚨 수입 발주 기안")

        # 8. 카운터 & 우선순위(미발주 긴급발주 > 입고대기) & CSV & HTML UI 확인
        self.assertEqual(stats["in_transit_count"], 5)
        self.assertGreater(tz03["risk_score"], gl04["risk_score"])
        self.assertGreater(gl02["risk_score"], gl04["risk_score"])

        csv_text = self.client.get("/api/export-master-csv").data.decode("utf-8-sig")
        self.assertIn("🚢 입고 예정 (11-18)", csv_text)
        self.assertIn("📦 11-18 입고 대기", csv_text)

        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        for marker in ('id="statInboundWait"', "setStatusFilter('inbound')", "발주 완료 · 입고 예정:", "입고포함"):
            self.assertIn(marker, html)
        print("OK: [Admin Test 14] 입고 예정일·수량 등록 시 입고 대기/예정/소진주의/추가검토 전환 검증 완료")

    def test_15_unmapped_explorer_hide_restore_persistence(self):
        """CSV 미매핑 탐색기 상품별 숨기기/복원/전체복원 API·정규화·백업복원·자동해제·UI 검증"""
        # 1. 비인증 차단 확인
        unauth_res = self.client.post("/api/admin/unmapped-explorer/hidden", json={"key": "sales::TEST1", "hidden": True})
        self.assertEqual(unauth_res.status_code, 401)

        headers = self._login("1500")

        # 2. 잘못된 요청 검증
        bad_res1 = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={"action": "unknown"})
        self.assertEqual(bad_res1.status_code, 400)
        bad_res2 = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={"key": "badformat", "hidden": True})
        self.assertEqual(bad_res2.status_code, 400)

        # 3. 3개 채널 개별 숨기기 + 대소문자/공백 정규화 + 중복 방지
        r1 = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "key": " sales::unmap_s1 ",
            "hidden": True,
            "label": "UNMAP_S1"
        })
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(json.loads(r1.data)["hidden_unmapped_codes"], ["sales::UNMAP_S1"])

        # 동일 키 대문자 재요청 시 중복 추가되지 않아야 함
        r1_dup = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "key": "SALES::UNMAP_S1",
            "hidden": True
        })
        self.assertEqual(r1_dup.status_code, 200)
        self.assertEqual(json.loads(r1_dup.data)["hidden_unmapped_codes"], ["sales::UNMAP_S1"])

        r2 = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "key": "gaemi::pj_unmap_g1||옵션 A ",
            "hidden": True
        })
        self.assertEqual(r2.status_code, 200)
        r3 = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "key": "coupang:: 999888777 ",
            "hidden": True
        })
        self.assertEqual(r3.status_code, 200)
        expected_three = ["sales::UNMAP_S1", "gaemi::PJ_UNMAP_G1||옵션 A", "coupang::999888777"]
        self.assertEqual(json.loads(r3.data)["hidden_unmapped_codes"], expected_three)

        # 4. /api/admin/config 및 로컬 재부팅(load_state), JSON 백업 내보내기/복원 영속성 검증
        cfg1 = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        self.assertEqual(cfg1["hidden_unmapped_codes"], expected_three)

        backup_json = json.loads(self.client.get("/api/admin/backup/export", headers=headers).data)
        self.assertEqual(backup_json["hidden_unmapped_codes"], expected_three)

        with app_module.data_lock:
            app_module.app_state["hidden_unmapped_codes"] = []
        app_module.load_state()
        self.assertEqual(app_module.app_state["hidden_unmapped_codes"], expected_three)

        # 전체 복원(clear) -> 빈 목록
        clr_res = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={"action": "clear"})
        self.assertEqual(clr_res.status_code, 200)
        self.assertEqual(json.loads(clr_res.data)["hidden_unmapped_codes"], [])

        # JSON 백업 복원(import) 시 3건 다시 복구
        imp_res = self.client.post("/api/admin/backup/import", headers=headers, json={"data": backup_json})
        self.assertEqual(imp_res.status_code, 200)
        cfg2 = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        self.assertEqual(cfg2["hidden_unmapped_codes"], expected_three)

        # 5. 다건 일괄 숨기기/일괄 복원 + 단건 복원 + 매핑 연결 시 숨김 목록 자동 해제 검증
        self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "action": "hide",
            "keys": ["sales::KEEP_HIDDEN", "gaemi::PJ_UNMAP_G1||와일드테스트", "coupang::555444333", "sales::BULK_RESTORE_1", "coupang::777666555"]
        })
        batch_unhide = self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "action": "unhide",
            "keys": ["sales::BULK_RESTORE_1", "coupang::777666555"]
        })
        self.assertEqual(batch_unhide.status_code, 200)
        self.assertNotIn("sales::BULK_RESTORE_1", json.loads(batch_unhide.data)["hidden_unmapped_codes"])
        self.assertNotIn("coupang::777666555", json.loads(batch_unhide.data)["hidden_unmapped_codes"])
        self.client.post("/api/admin/unmapped-explorer/hidden", headers=headers, json={
            "key": "coupang::999888777",
            "hidden": False
        })
        cfg3 = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        self.assertNotIn("coupang::999888777", cfg3["hidden_unmapped_codes"])
        self.assertIn("sales::KEEP_HIDDEN", cfg3["hidden_unmapped_codes"])

        # UNMAP_S1, PJ_UNMAP_G1(와일드카드 *로 2개 옵션 모두), 555444333 을 20DC02에 매핑 -> 숨김에서 자동 해제됨!
        map_res = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20DC02",
            "sales_codes": ["20DC02", "UNMAP_S1"],
            "gaemi_mappings": [{"gaemi_code": "PJ_UNMAP_G1", "option_name": "*"}],
            "options": [{"id": "555444333", "bundle": 1, "name": "단품"}]
        })
        self.assertEqual(map_res.status_code, 200)
        cfg4 = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        self.assertEqual(cfg4["hidden_unmapped_codes"], ["sales::KEEP_HIDDEN"])

        # 구버전 백업(hidden_unmapped_codes 없음) 로드 시 [] 안전 초기화
        app_module._apply_loaded_state({"master_products": copy.deepcopy(app_module.app_state["master_products"])})
        self.assertEqual(app_module.app_state["hidden_unmapped_codes"], [])

        # 6. 프론트엔드 탐색기 숨기기 및 일괄 변경 UI/JS 마커 및 PIN 미노출 검증
        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        for marker in (
            'id="acMapCsvHiddenMode"',
            'id="acMapCsvHiddenSummary"',
            'id="acMapCsvHiddenCountBadge"',
            'id="acMapCsvBulkBar"',
            'id="acMapCsvSelectAll"',
            'id="acMapCsvSelectAllBar"',
            'id="btnAcMapBulkHideSelected"',
            'id="btnAcMapBulkRestoreSelected"',
            'id="btnAcMapBulkHideFiltered"',
            "toggleHideUnmappedCsvRow",
            "toggleAcMapOnlyHiddenQuick",
            "clearAllHiddenUnmappedCsvRows",
            "toggleSelectAllAcMapExplorerRows",
            "onClickAcMapExplorerCheckbox",
            "bulkToggleHideAcMapCsvRows",
            "/api/admin/unmapped-explorer/hidden",
            "숨김 제외 (필요한 항목만)",
            "숨김 항목만 보기",
            "선택 일괄 숨김",
            "선택 일괄 복원",
            "필터 전체 숨김",
            "숨기기",
            "복원",
        ):
            self.assertIn(marker, html, f"Missing UI/JS marker: {marker}")
        print("OK: [Admin Test 15] 미매핑 코드 탐색기 숨기기/복원/전체복원·백업·자동해제·UI 검증 완료")

    def test_16_coupang_return_grade_mapping_and_bundle_display(self):
        """쿠팡 G열(상품등급 '반품') 파싱·매핑·정상/반품 분리집계·번들별 반품재고·UI 검증"""
        headers = self._login("1500")
        self.assertEqual(self.client.post("/api/load-sample", headers=headers).status_code, 200)

        # 1. 실제 샘플 헤더 구조(G열=상품등급, index 6) + 정상/다양한 반품 등급 업로드 파싱 검증
        header1 = [
            "No.", "등록상품 ID", "옵션 ID", "SKU ID", "등록상품명", "옵션명", "상품등급",
            "판매가능재고 (실시간 기준)", "입고예정재고(실시간 기준)", "아이템위너",
            "최근 매출 (번들 매출 제외)", "", "최근 판매수량", "", "예상 소진일 (DOC)"
        ]
        header2 = ["", "", "", "", "", "", "", "", "", "", "지난 7일", "지난 30일", "지난 7일", "지난 30일", ""]
        sample_rows = [
            header1,
            header2,
            # 정상 1개입 (기존 엘레올로지 20EO02 옵션)
            ["1", "16323534607", "95844803290", "77323013", "[고항산화]엘레올로지 500ml", "1개 500ml", "NEW", "100", "0", "아이템위너", "0", "0", "5", "20", "20"],
            # 정상 2개입
            ["2", "16323534607", "95849997061", "77323012", "[고항산화]엘레올로지 500ml", "2개 500ml", "NEW", "10", "0", "아이템위너", "0", "0", "2", "6", "5"],
            # 반품 1개입 (최상)
            ["3", "16323534607", "95999000011", "88000011", "[고항산화]엘레올로지 500ml", "1개 500ml", "반품 - 최상", "3", "0", "아이템위너", "0", "0", "1", "2", "10"],
            # 반품 1개입 (상 · 동일 번들 다중 반품 등급 합산 확인용)
            ["4", "16323534607", "95999000013", "88000013", "[고항산화]엘레올로지 500ml", "1개 500ml", "반품-상", "4", "0", "아이템위너", "0", "0", "0", "1", "0"],
            # 반품 2개입 (미개봉 · 명시적 is_return 플래그 없이 CSV G열만으로 반품 판별되는지도 동시 검증!)
            ["5", "16323534607", "95999000012", "88000012", "[고항산화]엘레올로지 500ml", "2개 500ml", "반품(미개봉)", "2", "0", "아이템위너", "0", "0", "1", "1", "0"],
            # 결품 정상재고 0 + 반품재고 5개 테스트용 별도 상품(20TZ03) 반품 행
            ["6", "16323539035", "95999000021", "88000021", "테라찌노 라비앙콜릴라 250ml", "1개 250ml", "반품 - 최상", "5", "0", "아이템위너", "0", "0", "0", "0", "0"],
        ]

        parsed = app_module.parse_coupang_rows(sample_rows)
        self.assertIn("95844803290", parsed)
        self.assertFalse(parsed["95844803290"]["is_return"])
        self.assertEqual(parsed["95844803290"]["grade"], "NEW")
        self.assertEqual(parsed["95844803290"]["product_id"], "16323534607")

        self.assertTrue(parsed["95999000011"]["is_return"])
        self.assertEqual(parsed["95999000011"]["grade"], "반품 - 최상")
        self.assertEqual(parsed["95999000011"]["product_id"], "16323534607")
        self.assertEqual(parsed["95999000011"]["stock"], 3)

        self.assertTrue(parsed["95999000012"]["is_return"])
        self.assertEqual(parsed["95999000012"]["grade"], "반품(미개봉)")

        # 최소 헤더(상품등급 없는 7열 CSV)에서 6번 인덱스(입고예정/수량)가 등급으로 오인되지 않아야 함
        minimal_rows = [
            ["No.", "등록상품 ID", "옵션 ID", "등록상품명", "옵션명", "판매가능재고", "입고예정재고"],
            ["1", "111", "777000111", "테스트", "1개", "10", "반품아님아니더라도"]
        ]
        min_parsed = app_module.parse_coupang_rows(minimal_rows)
        self.assertFalse(min_parsed["777000111"]["is_return"])
        self.assertEqual(min_parsed["777000111"]["grade"], "")

        # 업로드 API 반영 확인
        up_res = self.client.post("/api/upload-file-data", json={"source": "coupang", "rows": sample_rows})
        self.assertEqual(up_res.status_code, 200)
        up_json = json.loads(up_res.data)
        self.assertTrue(up_json["success"])
        self.assertEqual(up_json["return_count"], 4)

        # 2. 엘레올로지(20EO02)에 정상 + 반품 옵션 매핑 저장
        # (95999000011, 95999000013 은 is_return=True, 95999000012 는 일부러 is_return 생략해 CSV G열 기반 자동 반품 분류 확인)
        eo02_options = [
            {"id": "95844803290", "bundle": 1, "name": "1개 500ml"},
            {"id": "95849997061", "bundle": 2, "name": "2개 500ml"},
            {"id": "95999000011", "bundle": 1, "name": "1개 500ml (반품-최상)", "is_return": True},
            {"id": "95999000013", "bundle": 1, "name": "1개 500ml (반품-상)", "is_return": True},
            {"id": "95999000012", "bundle": 2, "name": "2개 500ml (반품)"},
        ]
        m_res = self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "20EO02",
            "options": eo02_options
        })
        self.assertEqual(m_res.status_code, 200)
        saved_eo02 = json.loads(m_res.data)["product"]
        # 정상 옵션은 기존 형식 유지(is_return 키 없음), 반품 지정 옵션은 is_return=True 저장
        self.assertNotIn("is_return", saved_eo02["options"][0])
        self.assertTrue(saved_eo02["options"][2]["is_return"])

        # 3. 정상재고=0 / 개미재고=0 인 품목(20TZ03)에 반품재고 5개 매핑 시 정상 총가용재고에 합산되지 않고 결품 유지 확인
        self.client.post("/api/admin/product/save", headers=headers, json={
            "seller_code": "20TZ03",
            "name": "테라찌노 비앙콜릴라 250ml",
            "gaemi_mappings": [],
            "gaemi_codes": [],
            "options": [{"id": "95999000021", "bundle": 1, "name": "1개 250ml (반품)", "is_return": True}],
            "in_transit_qty": 0
        })

        # 4. /api/summary 에서 정상 vs 반품 분리 집계 & 번들별 그룹화 검증
        sum_json = json.loads(self.client.get("/api/summary").data)
        by_code = {m["seller_code"]: m for m in sum_json["masters"]}

        eo = by_code["20EO02"]
        # 정상 쿠팡: 1개입 100세트(100개) + 2개입 10세트(20개) = 110세트, 120개
        self.assertEqual(eo["coupang_package_total"], 110)
        self.assertEqual(eo["coupang_physical_total"], 120)
        # 정상 출고: 1개입 5*1 + 2개입 2*2 = 9개 (반품 출고 미포함)
        self.assertEqual(eo["coupang_sales_7d"], 9)
        self.assertEqual(eo["master_physical_total"], eo["gaemi_total"] + 120)

        # 반품 쿠팡: 1개입 (3 + 4 = 7세트 -> 7개) + 2개입 (2세트 -> 4개) = 9세트, 11개
        self.assertEqual(eo["coupang_return_package_total"], 9)
        self.assertEqual(eo["coupang_return_physical_total"], 11)
        self.assertEqual(eo["coupang_return_sales_7d"], 1 * 1 + 1 * 2)
        self.assertTrue(eo["has_coupang_return"])

        # 번들별 그룹화(coupang_bundle_groups) 검증
        groups = {g["bundle"]: g for g in eo["coupang_bundle_groups"]}
        self.assertEqual(sorted(groups.keys()), [1, 2])
        self.assertEqual(groups[1]["normal_package_stock"], 100)
        self.assertEqual(groups[1]["normal_physical_stock"], 100)
        self.assertEqual(groups[1]["return_package_stock"], 7)
        self.assertEqual(groups[1]["return_physical_stock"], 7)
        self.assertEqual(len(groups[1]["return_options"]), 2)
        self.assertEqual(groups[1]["return_options"][0]["grade"], "반품 - 최상")

        self.assertEqual(groups[2]["normal_package_stock"], 10)
        self.assertEqual(groups[2]["normal_physical_stock"], 20)
        self.assertEqual(groups[2]["return_package_stock"], 2)
        self.assertEqual(groups[2]["return_physical_stock"], 4)
        self.assertTrue(groups[2]["return_options"][0]["is_return"])
        self.assertEqual(groups[2]["return_options"][0]["grade"], "반품(미개봉)")

        # 결품 상품(20TZ03)은 반품 5개가 있어도 정상 재고 0개 / 결품 상태 유지
        tz03 = by_code["20TZ03"]
        self.assertEqual(tz03["master_physical_total"], 0)
        self.assertEqual(tz03["coupang_physical_total"], 0)
        self.assertEqual(tz03["coupang_return_physical_total"], 5)
        self.assertEqual(tz03["status"], "🔴 결품 (0개)")

        # stats 전체 반품 합계 확인 (20EO02 11개 + 20TZ03 5개 + 기타 매핑분 이상)
        self.assertGreaterEqual(sum_json["stats"]["total_coupang_return_stock"], 16)

        # 5. /api/admin/config, JSON 백업 내보내기/복원, CSV 내보내기 검증
        cfg = json.loads(self.client.get("/api/admin/config", headers=headers).data)
        self.assertTrue(cfg["coupang_inventory"]["95999000011"]["is_return"])
        self.assertEqual(cfg["coupang_inventory"]["95999000011"]["grade"], "반품 - 최상")
        self.assertEqual(cfg["coupang_inventory"]["95999000011"]["product_id"], "16323534607")

        backup_json = json.loads(self.client.get("/api/admin/backup/export", headers=headers).data)
        imp_res = self.client.post("/api/admin/backup/import", headers=headers, json={"data": backup_json})
        self.assertEqual(imp_res.status_code, 200)
        sum_after = json.loads(self.client.get("/api/summary").data)
        eo_after = next(m for m in sum_after["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(eo_after["coupang_return_physical_total"], 11)

        csv_text = self.client.get("/api/export-master-csv").data.decode("utf-8-sig")
        self.assertIn("쿠팡 반품(실물) 재고", csv_text)
        self.assertNotIn("비고", csv_text)

        # 6. UI/JS 마커 및 PIN 비노출 확인
        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        for marker in (
            'id="statCoupangReturnWrap"',
            'id="statCoupangReturnStock"',
            'value="coupang_return"',
            'id="cm_coupang_return_input"',
            "suggestMasterForCoupangCsvRow",
            "coupang_bundle_groups",
            "쿠팡 윙 옵션별 번들 · 실물 재고",
            "반품 실물",
            "🔄 반품",
        ):
            self.assertIn(marker, html, f"Missing Coupang return marker: {marker}")
        print("OK: [Admin Test 16] 쿠팡 G열(반품) 파싱·매핑·분리집계·번들별 반품 표시·백업·UI 검증 완료")

    def test_17_coupang_return_parser_edge_cases_and_legacy_remediation(self):
        """쿠팡 엑셀/CSV(10행 이상 배너·2단헤더·float ID·NFD·대체컬럼명) 파싱 및 기존 데이터 반품 전환(재업로드/즉시지정) 검증"""
        import unicodedata
        headers = self._login("1500")

        # 1. 실제 엑셀/CSV 변형 edge-case 파싱: 상단 8행 배너 + 2단 헤더 + 줄바꿈 헤더 + float ID(.0) + None + NFD 한글
        banner_rows = [["쿠팡 윙 재고 건전성 리포트"], ["다운로드 일시: 2026-11-11"], [], ["안내"], [""], [""], ["주의"], [""]]
        split_h1 = [
            "No.", "등록상품\nID", "옵션\nID", "SKU ID", "등록상품명", "옵션명", "상품\n상태",
            "판매가능재고\n(실시간 기준)", "입고예정재고\n(실시간 기준)", "아이템위너",
            "최근 매출", "", "최근\n판매수량", "", "예상 소진일\n(DOC)", "재고예상\n소진일", "고객반품 지난 30일"
        ]
        split_h2 = ["", "", "", "", "", "", "", "", "", "", "지난 7일", "지난 30일", "지난 7일", "지난 30일", "", "", ""]
        nfd_return_grade = unicodedata.normalize("NFD", "반품 - 최상")
        excel_style_rows = banner_rows + [
            split_h1,
            split_h2,
            [1, 16323600679.0, 95893436821.0, 77764478, "구욘 초코칩 통밀쿠키", "1개 265g", "NEW", 117.0, 0, None, None, None, 57.0, 120.0, 20, "20일 이내", 5],
            [2, 16323600679.0, 96254147518.0, 88888881, "구욘 초코칩 통밀쿠키", "1개 265g", nfd_return_grade, 1.0, None, None, None, None, 0.0, 0.0, "-", "-", 0],
            [3, "16323600679.0", "96135189857.0", "88888882", "구욘 초코칩 통밀쿠키", "2개 265g", "Return - Like New", "0.0", "0", "아이템위너", "0", "0", "0", "0", "-", "-", "0"],
        ]

        parsed = app_module.parse_coupang_rows(excel_style_rows)
        self.assertIn("95893436821", parsed)
        self.assertIn("96254147518", parsed)
        self.assertIn("96135189857", parsed)
        self.assertFalse(parsed["95893436821"]["is_return"])
        self.assertEqual(parsed["95893436821"]["stock"], 117)
        self.assertEqual(parsed["95893436821"]["sales_7d"], 57)
        self.assertEqual(parsed["95893436821"]["doc"], 20)
        self.assertEqual(parsed["95893436821"]["product_id"], "16323600679")

        self.assertTrue(parsed["96254147518"]["is_return"])
        self.assertEqual(parsed["96254147518"]["grade"], "반품 - 최상")
        self.assertEqual(parsed["96254147518"]["stock"], 1)
        self.assertTrue(parsed["96135189857"]["is_return"])
        self.assertEqual(parsed["96135189857"]["stock"], 0)

        # 2. 구버전(등급 G열 업데이트 이전) Google Drive 상태 재현:
        #    10GL04에 개미 475개 + 95893436821(117) + 96254147518(1, grade/is_return 누락 상태로 매핑) + 96135189857(0)
        with app_module.data_lock:
            app_module.app_state["gaemi_inventory"] = {
                "PJ1489270": [{
                    "product_name": "구욘 초코칩 쿠키",
                    "option_name": "구욘 초코칩 쿠키",
                    "stock": 475,
                    "reg_date": "",
                    "expiry_date": "",
                    "days_left": None
                }]
            }
            app_module.app_state["coupang_inventory"] = {
                "95893436821": {"product_name": "구욘 초코칩 쿠키", "option_name": "1개 265g", "stock": 117, "inbound": 0, "sales_7d": 57, "sales_30d": 120, "doc": 20},
                "96254147518": {"product_name": "구욘 초코칩 쿠키", "option_name": "1개 265g", "stock": 1, "inbound": 0, "sales_7d": 0, "sales_30d": 0, "doc": 0},
                "96135189857": {"product_name": "구욘 초코칩 쿠키", "option_name": "2개 265g", "stock": 0, "inbound": 0, "sales_7d": 0, "sales_30d": 0, "doc": 0},
            }

        self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "10GL04",
            "options": [
                {"id": "95893436821", "bundle": 1, "name": "1개 265g"},
                {"id": "96254147518", "bundle": 1, "name": "1개 265g"},
                {"id": "95893661910", "bundle": 2, "name": "2개 265g"},
                {"id": "96135189857", "bundle": 2, "name": "2개 265g"},
                {"id": "95893661912", "bundle": 3, "name": "3개 265g"},
            ]
        })

        s_legacy = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s_legacy["stats"]["coupang_missing_grade_count"], 3)
        gl04_legacy = next(m for m in s_legacy["masters"] if m["seller_code"] == "10GL04")
        opt_9625_legacy = next(o for o in gl04_legacy["options"] if o["id"] == "96254147518")
        self.assertTrue(opt_9625_legacy["grade_missing"])
        self.assertFalse(opt_9625_legacy["is_return"])
        # 업데이트 전에는 정상 118로 잡히고 총 가용 593
        self.assertEqual(gl04_legacy["coupang_physical_total"], 118)
        self.assertEqual(gl04_legacy["master_physical_total"], 593)

        # 3-A. 관리자 즉시 반품 전환(toggleCoupangOptionReturn 동작): CSV 재업로드 전에도 96254147518 만 is_return=True 지정 시 즉시 분리
        self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "10GL04",
            "options": [
                {"id": "95893436821", "bundle": 1, "name": "1개 265g"},
                {"id": "96254147518", "bundle": 1, "name": "1개 265g", "is_return": True},
                {"id": "95893661910", "bundle": 2, "name": "2개 265g"},
                {"id": "96135189857", "bundle": 2, "name": "2개 265g"},
                {"id": "95893661912", "bundle": 3, "name": "3개 265g"},
            ]
        })
        s_toggled = json.loads(self.client.get("/api/summary").data)
        gl04_tog = next(m for m in s_toggled["masters"] if m["seller_code"] == "10GL04")
        self.assertEqual(gl04_tog["coupang_physical_total"], 117)
        self.assertEqual(gl04_tog["coupang_return_physical_total"], 1)
        self.assertEqual(gl04_tog["master_physical_total"], 592)

        # 다시 수동 플래그 해제 후, 3-B. 새 쿠팡 파일 재업로드만으로 자동 반품 분리되는지 확인
        self.client.post("/api/admin/product/mappings/save", headers=headers, json={
            "seller_code": "10GL04",
            "options": [
                {"id": "95893436821", "bundle": 1, "name": "1개 265g"},
                {"id": "96254147518", "bundle": 1, "name": "1개 265g"},
                {"id": "95893661910", "bundle": 2, "name": "2개 265g"},
                {"id": "96135189857", "bundle": 2, "name": "2개 265g"},
                {"id": "95893661912", "bundle": 3, "name": "3개 265g"},
            ]
        })
        up_res = self.client.post("/api/upload-file-data", json={"source": "coupang", "rows": excel_style_rows})
        self.assertEqual(up_res.status_code, 200)
        self.assertEqual(json.loads(up_res.data)["return_count"], 2)

        s_after_upload = json.loads(self.client.get("/api/summary").data)
        self.assertEqual(s_after_upload["stats"]["coupang_missing_grade_count"], 0)
        self.assertEqual(s_after_upload["stats"]["coupang_return_option_count"], 2)
        gl04_auto = next(m for m in s_after_upload["masters"] if m["seller_code"] == "10GL04")
        self.assertEqual(gl04_auto["coupang_physical_total"], 117)
        self.assertEqual(gl04_auto["coupang_return_physical_total"], 1)
        self.assertEqual(gl04_auto["master_physical_total"], 592)
        b_groups = {g["bundle"]: g for g in gl04_auto["coupang_bundle_groups"]}
        self.assertEqual(b_groups[1]["normal_physical_stock"], 117)
        self.assertEqual(b_groups[1]["return_physical_stock"], 1)
        self.assertEqual(len(b_groups[1]["return_options"]), 1)
        self.assertEqual(b_groups[1]["return_options"][0]["id"], "96254147518")
        self.assertTrue(b_groups[1]["return_options"][0]["csv_is_return"])
        self.assertEqual(len(b_groups[2]["return_options"]), 1)
        self.assertEqual(b_groups[2]["return_options"][0]["id"], "96135189857")

        # 4. UI/JS 마커 및 인코딩/시트/즉시전환 코드, PIN 비노출 검증
        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        for marker in (
            "decodeTextBytes",
            "TextDecoder",
            "euc-kr",
            "pickWorkbookRows",
            "defval: ''",
            "toggleCoupangOptionReturn",
            'id="acMapCoupangLegacyAlert"',
            "ac-map-exp-return",
            "coupang_missing_grade_count",
            "🔄 반품 지정",
            "↩️ 정상 복귀",
            "등급미확인",
        ):
            self.assertIn(marker, html, f"Missing marker: {marker}")
        print("OK: [Admin Test 17] 쿠팡 파서 edge-case·인코딩·등급누락 경고·즉시반품지정·자동분리 검증 완료")

    def test_18_drive_boot_failure_overwrite_protection_and_recovery(self):
        """부팅 시 Google Drive 복원 실패 시 자동 덮어쓰기 차단, 수동 강제백업 가드, 자동복구+임시업로드 병합 검증"""
        headers = self._login("1500")
        exp_res = self.client.get("/api/admin/backup/export", headers=headers)
        remote_snap = json.loads(exp_res.data)
        remote_snap["master_products"][0]["note"] = "원격 Drive 최신 메모(절대 보존)"
        remote_snap["lot_expiry_overrides"] = {
            "PJ1489503||500ml||2026-09-15 16:31:50": "2028-04-30"
        }

        os.environ["GOOGLE_APPS_SCRIPT_URL"] = "https://script.google.com/macros/s/fake-id/exec"
        os.environ["GOOGLE_APPS_SCRIPT_TOKEN"] = "fake-secret-token"
        store_box = {"save_calls": 0, "load_calls": 0, "saved_payload": None, "remote_data": remote_snap}

        # 1. 부팅 시 Drive 통신 실패 시뮬레이션 -> sync_blocked=True, status='blocked'
        with patch("app.time.sleep", return_value=None), patch.dict(os.environ, {"ILJEOMODO_SKIP_BG_RECOVERY": "1"}):
            with patch("urllib.request.build_opener", side_effect=lambda *a, **kw: FakeOpener(store_box, fail_mode=True)):
                app_module.load_state()
            self.assertTrue(app_module.drive_sync_state["sync_blocked"])
            self.assertTrue(app_module.drive_sync_state["boot_restore_failed"])
            self.assertEqual(app_module.drive_sync_state["status"], "blocked")

            # 2. Drive가 아직 잠긴 상태에서 개미 CSV 업로드 발생 -> 로컬만 반영, Drive 덮어쓰기 차단!
            with open("개미창고_샘플.csv", encoding="utf-8") as f:
                gaemi_rows = list(csv.reader(f))
            with patch("urllib.request.build_opener", side_effect=lambda *a, **kw: FakeOpener(store_box, fail_mode=False)):
                up_res = self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_rows})
                self.assertEqual(up_res.status_code, 200)
                up_data = json.loads(up_res.data)
                self.assertTrue(up_data["drive_sync"]["sync_blocked"])
                self.assertEqual(up_data["drive_sync"]["status"], "blocked")
                self.assertEqual(store_box["save_calls"], 0, "잠금 상태에서 Drive 자동 덮어쓰기가 실행되면 안 됨")

                # 3. sync-now는 force=True 없이 409 require_force 반환해야 함
                sync_res = self.client.post("/api/admin/drive/sync-now", headers=headers, json={})
                self.assertEqual(sync_res.status_code, 409)
                sync_json = json.loads(sync_res.data)
                self.assertTrue(sync_json.get("require_force"))
                self.assertEqual(store_box["save_calls"], 0)

                # 4. 백그라운드 복구 1회 실행 -> 원격 메모 + 소비기한 오버라이드 복원 + 차단 중 업로드된 개미 CSV 유지 + 안전 동기화
                ok = app_module._attempt_boot_drive_recovery_once()
                self.assertTrue(ok)
                self.assertFalse(app_module.drive_sync_state["sync_blocked"])
                self.assertFalse(app_module.drive_sync_state["boot_restore_failed"])
                self.assertEqual(app_module.drive_sync_state["status"], "synced")
                self.assertEqual(app_module.app_state["master_products"][0]["note"], "원격 Drive 최신 메모(절대 보존)")
                self.assertEqual(
                    app_module.app_state["lot_expiry_overrides"].get("PJ1489503||500ml||2026-09-15 16:31:50"),
                    "2028-04-30"
                )
                self.assertGreater(len(app_module.app_state["gaemi_inventory"]), 0)
                self.assertEqual(store_box["save_calls"], 1)
                self.assertEqual(
                    store_box["saved_payload"]["master_products"][0]["note"],
                    "원격 Drive 최신 메모(절대 보존)"
                )

                # 5. 빈 master_products를 가진 원격 응답은 복원 거부(기존 데이터 보호)
                store_box["remote_data"] = {"master_products": []}
                pull_bad = self.client.post("/api/admin/drive/pull-now", headers=headers)
                self.assertEqual(pull_bad.status_code, 500)
                self.assertEqual(app_module.app_state["master_products"][0]["note"], "원격 Drive 최신 메모(절대 보존)")

                # 6. 잠금 상태에서 force=True 로 명시적 덮어쓰기 시 잠금 해제 검증
                app_module.drive_sync_state["sync_blocked"] = True
                app_module.drive_sync_state["status"] = "blocked"
                force_res = self.client.post("/api/admin/drive/sync-now", headers=headers, json={"force": True})
                self.assertEqual(force_res.status_code, 200)
                self.assertFalse(json.loads(force_res.data)["drive_sync"]["sync_blocked"])

        print("OK: [Admin Test 18] Google Drive 부팅 복원 실패 시 덮어쓰기 차단 및 자동 복구·병합 검증 완료")

    def test_19_excel_serial_lot_key_normalization_case_nfc_and_clear(self):
        """Excel 시리얼 등록일·소수점 수량·NFD/대소문자 매칭·오버라이드 Alias 정리 및 동일 일자 다른 로트 격리 검증"""
        import unicodedata
        headers = self._login("1500")

        # 1. 기존 Drive 데이터에 Excel 시리얼 float(46280.68877314815 == 2026-09-15 16:31:50) 로 저장된 오버라이드 재현
        with app_module.data_lock:
            app_module.app_state["lot_expiry_overrides"] = {
                "PJ1489503||500ml||46280.68877314815": "2027-09-20",
                # 동일 일자 다른 시각 로트 (10:00:00 vs 16:31:50) 격리 테스트
                "PJ1489503||500ml||2026-09-15 10:00:00": "2027-04-01",
            }
            app_module.app_state["gaemi_inventory"] = {
                "PJ1489503": [
                    {
                        "product_name": "데체코 엑스트라버진 올리브오일",
                        "option_name": "500ml",
                        "stock": 500,
                        "reg_date": 46280.68877314815,
                        "expiry_date": "",
                        "days_left": None,
                    },
                    {
                        "product_name": "데체코 엑스트라버진 올리브오일",
                        "option_name": "500ml",
                        "stock": 100,
                        "reg_date": "2026-09-15 10:00:00",
                        "expiry_date": "",
                        "days_left": None,
                    },
                ]
            }

        s_old = json.loads(self.client.get("/api/summary").data)
        m_eo02 = next(m for m in s_old["masters"] if m["seller_code"] == "20EO02")
        lots_map = {l["lot_key"]: l for l in m_eo02["csv_gaemi_lots"]}
        self.assertIn("PJ1489503||500ml||2026-09-15 16:31:50", lots_map)
        self.assertEqual(lots_map["PJ1489503||500ml||2026-09-15 16:31:50"]["expiry_date"], "2027-09-20")
        self.assertEqual(lots_map["PJ1489503||500ml||2026-09-15 10:00:00"]["expiry_date"], "2027-04-01")

        # 2. Excel (.xlsx) Raw Serial + float string('500.0') + NFD 옵션명 + 소문자 레거시/명시 코드 업로드 테스트
        nfd_opt = unicodedata.normalize("NFD", "구욘 다크초코")
        gaemi_xlsx_rows = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            [1, "PJ1489503", "데체코 EV 올리브오일", "500ml", "정상", "1000", "500", "500.0", 46280.68877314815],
            [2, "20EK03", "엘레올로지", nfd_opt, "정상", 10, 0, "10.0", "2026-10-01 09:00:00"],
            [3, "10GL02", "구욘", unicodedata.normalize("NFD", "구욘 오렌지 265g"), "정상", 20, 0, "20.0", "2026-10-02 09:00:00"],
        ]
        # 20EK03 는 소문자 명시 매핑 + NFC 옵션, 10GL02 는 소문자 레거시 gaemi_codes 로 매핑하여 대소문자/NFC 정규화 동시 검증
        with app_module.data_lock:
            for m in app_module.app_state["master_products"]:
                if m["seller_code"] == "20EK03":
                    m["gaemi_codes"] = ["20ek03"]
                    m["gaemi_mappings"] = [{"gaemi_code": "20ek03", "option_name": unicodedata.normalize("NFC", "구욘 다크초코")}]
                elif m["seller_code"] == "10GL02":
                    m["gaemi_mappings"] = []
                    m["gaemi_codes"] = ["10gl02"]

        up_xlsx = self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_xlsx_rows})
        self.assertEqual(up_xlsx.status_code, 200)

        # reg_date가 '2026-09-15 16:31:50' 으로 정규화되었고 stock '500.0' -> 500 파싱 검증
        self.assertEqual(app_module.app_state["gaemi_inventory"]["PJ1489503"][0]["reg_date"], "2026-09-15 16:31:50")
        self.assertEqual(app_module.app_state["gaemi_inventory"]["PJ1489503"][0]["stock"], 500)
        self.assertEqual(app_module.app_state["gaemi_inventory"]["20EK03"][0]["stock"], 10)
        self.assertEqual(app_module.app_state["gaemi_inventory"]["10GL02"][0]["stock"], 20)

        s_xlsx = json.loads(self.client.get("/api/summary").data)
        m_eo02_2 = next(m for m in s_xlsx["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(m_eo02_2["nearest_expiry_date"], "2027-09-20")
        m_ek03 = next(m for m in s_xlsx["masters"] if m["seller_code"] == "20EK03")
        self.assertEqual(m_ek03["gaemi_total"], 10)
        m_gl02 = next(m for m in s_xlsx["masters"] if m["seller_code"] == "10GL02")
        self.assertEqual(m_gl02["gaemi_total"], 20)

        # 3. 동일 데이터를 이번엔 CSV 문자열 등록일('2026-09-15 16:31:50')로 재업로드해도 동일 소비기한 보존
        gaemi_csv_rows = [
            ["번호", "상품코드", "상품명", "옵션명", "상태", "입고량", "출고량", "재고량", "등록일"],
            [1, "PJ1489503", "데체코 EV 올리브오일", "500ml", "정상", "1000", "500", "480", "2026-09-15 16:31:50"],
        ]
        up_csv = self.client.post("/api/upload-file-data", json={"source": "gaemi", "rows": gaemi_csv_rows})
        self.assertEqual(up_csv.status_code, 200)
        s_csv = json.loads(self.client.get("/api/summary").data)
        m_eo02_3 = next(m for m in s_csv["masters"] if m["seller_code"] == "20EO02")
        self.assertEqual(m_eo02_3["nearest_expiry_date"], "2027-09-20")

        # 4. 정규화 키로 __RESTORE__ 실행 시 과거 Excel 시리얼 별칭 키까지 함께 깔끔히 제거되는지 검증
        save_res = self.client.post("/api/admin/product/save", headers=headers, json={
            "id": m_eo02_3["id"],
            "seller_code": "20EO02",
            "name": m_eo02_3["name"],
            "product_type": m_eo02_3["product_type"],
            "category": m_eo02_3["category"],
            "csv_lot_expiries": {
                "PJ1489503||500ml||2026-09-15 16:31:50": "__RESTORE__"
            }
        })
        self.assertEqual(save_res.status_code, 200)
        self.assertNotIn("PJ1489503||500ml||46280.68877314815", app_module.app_state["lot_expiry_overrides"])
        self.assertNotIn("PJ1489503||500ml||2026-09-15 16:31:50", app_module.app_state["lot_expiry_overrides"])

        # 5. 매출 파서 Excel 시리얼 주문일 + float string('3.0') 주문수량 검증
        sales_rows = [
            ["주문일", "묶음번호", "쇼핑몰", "온라인상품명", "주문수량", "판매자관리코드"],
            [46280.5, "B1", "스마트스토어", "테스트", "3.0", "20ek03"],
        ]
        parsed_sales = app_module.parse_sales_rows(sales_rows)
        self.assertIn("20EK03", parsed_sales)
        self.assertEqual(parsed_sales["20EK03"]["total_7d"], 3)
        self.assertEqual(parsed_sales["20EK03"]["orders"][0]["date"], "2026-09-15 12:00:00")

        # 6. 프론트엔드 보호 UI 및 정규화 JS 함수 존재 검증 (PIN 비노출 유지)
        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        for marker in (
            'id="driveProtectionBanner"',
            "pullDriveNowFromBanner",
            "excelSerialToJsIso",
            "normalizeJsGaemiRegDate",
            "canonicalizeJsLotKey",
            "resolveJsLotOverride",
        ):
            self.assertIn(marker, html, f"Missing marker: {marker}")

        print("OK: [Admin Test 19] Excel 시리얼/CSV 양방향 로트키 정규화·NFC/대소문자 매칭·오버라이드 정리 검증 완료")

    def test_20_compact_bundle_subrow_detail_modal_and_sales_date_norm(self):
        """인라인 행 확장은 쿠팡 번들 재고+세부정보보기만 표시하고, 전체 내역은 팝업 모달로 분리되며, 기존 매출 Excel 시리얼 날짜도 정규화되는지 검증"""
        # 1. 기존 저장 데이터(구 Excel 시리얼 날짜 포함)가 /api/summary 에서 사람이 읽을 수 있는 일시로 정규화되는지 확인
        with app_module.data_lock:
            app_module.app_state["sales_inventory"] = {
                "10GL04": {
                    "total_7d": 6,
                    "total_30d": 6,
                    "orders": [
                        {"date": "46316.69378472222", "qty": 6, "mall": "스마트스토어", "name": "구온 초코칩"}
                    ]
                },
                "S100009": {
                    "total_7d": 6,
                    "total_30d": 6,
                    "orders": [
                        {"date": "46307.60758101852", "qty": 3, "mall": "카페24(신)", "name": "B2B 박스"},
                        {"date": "46318.446921296294", "qty": 3, "mall": "카페24(신)", "name": "B2B 박스"},
                    ]
                }
            }

        summary = json.loads(self.client.get("/api/summary").data)
        gl04 = next(m for m in summary["masters"] if m["seller_code"] == "10GL04")
        self.assertGreaterEqual(len(gl04["recent_orders"]), 3)
        dates = [o["date"] for o in gl04["recent_orders"]]
        # raw float serial이 남아있지 않아야 하며 최신순 정렬(2026-10-23 -> 2026-10-21 -> 2026-10-12)이어야 함
        for d in dates:
            self.assertNotIn("463", d)
            self.assertRegex(d, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(dates, sorted(dates, reverse=True))

        # 2. HTML/JS 마커 및 UI 구조 분리(인라인=쿠팡 번들 전용 + 세부정보보기 / 팝업=전체 세부정보) 검증
        html = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("1500", html)
        self.assertNotIn("비고", html)
        self.assertNotIn("'biko'", html)

        for marker in (
            'id="productDetailModal"',
            'id="pd_modal_badges"',
            'id="pd_modal_title"',
            'id="pd_modal_body"',
            'id="pd_modal_admin_actions"',
            "openProductDetailModal",
            "closeProductDetailModal",
            "renderProductDetailModal",
            "세부정보보기",
            "번들 재고 펼치기",
            "번들 재고 접기",
            "쿠팡 윙 옵션별 번들 · 실물 재고",
            "subrow-sticky-shell",
        ):
            self.assertIn(marker, html, f"Missing UI/JS marker: {marker}")

        # 인라인 subrow 렌더 블록에는 소비기한 FIFO/소진 시뮬레이션/쿠팡외 출고가 없어야 하고,
        # 팝업 모달 렌더 함수(renderProductDetailModal)에는 모두 포함되어야 함
        subrow_start = html.find('<tr id="subrow_${escapeHtml(m.id)}"')
        tfoot_start = html.find("// 5. 하단 합계란(tfoot) 렌더링")
        self.assertGreater(subrow_start, 0)
        self.assertGreater(tfoot_start, subrow_start)
        subrow_section = html[subrow_start:tfoot_start]
        self.assertNotIn("개미창고 소비기한 · FIFO", subrow_section)
        self.assertNotIn("소진 시뮬레이션:", subrow_section)
        self.assertNotIn("B2B/세트 연동:", subrow_section)

        modal_fn_start = html.find("function renderProductDetailModal(prodId)")
        modal_fn_end = html.find("async function toggleCoupangOptionReturn(")
        self.assertGreater(modal_fn_start, 0)
        self.assertGreater(modal_fn_end, modal_fn_start)
        modal_fn_section = html[modal_fn_start:modal_fn_end]
        for detail_marker in (
            "개미창고 소비기한 · FIFO",
            "소진 시뮬레이션:",
            "B2B/세트 연동:",
            "쿠팡외 출고:",
            "normalizeJsGaemiRegDate",
            "판매 정체(소진무) 알람:",
        ):
            self.assertIn(detail_marker, modal_fn_section, f"Missing detail modal marker: {detail_marker}")

        print("OK: [Admin Test 20] 쿠팡 번들 전용 인라인 확장 + 세부정보보기 팝업 모달 분리 + 매출 주문일 정규화 검증 완료")


if __name__ == '__main__':
    unittest.main()
