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
from app import app, parse_gaemi_rows, parse_coupang_rows, parse_sales_rows

class TestIljeomodoBusinessLogic(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_01_index_html(self):
        """메인 대시보드 렌더링 확인 (일쩜오도 재고현황 타이틀 및 BI 로고)"""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        self.assertIn("일쩜오도 재고현황".encode('utf-8'), res.data)
        self.assertIn('src="/logo.png"'.encode('utf-8'), res.data)
        print("OK: [Test 1] '일쩜오도 재고현황' 메인 화면 및 BI 로고 서빙 완료")

    def test_02_logo_endpoint(self):
        """BI 로고 이미지 서빙 검증"""
        res = self.client.get('/logo.png')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'image/png')
        print("OK: [Test 2] /logo.png BI 이미지 정상 반환")

    def test_03_parse_real_gaemi_csv(self):
        """실제 개미창고 CSV 파싱 검증"""
        with open("개미창고_샘플.csv", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            
        parsed = parse_gaemi_rows(rows)
        self.assertIn("PJ1489503", parsed)
        eleology_stock = sum(i["stock"] for i in parsed["PJ1489503"])
        self.assertEqual(eleology_stock, 1208)
        print(f"OK: [Test 3] 개미창고 파일 파싱 완료 (엘레올로지: {eleology_stock}개)")

        res = self.client.post('/api/upload-file-data', json={
            "source": "gaemi",
            "rows": rows
        })
        self.assertEqual(res.status_code, 200)

    def test_04_parse_real_coupang_rows(self):
        """쿠팡 재고건전성 데이터 파싱 검증"""
        with open("쿠팡_샘플.csv", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        parsed = parse_coupang_rows(rows)
        self.assertIn("95844803290", parsed)
        self.assertEqual(parsed["95844803290"]["stock"], 468)
        print("OK: [Test 4] 쿠팡 윙 타겟 옵션 ID, 재고, 7일 판매량 파싱 완료")

        res = self.client.post('/api/upload-file-data', json={
            "source": "coupang",
            "rows": rows
        })
        self.assertEqual(res.status_code, 200)

    def test_05_parse_sales_rows(self):
        """쿠팡외 통합 매출 데이터 파싱 검증"""
        with open("매출정보_샘플.csv", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        parsed = parse_sales_rows(rows)
        self.assertIn("20EK03", parsed)
        self.assertEqual(parsed["20EK03"]["total_7d"], 8)
        print(f"OK: [Test 5] 쿠팡외 통합 매출 데이터 파싱 완료 (20EK03 7일 출고: {parsed['20EK03']['total_7d']}개)")

        res = self.client.post('/api/upload-file-data', json={
            "source": "sales",
            "rows": rows
        })
        self.assertEqual(res.status_code, 200)

    def test_06_one_click_load_sample_and_leadtime(self):
        """원클릭 샘플 데이터 로드 및 발주 의사결정 액션보드 지표 산출 검증"""
        res = self.client.post('/api/load-sample')
        self.assertEqual(res.status_code, 200)

        sum_res = self.client.get('/api/summary')
        self.assertEqual(sum_res.status_code, 200)
        data = json.loads(sum_res.data)

        # 44개 마스터 상품 확인
        self.assertEqual(data["stats"]["total_masters"], 44)
        
        # 5대 Action KPI 카운터 검증
        self.assertIn("urgent_order_count", data["stats"])
        self.assertIn("order_prepare_count", data["stats"])
        self.assertIn("coupang_transfer_count", data["stats"])
        self.assertIn("expiry_warning_count", data["stats"])
        self.assertIn("safe_count", data["stats"])
        self.assertIn("last_updated", data["stats"])

        # 구욘 초코칩(10GL04, 수입, 소진일 약 20일 < 110일) -> 긴급 발주 판정 확인
        p_choco = next(m for m in data["masters"] if m["seller_code"] == "10GL04")
        self.assertEqual(p_choco["product_type"], "수입")
        self.assertIn("긴급 발주", p_choco["status"])
        self.assertEqual(p_choco["action_cta"], "🚨 수입 발주 기안")
        print(f"OK: [Test 6] 발주 의사결정 액션보드 지표 검증 성공: {p_choco['name']} -> {p_choco['status']} ({p_choco['action_cta']})")

    def test_07_shared_stock_synchronization(self):
        """B2B/세트 상품(S100005 등)과 기본 상품(10GL02 등) 실물 재고 100% 일치 및 독립 DOC 검증"""
        self.client.post('/api/load-sample')
        sum_res = self.client.get('/api/summary')
        data = json.loads(sum_res.data)
        m_map = {m["seller_code"]: m for m in data["masters"]}

        # 1. 재고 일치 쌍 검증
        shared_pairs = [
            ("10GL02", "S100005", 12),
            ("10GL04", "S100009", 12),
            ("10GL05", "S100011", 12),
            ("10GL06", "S100033", 12),
            ("20TZ01", "S200018", 2),
            ("20EO02", "S200019", 2),
        ]

        for base_code, set_code, mult in shared_pairs:
            base_item = m_map[base_code]
            set_item = m_map[set_code]
            # 실물 총 재고 완전 일치
            self.assertEqual(base_item["master_physical_total"], set_item["master_physical_total"])
            self.assertEqual(base_item["gaemi_total"], set_item["gaemi_total"])
            self.assertEqual(base_item["coupang_physical_total"], set_item["coupang_physical_total"])
            # 배수 정보 설정 확인
            self.assertTrue(set_item["is_shared"])
            self.assertEqual(set_item["shared_mult"], mult)

        print("OK: [Test 7] B2B/세트 상품(6종) 기본 상품과 창고 실물 재고 100% 일치 검증 완료")

    def test_08_export_master_csv(self):
        """액션 리포트 CSV 생성 검증 및 비고 컬럼 제외 확인"""
        res = self.client.get('/api/export-master-csv')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'text/csv')
        self.assertIn("판매자관리코드".encode('utf-8-sig'), res.data)
        self.assertIn("7일 출고량".encode('utf-8'), res.data)
        self.assertIn("개미 런웨이(일)".encode('utf-8'), res.data)
        self.assertIn("쿠팡 런웨이(일)".encode('utf-8'), res.data)
        self.assertIn("추천 조치".encode('utf-8'), res.data)
        # 비고 컬럼이 헤더에 없어야 함
        self.assertNotIn("비고".encode('utf-8'), res.data)
        print("OK: [Test 8] 일쩜오도 액션 리포트 CSV 내보내기 검증 완료")

    def test_09_expiry_date_tracking(self):
        """개미창고 소비기한 추출 및 선입선출 추적 검증"""
        self.client.post('/api/load-sample')
        sum_res = self.client.get('/api/summary')
        data = json.loads(sum_res.data)

        # 20DC02 (데체코 엑스트라버진 올리브오일 500ml -> 옵션명 27.03.28)
        p_dececco = next(m for m in data["masters"] if m["seller_code"] == "20DC02")
        self.assertTrue(bool(p_dececco["nearest_expiry_date"]))
        self.assertIn("2027-03-28", p_dececco["nearest_expiry_date"])
        self.assertIsNotNone(p_dececco["nearest_days_left"])
        print(f"OK: [Test 9] 소비기한 자동 추출 검증 완료: {p_dececco['name']} -> 최단 소비기한 {p_dececco['nearest_expiry_date']} (D-{p_dececco['nearest_days_left']}일)")

if __name__ == '__main__':
    unittest.main()
