"""Export dashboard data from source records, without relying on Excel caches."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from calendar import monthrange
from decimal import Decimal, ROUND_HALF_UP

try:
    from .common import ROOT, KST, as_date, money, won, write_json, yesterday
    from . import filter_bank_and_hometax as bank_tax
    from . import generate_monthly_pnl as pnl
except ImportError:
    from common import ROOT, KST, as_date, money, won, write_json, yesterday
    import filter_bank_and_hometax as bank_tax
    import generate_monthly_pnl as pnl


def number(value):
    if value is None or value == '':
        return None
    value = money(value)
    return int(value) if value == value.to_integral_value() else float(value)


def cash_summary(rows):
    incoming = sum((money(r['deposit_amount']) for r in rows), Decimal(0))
    outgoing = sum((money(r['withdrawal_amount']) for r in rows), Decimal(0))
    categories = defaultdict(list)
    for row in rows:
        categories[row['category']].append(row)
    return dict(transaction_count=len(rows), deposit_total=number(incoming),
                withdrawal_total=number(outgoing), net_cash_flow=number(incoming - outgoing),
                categories=[dict(category=key, transaction_count=len(group),
                                 deposit_total=number(sum((money(r['deposit_amount']) for r in group), Decimal(0))),
                                 withdrawal_total=number(sum((money(r['withdrawal_amount']) for r in group), Decimal(0))))
                            for key, group in sorted(categories.items())])


def transaction(row):
    return dict(transaction_id=row['transaction_id'], date=row['transaction_date'],
                datetime=row['transaction_datetime'], customer=row['customer'],
                description=row['description'], category=row['category'], status=row['food_status'],
                deposit_amount=number(row['deposit_amount']), withdrawal_amount=number(row['withdrawal_amount']))


def profit_summary(detail):
    """Mirror per-item Excel rounding; missing costs remain null in full totals."""
    revenue = cost = fees = matched_revenue = matched_profit = Decimal(0)
    missing_cost = missing_fee = 0
    for row in detail:
        amount = money(row[7])
        revenue += amount
        # Excel serializes repeating-decimal unit costs at limited precision.
        # Stabilize the product before won rounding so JSON and Excel agree.
        item_cost = won((money(row[6]) * money(row[8])).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)) if row[8] is not None else None
        fee = money(row[11]) if len(row) > 11 and row[11] is not None else won(amount * money(row[10])) if row[10] is not None else None
        if item_cost is None:
            missing_cost += 1
        else:
            cost += item_cost
        if fee is None:
            missing_fee += 1
        else:
            fees += fee
        if item_cost is not None and fee is not None:
            matched_revenue += amount
            matched_profit += amount - item_cost - fee
    return dict(row_count=len(detail),
                total_quantity=number(sum((money(r[6]) for r in detail), Decimal(0))),
                active_sku_count=len({(r[0], r[2]) for r in detail}),
                has_data=bool(detail), revenue=number(revenue) if detail else None,
                cost=number(cost) if detail and not missing_cost else None,
                fees=number(fees) if detail and not missing_fee else None,
                contribution_profit=number(revenue - cost - fees) if detail and not (missing_cost or missing_fee) else None,
                matched_revenue=number(matched_revenue), matched_cost=number(cost),
                matched_contribution_profit=number(matched_profit),
                missing_cost_rows=missing_cost, missing_fee_rows=missing_fee)


def sales_summary(detail):
    channels = sorted({r[0] for r in detail})
    return dict(**profit_summary(detail), channels=[dict(channel=c, **profit_summary([r for r in detail if r[0] == c]))
                                                   for c in channels])


def product_transaction(row):
    summary = profit_summary([row])
    revenue, profit, quantity = summary['revenue'], summary['contribution_profit'], number(row[6])
    return dict(date=str(row[3]), channel=row[0], order_id=row[1], item_id=row[2],
                product_code=row[4], product_name=row[5], quantity=quantity,
                **summary, asp=round(revenue / quantity, 2) if quantity else None,
                profit=profit, margin_pct=round(profit / revenue * 100, 2)
                if profit is not None and revenue else None)


def bank_management_summary(rows):
    """Cash measures only: do not add to recognized sales or subtract twice."""
    b2b = {'B2B·카페24 매출(고정)', 'B2B 선물세트 매출(일회성)'}
    expenses = {'디자인·마케팅비', '인쇄·판촉물비', '부자재·패키징비',
                '입고·운반 용차비', '수입통관·포워딩비', '수입포워딩(국제물류)',
                '수입통관(관세사)'}
    return dict(b2b_deposit_total=number(sum((money(r['deposit_amount']) for r in rows
                    if set(r['category'].split('; ')) & b2b), Decimal(0))),
                food_sga_withdrawal_total=number(sum((money(r['withdrawal_amount']) for r in rows
                    if set(r['category'].split('; ')) & expenses), Decimal(0))))


def add_real_profit_estimates(series):
    """Use the latest two settled months, revenue-weighted, excluding marketplace fees.

    Keep current marketplace fees; add historical net logistics/agency/advertising
    and adjustments. Estimates are scenarios, not accrued or confirmed expenses.
    """
    for index, entry in enumerate(series):
        entry.update(estimated_real_profit=None, estimated_real_margin_pct=None,
                     estimated_additional_fees=None, estimation_basis=None)
        if not entry['provisional'] or entry['profit'] is None or not entry['revenue']:
            continue
        history = [r for r in series[:index] if r['data_status'] == 'settled'
                   and r['revenue'] and r['fees'] is not None
                   and 'marketplace' in r['fee_breakdown']][-2:]
        if not history:
            continue
        base = sum((money(r['revenue']) for r in history), Decimal(0))
        extra = sum((money(r['fees']) - money(r['fee_breakdown']['marketplace'])
                     for r in history), Decimal(0))
        rate = max(Decimal(0), extra / base)
        # Historical extras replace any already present current extras.
        current_extra = sum((money(v) for k, v in entry['fee_breakdown'].items()
                             if k != 'marketplace'), Decimal(0))
        additional = won(money(entry['revenue']) * rate - current_extra)
        estimate = money(entry['profit']) - additional
        entry.update(estimated_real_profit=number(estimate),
                     estimated_real_margin_pct=round(float(estimate / money(entry['revenue']) * 100), 2),
                     estimated_additional_fees=number(additional),
                     estimation_basis=dict(months=[r['month'] for r in history],
                         additional_cost_rate_pct=round(float(rate * 100), 4),
                         method='최근 확정 2개월 매출가중 비판매수수료 비용률; 물류·대행·광고·정산조정 포함',
                         cash_expenses_included=False))


def monthly_unverified_reference(month, report_date):
    """Daily store totals lack option IDs: keep them outside owned-product P&L."""
    path = ROOT / 'raw_data/sales/coupang' / f'{month}_coupang_sales.xlsx'
    if not path.exists():
        return None
    total, days = Decimal(0), set()
    for _, rows in pnl.read_excel_sheets(path):
        if not rows or '날짜' not in rows[0] or '매출(원)' not in rows[0]:
            continue
        headers = list(rows[0])
        for values in rows[1:]:
            row = dict(zip(headers, values))
            day = as_date(row['날짜'])
            if day.strftime('%Y-%m') != month or day > report_date:
                continue
            if day in days:
                raise ValueError(f'일별 총매출 날짜 중복: {path}: {day}')
            days.add(day)
            total += money(row['매출(원)'])
    if not days:
        return None  # An option-detail replacement is not a store-total reference.
    return dict(revenue=number(total), days=len(days), source=str(path),
                included_in_pnl=False, reason='상품/옵션 ID 없음: 자사 상품 범위·원가·수수료 확인 불가')


def sales_metrics(detail, report_date, provisional=False, previous_detail=None):
    groups, products = defaultdict(list), defaultdict(list)
    for row in detail:
        groups[(row[3], row[0])].append(row)
        products[row[4] or (row[0], row[5])].append(row)
    trend = [dict(date=str(day), channel=channel,
                  basis='cumulative_snapshot' if any(str(r[1]).startswith('임시집계:') for r in rows) else 'daily',
                  **profit_summary(rows)) for (day, channel), rows in sorted(groups.items())]

    def comparison(offset):
        previous = report_date - timedelta(days=offset)
        current_rows = [r for r in detail if r[3] == report_date]
        previous_rows = [r for r in detail if r[3] == previous]
        reason = None
        if provisional:
            reason = '누적 스냅샷이 포함되어 일별 비교 불가'
        elif not current_rows or not previous_rows:
            reason = '비교 기간 데이터 부족 (미수집 일자는 0으로 간주하지 않음)'
        elif {r[0] for r in current_rows} != {r[0] for r in previous_rows}:
            reason = '비교일 채널 범위 불일치'
        current, prior = profit_summary(current_rows), profit_summary(previous_rows)
        result = dict(current_date=str(report_date), comparison_date=str(previous), message=reason)
        for field in ('revenue', 'contribution_profit'):
            a, b = current[field], prior[field]
            result[field + '_pct'] = round((a-b)/abs(b)*100, 2) if not reason and a is not None and b not in (None, 0) else None
        if not reason and any(result[k] is None for k in ('revenue_pct', 'contribution_profit_pct')):
            result['message'] = '비교 기준액이 0이거나 원가/수수료 누락'
        return result

    monthly = profit_summary(detail)
    forecast = dict(month=report_date.strftime('%Y-%m'), as_of=str(report_date),
                    elapsed_days=report_date.day, total_days=monthrange(report_date.year, report_date.month)[1],
                    provisional=provisional, message='수집된 당월 누적액 기준 단순 추정. 미수집 매출 및 비용 미반영.')
    for field in ('revenue', 'contribution_profit'):
        value = monthly[field]
        forecast['daily_average_' + field] = number(money(value)/report_date.day) if value is not None else None
        forecast['projected_' + field] = number(won(money(value)/report_date.day*forecast['total_days'])) if value is not None else None
    ranked = []
    for rows in products.values():
        summary = profit_summary(rows)
        revenue, profit = summary['revenue'], summary['contribution_profit']
        ranked.append(dict(product_code=rows[0][4], product_name=rows[0][5],
                           quantity=number(sum((money(r[6]) for r in rows), Decimal(0))),
                           **summary, margin_pct=round(profit/revenue*100, 2) if profit is not None and revenue else None))
    mom = dict(revenue_pct=None, contribution_profit_pct=None,
               message='전월 동일 경과기간 데이터 및 수집 완전성 확인 필요')
    # Require every elapsed calendar day and matching channel coverage for MTD comparison.
    prior = previous_detail or []
    if not provisional and detail and prior:
        current_channels = {r[0] for r in detail}
        prior_channels = {r[0] for r in prior}
        complete = current_channels == prior_channels and all(
            {r[3].day for r in rows if r[0] == channel} == set(range(1, report_date.day + 1))
            for rows in (detail, prior) for channel in current_channels)
        if complete:
            mom['message'] = None
            previous_summary = profit_summary(prior)
            for field in ('revenue', 'contribution_profit'):
                a, b = monthly[field], previous_summary[field]
                mom[field + '_pct'] = round((a-b)/abs(b)*100, 2) if a is not None and b not in (None, 0) else None
            if any(mom[k] is None for k in ('revenue_pct', 'contribution_profit_pct')):
                mom['message'] = '비교 기준액이 0이거나 원가/수수료 누락'
    return dict(daily_trend=trend,
                growth_metrics=dict(dod=comparison(1), wow=comparison(7),
                                    mom=mom),
                monthly_forecast=forecast,
                top_products=dict(by_revenue=sorted(ranked, key=lambda r: (-r['revenue'], r['product_name']))[:10],
                                  by_quantity=sorted(ranked, key=lambda r: (-r['quantity'], r['product_name']))[:10]),
                transactions=[product_transaction(r) for r in detail])


def build_data(report_date, as_of, coupang_filename_month=None, bank_file=None):
    month = report_date.strftime('%Y-%m')
    rules = bank_tax.load_rules()
    if coupang_filename_month is None:
        coupang_filename_month = rules.get('dashboard_export', {}).get('coupang_filename_month', False)
    bank = bank_tax.read_bank(bank_file or ROOT / 'raw_data/bank', rules)
    bank = [r for r in bank if as_date(r['transaction_date']) <= as_of]
    food = [r for r in bank if r['food_status'] == '식품부문 포함']
    unknown = [r for r in bank if r['food_status'] == '미분류(확인필요)']
    sales_invoices = bank_tax.read_invoices(ROOT / 'raw_data/hometax')
    purchases = bank_tax.read_purchase_invoices(ROOT / 'raw_data/hometax')
    settlements = bank_tax.food_settlements(bank, sales_invoices, purchases, rules, as_of)
    for row in settlements:
        for key in ('sales_amount', 'purchase_offset', 'net_amount', 'paid_amount'):
            row[key] = number(row[key])
        row['outstanding_amount'] = max(0, row['net_amount'] - row['paid_amount']) if row['net_amount'] is not None else None
        row['is_overdue'] = bool(row['outstanding_amount'] and as_date(row['due_date']) < as_of)
    products, fees, sources = pnl.load_master()
    sales, unmatched, files, provisional = pnl.read_month_sales(
        month, products, sources, coupang_filename_month, report_date)
    # The common loader already selected one snapshot or the confirmed statement.
    # A month-only filename is valid; do not discard its unmatched-cost audit rows.
    detail, issues = pnl.prepare_rows(sales, products, fees)
    monthly_series = []
    all_sources = set(files)
    for period in pnl.reporting_months(report_date):
        period_sales, _, period_files, period_provisional = pnl.read_month_sales(
            period, products, sources, coupang_filename_month, report_date)
        all_sources.update(period_files)
        period_detail, period_issues = pnl.prepare_rows(pnl.latest_snapshots(period_sales), products, fees)
        summary = profit_summary(period_detail)
        revenue, profit = summary['revenue'], summary['contribution_profit']
        breakdown = defaultdict(Decimal)
        for row in period_sales:
            for key, value in row.get('fee_breakdown', {}).items():
                breakdown[key] += value
        settled = any(str(r['order_id']).startswith('정산:') for r in period_sales)
        monthly_series.append(dict(month=period, **summary, profit=profit,
                                   **bank_management_summary([r for r in food if r['transaction_date'][:7] == period]),
                                   settlement_audit=next((r['settlement_audit'] for r in period_sales if 'settlement_audit' in r), None),
                                   fee_breakdown={k: number(v) for k, v in breakdown.items()},
                                   sources=period_files,
                                   unverified_store_totals=monthly_unverified_reference(period, report_date),
                                   data_status='settled' if settled and not period_issues else 'partial' if period_detail else 'unavailable',
                                   message='정산서 우선·판매자쿠폰 차감·마스터 원가(묶음수량 반영)·실제 정산비용(광고비/보상 포함), VAT 포함' if settled else '수집된 자사 품목 자료만 집계; 일별 점포 총매출은 손익 합산 제외',
                                   margin_pct=round(profit / revenue * 100, 2) if profit is not None and revenue else None,
                                   issues=period_issues, provisional=period_provisional,
                                   settlements=[r for r in settlements if r['issue_date'][:7] == period]))
    add_real_profit_estimates(monthly_series)
    for index, entry in enumerate(monthly_series):
        prior = monthly_series[index - 1]['revenue'] if index else None
        entry['mom_growth_pct'] = round((entry['revenue'] - prior) / abs(prior) * 100, 2) if entry['revenue'] is not None and prior not in (None, 0) else None
    mom_growth_pct = monthly_series[-1]['mom_growth_pct'] if monthly_series else None
    previous_month = (report_date.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    previous_sales, _ = pnl.read_sales(previous_month)
    previous_detail, _ = pnl.prepare_rows([r for r in previous_sales if r['paid'].day <= report_date.day], products, fees)
    warnings = ['부분 자료 기준. 부가세 조정·환불·배송손익·인건비·광고비·고정비 미반영']
    if coupang_filename_month:
        warnings.append('미정산 쿠팡은 최신 옵션 상세 1개만 임시반영(파일명 일자 또는 원본 생성시각 KST 기준). 실제 집계기간·묶음 수량 확인 필요')
    for channel in ('cafe24', 'smartstore'):
        missing = [f'{month}-{d:02d}' for d in range(1, report_date.day + 1)
                   if not (ROOT / 'raw_data/sales' / channel / f'{month}-{d:02d}.csv').exists()]
        if missing:
            warnings.append(f'{channel} 미수집 일자: ' + ', '.join(missing))
    by_day, by_month = defaultdict(list), defaultdict(list)
    for row in food:
        by_day[row['transaction_date']].append(row)
        by_month[row['transaction_date'][:7]].append(row)
    classification = []
    for status in ('식품부문 포함', '식품부문 제외', '미분류(확인필요)'):
        classification.append(dict(status=status, **cash_summary([r for r in bank if r['food_status'] == status])))
    return dict(schema_version='1.3', monthly_series=monthly_series, mom_growth_pct=mom_growth_pct,
                mom_basis='당월 수집 누계 / 전월 수집 누계; 당월은 보고일까지, 동일 경과일 비교 아님', metadata=dict(
        generated_at=datetime.now(KST).isoformat(), report_date=str(report_date), as_of=str(as_of), month=month,
        currency='KRW', bank_count=len(bank), bank_transaction_start=min((r['transaction_date'] for r in bank), default=None),
        bank_transaction_end=max((r['transaction_date'] for r in bank), default=None),
        coupang_filename_month=coupang_filename_month, warnings=warnings,
        sources=sorted({r['source'].split(':')[0] for r in bank}) + sorted(all_sources)),
        cash_flow=dict(daily=dict(date=str(report_date), **cash_summary(by_day[str(report_date)])),
                       monthly=dict(month=month, **cash_summary(by_month[month])),
                       monthly_summary=[dict(month=r['month'], **cash_summary(by_month[r['month']]),
                                             **bank_management_summary(by_month[r['month']])) for r in monthly_series],
                       daily_history=[dict(date=k, **cash_summary(v)) for k, v in sorted(by_day.items())],
                       monthly_history=[dict(month=k, **cash_summary(v)) for k, v in sorted(by_month.items())],
                       transactions=[transaction(r) for r in sorted(food, key=lambda r: r['transaction_datetime'], reverse=True)],
                       recent_transactions=[transaction(r) for r in sorted(food, key=lambda r: r['transaction_datetime'], reverse=True)[:30]],
                       classification=classification),
        settlements=dict(items=settlements, source_sales_invoice_count=len(sales_invoices),
                         source_purchase_invoice_count=len(purchases),
                         outstanding_total=sum(r['outstanding_amount'] or 0 for r in settlements),
                         overdue_total=sum(r['outstanding_amount'] or 0 for r in settlements if r['is_overdue']),
                         prior_period_receipts=[r for r in settlements if r['net_amount'] is None]),
        sales_profit=dict(daily=dict(date=str(report_date), **sales_summary([r for r in detail if r[3] == report_date])),
                          monthly_series=monthly_series, mom_growth_pct=mom_growth_pct,
                          monthly=dict(month=month, **sales_summary(detail)),
                          daily_history=[dict(date=str(d), **sales_summary([r for r in detail if r[3] == d]))
                                         for d in sorted({r[3] for r in detail})],
                          **sales_metrics(detail, report_date, provisional, previous_detail),
                          provisional=provisional, issues=issues, unmatched_products=unmatched),
        unclassified=dict(count=len(unknown), customers=sorted({r['customer'] for r in unknown}),
                          transactions=[transaction(r) for r in unknown]))


def run(report_date=None, as_of=None, coupang_filename_month=None, bank_file=None, output=None):
    report_date = report_date or yesterday()
    as_of = as_of or report_date
    data = build_data(report_date, as_of, coupang_filename_month, bank_file)
    output = output or ROOT / 'web_dashboard/dashboard_data.json'
    write_json(output, data)
    print('월 | 매출액 | 매출원가 | 수수료/정산비용 | 기여이익 | 이익률(%) | MoM(%) | 상태')
    for row in data['monthly_series']:
        values = [row[k] for k in ('revenue', 'cost', 'fees', 'profit', 'margin_pct', 'mom_growth_pct')]
        print(row['month'] + ' | ' + ' | '.join('—' if v is None else f'{v:,.2f}' for v in values)
              + ' | ' + row['data_status'])
    print('월 | 총 판매수량 | 판매 SKU 수 | 현실 추정 기여이익 | 현실 추정 이익률(%) | B2B 입금 | 식품 판관비 출금')
    for row in data['monthly_series']:
        values = [row[k] for k in ('total_quantity', 'active_sku_count', 'estimated_real_profit',
                  'estimated_real_margin_pct', 'b2b_deposit_total', 'food_sga_withdrawal_total')]
        print(row['month'] + ' | ' + ' | '.join('—' if v is None else f'{v:,.2f}' for v in values))
    return dict(output=str(output), bank_count=data['metadata']['bank_count'],
                unclassified=data['unclassified']['count'], settlements=len(data['settlements']['items']),
                monthly_sales=data['sales_profit']['monthly']['revenue'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=yesterday())
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--coupang-filename-month', action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    print(run(args.date, args.as_of, args.coupang_filename_month))
