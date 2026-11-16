import json
import os
import tempfile
import shutil
import unittest
import copy
import csv
import io
from unittest.mock import patch
from pathlib import Path

from app import app, issue_admin_token
import app as app_module


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        original = app_module.DATA_FILE
        store = tempfile.TemporaryDirectory(prefix='dashboard_test_store_')
        self.addCleanup(store.cleanup)
        self.addCleanup(setattr, app_module, 'DATA_FILE', original)
        copied = Path(store.name) / 'inventory_master_store.json'
        if Path(original).is_file():
            shutil.copy2(original, copied)
        app_module.DATA_FILE = str(copied)

    def test_management_auth_and_csv(self):
        client = app.test_client()
        urls = ['/dashboard_data.json', '/api/dashboard-data', '/api/download-pnl-excel']
        urls += ['/api/export/' + name for name in ('sales', 'cashflow', 'settlements', 'unmatched')]
        for url in urls:
            self.assertNotEqual(client.get(url).status_code, 401)
            self.assertNotEqual(client.get(url, headers={'X-Admin-Token': 'forged'}).status_code, 401)
        self.assertEqual(client.post('/api/cash-flow/manual-transaction', json={}).status_code, 401)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'data.json'
            path.write_text(json.dumps({'sales_profit': {'transactions': [{'product_name': '=SUM(1,2)', 'revenue': 100}],
                                                       'unmatched_products': [['id', 'name', 'option', 'food', 100, 1, '', 'missing', 'file']]},
                                        'cash_flow': {'transactions': [{'customer': '한글,상호', 'deposit_amount': 10}]},
                                        'settlements': {'items': [{'customer': 'test'}]}}), encoding='utf-8')
            with patch.dict(app.config, DASHBOARD_DATA_FILE=str(path)):
                headers = {}
                for name in ('sales', 'cashflow', 'settlements', 'unmatched'):
                    response = client.get('/api/export/' + name, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.data.startswith(b'\xef\xbb\xbf'))
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
                    self.assertEqual(len(rows), 2)
                    if name == 'sales':
                        self.assertEqual(rows[1][0], "'=SUM(1,2)")
                self.assertEqual(client.get('/api/export/unknown', headers=headers).status_code, 404)

    def test_share_inventory_is_read_only_and_inventory_only(self):
        summary = {'masters': [{'name': '공유품목', 'seller_code': 'P', 'gaemi_total': 7,
                               'coupang_physical_total': 3, 'coupang_return_physical_total': 1,
                               'master_physical_total': 10, 'sales_30d': 'SECRET_SALES'}],
                   'stats': {'last_updated': '2026-11-15'}, 'logs': ['SECRET_LOG']}
        with patch.object(app_module, 'get_summary', return_value=app.response_class(
                json.dumps(summary), mimetype='application/json')):
            response = app.test_client().get('/share/inventory')
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-share-mode="true"', html)
        self.assertIn('공유품목', html)
        for forbidden in ('SECRET_SALES', 'SECRET_LOG', '/api/dashboard-data', '<button', '<script'):
            self.assertNotIn(forbidden, html)

    def test_latest_inventory_selection_replaces_snapshot(self):
        state = copy.deepcopy(app_module.app_state)
        signatures = dict(app_module._raw_inventory_signatures)
        try:
            with tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                gaemi = '상품코드,상품명,옵션명,재고량\nP,상품,옵션,{}\n'
                (folder / '2026-11-13_3pl_inventory.csv').write_text(gaemi.format(99), encoding='utf-8')
                (folder / '2026-11-14_3pl_inventory.csv').write_text(gaemi.format(7), encoding='utf-8')
                coupang = '옵션 ID,등록상품명,옵션명,판매가능재고\n{},상품,옵션,{}\n'
                (folder / '2026-11-13_coupang_inventory.csv').write_text(coupang.format('123', 9), encoding='utf-8')
                with patch.dict(app.config, INVENTORY_RAW_DIR=temp), patch.object(
                        app_module, 'inventory_whitelist', return_value=dict(
                            path=str(Path(app_module.BASE_DIR).parent / 'master_data' / '오키드상품정보.xlsx'),
                            gaemi={'P'}, coupang={'123': 'P', '456': 'P'})):
                    app_module.refresh_raw_inventory()
                    self.assertEqual(app_module.app_state['gaemi_inventory']['P'][0]['stock'], 7)
                    (folder / '2026-11-15_coupang_inventory.csv').write_text(coupang.format('456', 3), encoding='utf-8')
                    app_module.refresh_raw_inventory()
                    self.assertNotIn('123', app_module.app_state['coupang_inventory'])
                    self.assertEqual(app_module.app_state['coupang_inventory']['456']['stock'], 3)
        finally:
            app_module.app_state.clear()
            app_module.app_state.update(state)
            app_module._raw_inventory_signatures.clear()
            app_module._raw_inventory_signatures.update(signatures)

    def test_json_routes_and_missing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'dashboard_data.json'
            original = app.config.get('DASHBOARD_DATA_FILE')
            app.config['DASHBOARD_DATA_FILE'] = str(path)
            try:
                client = app.test_client()
                client.environ_base['HTTP_X_ADMIN_TOKEN'] = issue_admin_token()[0]
                self.assertEqual(client.get('/dashboard_data.json').status_code, 404)
                expected = {'schema_version': '1.0', 'unclassified': {'count': 0}}
                path.write_text(json.dumps(expected), encoding='utf-8')
                for url in ('/dashboard_data.json', '/api/dashboard-data'):
                    with client.get(url) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.get_json(), expected)
                        self.assertEqual(response.mimetype, 'application/json')
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
            finally:
                if original is None:
                    app.config.pop('DASHBOARD_DATA_FILE', None)
                else:
                    app.config['DASHBOARD_DATA_FILE'] = original

    def test_download_pnl_excel(self):
        client = app.test_client()
        client.environ_base['HTTP_X_ADMIN_TOKEN'] = issue_admin_token()[0]
        res = client.get('/api/download-pnl-excel')
        # If the file exists in output_excel, status is 200 and mimetype is excel
        if res.status_code == 200:
            self.assertIn('sheet', res.mimetype.lower())
            self.assertIn('attachment', res.headers.get('Content-Disposition', '').lower())
        else:
            self.assertEqual(res.status_code, 404)

    def test_manual_transaction_api(self):
        with tempfile.TemporaryDirectory() as temp:
            d_file = Path(temp) / 'dashboard_data.json'
            m_file = Path(temp) / 'manual_transactions.json'
            
            orig_d = app.config.get('DASHBOARD_DATA_FILE')
            orig_m = app.config.get('MANUAL_TX_FILE')
            app.config['DASHBOARD_DATA_FILE'] = str(d_file)
            app.config['MANUAL_TX_FILE'] = str(m_file)

            try:
                # 초기 대시보드 데이터 작성
                init_data = {
                    'cash_flow': {
                        'monthly': {
                            'month': '2026-11',
                            'deposit_total': 1000,
                            'withdrawal_total': 500,
                            'net_cash_flow': 500,
                            'categories': [
                                {'category': '타계좌입금', 'amount': 1000, 'transaction_count': 1}
                            ]
                        },
                        'recent_transactions': []
                    }
                }
                d_file.write_text(json.dumps(init_data, ensure_ascii=False), encoding='utf-8')
                client = app.test_client()
                client.environ_base['HTTP_X_ADMIN_TOKEN'] = issue_admin_token()[0]

                # 1. 유효하지 않은 데이터 검증 (Bad Request 400)
                bad_cases = [
                    {},
                    {'date': 'invalid-date', 'type': 'deposit', 'amount': 10000, 'partner': '테스트'},
                    {'date': '2026-11-15', 'type': 'unknown', 'amount': 10000, 'partner': '테스트'},
                    {'date': '2026-11-15', 'type': 'deposit', 'amount': -500, 'partner': '테스트'},
                    {'date': '2026-11-15', 'type': 'deposit', 'amount': 10000, 'partner': ''},
                ]
                for payload in bad_cases:
                    res = client.post('/api/cash-flow/manual-transaction', json=payload)
                    self.assertEqual(res.status_code, 400)
                    self.assertIn('error', res.get_json())

                # 2. 정상 입금 등록 테스트
                good_payload = {
                    'date': '2026-11-15',
                    'type': 'deposit',
                    'amount': 250000,
                    'partner': '테스트거래처',
                    'category': '타계좌입금',
                    'memo': '수기 입금 테스트'
                }
                res = client.post('/api/cash-flow/manual-transaction', json=good_payload)
                self.assertEqual(res.status_code, 200)
                json_res = res.get_json()
                self.assertTrue(json_res.get('success'))

                # 3. manual_transactions.json 파일 생성 및 저장 확인
                self.assertTrue(m_file.exists())
                saved_manuals = json.loads(m_file.read_text(encoding='utf-8'))
                self.assertEqual(len(saved_manuals), 1)
                self.assertEqual(saved_manuals[0]['amount'], 250000)
                self.assertEqual(saved_manuals[0]['name'], '테스트거래처')

                # 4. dashboard_data.json 합계 및 최근 거래 반영 확인
                updated_d = json.loads(d_file.read_text(encoding='utf-8'))
                monthly = updated_d['cash_flow']['monthly']
                self.assertEqual(monthly['deposit_total'], 1000 + 250000)
                self.assertEqual(monthly['net_cash_flow'], 500 + 250000)
                recent = updated_d['cash_flow']['recent_transactions']
                self.assertEqual(len(recent), 1)
                self.assertEqual(recent[0]['name'], '테스트거래처')

            finally:
                if orig_d is None:
                    app.config.pop('DASHBOARD_DATA_FILE', None)
                else:
                    app.config['DASHBOARD_DATA_FILE'] = orig_d
                if orig_m is None:
                    app.config.pop('MANUAL_TX_FILE', None)
                else:
                    app.config['MANUAL_TX_FILE'] = orig_m
