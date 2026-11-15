import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from . import generate_monthly_pnl as pnl
from .export_dashboard_json import profit_summary


class AgencySettlementTests(unittest.TestCase):
    def test_actual_months_and_cost_basis(self):
        products, fees, _ = pnl.load_master()
        expected = {'2026-09': (30, 21324660, 8031224, 12827484.4, 465951.6),
                    '2026-10': (27, 24281610, 9320619, 16402812, -1441821)}
        for month, totals in expected.items():
            rows, unmatched, paths = pnl.read_agency_settlement(month, date(2026, 11, 15))
            detail, issues = pnl.prepare_rows(rows, products, fees)
            result = profit_summary(detail)
            self.assertEqual((len(rows), result['revenue'], result['cost'], result['fees'], result['contribution_profit']), totals)
            self.assertEqual(unmatched, [])
            self.assertEqual(issues, [])
            self.assertEqual(len(paths), 1)
            self.assertTrue(all(r['item_id'] in pnl.load_whitelist()['coupang'] for r in rows))
            self.assertEqual(sum((r['actual_fee'] for r in rows), Decimal(0)), Decimal(str(result['fees'])))
        self.assertEqual(rows[0]['settlement_audit']['excluded_3p_rows'], 17)
        self.assertEqual(rows[0]['settlement_audit']['excluded_3p_gross'], 546900)

    def test_statement_replaces_normalized_and_snapshot_coupang(self):
        original = [dict(channel='coupang', paid=date(2026, 9, 1), source='old.csv'),
                    dict(channel='cafe24', paid=date(2026, 9, 2), source='shop.csv')]
        settled = [dict(channel='coupang_growth', paid=date(2026, 9, 30), source='statement.xlsx')]
        with patch.object(pnl, 'read_sales', return_value=(original, ['old.csv', 'shop.csv'])), \
             patch.object(pnl, 'read_agency_settlement', return_value=(settled, [], ['statement.xlsx'])), \
             patch.object(pnl, 'read_coupang_temporary') as temporary:
            rows, _, files, provisional = pnl.read_month_sales('2026-09', {}, [], True, date(2026, 11, 15))
        self.assertEqual([r['channel'] for r in rows], ['cafe24', 'coupang_growth'])
        self.assertNotIn('old.csv', files)
        self.assertFalse(provisional)
        temporary.assert_not_called()

    def test_statement_priority_is_independent_of_cutoff(self):
        earlier = pnl.read_agency_settlement('2026-09', date(2026, 9, 15))
        closed = pnl.read_agency_settlement('2026-09', date(2026, 11, 15))
        self.assertEqual(earlier, closed)
        self.assertTrue(all(r['paid'] == date(2026, 9, 30) for r in earlier[0]))
        self.assertIsNone(pnl.read_agency_settlement('2026-11', date(2026, 11, 15)))

    def test_bundle_and_actual_zero_fee(self):
        self.assertEqual(pnl.bundle_units('3개\xa0500ml'), 3)
        self.assertEqual(pnl.bundle_units('상품 1개 500ml, 2개'), 2)
        with self.assertRaises(ValueError):
            pnl.bundle_units('수량미상')
        row = ['shop', 'order', 'item', date(2026, 9, 30), 'P', 'name', 2, 100, 10, None, None, 0]
        self.assertEqual(profit_summary([row])['contribution_profit'], 80)
        row[11] = -5
        self.assertEqual(profit_summary([row])['contribution_profit'], 85)

    def test_workbook_keeps_actual_fee_and_traceable_formula(self):
        rows, _, files = pnl.read_agency_settlement('2026-09')
        products, fees, sources = pnl.load_master()
        detail, issues = pnl.prepare_rows(rows, products, fees)
        book = pnl.build_workbook('2026-09', detail, issues, sources, files, [])
        try:
            self.assertEqual(book['매출상세']['L2'].value, detail[0][11])
            self.assertIn('G2*I2', book['매출상세']['J2'].value)
            self.assertIn('H2-J2-L2', book['매출상세']['M2'].value)
            self.assertGreater(detail[5][6], 0)
        finally:
            book.close()
