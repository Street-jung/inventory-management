"""Inspect original Excel layouts without changing the input workbooks."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from common import read_excel_sheets

ROOT = Path(__file__).resolve().parents[1]


def inspect_file(path):
    sheets = []
    tables = read_excel_sheets(path)
    for name, rows in tables:
        preview = []
        for number, row in enumerate(rows[:40], 1):
            cells = {str(index): value for index, value in enumerate(row, 1)
                     if value is not None and str(value).strip()}
            if cells:
                preview.append({'row': number, 'cells_by_column_number': cells})
        sheets.append({'sheet': name, 'row_count': len(rows),
                       'column_count': max((len(row) for row in rows), default=0),
                       'first_40_rows': preview})
    return {'file': str(path.relative_to(ROOT)), 'sheets': sheets}


def serialize(value):
    return value.isoformat() if isinstance(value, (datetime, date)) else str(value)


def main():
    results = []
    for folder in ('master_data', 'raw_data'):
        for path in sorted((ROOT / folder).rglob('*')):
            if path.suffix.lower() in ('.xlsx', '.xlsm', '.xls') and not path.name.startswith('~$'):
                results.append(inspect_file(path))
    output = ROOT / 'output_excel' / 'inspection' / 'excel_structure.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=serialize), encoding='utf-8')
    for result in results:
        print(result['file'])
        for sheet in result['sheets']:
            print(f"  {sheet['sheet']}: {sheet['row_count']}행, {sheet['column_count']}열")
    print(f'구조 확인 보고서: {output}')


if __name__ == '__main__':
    main()
