"""Build payment-date monthly contribution P&L from every normalized channel CSV."""
from __future__ import annotations

import argparse
import calendar
import csv
import re
from datetime import date
from decimal import Decimal

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    from .common import *
    from .channel_whitelist import load_whitelist, normalize_code, latest_snapshots
except ImportError:
    from common import *
    from channel_whitelist import load_whitelist, normalize_code, latest_snapshots


def load_master(folder=None):
    folder = Path(folder) if folder else ROOT / 'master_data'
    products, fees, sources = {}, {}, []
    for path in sorted(folder.glob('*.xlsx')):
        if path.name.startswith('~$'):
            continue
        book = load_workbook(path, data_only=True, read_only=True)
        try:
            if '상품data' not in book.sheetnames or '비용data' not in book.sheetnames:
                continue
            sources.append(str(path))
            for row in book['상품data'].iter_rows(min_row=3, values_only=True):
                for offset in (0, 12):
                    if len(row) <= offset or not row[offset]:
                        continue
                    code = str(row[offset]).strip()
                    # Master codes prefixed 90 are explicitly nonfood in CODE분류.
                    name = row[offset + 1]
                    raw_cost = row[offset + 5]
                    try:
                        cost = money(raw_cost) if raw_cost is not None and str(raw_cost).strip() else None
                        if cost is None:
                            raise ValueError('missing cost')
                        if cost < 0:
                            raise ValueError('negative cost')
                    except ValueError:
                        cost = None
                    record = dict(name=name, cost=cost, food=not code.startswith(('90', 'S9')), source=str(path))
                    if code in products and products[code] != record:
                        raise ValueError(f'기준표 상품코드 중복·충돌: {code}')
                    products[code] = record
            for row in book['비용data'].iter_rows(min_row=4, values_only=True):
                if not row[0]:
                    continue
                rate = money(row[1])
                if rate < 0 or rate > 1:
                    raise ValueError(f'수수료율은 0~1이어야 합니다: {row[0]}')
                platform = str(row[0]).strip()
                if platform in fees and fees[platform] != rate:
                    raise ValueError(f'수수료 기준 중복·충돌: {platform}')
                fees[platform] = rate
        finally:
            book.close()
    if not products or not fees:
        raise ValueError('상품data/비용data 기준표를 찾지 못했습니다')
    return products, fees, sources


def fee_for(channel, fees):
    aliases = {'cafe24': '카페24 — KG이니시스', 'smartstore': '네이버 스마트스토어',
               'naver_ndelivery': '네이버 N배송', 'coupang': '쿠팡 일반 셀러 — Wing',
               'coupang_growth': '쿠팡 로켓그로스'}
    key = aliases.get(channel, channel)
    return fees.get(key)


def add_coupang_reference(book, folder=None):
    """Keep undated item metrics visible without inventing a payment period."""
    folder = Path(folder) if folder else ROOT / 'raw_data' / 'sales' / 'coupang'
    sheet = book.create_sheet('쿠팡집계_기간미확인')
    sheet.append(['월손익 미반영: 원본에 집계기간·결제일 없음. 파일명은 수집일일 수 있음'])
    sheet.append(['옵션 ID', '상품명', '카테고리', '판매방식', '매출(원)', '판매량',
                  '총 매출(원)', '총 취소 금액(원)', '원본파일'])
    count = 0
    whitelist = load_whitelist()
    for path in sorted(folder.glob('*.xlsx')):
        if '정산' in path.stem:
            continue
        for name, rows in read_excel_sheets(path):
            for index, values in enumerate(rows[:40]):
                headers = [str(v).strip() if v is not None else '' for v in values]
                if not {'옵션 ID', '매출(원)', '판매량'}.issubset(headers):
                    continue
                for values in rows[index + 1:]:
                    row = dict(zip(headers, values))
                    if normalize_code(row.get('옵션 ID')) not in whitelist['coupang']:
                        continue
                    sheet.append([str(row['옵션 ID']), row.get('상품명'), row.get('카테고리'),
                                  row.get('판매방식'), float(money(row['매출(원)'])),
                                  float(money(row['판매량'])), float(money(row['총 매출(원)'])),
                                  float(money(row['총 취소 금액(원)'])), str(path)])
                    count += 1
                break
    sheet.freeze_panes = 'A3'
    sheet.auto_filter.ref = f'A2:I{max(2, sheet.max_row)}'
    for col, width in {'A': 20, 'B': 70, 'C': 22, 'D': 20, 'E': 18,
                       'F': 14, 'G': 18, 'H': 22, 'I': 70}.items():
        sheet.column_dimensions[col].width = width
    for row in sheet.iter_rows(min_row=3):
        for cell in row:
            if cell.data_type == 'f':
                cell.data_type = 's'
    return count


def read_sales(month, folder=None):
    folder = Path(folder) if folder else ROOT / 'raw_data' / 'sales'
    rows, seen, files = [], {}, []
    whitelist = load_whitelist()
    for path in sorted(folder.rglob('*.csv')):
        with path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            required = set(SALES_FIELDS) - {'source_json'}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f'매출 CSV 헤더 오류: {path}')
            for row in reader:
                if row['channel'] in ('coupang', 'coupang_growth'):
                    code = whitelist['coupang'].get(normalize_code(row['item_id']))
                    if code is None:
                        continue
                    row['product_code'] = code
                paid = as_date(row['payment_date'])
                if paid.strftime('%Y-%m') != month:
                    continue
                key = (row['channel'], row['order_id'], row['item_id'])
                if not all(key):
                    raise ValueError(f'매출 고유키 누락: {path}')
                comparable = {k: v for k, v in row.items() if k != 'source_json'}
                if key in seen:
                    if seen[key] != comparable:
                        raise ValueError(f'동일 매출 품목 값 충돌: {key}')
                    continue
                seen[key] = comparable
                if row['currency'] != 'KRW':
                    raise ValueError(f'KRW 외 통화 환산 설정 필요: {key}')
                quantity, amount = money(row['quantity']), money(row['sales_amount'])
                if quantity < 0 or amount < 0:
                    raise ValueError(f'음수 매출은 별도 환불 명세가 필요합니다: {key}')
                row.update(quantity=quantity, sales_amount=amount, paid=paid, source=str(path))
                rows.append(row)
                if str(path) not in files:
                    files.append(str(path))
    return rows, files


def agency_settlement_path(month):
    year, mm = map(int, month.split('-'))
    period = re.compile(rf'^(?:{year}년\s*(?:{mm}|{mm:02d})월(?:\D|$)|{month}(?:\D|$))')
    paths = sorted(p for p in (ROOT / 'raw_data/sales/coupang').glob('*정산*.xlsx')
                   if not p.name.startswith('~$') and period.match(p.stem))
    if len(paths) > 1:
        raise ValueError(f'정산서 중복: {month}')
    return paths[0] if paths else None


def bundle_units(option_name):
    counts = re.findall(r'(\d+)\s*개', str(option_name).replace('\xa0', ' '))
    if not counts or int(counts[-1]) < 1:
        raise ValueError(f'옵션 묶음수량 확인 필요: {option_name}')
    return int(counts[-1])


def read_agency_settlement(month, through_date=None):
    """Use the settled option schedule once, never also add its RAW or snapshots.

    Monetary basis follows the existing dashboard: VAT-inclusive cash amounts.
    CFS costs are VAT-exclusive in 2P; marketplace fees already include VAT.
    """
    path = agency_settlement_path(month)
    if path is None:
        return None
    first = date.fromisoformat(month + '-01')
    period_end = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    # A confirmed statement owns the entire attribution month, even when a
    # historical cutoff is supplied. Never silently replace it with a snapshot.
    allowed = load_whitelist()['coupang']
    book = load_workbook(path, read_only=True, data_only=True)
    rows = []
    audit = dict(source=str(path), attribution_month=month,
                 basis='VAT 포함; 순매출=판매액-판매자쿠폰; 현재 마스터 원가×환산수량',
                 cover_date_warning='갑지 날짜는 2025년 표기; RAW 세금계산서 발행월로 2026년 귀속 검증',
                 raw_blank_fee_total_policy='RAW AC 공란은 정산서 SUMIF와 동일하게 합산 제외; AA의 할인 전 수수료로 대체하지 않음',
                 excluded_3p_rows=0, excluded_3p_gross=0, raw_blank_fee_total_rows=0)
    try:
        required = {'2P', '견적(을지)', '매출(주문,할인)RAW'}
        if not required.issubset(book.sheetnames):
            raise ValueError(f'정산서 필수 시트 누락: {path}')
        raw = book['매출(주문,할인)RAW']
        raw_totals = [Decimal(0)] * 4
        for values in raw.iter_rows(min_row=3, values_only=True):
            if not values[11]:
                continue
            if str(values[2]) != month:
                raise ValueError(f'정산서 RAW 귀속월 불일치: {path}: {values[2]}')
            audit['raw_blank_fee_total_rows'] += int(values[28] is None)
            for i, col in enumerate((16, 17, 22, 28)):
                # SUMIF in the signed-off option schedule ignores blank AC
                # cells; never substitute the pre-discount AA fee for AC.
                raw_totals[i] += Decimal(0) if col == 28 and values[col] is None else money(values[col])
        schedule = list(book['2P'].values)
        expected = ['옵션ID', '등록상품명', '옵션명', '수량', '매출', '할인쿠폰',
                    '판매수수료', '입출고비', '배송비', '보관비', '재고손실보상', '바코드부가서비스', '상품원가']
        if [str(v).replace('\n', '') for v in schedule[0][:13]] != expected:
            raise ValueError(f'2P 컬럼 변경: {path}')
        option_rows = [r for r in schedule[1:] if normalize_code(r[0]).isdigit()]
        totals = [sum((money(r[c]) for r in option_rows), Decimal(0)) for c in (3, 4, 5, 6)]
        if totals != raw_totals:
            raise ValueError(f'2P와 매출 RAW 불일치(수량/매출/쿠폰/판매수수료): {path}: {totals} != {raw_totals}')
        # Read the TOTAL block only; later rows repeat the 2P/3P breakdown.
        cover = {r[1]: r for r in book['견적(을지)'].iter_rows(min_row=4, max_row=13, values_only=True)}
        gross_total = money(cover['매출'][4])
        agency_total = money(cover['자사 수수료'][4])
        rate = (agency_total / gross_total).quantize(Decimal('0.00000001'))
        if rate < 0 or rate > 1:
            raise ValueError(f'대행수수료율 오류: {path}')
        ad_total = money(cover['광고비'][4])
        # Allocate a monthly bill only to the owned share of its 2P sales.
        # Last eligible row receives the rounding remainder, not a second bill.
        owned = [r for r in option_rows if normalize_code(r[0]) in allowed]
        owned_gross = sum((money(r[4]) for r in owned), Decimal(0))
        ad_target = (ad_total * owned_gross / totals[1]).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if totals[1] else Decimal(0)
        ad_allocated = Decimal(0)
        seen = set()
        for index, r in enumerate(owned):
            option = normalize_code(r[0])
            if option in seen:
                raise ValueError(f'2P 옵션 중복: {path}: {option}')
            seen.add(option)
            gross, coupon = money(r[4]), money(r[5])
            ad = ad_target - ad_allocated if index == len(owned) - 1 else (ad_target * gross / owned_gross).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if owned_gross else Decimal(0)
            ad_allocated += ad
            parts = dict(marketplace=money(r[6]), fulfillment=money(r[7]) * Decimal('1.1'),
                         shipping=money(r[8]) * Decimal('1.1'), storage=money(r[9]) * Decimal('1.1'),
                         barcode=money(r[11]) * Decimal('1.1'), agency=gross * rate,
                         advertising=ad, inventory_compensation=-money(r[10]))
            rows.append(dict(channel='coupang_growth', order_id=f'정산:{month}:2P:{option}', item_id=option,
                             paid=period_end, product_code=allowed[option], product_name=r[1] + ' / ' + r[2],
                             quantity=money(r[3]) * bundle_units(r[2]), sales_amount=gross - coupon,
                             actual_fee=sum(parts.values(), Decimal(0)), fee_breakdown=parts,
                             status='확정 정산 / 2P / VAT 포함 / 광고비·보상 포함', source=str(path)))
        if '3P(판매자배송건)' in book.sheetnames:
            sheet = book['3P(판매자배송건)']
            headers = [str(v).strip() for v in next(sheet.iter_rows(min_row=5, max_row=5, values_only=True))]
            required_headers = {'주문번호', '옵션 ID', '옵션명', '판매수량', '환불수량', '판매액', '판매수수료'}
            if not required_headers.issubset(headers):
                raise ValueError(f'3P 컬럼 변경: {path}')
            seen = set()
            for n, values in enumerate(sheet.iter_rows(min_row=6, values_only=True), 6):
                r = dict(zip(headers, values))
                option = normalize_code(r.get('옵션 ID'))
                if option not in allowed:
                    if option.isdigit() and r.get('주문번호'):
                        audit['excluded_3p_rows'] += 1
                        audit['excluded_3p_gross'] += float(money(r['판매액']))
                    continue
                key = (str(r['주문번호']), option)
                if key in seen:
                    raise ValueError(f'3P 주문/옵션 중복: {path}: {key}')
                seen.add(key)
                gross = money(r['판매액'])
                parts = dict(marketplace=money(r['판매수수료']),
                             service_adjustment=money(r['서비스이용료소급분']),
                             myshop_discount=-money(r['마이샵수수료할인']), agency=gross * rate)
                rows.append(dict(channel='coupang', order_id=f'정산:{month}:3P:{key[0]}', item_id=option,
                                 paid=period_end, product_code=allowed[option], product_name=r['옵션명'],
                                 quantity=(money(r['판매수량']) - money(r['환불수량'])) * bundle_units(r['옵션명']),
                                 sales_amount=gross - money(r['판매자 할인쿠폰(A+B)']),
                                 actual_fee=sum(parts.values(), Decimal(0)), fee_breakdown=parts,
                                 status='확정 정산 / 3P / VAT 포함', source=str(path)))
        for row in rows:
            row['settlement_audit'] = audit
        return rows, [], [str(path)]
    finally:
        book.close()


def read_month_sales(month, products, sources, temporary=False, through_date=None):
    sales, files = read_sales(month)
    sales = [r for r in sales if through_date is None or r['paid'] <= through_date]
    settled = read_agency_settlement(month, through_date)
    if settled is not None:
        sales = [r for r in sales if r['channel'] not in ('coupang', 'coupang_growth')]
        rows, unmatched, paths = settled
        return sales + rows, unmatched, sorted({r['source'] for r in sales} | set(paths)), False
    unmatched = []
    if temporary:
        if any(r['channel'] in ('coupang', 'coupang_growth') for r in sales):
            raise ValueError('쿠팡 주문자료와 임시집계 중복 위험')
        rows, unmatched, paths = read_coupang_temporary(month, products, sources, through_date)
        sales.extend(rows)
        files.extend(paths)
    return latest_snapshots(sales), unmatched, files, temporary


def select_coupang_snapshot(month, through_date=None, folder=None):
    """Choose one option-detail export by actual as-of date, then export time.

    Dated filenames define their as-of date. Month-only exports use OOXML's
    original creation time (UTC -> KST), capped at month end. File mtime is a
    labelled fallback only when usable creation metadata is absent. Daily store
    totals without option IDs never participate. No cutoff means today (KST).
    """
    cutoff = through_date or today()
    first = date.fromisoformat(month + '-01')
    month_end = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    folder = Path(folder) if folder else ROOT / 'raw_data/sales/coupang'
    candidates = []
    for path in sorted(folder.glob('*.xlsx')):
        dated = re.fullmatch(r'(\d{4}-\d{2}-\d{2})_coupang_sales\.xlsx', path.name)
        monthly = path.name == f'{month}_coupang_sales.xlsx'
        if not monthly and not (dated and dated[1][:7] == month):
            continue
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            if 'vendor item metrics' not in book.sheetnames:
                continue
            headers = next(book['vendor item metrics'].iter_rows(max_row=1, values_only=True), ())
            if not {'옵션 ID', '매출(원)', '판매량'}.issubset(headers):
                continue
            created = book.properties.created
            if created is not None:
                created = (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created).astimezone(KST)
            exported = created if created is not None and created.date() >= first else datetime.fromtimestamp(path.stat().st_mtime, KST)
            basis = 'filename_date' if dated else 'workbook_created_kst' if exported == created else 'file_mtime_kst'
            as_of = date.fromisoformat(dated[1]) if dated else min(exported.date(), month_end)
            if not first <= as_of <= min(cutoff, month_end):
                continue
            # Stable metadata wins a date tie; monthly detail wins an exact tie.
            candidates.append(((as_of, exported.timestamp(), monthly, path.name), path, as_of, basis))
        finally:
            book.close()
    if not candidates:
        return None
    _, path, as_of, basis = max(candidates, key=lambda candidate: candidate[0])
    return path, as_of, basis


def read_coupang_temporary(month, products, sources, through_date=None):
    settled = read_agency_settlement(month, through_date)
    if settled is not None:
        return settled
    whitelist = load_whitelist()
    canonical_codes = {code.upper(): code for code in products}
    result, unmatched, files, seen = [], [], [], set()
    selected = select_coupang_snapshot(month, through_date)
    for path, paid, basis in [selected] if selected else []:
        files.append(str(path))
        for name, values in read_excel_sheets(path):
            if name != 'vendor item metrics':
                continue
            headers = [str(v).strip() for v in values[0]]
            for values_row in values[1:]:
                row = dict(zip(headers, values_row))
                option = normalize_code(row['옵션 ID'])
                if option not in whitelist['coupang']:
                    continue
                key = (paid, option, row['판매방식'])
                if key in seen:
                    raise ValueError(f'쿠팡 임시집계 중복 옵션: {key}')
                seen.add(key)
                code = canonical_codes.get(whitelist['coupang'][option], whitelist['coupang'][option])
                method = '화이트리스트 옵션ID'
                product = products.get(code)
                food = row['카테고리'] == '식품'
                amount, quantity = money(row['매출(원)']), money(row['판매량'])
                if product is None or product['cost'] is None:
                    unmatched.append([option, row['상품명'], row['옵션명'], row['카테고리'],
                                      float(amount), float(quantity), code,
                                      '등록된 자사 상품 원가 누락' if food else '비식품 제외', str(path)])
                if not food:
                    continue
                result.append(dict(channel='coupang_growth' if row['판매방식'] == '로켓그로스' else 'coupang',
                                   order_id='임시집계:' + str(paid), item_id=option, paid=paid,
                                   product_code=code, product_name=row['옵션명'], quantity=quantity,
                                   sales_amount=amount, status=f'임시반영 / {basis} / ' + method,
                                   source=str(path)))
    return result, unmatched, files


def prepare_rows(sales, products, fees):
    detail, issues = [], []
    whitelist = load_whitelist()
    canonical_codes = {code.upper(): code for code in products}
    for row in sales:
        if row['channel'] in ('coupang', 'coupang_growth'):
            mapped = whitelist['coupang'].get(normalize_code(row['item_id']))
            if mapped is None:
                continue
            row = dict(row, product_code=canonical_codes.get(mapped, mapped))
        code = row['product_code'].strip()
        product = products.get(code)
        actual_fee = row.get('actual_fee')
        rate = fee_for(row['channel'], fees) if actual_fee is None else None
        problems = []
        if product is None:
            problems.append('상품코드 미등록')
        elif not product['food']:
            issues.append([row['channel'], row['order_id'], row['item_id'], code, '비식품 제외', row['source']])
            continue
        elif product['cost'] is None:
            problems.append('원가 누락/수식 오류')
        if rate is None and actual_fee is None:
            problems.append('채널 수수료 미등록')
        cost = product['cost'] if product else None
        # Unmapped sales remain visible; totals involving missing costs are unavailable.
        detail.append([row['channel'], row['order_id'], row['item_id'], row['paid'], code,
                       row['product_name'], float(row['quantity']), float(row['sales_amount']),
                       float(cost) if cost is not None else None, None,
                       float(rate) if rate is not None else None, float(actual_fee) if actual_fee is not None else None, None,
                       '; '.join(problems) or '정상', row['status'], row['source']])
        if problems:
            issues.append([row['channel'], row['order_id'], row['item_id'], code, '; '.join(problems), row['source']])
    return detail, issues


def build_workbook(month, detail, issues, sources, files, coverage):
    book = Workbook()
    summary = book.active
    summary.title = '월간손익'
    summary.append([f'{month} 식품부문 결제일 기준 손익 (KRW)'])
    summary.append(['범위', '상품 결제액(배송비 제외) − 기준 원가 − 추정 수수료'])
    summary.append(['결산 상태', '확인필요' if issues or coverage else '집계완료(아래 범위 확인)'])
    summary.append(['유의사항', '부가세 조정·환불·배송손익·인건비·광고비·고정비 미반영: 기여이익 보고서'])
    summary.append(['기준표 적용', '현재 기준표 일괄 적용; 과거 유효기간 및 세트 부자재 누락은 별도 확인'])
    summary.append(['채널', '상품 결제액', '매출원가', '매출총이익', '추정 수수료', '기여이익'])
    sheet = book.create_sheet('매출상세')
    sheet.append(['채널', '주문번호', '품목번호', '결제일', '상품코드', '상품명', '수량', '상품 결제액',
                  '기준 단위원가', '매출원가', '수수료율', '추정 수수료', '기여이익', '매핑상태', '주문상태', '원본파일'])
    for index, values in enumerate(detail, 2):
        sheet.append(values)
        sheet.cell(index, 10, f'=IF(ISNUMBER(I{index}),ROUND(ROUND(G{index}*I{index},6),0),"확인필요")')
        if values[11] is None:
            sheet.cell(index, 12, f'=IF(ISNUMBER(K{index}),ROUND(H{index}*K{index},0),"확인필요")')
        sheet.cell(index, 13, f'=IF(AND(ISNUMBER(J{index}),ISNUMBER(L{index})),H{index}-J{index}-L{index},"확인필요")')
        sheet.cell(index, 4).number_format = 'yyyy-mm-dd'
        sheet.cell(index, 11).number_format = '0.00%'
        for col in (7, 8, 9, 10, 12, 13):
            sheet.cell(index, col).number_format = '#,##0;[Red](#,##0);–'
    end = max(2, len(detail) + 1)
    channels = sorted({r[0] for r in detail})
    for index, channel in enumerate(channels, 7):
        summary.cell(index, 1, channel)
        summary.cell(index, 1).data_type = 's'
        for col, source_col in ((2, 'H'), (3, 'J'), (5, 'L')):
            value = f"SUMIFS('매출상세'!{source_col}$2:{source_col}${end},'매출상세'!A$2:A${end},A{index})"
            if col in (3, 5):
                value = f'IF(COUNTIFS(\'매출상세\'!A$2:A${end},A{index},\'매출상세\'!{source_col}$2:{source_col}${end},"확인필요")>0,"확인필요",{value})'
            summary.cell(index, col, '=' + value)
        summary.cell(index, 4, f'=IF(ISNUMBER(C{index}),B{index}-C{index},"확인필요")')
        summary.cell(index, 6, f'=IF(AND(ISNUMBER(D{index}),ISNUMBER(E{index})),D{index}-E{index},"확인필요")')
    total_row = 7 + len(channels)
    summary.cell(total_row, 1, '전체 합계')
    for col in range(2, 7):
        letter = get_column_letter(col)
        if channels:
            summary.cell(total_row, col, f'=IF(COUNTIF({letter}7:{letter}{total_row-1},"확인필요")>0,"확인필요",SUM({letter}7:{letter}{total_row-1}))')
        else:
            summary.cell(total_row, col, '자료 없음')
    review = book.create_sheet('확인사항')
    review.append(['채널', '주문번호', '품목번호', '상품코드', '확인내용', '원본파일'])
    for issue in issues:
        review.append(issue)
    for warning in coverage:
        review.append(['수집범위', '', '', '', warning, ''])
    review.append(['회계정책', '', '', '', '환불·취소는 최초 결제액에서 자동 차감하지 않음. 월말 별도 반영 필요', ''])
    inputs = book.create_sheet('입력출처')
    inputs.append(['구분', '파일/내용'])
    for source in sources:
        inputs.append(['기준표', source])
    for source in files:
        inputs.append(['매출', source])
    inputs.append(['매핑', '상품data A:F(단품), M:R(세트); 비용data A:B; 90/S9 접두 코드는 비식품 제외'])
    for ws in book:
        ws.freeze_panes = 'B7' if ws == summary else 'A2'
        ws.auto_filter.ref = f'A6:F{total_row}' if ws == summary else ws.dimensions
        header_row = 6 if ws == summary else 1
        for cell in ws[header_row]:
            cell.fill = PatternFill('solid', fgColor='17365D')
            cell.font = Font(name='맑은 고딕', color='FFFFFF', bold=True)
        for row in ws:
            for cell in row:
                if cell.row != header_row:
                    cell.font = Font(name='맑은 고딕', size=10)
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                # Prevent source text from becoming an Excel formula.
                if cell.data_type == 'f' and not (ws == summary or (ws == sheet and cell.column in (10, 12, 13))):
                    cell.data_type = 's'
        for col in range(1, ws.max_column + 1):
            width = 24
            if ws == sheet and col in (6, 14, 16):
                width = 46
            elif ws in (review, inputs) and col == ws.max_column:
                width = 80
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.sheet_view.showGridLines = False
        ws.print_options.horizontalCentered = True
    for index in range(2, 6):
        summary.merge_cells(start_row=index, start_column=2, end_row=index, end_column=6)
        summary.row_dimensions[index].height = 32
    summary['A1'].font = Font(name='맑은 고딕', size=15, bold=True, color='17365D')
    summary.merge_cells('A1:F1')
    for row in summary.iter_rows(min_row=7, max_row=total_row, min_col=2, max_col=6):
        for cell in row:
            cell.number_format = '#,##0;[Red](#,##0);–'
    book.calculation.fullCalcOnLoad = True
    book.calculation.forceFullCalc = True
    book.calculation.calcMode = 'auto'
    return book


def run(month=None, coupang_filename_month=False, through_date=None, output_dir=None):
    month = month or yesterday().strftime('%Y-%m')
    first = date.fromisoformat(month + '-01')
    if first.strftime('%Y-%m') != month:
        raise ValueError('월 형식: YYYY-MM')
    products, fees, sources = load_master()
    sales, unmatched, files, provisional = read_month_sales(
        month, products, sources, coupang_filename_month, through_date)
    detail, issues = prepare_rows(sales, products, fees)
    coverage = []
    last_day = min(date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]), through_date or yesterday())
    for channel in ('cafe24', 'smartstore'):
        missing = [day for day in range(1, last_day.day + 1) if first <= last_day and not
                   (ROOT / 'raw_data' / 'sales' / channel / f'{month}-{day:02d}.csv').exists()]
        if missing:
            coverage.append(f'{channel} 미수집 일자: ' + ','.join(map(str, missing)))
    if not sales:
        coverage.append('해당 월 매출 자료 없음. 0원 결산으로 해석하지 마세요.')
    if provisional:
        coverage.append('임시반영: 일자 파일명/월별 파일 원본 생성시각(KST)으로 기준일 결정, 최신 옵션 상세 1개만 반영. 채널코드맵핑 옵션ID 일치만 허용. 실제 집계기간·묶음수량 확인 필요')
    elif not agency_settlement_path(month) and list((ROOT / 'raw_data' / 'sales' / 'coupang').glob('*.xlsx')):
        coverage.append('쿠팡 상품별 집계는 결제일·집계기간이 없어 월손익 미반영. 쿠팡집계_기간미확인 시트 참조')
    book = build_workbook(month, detail, issues, sources, files, coverage)
    reference_rows = add_coupang_reference(book)
    if agency_settlement_path(month) and not provisional:
        book['월간손익']['A1'] = f'{month} 식품부문 정산서 기준 기여손익 (VAT 포함)'
        book['월간손익']['B2'] = '순매출(판매자쿠폰 차감) − 마스터 원가(묶음수량 반영) − 실제 정산비용'
        book['월간손익']['B4'] = '정산비용: 판매수수료·물류비·대행료·광고비, 재고손실보상 차감. 현재 마스터 원가 적용.'
        book['월간손익']['E6'] = '정산비용/추정 수수료'
        book['월간손익']['B6'] = '순매출액'
        book['매출상세']['D1'] = '정산 귀속월말'
        book['매출상세']['G1'] = '기준상품 환산수량'
        book['매출상세']['H1'] = '순매출액(쿠폰 차감)'
        book['매출상세']['L1'] = '실제 정산비용'
        ws = book.create_sheet('정산비용근거')
        ws.append(['옵션ID', '매핑코드', '항목', '금액(VAT 포함)', '원본파일'])
        for sale in sales:
            for key, value in sale.get('fee_breakdown', {}).items():
                ws.append([sale['item_id'], sale['product_code'], key, float(value), sale['source']])
        ws.freeze_panes = 'A2'
        for col in ('A', 'B', 'C', 'D', 'E'):
            ws.column_dimensions[col].width = 24 if col != 'E' else 85
    if provisional:
        book['월간손익']['A1'] = f'{month} 식품부문 임시 손익 (쿠팡 최신 옵션 상세 기준)'
        book['매출상세']['D1'] = '결제일/임시 기준일'
        book['쿠팡집계_기간미확인']['A1'] = '쿠팡 식품은 파일명 날짜 기준 임시반영. 화이트리스트 자사 상품 집계 참고용이며 이 시트는 별도 합산하지 않음'
        ws = book.create_sheet('원가미매칭_목록')
        ws.append(['옵션ID', '상품명', '옵션명', '카테고리', '매출(원)', '판매량', '매칭코드', '처리상태', '원본파일'])
        for row in unmatched:
            ws.append(row)
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        for col in range(1, 10):
            ws.column_dimensions[get_column_letter(col)].width = 55 if col in (2, 3, 9) else 22
        for cell in ws[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='17365D')
    path = (Path(output_dir) if output_dir else ROOT / 'output_excel') / f'{month}_식품부문_손익결산서.xlsx'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp.xlsx')
    book.save(temp)
    book.close()
    temp.replace(path)
    return dict(output=str(path), rows=len(detail), review=len(issues),
                coverage_warnings=len(coverage), undated_coupang_rows=reference_rows,
                unmatched=len(unmatched), temporary_mode=provisional)


def reporting_months(through_date):
    first = date(2026, 9, 1)
    months = []
    while first <= through_date:
        months.append(first.strftime('%Y-%m'))
        first = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return months


def run_all(through_date=None, coupang_filename_month=False, output_dir=None):
    through_date = through_date or yesterday()
    return [dict(month=month, **run(month, coupang_filename_month, through_date, output_dir))
            for month in reporting_months(through_date)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--month', help='YYYY-MM, 기본값 전일이 속한 월')
    parser.add_argument('--coupang-filename-month', action='store_true', help='쿠팡 파일명 날짜 기준 월 손익 임시 반영')
    args = parser.parse_args()
    print(run(args.month, args.coupang_filename_month) if args.month else
          run_all(coupang_filename_month=args.coupang_filename_month))


if __name__ == '__main__':
    main()
