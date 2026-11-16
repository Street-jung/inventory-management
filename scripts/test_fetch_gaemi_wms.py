"""Offline collector checks. All writes stay in a temporary scripts directory."""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_gaemi_wms as gaemi
from scripts.common import read_excel_sheets

HEADERS = '번호,상품코드,상품명,옵션명,상태,입고량,출고량,재고량,등록일\n'
ROW = '1,PJ1,상품,500ml 2027-01-01,입고완료,10,3,7,2026-11-15\n'


class GaemiCollectorTests(unittest.TestCase):
    def test_mislabeled_csv_becomes_two_compatible_real_xlsx(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / 'export.xlsx'
            source.write_text(HEADERS+ROW,encoding='utf-8-sig')
            with patch.object(gaemi, 'ROOT', root):
                result = gaemi.publish(source,date(2026,11,15))
            self.assertEqual(result['rows'],1)
            for key in ('path','compatibility_path'):
                path = Path(result[key])
                self.assertTrue(path.read_bytes().startswith(b'PK'))
                rows = read_excel_sheets(path)[0][1]
                self.assertEqual(rows[0][1], '상품코드')
                self.assertEqual(rows[1][7], '7')

    def test_invalid_or_empty_download_never_replaces_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory)
            source=root/'download.xlsx'
            source.write_text(HEADERS+ROW,encoding='utf-8')
            with patch.object(gaemi,'ROOT',root):
                result=gaemi.publish(source,date(2026,11,15))
                before={key:Path(result[key]).read_bytes() for key in ('path','compatibility_path')}
                for bad in ('<html>login</html>',HEADERS,HEADERS+ROW.replace(',7,',',-1,')):
                    source.write_text(bad,encoding='utf-8')
                    with self.assertRaises(ValueError):
                        gaemi.publish(source,date(2026,11,15))
                    for key in before:
                        self.assertEqual(Path(result[key]).read_bytes(),before[key])

    def test_fallback_validates_and_skips_broken_newer_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory)
            folder=root/'raw_data/inventory'
            folder.mkdir(parents=True)
            good=folder/'2026-11-14_3pl_inventory.xlsx'
            good.write_text(HEADERS+ROW,encoding='utf-8')
            (folder/'2026-11-15_gaemi_inventory.xlsx').write_text('bad',encoding='utf-8')
            with patch.object(gaemi,'ROOT',root):
                self.assertEqual(gaemi.fallback('session expired')['path'],str(good))

    def test_alias_publish_error_rolls_back_the_canonical_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory)
            source=root/'export.xlsx'
            source.write_text(HEADERS+ROW,encoding='utf-8')
            with patch.object(gaemi,'ROOT',root):
                result=gaemi.publish(source,date(2026,11,15))
                before=Path(result['path']).read_bytes()
                original_replace=Path.replace
                def replace(path,target):
                    if str(target).endswith('_3pl_inventory.xlsx'):
                        raise PermissionError('alias locked')
                    return original_replace(path,target)
                source.write_text(HEADERS+ROW.replace(',7,',',6,'),encoding='utf-8')
                with patch.object(Path,'replace',replace),self.assertRaises(PermissionError):
                    gaemi.publish(source,date(2026,11,15))
                self.assertEqual(Path(result['path']).read_bytes(),before)

    def test_unconfigured_returns_fallback_without_opening_browser(self):
        with patch.object(gaemi,'load_env'),patch.dict(os.environ,{'GAEMI_WMS_URL':''}), \
                patch.object(gaemi,'fallback',return_value={'status':'fallback','path':'old.xlsx'}) as fallback:
            self.assertEqual(gaemi.run()['status'],'fallback')
            self.assertIn('GAEMI_WMS_URL',fallback.call_args.args[0])

    def test_summary_does_not_double_count_compatibility_copy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory)
            source=root/'export.csv'
            source.write_text(HEADERS+ROW,encoding='utf-8')
            with patch.object(gaemi,'ROOT',root):
                gaemi.publish(source,date(2026,11,15))
            with patch('scripts.channel_whitelist.load_whitelist',return_value={'gaemi':{'PJ1'}}):
                summary=gaemi.snapshot_summary(date(2026,11,15),root)
                self.assertEqual(summary['rows'],1)
                self.assertEqual(summary['total_stock'],7)
                self.assertEqual(summary['company_stock'],7)
                self.assertIn('_gaemi_',summary['source'])
                self.assertIsNone(gaemi.snapshot_summary(date(2026,11,14),root))

    def test_real_legacy_file_is_supported_read_only(self):
        path=gaemi.ROOT/'raw_data/inventory/2026-11-14_3pl_inventory.xlsx'
        if not path.exists():
            self.skipTest('legacy fixture unavailable')
        _,_,headers,count=gaemi.inventory_sheet(path)
        self.assertIn('옵션명',headers)
        self.assertGreater(count,0)

    def test_pipeline_continues_if_collector_raises(self):
        from scripts import run_daily_pipeline as pipeline,fetch_coupang_wing
        with patch('sys.argv',['pipeline','--skip-cloud','--skip-api-fetch']), \
                patch.object(fetch_coupang_wing,'run',return_value={}), \
                patch.object(gaemi,'run',side_effect=RuntimeError('browser unavailable')), \
                patch.object(pipeline.filter_bank_and_hometax,'run',return_value={}), \
                patch.object(pipeline.generate_monthly_pnl,'run_all',return_value={}), \
                patch.object(pipeline.export_dashboard_json,'run',return_value={}) as export:
            self.assertEqual(pipeline.main(),0)
            export.assert_called_once()


if __name__=='__main__':
    unittest.main()
