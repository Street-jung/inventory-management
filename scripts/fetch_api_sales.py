"""Collect payment-date sales. Raw API pages are retained beside normalized CSVs."""
from __future__ import annotations

import argparse
import base64
import os
import re
import time
import logging
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal


try:
    from .common import *
except ImportError:
    from common import *

LOG = logging.getLogger('fetch_api_sales')
TOKEN_CACHE = ROOT / 'scripts' / '.cafe24_tokens.json'


def cafe24_tokens(mall):
    tokens = {'access_token': os.getenv('CAFE24_ACCESS_TOKEN', ''),
              'refresh_token': os.getenv('CAFE24_REFRESH_TOKEN', '')}
    if TOKEN_CACHE.exists():
        cached = read_json(TOKEN_CACHE)
        if cached.get('mall_id') == mall and cached.get('client_id') == os.getenv('CAFE24_CLIENT_ID', ''):
            tokens.update({k: cached[k] for k in tokens if cached.get(k)})
    return tokens


def refresh_cafe24(session, mall, tokens):
    if not tokens.get('refresh_token'):
        raise RuntimeError('카페24 HTTP 401: Access Token 인증 실패, CAFE24_REFRESH_TOKEN이 없어 자동 갱신 불가')
    client, secret = require_env('CAFE24_CLIENT_ID', 'CAFE24_CLIENT_SECRET')
    LOG.warning('카페24: Access Token 인증 실패. Refresh Token으로 1회 자동 갱신 시도')
    # Refresh tokens rotate. Do not blindly retry this POST after a lost response.
    try:
        response = session.post(f'https://{mall}.cafe24api.com/api/v2/oauth/token',
                                auth=(client, secret),
                                data={'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']},
                                timeout=(10, 60))
    except Exception:
        raise RuntimeError('카페24 토큰 갱신 네트워크 실패; 재시도 전 토큰 상태 확인 필요') from None
    if not response.ok:
        reasons = {400: 'Refresh Token 만료/무효 또는 요청 설정 확인',
                   401: 'CLIENT_ID/CLIENT_SECRET 인증 실패 또는 앱 권한 확인',
                   403: '앱 접근 권한 확인'}
        raise RuntimeError(f'카페24 토큰 갱신 HTTP {response.status_code}: ' +
                           reasons.get(response.status_code, '토큰 서버 오류'))
    try:
        payload = response.json()
        if not all(isinstance(payload.get(k), str) and payload[k] for k in ('access_token', 'refresh_token')):
            raise ValueError()
    except Exception:
        raise RuntimeError('카페24 토큰 갱신 응답에 새 Access/Refresh Token이 없습니다') from None
    tokens.update({k: payload[k] for k in ('access_token', 'refresh_token')})
    write_json(TOKEN_CACHE, dict(mall_id=mall, client_id=client, **tokens,
                               expires_at=payload.get('expires_at'),
                               refresh_token_expires_at=payload.get('refresh_token_expires_at')))
    LOG.info('카페24: 토큰 갱신 및 회전된 Refresh Token 저장 완료')
    return tokens['access_token']


def safe_failure(exc):
    message = str(exc)
    for key, value in os.environ.items():
        if any(word in key for word in ('TOKEN', 'SECRET', 'PASSWORD')) and len(value) > 4:
            message = message.replace(value, '[REDACTED]')
    return message[:500]


def normalize_cafe24(order, target, source):
    paid = order.get('payment_date')
    if not paid or as_date(paid) != target:
        raise ValueError('카페24 결제일이 요청일과 다릅니다')
    items = order.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('카페24 품목 상세 누락')
    # Allocate order-level paid cash (excluding delivery) by item selling amount.
    # This preserves coupons/points rather than multiplying list price alone.
    amount = order.get('actual_order_amount')
    if not isinstance(amount, dict) or 'payment_amount' not in amount:
        raise ValueError('카페24 actual_order_amount.payment_amount 누락')
    if 'shipping_fee' not in amount:
        raise ValueError('카페24 배송비 분리 금액 누락')
    revenue = won(money(amount['payment_amount']) - money(amount['shipping_fee']))
    weights = [(money(i['product_price']) + money(i.get('option_price', 0))) * money(i['quantity']) for i in items]
    total = sum(weights)
    if total <= 0 or revenue < 0:
        raise ValueError('카페24 결제액 배분 불가')
    allocated = Decimal(0)
    rows = []
    for index, (item, weight) in enumerate(zip(items, weights)):
        value = revenue - allocated if index == len(items) - 1 else won(revenue * weight / total)
        allocated += value
        rows.append(dict(channel='cafe24', order_id=str(order['order_id']),
                         item_id=str(item['order_item_code']), payment_date=str(paid),
                         product_code=item.get('custom_product_code') or item.get('custom_variant_code') or item.get('product_code', ''),
                         product_name=item.get('product_name', ''), quantity=item['quantity'],
                         sales_amount=str(value), status=item.get('order_status', ''),
                         currency=order.get('currency', 'KRW'), source_json=source))
    return rows


def fetch_cafe24(target, session):
    mall = require_env('CAFE24_MALL_ID')[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', mall):
        raise ValueError('CAFE24_MALL_ID 형식 오류')
    base = f'https://{mall}.cafe24api.com/api/v2/admin'
    tokens = cafe24_tokens(mall)
    token = tokens['access_token']
    refreshed = False
    if not token:
        token = refresh_cafe24(session, mall, tokens)
        refreshed = True
    headers = {'Authorization': f'Bearer {token}'}
    if os.getenv('CAFE24_API_VERSION'):
        headers['X-Cafe24-Api-Version'] = os.environ['CAFE24_API_VERSION']
    pages, rows, seen = [], [], set()
    for offset in range(0, 15001, 1000):
        params = dict(start_date=f'{target} 00:00:00', end_date=f'{target} 23:59:59',
                      date_type='pay_date', embed='items', limit=1000, offset=offset,
                      shop_no=int(os.getenv('CAFE24_SHOP_NO', '1')))
        try:
            page = request_json(session, 'GET', base + '/orders', headers=headers, params=params)
        except APIHTTPError as exc:
            if exc.status_code != 401 or refreshed:
                raise
            LOG.warning('카페24 주문조회 API HTTP 401: Access Token 만료/무효 또는 앱 접근권한 확인 필요')
            refreshed = True
            headers['Authorization'] = 'Bearer ' + refresh_cafe24(session, mall, tokens)
            page = request_json(session, 'GET', base + '/orders', headers=headers, params=params)
        orders = page.get('orders')
        if not isinstance(orders, list):
            raise ValueError('카페24 orders 응답 구조 오류')
        pages.append(page)
        for order in orders:
            normalized = normalize_cafe24(order, target, f'{target}.json')
            for row in normalized:
                key = (row['order_id'], row['item_id'])
                if key in seen:
                    raise ValueError('카페24 페이지 간 품목 중복')
                seen.add(key)
                rows.append(row)
        if len(orders) < 1000:
            return pages, rows
    raise RuntimeError('카페24 offset 한도 도달: 시간 구간을 분할해 재수집해야 합니다')


def naver_token(session):
    import bcrypt
    client, secret = require_env('NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET')
    timestamp = str(int(time.time() * 1000))
    signature = base64.b64encode(bcrypt.hashpw(f'{client}_{timestamp}'.encode(), secret.encode())).decode()
    data = dict(client_id=client, timestamp=timestamp, client_secret_sign=signature,
                grant_type='client_credentials', type=os.getenv('NAVER_AUTH_TYPE', 'SELF'))
    if data['type'] == 'SELLER':
        data['account_id'] = require_env('NAVER_ACCOUNT_ID')[0]
    try:
        return request_json(session, 'POST', 'https://api.commerce.naver.com/external/v1/oauth2/token', data=data)['access_token']
    except APIHTTPError as exc:
        raise RuntimeError(f'스마트스토어 토큰 발급 API HTTP {exc.status_code}: Client ID/Secret·서명·판매자 권한 확인') from None


def normalize_smartstore(entry, target, source):
    content = entry['content']
    order, product = content['order'], content['productOrder']
    if as_date(order['paymentDate']) != target:
        raise ValueError('네이버 결제일이 요청일과 다릅니다')
    return dict(channel='smartstore', order_id=str(order['orderId']),
                item_id=str(entry['productOrderId']), payment_date=order['paymentDate'],
                product_code=product.get('optionManageCode') or product.get('sellerProductCode', ''),
                product_name=product.get('productName', ''), quantity=product.get('initialQuantity', product['quantity']),
                sales_amount=str(money(product.get('initialPaymentAmount', product['totalPaymentAmount']))),
                status=product.get('productOrderStatus', ''), currency='KRW', source_json=source)


def fetch_smartstore(target, session):
    headers = {'Authorization': f'Bearer {naver_token(session)}'}
    pages, rows, seen = [], [], set()
    start = datetime.combine(target, dt_time.min, KST)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    for number in range(1, 10001):
        params = {'from': start.isoformat(timespec='milliseconds'), 'to': end.isoformat(timespec='milliseconds'),
                  'rangeType': 'PAYED_DATETIME', 'pageSize': 300, 'page': number}
        try:
            page = request_json(session, 'GET', 'https://api.commerce.naver.com/external/v1/pay-order/seller/product-orders',
                                headers=headers, params=params)
        except APIHTTPError as exc:
            raise RuntimeError(f'스마트스토어 주문조회 API HTTP {exc.status_code}: 발급 토큰·주문조회 권한 확인') from None
        data = page.get('data', {})
        contents = data.get('contents')
        if not isinstance(contents, list):
            raise ValueError('네이버 data.contents 응답 구조 오류')
        pages.append(page)
        for entry in contents:
            row = normalize_smartstore(entry, target, f'{target}.json')
            if row['item_id'] in seen:
                raise ValueError('네이버 페이지 간 상품주문 중복')
            seen.add(row['item_id'])
            rows.append(row)
        if len(contents) < 300:
            return pages, rows
    raise RuntimeError('네이버 페이지 한도 초과')


def run(target=None, channels=('cafe24', 'smartstore')):
    import requests
    load_env()
    target = target or yesterday()
    results = {}
    with requests.Session() as session:
        for channel in channels:
            folder = ROOT / 'raw_data' / 'sales' / channel
            try:
                pages, rows = globals()['fetch_' + channel](target, session)
                # No file is published if fetching/normalizing fails.
                write_json(folder / f'{target}.json', {'payment_date': target, 'pages': pages})
                write_csv(folder / f'{target}.csv', rows, SALES_FIELDS)
                results[channel] = len(rows)
            except Exception as exc:
                reason = safe_failure(exc)
                if channel == 'smartstore' and '401' in reason:
                    reason += ' (스마트스토어 Client ID/Secret·전자서명·판매자 권한 확인)'
                existing = sorted(folder.glob('*.csv'))
                latest = str(existing[-1]) if existing else None
                LOG.warning('%s 수집 실패 [%s]: %s. 기존 파일 유지: %s. 다음 채널/결산 계속',
                            channel, type(exc).__name__, reason, latest or '기존 파일 없음')
                results[channel] = dict(status='fallback', reason=reason, existing_latest=latest)
    return results


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=yesterday())
    parser.add_argument('--channel', choices=['all', 'cafe24', 'smartstore'], default='all')
    args = parser.parse_args()
    print(run(args.date, ('cafe24', 'smartstore') if args.channel == 'all' else (args.channel,)))


if __name__ == '__main__':
    main()
