"""Tag Hana transactions and reconcile tax invoices with deposits using FIFO."""
from __future__ import annotations

import argparse
import calendar
import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal

try:
    from .common import *
except ImportError:
    from common import *

BANK_DATE = ('거래일시', '거래일자', '거래일', '거래일시(일자)')
BANK_AMOUNT = ('입금액', '입금금액', '맡기신금액', '입금')
BANK_NAME = ('의뢰인/수취인', '거래처명', '의뢰인', '보낸분', '보내신분', '적요', '거래내용')
INVOICE_DATE = ('작성일자', '작성일', '발급일자')
INVOICE_NAME = ('공급받는자상호', '공급받는자 상호', '공급받는자 상호(법인명)')
INVOICE_AMOUNT = ('합계금액', '합계', '총금액')


def load_rules():
    rules = read_json(ROOT / 'config' / 'food_filter_rules.json')
    for key in ('include_keywords', 'exclude_keywords'):
        if not isinstance(rules.get(key), list) or any(not isinstance(x, str) or not x.strip() for x in rules[key]):
            raise ValueError(f'{key}: 비어 있지 않은 문자열 배열이 필요합니다')
    terms = rules.get('b2b_payment_terms_days', {})
    categories = rules.get('category_by_keyword', {})
    if (not isinstance(categories, dict) or set(categories) != set(rules['include_keywords'])
            or any(not isinstance(v, str) or not v.strip() for v in categories.values())):
        raise ValueError('category_by_keyword: 모든 포함 키워드에 세부 카테고리가 필요합니다')
    if '기본거래처' not in terms or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in terms.values()):
        raise ValueError('b2b_payment_terms_days: 기본거래처 및 0 이상의 정수 필요')
    return rules


def classify(text, rules):
    text = str(text).casefold()
    include = [k for k in rules['include_keywords'] if k.casefold() in text]
    exclude = [k for k in rules['exclude_keywords'] if k.casefold() in text]
    if include and exclude:
        return '미분류(확인필요)', '포함·제외 충돌: ' + ', '.join(include + exclude)
    if exclude:
        return '식품부문 제외', ', '.join(exclude)
    if include:
        return '식품부문 포함', ', '.join(include)
    return '미분류(확인필요)', '키워드 미일치'


def bank_category(text, status, rules):
    if status != '식품부문 포함':
        return ''
    matches = [category for keyword, category in rules['category_by_keyword'].items()
               if keyword.casefold() in text.casefold()]
    return '; '.join(dict.fromkeys(matches))


def apply_one_off_override(record, rules):
    """Match only reviewed bank transactions, never future counterparty activity."""
    matches = [rule for rule in rules.get('one_off_overrides', [])
               if all(str(record.get(key, '')) == str(rule[key])
                      for key in ('transaction_id', 'transaction_date', 'customer'))
               and all(money(record[key]) == money(rule[key])
                       for key in ('deposit_amount', 'withdrawal_amount'))]
    if len(matches) > 1:
        raise ValueError('일회성 거래 예외 중복: ' + record['transaction_id'])
    if matches:
        rule = matches[0]
        record.update(food_status=rule['food_status'], category=rule['category'],
                      reason='사용자 검수 일회성 예외: ' + rule['note'])
    return bool(matches)


def party_key(name, rules):
    # Exact normalized names only: no substring matching of unrelated customers.
    normalize = lambda value: re.sub(r'\s+|주식회사|\(주\)|㈜', '', str(value)).casefold()
    clean = normalize(name)
    aliases = {normalize(k): normalize(v)
               for k, v in rules.get('customer_aliases', {}).items()}
    return aliases.get(clean, clean)


def read_bank(folder, rules):
    records, seen = [], set()
    for row in excel_tables(folder, [BANK_DATE, BANK_AMOUNT, BANK_NAME]):
        raw_date = pick(row, *BANK_DATE)
        if raw_date is None:
            # Ignore only explicitly labelled footer rows.
            if any(str(v).strip() in ('합계', '총합계', '잔액') for v in row.values()):
                continue
            raise ValueError(f'은행 거래일 누락: {row["_source"]}')
        tx_date = as_date(raw_date)
        amount = money(pick(row, *BANK_AMOUNT, default=0))
        name = str(pick(row, *BANK_NAME, default='')).strip()
        # Only party/description fields determine the business classification.
        text = ' '.join(str(row[k]) for k in dict.fromkeys(BANK_NAME + ('적요',))
                        if row.get(k) is not None)
        status, reason = classify(text, rules)
        # Fingerprint overlap exports. Without balance/time/transaction ID, identical
        # same-day deposits cannot be distinguished: retain a review flag.
        explicit = pick(row, '거래번호', '거래고유번호')
        identity = str(explicit) if explicit else json.dumps(
            {k: str(v) for k, v in row.items() if not k.startswith('_')}, sort_keys=True, ensure_ascii=False)
        uid = hashlib.sha256(identity.encode()).hexdigest()[:24]
        if uid in seen:
            continue
        seen.add(uid)
        records.append(dict(transaction_id=uid, transaction_date=str(tx_date),
                            transaction_datetime=str(raw_date), transaction_type=str(pick(row, '구분', default='')),
                            customer=name, deposit_amount=str(amount), food_status=status,
                            category=bank_category(text, status, rules),
                            withdrawal_amount=str(money(pick(row, '출금', '출금액', default=0))),
                            description=str(pick(row, '적요', default='')),
                            reason=reason, source=row['_source'],
                            identity_review='' if explicit else '거래번호 없음: 동일 거래 중복 제거 기준 확인 필요'))
    fee_rule = rules.get('remittance_fee_rule', {})
    suppliers = [r for r in records if any(k.casefold() in (r['customer'] + ' ' + r['description']).casefold()
                 for k in fee_rule.get('supplier_keywords', [])) and money(r['withdrawal_amount']) > 0]
    for record in records:
        # Reviewed exceptions take precedence over the generic remittance fee rule.
        if apply_one_off_override(record, rules):
            continue
        text = record['customer'] + ' ' + record['description']
        if not fee_rule.get('keyword') or fee_rule['keyword'] not in text:
            continue
        refs = set(re.findall(r'OTT\d+', text, re.I))
        matched = [r for r in suppliers if refs.intersection(re.findall(r'OTT\d+', r['customer'] + ' ' + r['description'], re.I))
                   or r['transaction_datetime'] == record['transaction_datetime']]
        if matched or fee_rule.get('include_without_match', False):
            record.update(food_status='식품부문 포함', category=fee_rule['category'],
                          reason='식품 수입송금 참조번호/거래일시 일치' if matched else '사용자 지정 기본 식품 해외송금 수수료')
    return records


def end_of_next_month(issued):
    year, month = (issued.year + 1, 1) if issued.month == 12 else (issued.year, issued.month + 1)
    return date(year, month, calendar.monthrange(year, month)[1])


def read_purchase_invoices(folder):
    result, seen = [], {}
    for row in excel_tables(folder, [INVOICE_DATE, ('상호',) + INVOICE_NAME, INVOICE_AMOUNT]):
        buyer = pick(row, *INVOICE_NAME)
        if party_key(buyer, {}) != '오키드인더스트리':
            continue
        supplier = pick(row, '공급자상호', '공급자 상호')
        uid = str(pick(row, '승인번호', default='')).strip()
        if not supplier or not uid:
            raise ValueError(f'매입 공급자/승인번호 누락: {row["_source"]}')
        record = dict(invoice_id=uid, customer=str(supplier).strip(),
                      issue_date=str(as_date(pick(row, *INVOICE_DATE))),
                      total_amount=str(money(pick(row, *INVOICE_AMOUNT))), source=row['_source'])
        comparable = {k: v for k, v in record.items() if k != 'source'}
        if uid in seen:
            if seen[uid] != comparable:
                raise ValueError(f'매입 승인번호 충돌: {uid}')
            continue
        seen[uid] = comparable
        result.append(record)
    return result


def food_settlements(bank, invoices, purchases, rules, as_of):
    """Aggregate netted customers by attribution month; reserve known prior receipts."""
    output, used = [], set()
    for customer, policy in rules.get('settlement_rules', {}).items():
        same_party = lambda name: party_key(name, rules) == party_key(customer, rules)
        sales = [r for r in invoices if same_party(r['customer']) and as_date(r['issue_date']) <= as_of]
        buys = [r for r in purchases if same_party(r['customer']) and as_date(r['issue_date']) <= as_of]
        months = sorted({r['issue_date'][:7] for r in sales + buys})
        for override in rules.get('deposit_period_overrides', []):
            if not same_party(override['customer']):
                continue
            deposits = [r for r in bank if same_party(r['customer']) and r['transaction_date'] == override['transaction_date']
                        and as_date(r['transaction_date']) <= as_of and money(r['deposit_amount']) == money(override['amount'])]
            for deposit in deposits:
                used.add(deposit['transaction_id'])
            if deposits and override['attribution_month'] not in months:
                output.append(dict(issue_date=override['invoice_date'], customer=customer,
                                   sales_amount='', purchase_offset='', net_amount='',
                                   due_date=str(end_of_next_month(as_date(override['invoice_date']))),
                                   paid_amount=str(sum((money(r['deposit_amount']) for r in deposits), Decimal(0))),
                                   status='9월분 정산 입금완료(지연)',
                                   note='사용자 확인 입금 귀속; 9월 계산서 미조회, 매출·순액 추정 안 함'))
        pool = [dict(r, remaining=money(r['deposit_amount'])) for r in bank
                if same_party(r['customer']) and r['transaction_id'] not in used
                and as_date(r['transaction_date']) <= as_of and money(r['deposit_amount']) > 0]
        pool.sort(key=lambda r: (r['transaction_date'], r['transaction_id']))
        for month in months:
            month_sales = [r for r in sales if r['issue_date'][:7] == month]
            month_buys = [r for r in buys if r['issue_date'][:7] == month]
            gross = sum((money(r['total_amount']) for r in month_sales), Decimal(0))
            offset = sum((money(r['total_amount']) for r in month_buys), Decimal(0)) if month >= policy['netting_start_month'] else Decimal(0)
            target = gross - offset
            issued = max((as_date(r['issue_date']) for r in month_sales + month_buys))
            due = end_of_next_month(issued)
            paid = Decimal(0)
            if target > 0 and month_sales:
                for override in rules.get('deposit_period_overrides', []):
                    if same_party(override['customer']) and override['attribution_month'] == month:
                        paid += sum((money(r['deposit_amount']) for r in bank if r['transaction_id'] in used
                                    and r['transaction_date'] == override['transaction_date']
                                    and money(r['deposit_amount']) == money(override['amount'])), Decimal(0))
                for deposit in pool:
                    if deposit['transaction_date'] >= str(issued) and paid < target:
                        allocated = min(target - paid, deposit['remaining'])
                        paid += allocated
                        deposit['remaining'] -= allocated
            state = ('확인필요(매출계산서 없음)' if not month_sales else '확인필요(순액 음수)' if target < 0
                     else '상계완료' if target == 0 else '입금완료' if paid >= target
                     else '일부입금' if paid else '기한미도래' if due >= as_of else '연체(대조상 미수)')
            output.append(dict(issue_date=str(issued), customer=customer, sales_amount=str(gross),
                               purchase_offset=str(offset), net_amount=str(target), due_date=str(due),
                               paid_amount=str(paid), status=state,
                               note=f'귀속월 {month}; 매출 {len(month_sales)}건/매입 {len(month_buys)}건; 합계금액(VAT 포함) 기준'))
    normal = [r for r in invoices if classify(r['customer'], rules)[0] == '식품부문 포함'
              and not any(party_key(r['customer'], rules) == party_key(c, rules) for c in rules.get('settlement_rules', {}))]
    for row in reconcile(bank, normal, rules, as_of):
        output.append(dict(issue_date=row['issue_date'], customer=row['customer'], sales_amount=row['total_amount'],
                           purchase_offset='0', net_amount=row['total_amount'], due_date=row['due_date'],
                           paid_amount=row['paid_amount'], status=row['status'], note=row['source']))
    return output


def read_invoices(folder):
    invoices, seen = [], {}
    for row in excel_tables(folder, [INVOICE_DATE, ('상호',) + INVOICE_NAME, INVOICE_AMOUNT]):
        if '매입 전자' in row.get('_context', ''):
            continue
        if pick(row, *INVOICE_DATE) is None and any(str(v).strip() in ('합계', '총합계') for v in row.values()):
            continue
        supplier = pick(row, '공급자상호', '공급자 상호')
        if supplier is None:
            raise ValueError(f'공급자 상호 컬럼 누락: {row["_source"]}')
        normalize = lambda value: re.sub(r'\s+|주식회사|\(주\)|㈜', '', str(value))
        if normalize(supplier) != '오키드인더스트리':
            continue
        customer = pick(row, *INVOICE_NAME)
        if not customer:
            raise ValueError(f'공급받는자 상호 누락: {row["_source"]}')
        uid = str(pick(row, '승인번호', '전자세금계산서 승인번호', default='')).strip()
        if not uid:
            raise ValueError(f'세금계산서 승인번호 누락: {row["_source"]}')
        record = dict(invoice_id=uid, issue_date=str(as_date(pick(row, *INVOICE_DATE))),
                      customer=str(customer).strip(),
                      total_amount=str(money(pick(row, *INVOICE_AMOUNT))), source=row['_source'],
                      explicit_due=pick(row, '약정결제일', '결제예정일', '지급기일'))
        comparable = {k: v for k, v in record.items() if k != 'source'}
        if uid in seen:
            if seen[uid] != comparable:
                raise ValueError(f'동일 승인번호 금액·거래처 충돌: {uid}')
            continue
        seen[uid] = comparable
        invoices.append(record)
    return invoices


def reconcile(bank, invoices, rules, as_of, all_invoices=False):
    """FIFO allocation, partial/grouped payments. Unknown/excluded deposits stay out.

    Requires complete open-invoice and bank history; advances before invoice date are
    left for review, rather than silently paying a later invoice.
    """
    pool = []
    for deposit in bank:
        if any(party_key(deposit['customer'], rules) == party_key(o['customer'], rules)
               and deposit['transaction_date'] == o['transaction_date']
               and money(deposit['deposit_amount']) == money(o['amount'])
               for o in rules.get('deposit_period_overrides', [])
               if not any(i['issue_date'][:7] == o['attribution_month'] and
                          party_key(i['customer'], rules) == party_key(o['customer'], rules) for i in invoices)):
            continue
        if (all_invoices or deposit['food_status'] == '식품부문 포함') and as_date(deposit['transaction_date']) <= as_of:
            amount = money(deposit['deposit_amount'])
            if amount > 0:
                pool.append(dict(deposit, remaining=amount))
    pool.sort(key=lambda d: (d['transaction_date'], d['transaction_id']))
    output = []
    for inv in sorted(invoices, key=lambda r: (r['issue_date'], r['invoice_id'])):
        issued = as_date(inv['issue_date'])
        if issued > as_of:
            continue
        amount = money(inv['total_amount'])
        status, reason = classify(inv['customer'], rules)
        terms = rules['b2b_payment_terms_days']
        days = terms.get(inv['customer'], terms['기본거래처'])
        due = as_date(inv['explicit_due']) if inv.get('explicit_due') else issued + timedelta(days=days)
        if any(party_key(inv['customer'], rules) == party_key(c, rules) for c in rules.get('settlement_rules', {})):
            due = end_of_next_month(issued)
        remaining, matches, matched_dates = amount, [], []
        if (all_invoices or status == '식품부문 포함') and amount > 0:
            for deposit in pool:
                if (party_key(deposit['customer'], rules) == party_key(inv['customer'], rules)
                        and as_date(deposit['transaction_date']) >= issued and deposit['remaining'] > 0):
                    used = min(remaining, deposit['remaining'])
                    deposit['remaining'] -= used
                    remaining -= used
                    matches.append(f'{deposit["transaction_id"]}:{used}')
                    matched_dates.append(f'{deposit["transaction_date"]} ({used:,.0f}원)')
                    if remaining == 0:
                        break
        state = ('확인필요(수정·음수 계산서)' if all_invoices and amount <= 0 else
                 '입금완료' if all_invoices and remaining == 0 else
                 '일부입금' if all_invoices and remaining < amount else
                 '미매칭(확인필요)' if all_invoices else
                 '제외' if status == '식품부문 제외' else '확인필요' if status != '식품부문 포함' or amount <= 0
                 else '입금완료' if remaining == 0 else '연체(대조상 미수)' if due < as_of else '기한미도래')
        output.append(dict(invoice_id=inv['invoice_id'], customer=inv['customer'], issue_date=str(issued),
                           due_date=str(due), total_amount=str(amount), paid_amount=str(amount - remaining),
                           outstanding_amount=str(remaining), status=state, matched_transactions=';'.join(matches),
                           matched_deposit_dates='; '.join(matched_dates),
                           food_status=status, reason=reason, source=inv['source']))
    return output


def send_telegram_alert(overdue, as_of):
    """Send only when explicitly called; deduplicate successful daily alerts."""
    if not overdue:
        return 0
    import requests
    token, chat = require_env('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')
    state_path = ROOT / 'output_excel' / 'telegram_alert_state.json'
    state = read_json(state_path) if state_path.exists() else {}
    sent = 0
    with requests.Session() as session:
        for row in overdue:
            key = hashlib.sha256(f'{chat}|{as_of}|{row["invoice_id"]}|{row["outstanding_amount"]}'.encode()).hexdigest()
            if key in state:
                continue
            message = (f'[식품부문 대조상 미수 확인] 기준일 {as_of}\n거래처: {row["customer"]}\n'
                       f'승인번호: {row["invoice_id"]}\n약정일: {row["due_date"]}\n'
                       f'미수잔액: {row["outstanding_amount"]}원\n'
                       '선수금·수정계산서·자료 누락 여부 확인 필요')
            # No automatic retry of message POST: timeout may follow a delivered message.
            try:
                response = session.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                        json={'chat_id': chat, 'text': message}, timeout=(10, 30))
            except requests.RequestException:
                raise RuntimeError('텔레그램 전송 결과 불명: 중복 전송 방지를 위해 수동 확인 필요') from None
            if not response.ok or not response.json().get('ok'):
                raise RuntimeError('텔레그램 알림 전송 실패')
            state[key] = str(as_of)
            write_json(state_path, state)
            sent += 1
    return sent


def summarize_unclassified(bank, as_of=None, start=date(2026, 9, 1)):
    """Keep original counterparty text; aggregate duplicates without classifying."""
    as_of = as_of or today()
    grouped = {}
    for row in bank:
        if row['food_status'] != '미분류(확인필요)' or not start <= as_date(row['transaction_date']) <= as_of:
            continue
        name = row['customer']
        item = grouped.setdefault(name, dict(customer=name, count=0, deposits=Decimal(0), withdrawals=Decimal(0)))
        item['count'] += 1
        item['deposits'] += money(row['deposit_amount'])
        item['withdrawals'] += money(row['withdrawal_amount'])
    return [grouped[name] for name in sorted(grouped)]


def print_unclassified_summary(bank, as_of=None):
    rows = summarize_unclassified(bank, as_of)
    count = sum(r['count'] for r in rows)
    deposits = sum((r['deposits'] for r in rows), Decimal(0))
    withdrawals = sum((r['withdrawals'] for r in rows), Decimal(0))
    print(f'\n9월 1일 이후 은행 미분류: {count}건 / 거래처 {len(rows)}곳 (포함·제외 판단 전 원문 유지)')
    print('| 번호 | 의뢰인/수취인 | 건수 | 입금 합계 | 출금 합계 |')
    print('|---:|---|---:|---:|---:|')
    for index, row in enumerate(rows, 1):
        print(f"| {index} | {row['customer']} | {row['count']} | {row['deposits']:,.0f} | {row['withdrawals']:,.0f} |")
    print(f'| 합계 | {len(rows)}곳 | {count} | {deposits:,.0f} | {withdrawals:,.0f} |')
    return rows


def run(as_of=None, notify=False, bank_file=None, all_invoices=False, summary_only=False, output_dir=None):
    if notify:
        load_env()
    as_of = as_of or today()
    rules = load_rules()
    bank_path = Path(bank_file) if bank_file else ROOT / 'raw_data' / 'bank'
    if bank_file and not bank_path.is_file():
        raise FileNotFoundError(bank_path)
    print(f'은행 입력: {bank_path}')
    bank = read_bank(bank_path, rules)
    print('| 거래일자 | 의뢰인·수취인 (적요) | 입금액 | 출금액 | 분류상태 | category |')
    print('|---|---|---:|---:|---|---|')
    for row in ([] if summary_only else bank):
        print(f'| {row["transaction_date"]} | {row["customer"]} ({row["description"]}) | '
              f'{money(row["deposit_amount"]):,.0f} | {money(row["withdrawal_amount"]):,.0f} | {row["food_status"]} | {row["category"]} |')
    print('\n| 은행 분류 | 건수 | 입금 합계 | 출금 합계 |')
    print('|---|---:|---:|---:|')
    for status in ('식품부문 포함', '식품부문 제외', '미분류(확인필요)'):
        group = [r for r in bank if r['food_status'] == status]
        print(f'| {status} | {len(group)} | {sum((money(r["deposit_amount"]) for r in group), Decimal(0)):,.0f} | '
              f'{sum((money(r["withdrawal_amount"]) for r in group), Decimal(0)):,.0f} |')
    invoices = read_invoices(ROOT / 'raw_data' / 'hometax')
    purchases = read_purchase_invoices(ROOT / 'raw_data' / 'hometax')
    result = reconcile(bank, invoices, rules, as_of, all_invoices)
    food_result = food_settlements(bank, invoices, purchases, rules, as_of)
    print('\n식품부문 전용 대조표 (합계금액/VAT 포함, —는 원본 계산서 미조회)')
    print('| 작성일자 | 거래처 | 매출액 | 매입상계액 | 순입금예정액 | 약정입금기한 | 실제입금액 | 대조상태 |')
    print('|---|---|---:|---:|---:|---|---:|---|')
    for row in food_result:
        fmt = lambda key: f'{money(row[key]):,.0f}' if row[key] != '' else '—'
        print(f'| {row["issue_date"]} | {row["customer"]} | {fmt("sales_amount")} | {fmt("purchase_offset")} | '
              f'{fmt("net_amount")} | {row["due_date"]} | {fmt("paid_amount")} | {row["status"]} |')
        print(f'  {row["note"]}')
    print('\n1529800 계좌 원본 상세 (계좌별 중복 제거 없이 거래행 전체):')
    print('| 거래일시 | 적요 | 의뢰인·수취인 | 입금액 | 출금액 | 구분 |')
    print('|---|---|---|---:|---:|---|')
    for row in bank:
        if row['customer'].startswith('1529800'):
            print(f'| {row["transaction_datetime"]} | {row["description"]} | {row["customer"]} | '
                  f'{money(row["deposit_amount"]):,.0f} | {money(row["withdrawal_amount"]):,.0f} | {row["transaction_type"]} |')
    print_unclassified_summary(bank, as_of)
    overdue = [r for r in result if r['status'] == '연체(대조상 미수)']
    if notify and invoices and not bank:
        raise ValueError('은행 거래 자료가 없어 연체 알림을 보낼 수 없습니다')
    folder = Path(output_dir) if output_dir else ROOT / 'output_excel' / 'reconciliation'
    write_csv(folder / f'{as_of}_식품부문대조.csv', food_result,
              ['issue_date', 'customer', 'sales_amount', 'purchase_offset', 'net_amount', 'due_date', 'paid_amount', 'status', 'note'])
    write_csv(folder / f'{as_of}_은행분류.csv', bank,
              ['transaction_id', 'transaction_date', 'transaction_datetime', 'transaction_type', 'customer', 'description', 'deposit_amount', 'withdrawal_amount', 'food_status', 'category', 'reason', 'source', 'identity_review'])
    fields = ['invoice_id', 'customer', 'issue_date', 'due_date', 'total_amount', 'paid_amount',
              'outstanding_amount', 'status', 'matched_transactions', 'matched_deposit_dates', 'food_status', 'reason', 'source']
    write_csv(folder / f'{as_of}_세금계산서대조.csv', result, fields)
    write_csv(folder / f'{as_of}_연체미수.csv', overdue, fields)
    return dict(bank=len(bank), invoices=len(invoices), overdue=len(overdue),
                alerts=send_telegram_alert(overdue, as_of) if notify else 0,
                warning='은행 조회기간과 계산서 기간이 다르며 수정계산서·거래처 표기 차이가 있어 미수 확정 전 확인 필요')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', type=date.fromisoformat, default=today())
    parser.add_argument('--notify', action='store_true', help='텔레그램 알림 실제 전송')
    parser.add_argument('--bank-file', type=Path, help='대조할 은행 파일 지정. 생략하면 은행 폴더 전체')
    parser.add_argument('--all-invoices', action='store_true', help='식품 분류와 별도로 전체 거래처 입금 대조')
    parser.add_argument('--summary-only', action='store_true', help='은행 개별 내역 대신 요약 출력')
    parser.add_argument('--unclassified-only', action='store_true', help='9월 이후 미분류 거래처별 입출금만 출력(파일 쓰기 없음)')
    args = parser.parse_args()
    if args.unclassified_only:
        print_unclassified_summary(read_bank(args.bank_file or ROOT / 'raw_data/bank', load_rules()), args.as_of)
    else:
        print(run(args.as_of, args.notify, args.bank_file, args.all_invoices, args.summary_only))


if __name__ == '__main__':
    main()
