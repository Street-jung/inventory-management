import os
import sys

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import unittest
import json
from app import app

class TestIljeomodoAppEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_01_index_html(self):
        """메인 대시보드 서빙 검증"""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        self.assertIn("일쩜오도 재고현황".encode('utf-8'), res.data)
        print("OK: [Endpoint Test 1] 일쩜오도 재고현황 메인 HTML 정상 응답")

    def test_02_summary_api(self):
        """통합 집계 API 검증 (44개 마스터 상품)"""
        res = self.client.get('/api/summary')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn("masters", data)
        self.assertIn("stats", data)
        self.assertEqual(data["stats"]["total_masters"], 44)
        print(f"OK: [Endpoint Test 2] 44대 마스터 상품 집계 API 검증 성공")

    def test_03_load_sample_endpoint(self):
        """원클릭 샘플 데이터 로드 API 검증"""
        res = self.client.post('/api/load-sample')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data["success"])
        print("OK: [Endpoint Test 3] /api/load-sample 엔드포인트 정상 작동")

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
        print("OK: [Endpoint Test 5] 메인 HTML 컬럼 정의에서 비고 컬럼 제외 검증 완료")

if __name__ == '__main__':
    unittest.main()
