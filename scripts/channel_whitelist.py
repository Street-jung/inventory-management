"""Company ownership comes exclusively from Orchid's channel mapping sheet."""
from __future__ import annotations

import re
from pathlib import Path

try:
    from .common import ROOT, read_excel_sheets
except ImportError:
    from common import ROOT, read_excel_sheets

MASTER_FILE = ROOT / 'master_data' / '오키드상품정보.xlsx'
HEADERS = ('판매자관리코드', '개미창고 상품코드', '개미창고 옵션명', '쿠팡 옵션 ID')


def normalize_code(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_whitelist(path=None):
    path = Path(path) if path else MASTER_FILE
    sheets = dict(read_excel_sheets(path))
    if '채널코드맵핑' not in sheets:
        raise ValueError('화이트리스트 채널코드맵핑 시트가 없습니다')
    rows = sheets['채널코드맵핑']
    index = next((i for i, row in enumerate(rows) if tuple(row[:4]) == HEADERS), None)
    if index is None:
        raise ValueError('화이트리스트 필수 컬럼이 없습니다')
    records, coupang, gaemi, codes = [], {}, set(), set()
    for row in rows[index + 1:]:
        if not row or not row[0]:
            continue
        record = dict(zip(HEADERS, row))
        code = normalize_code(row[0]).upper()
        codes.add(code)
        gaemi_code = normalize_code(record.get(HEADERS[1]))
        if gaemi_code:
            gaemi.add(gaemi_code)
        for option in re.split(r'[,;\n]+', normalize_code(record.get(HEADERS[3]))):
            option = option.strip()
            if not option:
                continue
            if option in coupang and coupang[option] != code:
                raise ValueError(f'쿠팡 옵션 ID 복수 상품 매핑: {option}')
            coupang[option] = code
        records.append(record)
    if not codes:
        raise ValueError('화이트리스트가 비어 있습니다')
    return dict(path=str(path), headers=list(HEADERS), records=records,
                product_codes=codes, coupang=coupang, gaemi=gaemi)


def filter_inventory(parsed, source, whitelist):
    allowed = whitelist['gaemi'] if source == 'gaemi' else whitelist['coupang']
    return {key: value for key, value in parsed.items() if normalize_code(key) in allowed}


def latest_snapshots(sales):
    """A later month-to-date snapshot replaces all earlier snapshots."""
    dates = [r['paid'] for r in sales if str(r['order_id']).startswith('임시집계:')]
    latest = max(dates) if dates else None
    return [r for r in sales if not str(r['order_id']).startswith('임시집계:') or r['paid'] == latest]
