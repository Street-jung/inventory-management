"""Validation and local ledger operations; no accounting or cash-flow promotion."""
import copy
import hashlib
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
try:
    from .manual_sales_store import ManualSalesError, encoded
except ImportError:
    from manual_sales_store import ManualSalesError, encoded

MAX_MONEY = 10**12


def invalid(field, message='입력 형식이나 허용 범위를 확인하세요.', code='VALIDATION_ERROR'):
    raise ManualSalesError(422, code, message, field)


def obj(value, allowed, field):
    if not isinstance(value, dict) or any(k not in allowed for k in value):
        invalid(field, '허용되지 않은 필드 또는 객체 형식입니다.')
    return value


def text(value, field, maximum, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()) or '\x00' in value:
        invalid(field)
    return value.strip() or None


def money(value, field):
    if type(value) is not int or not 0 <= value <= MAX_MONEY:
        invalid(field, '금액은 허용 범위의 0 이상 정수원이어야 합니다.')
    return value


def integer(value, field, minimum=1, maximum=2**31-1):
    if type(value) is not int or not minimum <= value <= maximum:
        invalid(field)
    return value


def calendar(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        invalid(field)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        invalid(field, '실제 달력 날짜를 입력하세요.')
    if not 1900 <= parsed.year <= 2100:
        invalid(field)
    return parsed.isoformat()


def uuid_value(value, field):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            invalid(field)
    except (ValueError, AttributeError):
        invalid(field)
    return value


def idem_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', value):
        invalid('Idempotency-Key', '재시도 키를 확인하세요.', 'INVALID_IDEMPOTENCY_KEY')
    return value


def quantity(value, field):
    if type(value) not in (str, int) or isinstance(value, bool) or len(str(value)) > 32:
        invalid(field)
    token = str(value)
    if not re.fullmatch(r'\d+(?:\.\d{1,3})?', token):
        invalid(field, '수량은 소수 3자리 이내 십진수로 입력하세요.')
    try:
        amount = Decimal(token)
    except InvalidOperation:
        invalid(field)
    if not amount.is_finite() or not 0 < amount <= 1000000:
        invalid(field)
    return format(amount, 'f').rstrip('0').rstrip('.') if '.' in token else str(int(amount))


def normalize(payload, operation):
    fields = {'sale_date','counterparty','lines','order_discount','shipping_amount','vat_status','evidence_refs','memo','client_totals'}
    if operation == 'update':
        fields |= {'expected_revision','reason'}
    obj(payload, fields, 'body')
    party = obj(payload.get('counterparty'), {'id','name'}, 'counterparty')
    result = dict(sale_date=calendar(payload.get('sale_date'), 'sale_date'),
                  counterparty=dict(id=text(party.get('id'), 'counterparty.id', 100), name=text(party.get('name'), 'counterparty.name', 200, True)))
    lines = payload.get('lines')
    if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
        invalid('lines')
    prepared, line_ids, online_ids = [], set(), set()
    for index, line in enumerate(lines):
        prefix = f'lines.{index}'
        allowed = {'product_code','product_name','variant_key','quantity','unit','amount_mode','goods_amount','gross_amount','unit_price','item_discount_amount','online_link'}
        if operation == 'update':
            allowed.add('line_id')
        obj(line, allowed, prefix)
        code = text(line.get('product_code'), prefix+'.product_code', 80)
        name = text(line.get('product_name'), prefix+'.product_name', 200)
        if code is None and name is None:
            invalid(prefix+'.product_name', '품목코드 또는 품목명을 입력하세요.')
        qty = quantity(line.get('quantity'), prefix+'.quantity')
        unit = line.get('unit')
        if unit not in ('ea','box','set','kg','l'):
            invalid(prefix+'.unit')
        mode = line.get('amount_mode')
        discount = money(line.get('item_discount_amount', 0), prefix+'.item_discount_amount')
        price = money(line['unit_price'], prefix+'.unit_price') if 'unit_price' in line else None
        if mode == 'final':
            if 'gross_amount' in line:
                invalid(prefix+'.gross_amount', '최종금액 방식에서는 총상품가를 함께 입력하지 않습니다.')
            goods = money(line.get('goods_amount'), prefix+'.goods_amount'); gross = None
        elif mode == 'gross_discount':
            if 'goods_amount' in line:
                invalid(prefix+'.goods_amount', '총상품가 방식에서는 최종금액을 직접 입력하지 않습니다.')
            gross = money(line.get('gross_amount'), prefix+'.gross_amount')
            if discount > gross:
                invalid(prefix+'.item_discount_amount')
            if price is not None and Decimal(qty)*price != gross:
                invalid(prefix+'.gross_amount', '수량과 단가의 곱이 총상품가와 다릅니다.')
            goods = gross-discount
        else:
            invalid(prefix+'.amount_mode')
        line_id = uuid_value(line['line_id'], prefix+'.line_id') if line.get('line_id') is not None else None
        if line_id is not None:
            if line_id in line_ids:
                invalid(prefix+'.line_id', '같은 품목 ID를 반복할 수 없습니다.')
            line_ids.add(line_id)
        link = line.get('online_link')
        if link is not None:
            obj(link, {'channel','order_key','item_key'}, prefix+'.online_link')
            channel = link.get('channel')
            if channel not in ('smartstore','cafe24','coupang','coupang_growth'):
                invalid(prefix+'.online_link.channel')
            link = dict(channel=channel, order_key=text(link.get('order_key'), prefix+'.online_link.order_key', 160, True),
                        item_key=text(link.get('item_key'), prefix+'.online_link.item_key', 160, True))
            identity = tuple(link.values())
            if identity in online_ids:
                invalid(prefix+'.online_link', '같은 온라인 품목을 반복 연결할 수 없습니다.')
            online_ids.add(identity)
        prepared.append(dict(line_id=line_id, product_code=code, product_name=name, variant_key=text(line.get('variant_key'), prefix+'.variant_key', 100),
                             quantity=qty, unit=unit, amount_mode=mode, unit_price=price, gross_amount=gross,
                             item_discount_amount=discount, goods_amount=goods, online_link=link))
    subtotal = money(sum(r['goods_amount'] for r in prepared), 'totals.goods_amount')
    discount = obj(payload.get('order_discount', {}), {'amount','allocation_status','allocations'}, 'order_discount')
    amount = money(discount.get('amount', 0), 'order_discount.amount')
    if amount > subtotal:
        invalid('order_discount.amount')
    status = discount.get('allocation_status', 'unallocated') if amount else 'not_applicable'
    allocations = discount.get('allocations', [])
    if not isinstance(allocations, list) or len(allocations) > len(prepared) or status not in ('unallocated','allocated','not_applicable'):
        invalid('order_discount')
    mapped = {}
    for entry in allocations:
        obj(entry, {'line_index','amount'}, 'order_discount.allocations')
        index = integer(entry.get('line_index'), 'order_discount.line_index', 0, len(prepared)-1)
        value = money(entry.get('amount'), 'order_discount.allocation.amount')
        if index in mapped or value > prepared[index]['goods_amount']:
            invalid('order_discount.allocations')
        mapped[index] = value
    if (status == 'allocated' and sum(mapped.values()) != amount) or (status != 'allocated' and mapped) or (amount and status == 'not_applicable'):
        invalid('order_discount.allocations', '주문할인 배분 상태와 합계가 일치하지 않습니다.')
    for index, line in enumerate(prepared):
        allocated = mapped.get(index, 0) if status != 'unallocated' else None
        line['order_discount_allocation_amount'] = allocated
        line['goods_after_order_discount'] = line['goods_amount']-allocated if allocated is not None else None
    shipping = money(payload.get('shipping_amount', 0), 'shipping_amount')
    totals = dict(goods_amount=subtotal, order_discount_amount=amount, goods_after_order_discount=subtotal-amount,
                  shipping_amount=shipping, transaction_amount=money(subtotal-amount+shipping, 'totals.transaction_amount'))
    client = obj(payload.get('client_totals', {}), set(totals), 'client_totals')
    for key, value in client.items():
        if money(value, 'client_totals.'+key) != totals[key]:
            invalid('client_totals.'+key, '화면 합계와 서버 계산이 일치하지 않습니다.', 'TOTAL_MISMATCH')
    vat = payload.get('vat_status', 'unverified')
    if vat not in ('unverified','included','excluded','exempt'):
        invalid('vat_status')
    refs = payload.get('evidence_refs', [])
    if not isinstance(refs, list) or len(refs) > 10:
        invalid('evidence_refs')
    clean_refs = []
    for entry in refs:
        obj(entry, {'type','reference'}, 'evidence_refs')
        if entry.get('type') not in ('order','invoice','delivery','receipt','other'):
            invalid('evidence_refs.type')
        clean_refs.append(dict(type=entry['type'], reference=text(entry.get('reference'), 'evidence_refs.reference', 500, True)))
    result.update(lines=prepared, order_discount=dict(amount=amount, allocation_status=status,
                  allocations=[dict(line_index=k, amount=v) for k,v in sorted(mapped.items())]),
                  shipping_amount=shipping, totals=totals, vat_status=vat, vat_amount=None,
                  evidence_refs=clean_refs, memo=text(payload.get('memo'), 'memo', 2000))
    return result


class ManualSalesService:
    def __init__(self, store, product_provider=None):
        self.store, self.product_provider = store, product_provider

    def catalog(self):
        if self.product_provider is None:
            return {}
        try:
            catalog = self.product_provider()
            if not isinstance(catalog, dict) or any(not isinstance(k,str) or not k.strip() or len(k)>80 or not isinstance(v,str) or not v.strip() or len(v)>200 for k,v in catalog.items()):
                raise ValueError('catalog')
            return catalog
        except Exception:
            raise ManualSalesError(503, 'CATALOG_UNAVAILABLE', '상품 조회목록을 사용할 수 없습니다.') from None

    def enrich(self, document):
        catalog = self.catalog()
        for line in document['lines']:
            if line['product_code'] in catalog:
                line['product_name'] = catalog[line['product_code']]
                line['product_identity_status'] = 'catalog_match'
            else:
                if not line['product_name']:
                    invalid('lines.product_name', '미등록 품목은 확인할 품목명을 함께 입력하세요.')
                line['product_identity_status'] = 'unreviewed_entry'
        return document

    def command(self, operation, payload, request_key, actor, sale_id=None):
        request_key = idem_key(request_key)
        if sale_id is not None:
            uuid_value(sale_id, 'sale_id')
        if operation == 'cancel':
            obj(payload, {'expected_revision','reason'}, 'body')
            document = None
        else:
            document = normalize(payload, operation)
        revision = integer(payload.get('expected_revision'), 'expected_revision') if operation != 'create' else None
        reason = text(payload.get('reason'), 'reason', 2000, True) if operation != 'create' else None
        canonical = dict(operation=operation, sale_id=sale_id, document=document, expected_revision=revision, reason=reason)
        request_hash = hashlib.sha256(encoded(canonical).encode('utf-8')).hexdigest()
        return self.store.mutate(operation, request_key, request_hash, actor, document=document, sale_id=sale_id,
                                 expected_revision=revision, reason=reason, enrich=self.enrich)
