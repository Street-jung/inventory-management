import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from .export_dashboard_json import (build_data, profit_summary, run, sales_metrics,
                                    product_transaction, add_real_profit_estimates,
                                    bank_management_summary)


class DashboardExportTests(unittest.TestCase):
    def test_quantity_skus_and_item_economics(self):
        row = ['shop', 'o', 'sku', date(2026, 11, 15), 'P', 'name', 3, 300, 20, None, .1]
        second = list(row)
        second[1], second[6] = 'another-order', 2
        summary = profit_summary([row, second])
        self.assertEqual(summary['row_count'], 2)
        self.assertEqual(summary['total_quantity'], 5)
        self.assertEqual(summary['active_sku_count'], 1)
        item = product_transaction(row)
        self.assertEqual((item['asp'], item['cost'], item['fees'], item['profit'], item['margin_pct']),
                         (100, 60, 30, 210, 70))
        row[6] = 0
        self.assertIsNone(product_transaction(row)['asp'])
        row[8] = None
        self.assertIsNone(product_transaction(row)['profit'])
        self.assertIsNone(product_transaction(row)['margin_pct'])

    def test_estimates_weighting_no_double_count_and_missing_history(self):
        history = [dict(month='2026-09', data_status='settled', provisional=False,
                        revenue=100, fees=50, profit=20, fee_breakdown={'marketplace': 10}),
                   dict(month='2026-10', data_status='settled', provisional=False,
                        revenue=300, fees=210, profit=30, fee_breakdown={'marketplace': 30})]
        current = dict(month='2026-11', data_status='partial', provisional=True,
                       revenue=200, fees=30, profit=100,
                       fee_breakdown={'marketplace': 20, 'agency': 10})
        series = history + [current]
        add_real_profit_estimates(series)
        self.assertEqual(current['estimated_additional_fees'], 100)
        self.assertEqual(current['estimated_real_profit'], 0)
        self.assertEqual(current['estimated_real_margin_pct'], 0)
        self.assertEqual(current['estimation_basis']['additional_cost_rate_pct'], 55)
        self.assertEqual(current['profit'], 100)
        self.assertIsNone(history[0]['estimated_real_profit'])
        add_real_profit_estimates([current])
        self.assertIsNone(current['estimated_real_profit'])

    def test_bank_cash_category_boundaries(self):
        rows = [dict(category='B2B 선물세트 매출(일회성)', deposit_amount=100, withdrawal_amount=0),
                dict(category='부자재·패키징비', deposit_amount=0, withdrawal_amount=30),
                dict(category='수입포워딩(국제물류); 수입통관(관세사)', deposit_amount=0, withdrawal_amount=20),
                dict(category='상품 OEM 제조비', deposit_amount=0, withdrawal_amount=500)]
        self.assertEqual(bank_management_summary(rows),
                         dict(b2b_deposit_total=100, food_sga_withdrawal_total=50))

    def test_multiple_months_and_mom(self):
        from . import generate_monthly_pnl as pnl
        products, _, _ = pnl.load_master()
        code = next(k for k, v in products.items() if v['food'] and v['cost'] is not None)
        def read_sales(month):
            amount = {'2026-09': 100, '2026-10': 200, '2026-11': 300}.get(month)
            if amount is None:
                return [], []
            return [dict(channel='cafe24', order_id=month, item_id='item',
                         paid=date.fromisoformat(month + '-01'), product_code=code,
                         product_name='test', quantity=1, sales_amount=amount,
                         source='fixture', status='paid')], []
        with patch.object(pnl, 'read_sales', side_effect=read_sales), patch.object(pnl, 'read_agency_settlement', return_value=None):
            data = build_data(date(2026, 11, 15), date(2026, 11, 15), False)
        series = data['monthly_series']
        self.assertEqual([r['revenue'] for r in series], [100, 200, 300])
        self.assertEqual([r['mom_growth_pct'] for r in series], [None, 100, 50])
        self.assertEqual(data['mom_growth_pct'], 50)
        self.assertTrue(all(r['profit'] == r['revenue'] - r['cost'] - r['fees'] for r in series))

    def test_trends_growth_forecast_and_ranking(self):
        def row(day, amount, cost=10, code='P'):
            return ['shop', 'order', 'item', date(2026, 11, day), code, code, 2, amount, cost, None, .1]
        metrics = sales_metrics([row(13, 100), row(14, 200)], date(2026, 11, 14))
        self.assertEqual(metrics['growth_metrics']['dod']['revenue_pct'], 100)
        self.assertIsNone(metrics['growth_metrics']['wow']['revenue_pct'])
        self.assertEqual(metrics['monthly_forecast']['projected_revenue'], 643)
        self.assertEqual(metrics['daily_trend'][0]['basis'], 'daily')
        self.assertEqual(metrics['top_products']['by_quantity'][0]['quantity'], 4)
        missing = sales_metrics([row(14, 200, None)], date(2026, 11, 14), True)
        self.assertIsNone(missing['monthly_forecast']['projected_contribution_profit'])
        self.assertIsNone(missing['top_products']['by_revenue'][0]['margin_pct'])
        self.assertIsNone(missing['growth_metrics']['dod']['revenue_pct'])
        empty = sales_metrics([], date(2028, 2, 29))
        self.assertEqual(empty['monthly_forecast']['total_days'], 29)
        self.assertIsNone(empty['monthly_forecast']['projected_revenue'])

    def test_missing_cost_stays_null(self):
        detail = [['channel', 'order', 'item', date(2026, 11, 14), '', 'unknown', 2, 100, None, None, .1]]
        result = profit_summary(detail)
        self.assertEqual(result['revenue'], 100)
        self.assertEqual(result['fees'], 10)
        self.assertIsNone(result['cost'])
        self.assertIsNone(result['contribution_profit'])
        self.assertIsNone(profit_summary([])['revenue'])

    def test_actual_sources_and_json_round_trip(self):
        from .common import ROOT
        from . import filter_bank_and_hometax as bank_tax, generate_monthly_pnl as pnl
        report_date = date(2026, 11, 15)
        bank_fixture = ROOT / 'raw_data/bank'
        bank = [r for r in bank_tax.read_bank(bank_fixture, bank_tax.load_rules())
                if r['transaction_date'] <= str(report_date)]
        data = build_data(report_date, report_date, bank_file=bank_fixture)
        self.assertEqual(data['metadata']['bank_count'], len(bank))
        unknown = [r for r in bank if r['food_status'] == '미분류(확인필요)']
        self.assertEqual(data['unclassified']['count'], len(unknown))
        self.assertEqual(len(data['unclassified']['transactions']), len(unknown))
        self.assertEqual({r['status']: r['transaction_count'] for r in data['cash_flow']['classification']},
                         {status: sum(r['food_status'] == status for r in bank)
                          for status in ('식품부문 포함', '식품부문 제외', '미분류(확인필요)')})
        self.assertEqual(sum(r['transaction_count'] for r in data['cash_flow']['classification']), len(bank))
        settled = {r['issue_date'][:7]: r for r in data['settlements']['items'] if r['customer'] == '플랜디에이'}
        current = settled['2026-10']
        self.assertEqual(current['net_amount'], 2406974)
        self.assertEqual(current['paid_amount'], 0)
        self.assertEqual(current['due_date'], '2026-11-30')
        self.assertEqual(settled['2026-09']['sales_amount'], 6103956)
        self.assertEqual(settled['2026-09']['paid_amount'], 6103956)
        self.assertEqual(settled['2026-09']['outstanding_amount'], 0)
        self.assertFalse(any(r['issue_date'][:7] == '2026-09' for r in data['settlements']['prior_period_receipts']))
        monthly = data['sales_profit']['monthly']
        series = data['monthly_series']
        self.assertEqual([r['month'] for r in series], ['2026-09', '2026-10', '2026-11'])
        self.assertEqual(series[0]['profit'], 465951.6)
        self.assertEqual(series[1]['cost'], 9320619)
        self.assertEqual(series[0]['data_status'], 'settled')
        self.assertFalse(series[0]['provisional'])
        reference = series[0]['unverified_store_totals']
        if reference is not None:
            self.assertFalse(reference['included_in_pnl'])
            self.assertGreater(reference['days'], 0)
            self.assertIsNotNone(reference['revenue'])
        self.assertTrue(all('정산' in Path(p).stem for p in series[0]['sources']))
        self.assertEqual(series[0]['settlements'][0]['paid_amount'], 6103956)
        self.assertEqual(series[0]['settlements'][0]['sales_amount'], 6103956)
        self.assertEqual(series[-1]['profit'], data['sales_profit']['monthly']['contribution_profit'])
        self.assertEqual(data['mom_growth_pct'], round((monthly['revenue'] - series[1]['revenue']) / series[1]['revenue'] * 100, 2))
        self.assertEqual(monthly['revenue'], sum(c['revenue'] or 0 for c in monthly['channels']))
        products, fees, sources = pnl.load_master()
        rows, _, files, provisional = pnl.read_month_sales('2026-11', products, sources, True, report_date)
        selected = pnl.select_coupang_snapshot('2026-11', report_date)
        self.assertIn(str(selected[0]), files)
        self.assertEqual({r['source'] for r in rows if r['channel'] in ('coupang', 'coupang_growth')}, {str(selected[0])})
        detail, _ = pnl.prepare_rows(rows, products, fees)
        expected = profit_summary(detail)
        self.assertEqual(monthly['revenue'], expected['revenue'])
        self.assertEqual(monthly['row_count'], len(detail))
        self.assertEqual(monthly['total_quantity'], sum(r[6] for r in detail))
        self.assertEqual(monthly['active_sku_count'], 36)
        self.assertGreater(monthly['total_quantity'], monthly['active_sku_count'])
        self.assertLess(series[-1]['estimated_real_profit'], series[-1]['profit'])
        self.assertEqual(series[-1]['estimation_basis']['months'], ['2026-09', '2026-10'])
        self.assertEqual([r['month'] for r in data['cash_flow']['monthly_summary']],
                         ['2026-09', '2026-10', '2026-11'])
        for period, cash in zip(series, data['cash_flow']['monthly_summary']):
            self.assertEqual(period['b2b_deposit_total'], cash['b2b_deposit_total'])
            self.assertEqual(period['food_sga_withdrawal_total'], cash['food_sga_withdrawal_total'])
        self.assertEqual(monthly['missing_cost_rows'], 0)
        self.assertEqual(monthly['cost'], expected['cost'])
        self.assertEqual(monthly['contribution_profit'], monthly['revenue'] - monthly['cost'] - monthly['fees'])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'dashboard_data.json'
            run(report_date, report_date, bank_file=bank_fixture, output=output)
            restored = json.loads(output.read_text(encoding='utf-8'))
            self.assertIsInstance(restored['settlements']['outstanding_total'], int)
            self.assertFalse(output.with_suffix('.json.tmp').exists())
