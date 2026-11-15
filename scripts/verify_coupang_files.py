"""Inspect existing Coupang workbooks without treating them as new downloads."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from collections import Counter
from fetch_coupang_wing import today, validate_excel

ROOT = Path(__file__).resolve().parents[1]


def inspect(path, kind):
    validate_excel(path, kind)
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active
        rows = list(sheet.values)
        headers = [str(v or '') for v in rows[0]]
        if kind == 'sales':
            methods = Counter(str(row[headers.index('판매방식')]) for row in rows[1:])
            assert set(methods) <= {'로켓그로스'}, methods
            summary = {'data_rows': len(rows)-1, 'sales_methods': dict(methods),
                       'revenue': sum(float(row[headers.index('매출(원)')] or 0) for row in rows[1:]),
                       'quantity': sum(float(row[headers.index('판매량')] or 0) for row in rows[1:])}
        else:
            grade = headers.index('상품등급')
            stock = next(i for i,h in enumerate(headers) if '판매가능재고' in h and '실시간' in h)
            option = headers.index('옵션 ID')
            data = [row for row in rows[1:] if row[option]]
            summary = {'data_rows': len(data), 'grades': dict(Counter(row[grade] for row in data)),
                       'available_stock': sum(float(row[stock] or 0) for row in data),
                       'return_stock': sum(float(row[stock] or 0) for row in data if str(row[grade]).startswith('반품'))}
        return {'path': str(path.relative_to(ROOT)), 'bytes': path.stat().st_size,
                'summary': summary,
                'modified': datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'sheets': [{'name': sheet.title, 'rows': sheet.max_row,
                            'headers': [list(row) for row in sheet.iter_rows(max_row=2 if kind == 'inventory' else 1, values_only=True)]}
                           for sheet in book]}
    finally:
        book.close()


if __name__ == '__main__':
    paths = [(ROOT / f'raw_data/sales/coupang/{today()}_coupang_sales.xlsx', 'sales'),
             (ROOT / f'raw_data/inventory/{today()}_coupang_inventory.xlsx', 'inventory')]
    report = {'verified_at': datetime.now().isoformat(),
              'files': [inspect(path, kind) for path,kind in paths]}
    output = ROOT / 'raw_data/pipeline_verification/coupang_file_verification.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
