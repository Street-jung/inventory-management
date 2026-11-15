"""Reproducible company-only Coupang snapshot totals (native option units)."""
import argparse
import json
from datetime import date

try:
    from .common import ROOT, read_excel_sheets, money, write_json
    from .channel_whitelist import load_whitelist, normalize_code
except ImportError:
    from common import ROOT, read_excel_sheets, money, write_json
    from channel_whitelist import load_whitelist, normalize_code


def summarize(target):
    whitelist = load_whitelist()
    allowed = whitelist['coupang']
    sales_path = ROOT / f'raw_data/sales/coupang/{target}_coupang_sales.xlsx'
    stock_path = ROOT / f'raw_data/inventory/{target}_coupang_inventory.xlsx'
    sales, inventory = [], []
    for _, rows in read_excel_sheets(sales_path):
        headers = [str(h or '').strip() for h in rows[0]]
        if not {'옵션 ID', '매출(원)', '판매량', '판매방식'} <= set(headers):
            continue
        sales.extend(row for values in rows[1:] if
                     (row := dict(zip(headers, values))) and
                     normalize_code(row.get('옵션 ID')) in allowed and row.get('판매방식') == '로켓그로스')
    for _, rows in read_excel_sheets(stock_path):
        headers = [str(h or '').strip() for h in rows[0]]
        stock_col = next((h for h in headers if '판매가능재고' in h.replace(' ', '') and '실시간' in h), None)
        if not stock_col or '옵션 ID' not in headers:
            continue
        for values in rows[1:]:
            row = dict(zip(headers, values))
            if normalize_code(row.get('옵션 ID')) in allowed:
                row['_available_stock'] = money(row[stock_col])
                inventory.append(row)
    if not sales or not inventory:
        raise ValueError('화이트리스트 매출/재고 행이 없습니다')
    stock = sum(row['_available_stock'] for row in inventory)
    returns = sum(row['_available_stock'] for row in inventory if '반품' in str(row.get('상품등급', '')))
    return dict(date=str(target), sales_period=f'{target.replace(day=1)}~{target}',
                whitelist=whitelist['path'], sales_source=str(sales_path), inventory_source=str(stock_path),
                sales_rows=len(sales), inventory_rows=len(inventory),
                revenue=int(sum(money(row['매출(원)']) for row in sales)),
                quantity=int(sum(money(row['판매량']) for row in sales)),
                available_stock=int(stock), new_stock=int(stock-returns), return_stock=int(returns),
                quantity_basis='쿠팡 옵션 판매수량 (묶음 구성품 환산 전)',
                inventory_basis='화이트리스트 옵션 ID의 실시간 판매가능재고 합계, 입고예정재고 제외')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, required=True)
    args = parser.parse_args()
    result = summarize(args.date)
    write_json(ROOT / f'output_excel/{args.date}_coupang_whitelist_summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
