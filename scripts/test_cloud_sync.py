import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_dashboard.cloud_storage import CloudStore, atomic_write, backup, restore_latest, restore_archive, safe_path, inventory_allowlist


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / 'pc'
        self.cache = self.folder / 'render'
        self.store = CloudStore(mode='local', folder=self.folder / 'drive')
        self.write('web_dashboard/dashboard_data.json', json.dumps({
            'metadata': {'report_date': '2026-11-15', 'generated_at': '2026-11-15T09:00:00+09:00'},
            'sales_profit': {'monthly': {'revenue': 100}}}).encode())
        self.write('web_dashboard/inventory_master_store.json', b'{"master_products":[{"name":"local"}]}')
        self.write('raw_data/inventory/2026-11-14_3pl_inventory.csv', b'old')
        self.write('raw_data/inventory/2026-11-15_3pl_inventory.csv', b'new')
        self.write('raw_data/sales/2026-09-late.csv', b'historical input')
        self.write('output_excel/2026-11_test.xlsx', b'xlsx')
        self.write('output_excel/reconciliation/2026-11-15_report.csv', b'csv')
        self.write('config/.env', b'SECRET=not-for-cloud')

    def write(self, name, data):
        atomic_write(self.root / name, data)

    def test_initial_incremental_and_history_retention(self):
        first = backup(self.root, self.store)
        self.assertEqual(first['uploaded'], 7)
        second = backup(self.root, self.store)
        self.assertEqual(second['uploaded'], 0)
        self.assertEqual(second['unchanged'], 7)
        (self.root / 'raw_data/sales/2026-09-late.csv').unlink()
        self.write('raw_data/inventory/2026-11-15_3pl_inventory.csv', b'changed')
        third = backup(self.root, self.store)
        self.assertEqual(third['uploaded'], 1)
        manifest = self.store.manifest()
        self.assertIn('raw_data/sales/2026-09-late.csv', manifest['files'])
        self.assertNotIn('config/.env', manifest['files'])
        self.assertEqual(len(list((self.store.folder / 'manifests').glob('*.json'))), 3)

    def test_restore_latest_stock_dashboard_and_reports_preserves_admin(self):
        backup(self.root, self.store)
        result = restore_latest(self.cache, self.store)
        self.assertEqual(result['status'], 'synced')
        self.assertEqual((self.cache / 'raw_data/inventory/2026-11-15_3pl_inventory.csv').read_bytes(), b'new')
        self.assertFalse((self.cache / 'raw_data/inventory/2026-11-14_3pl_inventory.csv').exists())
        self.assertFalse((self.cache / 'web_dashboard/inventory_master_store.json').exists())
        self.assertTrue((self.cache / 'output_excel/reconciliation/2026-11-15_report.csv').exists())
        self.assertEqual(restore_latest(self.cache, self.store)['restored'], 0)

    def test_failed_upload_does_not_publish(self):
        backup(self.root, self.store)
        revision = self.store.manifest()['revision']
        self.write('raw_data/sales/extra.csv', b'new')
        with patch.object(self.store, 'put', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                backup(self.root, self.store)
        self.assertEqual(self.store.manifest()['revision'], revision)

    def test_corrupt_cloud_object_does_not_replace_any_local_file(self):
        backup(self.root, self.store)
        manifest = self.store.manifest()
        sha = manifest['files']['web_dashboard/dashboard_data.json']['sha256']
        (self.store.folder / 'objects' / sha).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            restore_latest(self.cache, self.store)
        self.assertFalse((self.cache / 'web_dashboard/dashboard_data.json').exists())
        self.assertFalse((self.cache / 'output_excel/2026-11_test.xlsx').exists())

    def test_rollback_concurrency_and_path_traversal_rejected(self):
        backup(self.root, self.store)
        manifest = self.store.manifest()
        older = dict(manifest, report_date='2026-10-31', revision='0' * 64)
        with self.assertRaises(ValueError):
            self.store.publish(older, manifest['revision'])
        with self.assertRaises(ValueError):
            self.store.publish(dict(manifest, revision='1' * 64), 'wrong')
        with self.assertRaises(ValueError):
            self.store.publish(dict(manifest, revision='2' * 64,
                                    dashboard_generated_at='2026-11-15T08:00:00+09:00'), manifest['revision'])
        for path in ('../escape', '/escape', 'C:/escape', 'master_data/../../escape.xlsx'):
            with self.assertRaises(ValueError):
                safe_path(self.cache, path)

    def test_dry_run_writes_nothing(self):
        self.write('raw_data/sales/original.zip', b'raw archive')
        self.write('raw_data/.env', b'not-a-source')
        result = backup(self.root, self.store, dry_run=True)
        self.assertEqual(result['uploaded'], 8)
        self.assertFalse(self.store.folder.exists())

    def test_full_archive_restore_and_specific_historical_revision(self):
        first = backup(self.root, self.store)
        self.write('raw_data/inventory/2026-11-15_3pl_inventory.csv', b'changed')
        backup(self.root, self.store)
        destination = self.folder / 'recovered'
        result = restore_archive(destination, self.store, first['revision'])
        self.assertEqual(result['restored'], 7)
        self.assertEqual((destination / 'raw_data/inventory/2026-11-15_3pl_inventory.csv').read_bytes(), b'new')
        self.assertTrue((destination / 'web_dashboard/inventory_master_store.json').exists())
        self.assertFalse((destination / 'config/.env').exists())
        with self.assertRaises(ValueError):
            restore_archive(destination, self.store)

    def test_gas_transport_and_checksum(self):
        store = CloudStore(mode='gas', url='https://example.invalid/exec', token='private')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"success":true,"data":null}')) as request:
            self.assertIsNone(store.manifest())
            body = json.loads(request.call_args.args[0].data)
            self.assertEqual(body['action'], 'backup_manifest')
            self.assertEqual(body['token'], 'private')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"success":true,"content_base64":"YmFk"}')):
            with self.assertRaises(ValueError):
                store.get('0' * 64)

    def test_standalone_render_whitelist_matches_canonical_source(self):
        from .channel_whitelist import load_whitelist
        expected = load_whitelist()
        actual = inventory_allowlist(expected['path'])
        self.assertEqual(actual['gaemi'], expected['gaemi'])
        self.assertEqual(actual['coupang'], expected['coupang'])
