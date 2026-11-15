"""Offline safety checks; no real login, raw-data changes, or dashboard writes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from scripts import fetch_coupang_wing as wing


def workbook(path, headers):
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.append(headers)
    book.save(path)
    book.close()


class CollectorTests(unittest.TestCase):
    def test_rejects_summary_and_html(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'file.xlsx'
            workbook(path, ['일자', '매출(원)', '판매량'])
            with self.assertRaises(ValueError):
                wing.validate_excel(path, 'sales')
            path.write_text('<html>로그인</html>', encoding='utf-8')
            with self.assertRaises(Exception):
                wing.validate_excel(path, 'sales')

    def test_fallback_skips_corrupt_latest_without_copying(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            good = root / 'raw_data/sales/coupang/2026-11-13_coupang_sales.xlsx'
            workbook(good, ['옵션 ID', '매출(원)', '판매량'])
            bad = good.with_name('2026-11-14_coupang_sales.xlsx')
            bad.write_bytes(b'not excel')
            before = good.read_bytes()
            with patch.object(wing, 'ROOT', root):
                self.assertEqual(wing.fallback('sales'), str(good))
            self.assertEqual(good.read_bytes(), before)
            self.assertEqual(len(list(good.parent.iterdir())), 2)

    def test_inventory_requires_return_stock(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'stock.xlsx'
            workbook(path, ['옵션 ID', '판매가능재고'])
            with self.assertRaises(ValueError):
                wing.validate_excel(path, 'inventory')
            workbook(path, ['옵션 ID', '판매가능재고 (실시간 기준)', '상품등급'])
            wing.validate_excel(path, 'inventory')
            workbook(path, ['옵션 ID', '물류센터 가용 재고', '반품 재고'])
            wing.validate_excel(path, 'inventory')
            workbook(path, ['옵션 ID', '판매가능재고 (실시간 기준)', '고객반품 지난 30일(전일자 기준)'])
            with self.assertRaises(ValueError):
                wing.validate_excel(path, 'inventory')

    def test_sales_keeps_growth_and_preserves_downloaded_original(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'sales.partial.xlsx'
            book = Workbook()
            book.active.append(['옵션 ID', '판매방식', '매출(원)', '판매량'])
            book.active.append(['123', '로켓그로스', 2000, 2])
            book.active.append(['456', '판매자배송', 1000, 1])
            book.save(path)
            book.close()
            original = path.read_bytes()
            wing.growth_only(path, Path(folder) / 'sales.xlsx')
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True)
            self.assertEqual(list(book.active.values)[1:], [('123', '로켓그로스', 2000, 2)])
            book.close()
            self.assertEqual((Path(folder) / 'source_exports/sales_all_methods.xlsx').read_bytes(), original)

    def test_expired_session_does_not_publish_or_abort(self):
        from types import SimpleNamespace
        context = unittest.mock.MagicMock()
        context.pages = [unittest.mock.MagicMock()]
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value.chromium.launch_persistent_context.return_value = context
        module = SimpleNamespace(sync_playwright=lambda: manager)
        with patch.dict('sys.modules', {'playwright.sync_api': module}), \
                patch.object(wing, 'PROFILE', Path(__file__).parent / 'missing_test_profile'), \
                patch.object(wing, 'collect_sales', side_effect=wing.LoginRequired()), \
                patch.object(wing, 'collect_inventory', side_effect=wing.LoginRequired()), \
                patch.object(wing, 'fallback', return_value='old.xlsx'):
            with patch.object(Path, 'mkdir'):
                result = wing.run()
        self.assertEqual(result['sales']['status'], 'fallback')
        self.assertEqual(result['inventory']['path'], 'old.xlsx')
        context.close.assert_called_once()

    def test_pipeline_continues_after_coupang_fallback(self):
        from scripts import run_daily_pipeline as pipeline, fetch_api_sales
        with patch('sys.argv', ['pipeline', '--skip-cloud']), \
                patch.object(wing, 'run', return_value={'sales': {'status': 'fallback'}}), \
                patch.object(fetch_api_sales, 'run', return_value={}), \
                patch.object(pipeline.filter_bank_and_hometax, 'run', return_value={}) as bank, \
                patch.object(pipeline.generate_monthly_pnl, 'run_all', return_value={}) as pnl, \
                patch.object(pipeline.export_dashboard_json, 'run', return_value={}) as export:
            self.assertEqual(pipeline.main(), 0)
        bank.assert_called_once()
        pnl.assert_called_once()
        export.assert_called_once()


if __name__ == '__main__':
    unittest.main()
