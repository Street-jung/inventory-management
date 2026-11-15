import unittest
import tempfile
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from .channel_whitelist import load_whitelist, filter_inventory
from .common import SALES_FIELDS, write_csv
from . import generate_monthly_pnl as pnl
from .export_dashboard_json import profit_summary


class WhitelistTests(unittest.TestCase):
    def test_normalized_sales_require_registered_option_not_seller_code(self):
        whitelist = load_whitelist()
        option, code = next(iter(whitelist['coupang'].items()))
        base = dict.fromkeys(SALES_FIELDS, '')
        base.update(channel='coupang_growth', order_id='order', payment_date='2026-11-14',
                    product_code=code, quantity='1', sales_amount='100', currency='KRW')
        with tempfile.TemporaryDirectory() as temp:
            write_csv(Path(temp) / 'sales.csv', [dict(base, item_id='FOREIGN'),
                                               dict(base, item_id=option, product_code='WRONG')], SALES_FIELDS)
            rows, _ = pnl.read_sales('2026-11', temp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['product_code'], code)

    def test_actual_master_and_inventory_codes(self):
        whitelist = load_whitelist()
        self.assertEqual(len(whitelist['product_codes']), 38)
        self.assertEqual(len(whitelist['coupang']), 39)
        self.assertEqual(len(whitelist['gaemi']), 28)
        for source, allowed in [('gaemi', whitelist['gaemi']), ('coupang', whitelist['coupang'])]:
            code = next(iter(allowed))
            self.assertEqual(filter_inventory({code: 3, 'FOREIGN': 9}, source, whitelist), {code: 3})
            self.assertEqual(filter_inventory({'FOREIGN': 9}, source, whitelist), {})

    def test_foreign_same_name_and_company_code_do_not_bypass_option_id(self):
        products, fees, _ = pnl.load_master()
        row = dict(channel='coupang_growth', order_id='order', item_id='FOREIGN',
                   paid=date(2026, 11, 14), product_code='10GL02', product_name=products['10GL02']['name'],
                   quantity=1, sales_amount=100, status='', source='foreign.xlsx')
        self.assertEqual(pnl.prepare_rows([row], products, fees), ([], []))

    def test_actual_coupang_and_missing_company_cost(self):
        products, fees, sources = pnl.load_master()
        with patch.object(pnl, 'today', return_value=date(2026, 11, 15)):
            sales, unmatched, files = pnl.read_coupang_temporary('2026-11', products, sources)
        dated_sales, _, dated_files = pnl.read_coupang_temporary('2026-11', products, sources, date(2026, 11, 15))
        self.assertEqual((sales, files), (dated_sales, dated_files))
        self.assertEqual(len(files), 1)
        detail, issues = pnl.prepare_rows(sales, products, fees)
        self.assertEqual(unmatched, [])
        self.assertEqual(issues, [])
        summary = profit_summary(detail)
        allowed = load_whitelist()['coupang']
        raw = pnl.read_excel_sheets(Path(files[0]))[0][1]
        source_rows = [dict(zip(raw[0], r)) for r in raw[1:]]
        owned = [r for r in source_rows if pnl.normalize_code(r['옵션 ID']) in allowed and r['카테고리'] == '식품']
        self.assertEqual(len(detail), len(owned))
        self.assertEqual(summary['revenue'], sum(Decimal(str(r['매출(원)'])) for r in owned))
        self.assertEqual(summary['contribution_profit'], summary['revenue'] - summary['cost'] - summary['fees'])
        code = sales[0]['product_code']
        products[code] = dict(products[code], cost=None)
        detail, issues = pnl.prepare_rows([sales[0]], products, fees)
        self.assertEqual(len(issues), 1)
        self.assertIsNone(profit_summary(detail)['contribution_profit'])

    def test_latest_snapshot_with_no_own_products_does_not_reuse_old_sales(self):
        products, _, sources = pnl.load_master()
        # The newest file remains authoritative even when every row is excluded.
        with patch.object(pnl, 'load_whitelist', return_value={'coupang': {}}):
            sales, unmatched, files = pnl.read_coupang_temporary('2026-11', products, sources)
        self.assertTrue(files)
        self.assertEqual((sales, unmatched), ([], []))

    def test_report_date_excludes_future_snapshot(self):
        products, fees, sources = pnl.load_master()
        sales, unmatched, files = pnl.read_coupang_temporary('2026-11', products, sources, date(2026, 11, 14))
        self.assertTrue(all('2026-11-14' in path for path in files))
        detail, issues = pnl.prepare_rows(sales, products, fees)
        self.assertEqual(len(detail), 28)
        self.assertEqual(profit_summary(detail)['contribution_profit'], 600670)
        self.assertEqual((unmatched, issues), ([], []))
        # Controlled export times make selection independent of file copy times.
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name, created, headers in (
                ('2026-11-14_coupang_sales.xlsx', datetime(2026, 11, 14, 1), ['옵션 ID', '매출(원)', '판매량']),
                ('2026-11-15_coupang_sales.xlsx', datetime(2026, 11, 15, 1), ['옵션 ID', '매출(원)', '판매량']),
                ('2026-11-16_coupang_sales.xlsx', datetime(2026, 11, 16, 1), ['옵션 ID', '매출(원)', '판매량']),
                ('2026-11_coupang_sales.xlsx', datetime(2026, 11, 15, 6), ['옵션 ID', '매출(원)', '판매량']),
            ):
                book = pnl.Workbook()
                book.active.title = 'vendor item metrics'
                book.active.append(headers)
                book.properties.created = created
                book.save(folder / name)
                book.close()
            with patch.object(pnl, 'today', return_value=date(2026, 11, 15)):
                implicit = pnl.select_coupang_snapshot('2026-11', folder=folder)
            explicit = pnl.select_coupang_snapshot('2026-11', date(2026, 11, 15), folder)
            self.assertEqual(implicit, explicit)
            self.assertEqual(explicit[0].name, '2026-11_coupang_sales.xlsx')
            self.assertEqual(explicit[1], date(2026, 11, 15))
            self.assertEqual(pnl.select_coupang_snapshot('2026-11', date(2026, 11, 14), folder)[0].name,
                             '2026-11-14_coupang_sales.xlsx')


if __name__ == '__main__':
    unittest.main()
