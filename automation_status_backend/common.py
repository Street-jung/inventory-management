"""Shared I/O; all paths are relative to project root, never the caller's cwd."""
from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
SALES_FIELDS = ['channel', 'order_id', 'item_id', 'payment_date', 'product_code',
                'product_name', 'quantity', 'sales_amount', 'status', 'currency', 'source_json']


def today():
    return datetime.now(KST).date()


def yesterday():
    return today() - timedelta(days=1)


def load_env():
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'config' / '.env', override=False, encoding='utf-8-sig')


def require_env(*keys):
    missing = [key for key in keys if not os.getenv(key, '').strip()]
    if missing:
        raise ValueError('config/.env 필수 설정 누락: ' + ', '.join(missing))
    return [os.environ[key].strip() for key in keys]


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temp.replace(path)


def write_csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def money(value):
    if value is None or str(value).strip() in ('', '-', '—'):
        raise ValueError('금액 또는 수량이 비어 있습니다')
    try:
        result = Decimal(str(value).replace(',', '').replace('원', '').strip())
        if not result.is_finite():
            raise ValueError('유효하지 않은 숫자')
        return result
    except InvalidOperation as exc:
        raise ValueError(f'숫자 변환 실패: {value!r}') from exc


def won(value):
    return money(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def as_date(value):
    if isinstance(value, datetime):
        return (value.astimezone(KST) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace('.', '-').replace('/', '-')
    if re.fullmatch(r'\d{8}', text):
        return datetime.strptime(text, '%Y%m%d').date()
    parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    return (parsed.astimezone(KST) if parsed.tzinfo else parsed).date()


class APIHTTPError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'API HTTP {status_code}; 인증·권한·조회기간을 확인하세요')


def request_json(session, method, url, **kwargs):
    """Bounded retries; never include URLs, tokens, response bodies in exceptions."""
    import requests
    for attempt in range(5):
        try:
            response = session.request(method, url, timeout=(10, 60), **kwargs)
        except requests.RequestException:
            if attempt == 4:
                raise RuntimeError('API 네트워크 오류 (5회 시도)') from None
            time.sleep(min(2 ** attempt, 16))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 4:
                retry = response.headers.get('Retry-After', '')
                time.sleep(min(float(retry) if retry.isdigit() else 2 ** attempt, 30))
                continue
        if not response.ok:
            raise APIHTTPError(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise RuntimeError('API가 JSON 응답을 반환하지 않았습니다') from None
    raise RuntimeError('API 재시도 소진')


def read_excel_sheets(path):
    """Read by file signature: several exports have misleading .xlsx suffixes."""
    from openpyxl import load_workbook
    path = Path(path)
    payload = path.read_bytes()
    signature = payload[:8]
    if signature.startswith(b'\xd0\xcf\x11\xe0'):
        import xlrd
        book = xlrd.open_workbook(str(path))
        return [(s.name, [[xlrd.xldate.xldate_as_datetime(c.value, book.datemode)
                          if c.ctype == xlrd.XL_CELL_DATE else c.value
                          for c in s.row(i)] for i in range(s.nrows)]) for s in book.sheets()]
    if signature.startswith(b'PK'):
        # Keep lazy XML readers from retaining Windows handles across replace.
        from io import BytesIO
        book = load_workbook(BytesIO(payload), read_only=True, data_only=True)
        try:
            return [(s.title, list(s.iter_rows(values_only=True))) for s in book]
        finally:
            book.close()
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        text = path.read_text(encoding='cp949')
    return [('CSV', list(csv.reader(text.splitlines()))) ]


def excel_tables(folder, required_groups):
    """Find headers in first 40 rows; fail on unknown schemas instead of dropping files."""
    from openpyxl import load_workbook
    folder = Path(folder)
    paths = [folder] if folder.is_file() else sorted(p for p in folder.rglob('*') if p.is_file() and not p.name.startswith('~$'))
    for path in paths:
        if path.suffix.lower() not in ('.xlsx', '.xlsm', '.xls', '.csv'):
            continue
        sheets = read_excel_sheets(path)
        found = False
        for sheet, rows in sheets:
            for index, row in enumerate(rows[:40]):
                headers = [str(v).strip() if v is not None else '' for v in row]
                if all(any(h in headers for h in group) for group in required_groups):
                    found = True
                    if '공급자사업자등록번호' in headers and '공급받는자사업자등록번호' in headers:
                        # Repeated names refer to supplier and buyer respectively.
                        headers[6] = '공급자상호'
                        headers[11] = '공급받는자상호'
                    context = ' '.join(str(v) for r in rows[:index] for v in r if v)
                    for number, values in enumerate(rows[index + 1:], index + 2):
                        if not any(v is not None and str(v).strip() for v in values):
                            continue
                        record = dict(zip(headers, values))
                        record['_context'] = context
                        record['_source'] = f'{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{sheet}:{number}'
                        yield record
                    break
        if not found:
            raise ValueError(f'지원하는 헤더를 찾지 못했습니다: {path.name}')


def pick(row, *names, default=None):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return value
    return default
