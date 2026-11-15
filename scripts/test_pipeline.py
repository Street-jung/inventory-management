"""Offline regression tests. Run: python -m unittest scripts.test_pipeline -v"""
import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from collections import defaultdict
from pathlib import Path

from .common import SALES_FIELDS, as_date, write_csv
from .fetch_api_sales import normalize_cafe24, normalize_smartstore
from .filter_bank_and_hometax import classify, reconcile, load_rules, read_bank, bank_category
from .generate_monthly_pnl import prepare_rows, read_sales, build_workbook, load_master, read_coupang_temporary
from .filter_bank_and_hometax import read_invoices, read_purchase_invoices, food_settlements, end_of_next_month
from .filter_bank_and_hometax import summarize_unclassified
from .filter_bank_and_hometax import apply_one_off_override
from .common import ROOT


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.rules = {'include_keywords': ['식품'], 'exclude_keywords': ['화공'],
                      'b2b_payment_terms_days': {'기본거래처': 15}}

    def test_classification_conflict(self):
        self.assertEqual(classify('식품 화공', self.rules)[0], '미분류(확인필요)')
        self.assertEqual(classify('식품', self.rules)[0], '식품부문 포함')
        self.assertEqual(classify('화공', self.rules)[0], '식품부문 제외')
        self.assertEqual(classify('모름', self.rules)[0], '미분류(확인필요)')

    def test_confirmed_bank_rules(self):
        rules = load_rules()
        rows = read_bank(ROOT / 'raw_data' / 'bank' / '2026-11-14_hana_bank.xlsx', rules)
        self.assertEqual(len(rows), 15)
        included = [r for r in rows if r['food_status'] == '식품부문 포함']
        excluded = [r for r in rows if r['food_status'] == '식품부문 제외']
        self.assertEqual((len(included), len(excluded)), (8, 7))
        self.assertTrue(all(r['category'] for r in included))
        self.assertEqual(sum(Decimal(r['withdrawal_amount']) for r in included), Decimal('4434952'))
        self.assertEqual(classify('500000000', rules)[0], '식품부문 제외')
        self.assertEqual(bank_category('uab ekko', '식품부문 포함', rules), '수입원물(에코 카놀라유)')
        self.assertEqual(bank_category('류지훈 지유관세', '식품부문 포함', rules), '수입통관(관세사)')
        self.assertEqual(classify('쿠팡페이 화공', rules)[0], '미분류(확인필요)')
        all_rows = read_bank(ROOT / 'raw_data/bank', rules)
        unknown = [r for r in all_rows if r['food_status'] == '미분류(확인필요)'
                   and '2026-09-01' <= r['transaction_date'] <= '2026-11-15']
        grouped = summarize_unclassified(all_rows, date(2026, 11, 15))
        self.assertEqual(sum(r['count'] for r in grouped), len(unknown))
        self.assertEqual(sum((r['deposits'] for r in grouped), Decimal(0)),
                         sum((Decimal(r['deposit_amount']) for r in unknown), Decimal(0)))
        self.assertEqual(len(grouped), len({r['customer'] for r in unknown}))

    def test_korea_payment_boundary(self):
        self.assertEqual(as_date('2026-10-31T15:01:00Z'), date(2026, 11, 1))

    def test_reviewed_transactions_and_future_scope(self):
        rules = load_rules()
        bank = read_bank(ROOT / 'raw_data/bank', rules)
        self.assertEqual(len(bank), 298)
        self.assertFalse([r for r in bank if r['food_status'] == '미분류(확인필요)'])
        food = [r for r in bank if r['food_status'] == '식품부문 포함']
        self.assertEqual(sum(Decimal(r['deposit_amount']) for r in food), Decimal('35462939'))
        self.assertEqual(sum(Decimal(r['withdrawal_amount']) for r in food), Decimal('153965206'))
        for override in rules['one_off_overrides']:
            record = dict(override)
            self.assertTrue(apply_one_off_override(record, rules))
            for key, value in [('transaction_date', '2026-11-01'),
                               ('transaction_id', 'different'), ('customer', 'different'),
                               ('deposit_amount', '1'), ('withdrawal_amount', '1')]:
                with self.subTest(transaction=override['transaction_id'], field=key):
                    self.assertFalse(apply_one_off_override(dict(override, **{key: value}), rules))
        for name in ['김지훈', '김일섭', '현명철', '(주)수월포장']:
            self.assertEqual(classify(name, rules)[0], '미분류(확인필요)')
        for name in ['김창종', '훈자건강마을', '비스카이익스프레스', '모모스튜디오', '청춘농장']:
            self.assertEqual(classify(name, rules)[0], '식품부문 포함')
        chemical_fees = [r for r in bank if 'OTT260900121' in r['description'] + r['customer']
                         and '당발송금수수료' in r['description'] + r['customer']]
        self.assertEqual(len(chemical_fees), 1)
        self.assertEqual(chemical_fees[0]['food_status'], '식품부문 제외')

    def test_partial_fifo_and_due_boundary(self):
        bank = [dict(transaction_id='deposit', transaction_date='2026-10-10', customer='식품사',
                     deposit_amount='150', food_status='식품부문 포함')]
        invoices = [dict(invoice_id=str(i), issue_date='2026-10-01', customer='식품사',
                         total_amount='100', source='test') for i in (1, 2)]
        result = reconcile(bank, invoices, self.rules, date(2026, 10, 17))
        self.assertEqual([r['outstanding_amount'] for r in result], ['0', '50'])
        self.assertEqual(result[1]['status'], '연체(대조상 미수)')
        boundary = reconcile([], invoices, self.rules, date(2026, 10, 16))
        self.assertEqual(boundary[0]['status'], '기한미도래')

    def test_prepayment_not_silently_allocated(self):
        bank = [dict(transaction_id='a', transaction_date='2026-09-30', customer='식품사',
                     deposit_amount='100', food_status='식품부문 포함')]
        invoice = [dict(invoice_id='i', issue_date='2026-10-01', customer='식품사', total_amount='100', source='test')]
        self.assertEqual(reconcile(bank, invoice, self.rules, date(2026, 11, 1))[0]['paid_amount'], '0')

    def test_cafe_allocation_reconciles(self):
        order = dict(order_id='o', payment_date='2026-11-01T12:00:00+09:00', currency='KRW',
                     actual_order_amount={'payment_amount': '10000', 'shipping_fee': '3000'},
                     items=[dict(order_item_code=str(i), product_price='3000', quantity=1) for i in range(3)])
        rows = normalize_cafe24(order, date(2026, 11, 1), 'test.json')
        self.assertEqual(sum(Decimal(r['sales_amount']) for r in rows), Decimal('7000'))

    def test_smartstore_initial_amount_preserved(self):
        entry = dict(productOrderId='p', content={'order': {'orderId': 'o', 'paymentDate': '2026-11-01T12:00:00+09:00'},
                     'productOrder': {'quantity': 2, 'initialQuantity': 2, 'totalPaymentAmount': 100,
                                      'initialPaymentAmount': 100, 'remainPaymentAmount': 50}})
        self.assertEqual(normalize_smartstore(entry, date(2026, 11, 1), 'test')['sales_amount'], '100')

    def test_sales_duplicates_and_month_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            row = dict.fromkeys(SALES_FIELDS, '')
            row.update(channel='cafe24', order_id='o', item_id='i', payment_date='2026-10-31T15:01:00Z',
                       quantity='1', sales_amount='100', currency='KRW')
            write_csv(folder / 'a.csv', [row], SALES_FIELDS)
            write_csv(folder / 'b.csv', [row], SALES_FIELDS)
            self.assertEqual(len(read_sales('2026-11', folder)[0]), 1)
            self.assertEqual(len(read_sales('2026-10', folder)[0]), 0)
            row['sales_amount'] = '200'
            write_csv(folder / 'b.csv', [row], SALES_FIELDS)
            with self.assertRaises(ValueError):
                read_sales('2026-11', folder)

    def test_missing_cost_not_zero_and_nonfood_excluded(self):
        sales = [dict(channel='smartstore', order_id='o', item_id='i', paid=date(2026, 11, 1),
                      product_code='unknown', product_name='상품', quantity=Decimal(1), sales_amount=Decimal(100),
                      status='PAYED', source='test')]
        detail, issues = prepare_rows(sales, {}, {'네이버 스마트스토어': Decimal('.0663')})
        self.assertIsNone(detail[0][8])
        self.assertEqual(len(issues), 1)
        book = build_workbook('2026-11', detail, issues, [], [], [])
        self.assertIn('확인필요', book['매출상세']['J2'].value)
        self.assertEqual(book['월간손익']['B3'].value, '확인필요')
        book.close()

    def test_actual_master_schema(self):
        products, fees, sources = load_master()
        self.assertEqual(products['10HF01']['cost'], Decimal(1992))
        self.assertEqual(products['S100001']['cost'], Decimal(5664))
        self.assertAlmostEqual(float(fees['네이버 스마트스토어']), .0663)

    def test_actual_invoice_supplier_and_buyer(self):
        invoices = read_invoices(ROOT / 'raw_data' / 'hometax')
        self.assertTrue(invoices)
        self.assertEqual(len(invoices), len({r['invoice_id'] for r in invoices}))
        self.assertTrue({'2026-09', '2026-10'}.issubset({r['issue_date'][:7] for r in invoices}))
        self.assertTrue(all('purchase' not in r['source'] for r in invoices))
        self.assertTrue(all('오키드인더스트리' not in r['customer'] for r in invoices))

    def test_all_invoice_deposits_and_dates(self):
        rules = load_rules()
        bank = read_bank(ROOT / 'raw_data/bank', rules)
        invoices = read_invoices(ROOT / 'raw_data' / 'hometax')
        result = reconcile(bank, invoices, rules, date(2026, 11, 15), all_invoices=True)
        self.assertEqual({r['invoice_id'] for r in result},
                         {r['invoice_id'] for r in invoices if r['issue_date'] <= '2026-11-15'})
        deposits = {r['transaction_id']: Decimal(r['deposit_amount']) for r in bank}
        allocated = defaultdict(Decimal)
        for row in result:
            used = Decimal(0)
            for match in filter(None, row['matched_transactions'].split(';')):
                transaction, amount = match.rsplit(':', 1)
                allocated[transaction] += Decimal(amount)
                used += Decimal(amount)
            self.assertEqual(used, Decimal(row['paid_amount']))
            self.assertEqual(Decimal(row['total_amount']) - Decimal(row['paid_amount']), Decimal(row['outstanding_amount']))
        self.assertTrue(all(amount <= deposits[key] for key, amount in allocated.items()))
        self.assertTrue(all(r['matched_deposit_dates'] for r in result if Decimal(r['paid_amount']) > 0))
        self.assertTrue(all(Decimal(r['paid_amount']) == 0 for r in result if Decimal(r['total_amount']) < 0))

    def test_plan_netting_and_prior_month_receipt(self):
        rules = load_rules()
        bank = read_bank(ROOT / 'raw_data/bank', rules)
        sales = read_invoices(ROOT / 'raw_data/hometax')
        purchases = read_purchase_invoices(ROOT / 'raw_data/hometax')
        rows = food_settlements(bank, sales, purchases, rules, date(2026, 11, 15))
        by_month = {r['issue_date'][:7]: r for r in rows if r['customer'] == '플랜디에이'}
        prior, current = by_month['2026-09'], by_month['2026-10']
        self.assertEqual(prior['paid_amount'], '6103956.0')
        self.assertEqual(Decimal(prior['sales_amount']), Decimal('6103956'))
        self.assertEqual(Decimal(prior['net_amount']), Decimal(prior['paid_amount']))
        self.assertEqual(prior['status'], '입금완료')
        self.assertEqual(Decimal(current['purchase_offset']), Decimal('22817796'))
        self.assertEqual(Decimal(current['net_amount']), Decimal('2406974'))
        self.assertEqual(Decimal(current['paid_amount']), 0)
        self.assertEqual(current['due_date'], '2026-11-30')
        self.assertEqual(current['status'], '기한미도래')
        self.assertEqual(end_of_next_month(date(2026, 12, 31)), date(2027, 1, 31))
        self.assertEqual(end_of_next_month(date(2028, 1, 31)), date(2028, 2, 29))
        fees = [r for r in bank if r['category'] == '해외송금수수료']
        self.assertTrue(fees)
        self.assertTrue(all(Decimal(r['withdrawal_amount']) > 0 for r in fees))
        self.assertTrue(all(r['reason'] in ('식품 수입송금 참조번호/거래일시 일치',
                                            '사용자 지정 기본 식품 해외송금 수수료') for r in fees))
        self.assertTrue(all(r['food_status'] == '식품부문 포함' for r in fees))

    def test_temporary_coupang_period_and_mapping(self):
        products, fees, sources = load_master()
        rows, unmatched, files = read_coupang_temporary('2026-11', products, sources, date(2026, 11, 15))
        self.assertTrue(rows)
        self.assertEqual(len(files), 1)
        self.assertTrue(all(r['paid'] <= date(2026, 11, 15) for r in rows))
        keys = {(r['item_id'], r['channel']) for r in rows}
        self.assertEqual(len(keys), len(rows))
        self.assertEqual(unmatched, [])
        october, _, october_files = read_coupang_temporary('2026-10', products, sources, date(2026, 11, 15))
        self.assertTrue(october)
        self.assertTrue(all(r['order_id'].startswith('정산:') for r in october))
        self.assertTrue(all('정산' in Path(p).stem for p in october_files))
        for row in rows:
            if row['product_code']:
                self.assertIn(row['product_code'], products)


if __name__ == '__main__':
    unittest.main()
