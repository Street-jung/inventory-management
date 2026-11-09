import os
import sys

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import unittest
import json
import shutil
import tempfile
import copy
import app as app_module
from app import app

class TestIljeomodoAppEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data_file = app_module.DATA_FILE
        cls._orig_state = copy.deepcopy(app_module.app_state)
        cls._orig_env_pin = os.environ.pop("ADMIN_PIN", None)
        cls._tmpdir = tempfile.mkdtemp(prefix="iljeomodo_test_app_")
        cls._tmp_data_file = os.path.join(cls._tmpdir, "inventory_master_store.json")
        if os.path.exists(cls._orig_data_file):
            shutil.copy2(cls._orig_data_file, cls._tmp_data_file)
        app_module.DATA_FILE = cls._tmp_data_file
        with app_module.data_lock:
            app_module.app_state["settings"]["admin_pin_custom"] = ""

    @classmethod
    def tearDownClass(cls):
        app_module.DATA_FILE = cls._orig_data_file
        with app_module.data_lock:
            app_module.app_state.clear()
            app_module.app_state.update(cls._orig_state)
        if cls._orig_env_pin is not None:
            os.environ["ADMIN_PIN"] = cls._orig_env_pin
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        self.client = app.test_client()

    def _get_admin_headers(self):
        res = self.client.post('/api/admin/verify-pin', json={"pin": "1500"})
        self.assertEqual(res.status_code, 200)
        token = json.loads(res.data)["token"]
        return {"X-Admin-Token": token}

    def test_01_index_html(self):
        """메인 대시보드 서빙 검증"""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn("일쩜오도 재고현황", html)
        self.assertIn("관리자 모드", html)
        self.assertNotIn("1500", html)
        print("OK: [Endpoint Test 1] 일쩜오도 재고현황 메인 HTML 및 관리자 모달 정상 응답")

    def test_02_summary_api(self):
        """통합 집계 API 검증 (38개 단품 마스터 상품, S-코드 세트 상품 제외 확인)"""
        res = self.client.get('/api/summary')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("masters", data)
        self.assertIn("stats", data)
        self.assertEqual(data["stats"]["total_masters"], 38)
        self.assertEqual(len(data["masters"]), 38)
        # S-코드 상품이 목록에서 제외되었는지 확인
        s_codes = [m["seller_code"] for m in data["masters"] if m["seller_code"].startswith("S")]
        self.assertEqual(len(s_codes), 0)
        print(f"OK: [Endpoint Test 2] 38대 단품 마스터 상품 집계 API 검증 성공 (S-코드 제외 확인)")

    def test_03_load_sample_endpoint(self):
        """원클릭 샘플 데이터 로드 API 검증 (PIN 인증 필수)"""
        unauth = self.client.post('/api/load-sample')
        self.assertEqual(unauth.status_code, 401)

        res = self.client.post('/api/load-sample', headers=self._get_admin_headers())
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data["success"])
        print("OK: [Endpoint Test 3] /api/load-sample 엔드포인트 PIN 보호 및 정상 작동")

    def test_04_export_csv_endpoint(self):
        """통합 CSV 리포트 다운로드 검증"""
        res = self.client.get('/api/export-master-csv')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'text/csv')
        self.assertIn("7일 출고량".encode('utf-8'), res.data)
        self.assertNotIn("비고".encode('utf-8'), res.data)
        print("OK: [Endpoint Test 4] /api/export-master-csv 리포트 다운로드 검증")

    def test_05_html_table_columns_no_biko(self):
        """대시보드 HTML 내 테이블 컬럼에서 비고 컬럼 제외 확인"""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn("'total_stock'", html)
        self.assertIn("'gaemi_stock'", html)
        self.assertIn("'coupang_stock'", html)
        self.assertNotIn("'biko'", html)

    def test_06_health_check(self):
        """24시간 클라우드 헬스체크 엔드포인트 검증"""
        res = self.client.get('/health')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "healthy")
        self.assertIn("drive_configured", data)
        print("OK: [Endpoint Test 6] /health 무중단 헬스체크 엔드포인트 정상 작동")
        print("OK: [Endpoint Test 5] 메인 HTML 컬럼 정의에서 비고 컬럼 제외 검증 완료")

if __name__ == '__main__':
    unittest.main()
