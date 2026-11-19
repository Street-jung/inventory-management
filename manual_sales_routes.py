"""Admin-only integration point. app.py/HTML and production storage stay untouched."""
import hashlib
import json
import os
import re
from functools import wraps
from pathlib import Path
from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
try:
    from .manual_sales_store import ManualSalesError, SQLiteManualSalesStore
    from .manual_sales_service import ManualSalesService, calendar, idem_key, integer, text, uuid_value, obj
except ImportError:
    from manual_sales_store import ManualSalesError, SQLiteManualSalesStore
    from manual_sales_service import ManualSalesService, calendar, idem_key, integer, text, uuid_value, obj

MAX_BODY = 128*1024


def enabled(value):
    return value is True or (isinstance(value, str) and value.strip().lower() in ('true','1','yes'))


def postgres_module():
    try:
        from . import manual_sales_postgres
    except ImportError:
        import manual_sales_postgres
    return manual_sales_postgres


def configuration(app, probe=False):
    config = app.config
    mode = config.get('MANUAL_SALES_STORAGE_MODE', 'off')
    state = dict(enabled=enabled(config.get('MANUAL_SALES_ENABLED', False)), writable=False,
                 storage_mode=mode if mode in ('off','sqlite_local','postgres') else 'unsupported', local_only=mode!='postgres', reason=None,
                 configured=False,connection_health='not_checked',schema_ready=None,write_ready=None)
    if not state['enabled']:
        state['reason'] = 'FEATURE_DISABLED'
    elif mode=='postgres':
        pg=postgres_module()
        try:
            settings=pg.PostgresSettings()
            state['configured']=True
            if not pg.driver_available():
                state['reason']='POSTGRES_DRIVER_UNAVAILABLE'
            elif probe:
                state.update(pg.PostgresManualSalesStore(settings).health())
                state['writable']=state['write_ready'] is True
        except ManualSalesError as exc:
            state['reason']=exc.code
    elif not enabled(config.get('MANUAL_SALES_DEVELOPMENT_MODE', False)):
        state['reason'] = 'DEVELOPMENT_MODE_REQUIRED'
    elif os.getenv('RENDER') or os.getenv('RENDER_SERVICE_ID'):
        state['reason'] = 'LOCAL_STORAGE_FORBIDDEN_ON_RENDER'
    elif mode != 'sqlite_local' or not config.get('MANUAL_SALES_DB_PATH'):
        state['reason'] = 'STORAGE_NOT_CONFIGURED'
    else:
        try:
            timeout = config.get('MANUAL_SALES_SQLITE_TIMEOUT', 5)
            if type(timeout) not in (float,int) or not 0 < timeout <= 30:
                raise ValueError('timeout')
            SQLiteManualSalesStore(config['MANUAL_SALES_DB_PATH'], timeout=timeout)
            state['writable'] = True
            state['configured'] = True
        except (ManualSalesError, ValueError, TypeError):
            state['reason'] = 'STORAGE_NOT_CONFIGURED'
    return state


def service():
    state = configuration(current_app)
    if state['storage_mode']=='postgres' and state['configured'] and state['reason'] is None:
        return ManualSalesService(postgres_module().PostgresManualSalesStore(),current_app.config.get('MANUAL_SALES_PRODUCT_PROVIDER'))
    if not state['writable']:
        raise ManualSalesError(503, state['reason'], '수기 매출 기록 기능이 설정되지 않았거나 사용할 수 없는 환경입니다.')
    store = SQLiteManualSalesStore(current_app.config['MANUAL_SALES_DB_PATH'], current_app.config.get('MANUAL_SALES_SQLITE_TIMEOUT', 5))
    return ManualSalesService(store, current_app.config.get('MANUAL_SALES_PRODUCT_PROVIDER'))


def body():
    if not request.is_json:
        raise ManualSalesError(415, 'JSON_REQUIRED', 'JSON 형식으로 요청하세요.')
    if request.content_length is not None and request.content_length > MAX_BODY:
        raise ManualSalesError(413, 'REQUEST_TOO_LARGE', '요청 내용이 너무 큽니다.')
    try:
        raw = request.stream.read(MAX_BODY+1)
    except RequestEntityTooLarge:
        raise ManualSalesError(413, 'REQUEST_TOO_LARGE', '요청 내용이 너무 큽니다.') from None
    except BadRequest:
        raise ManualSalesError(400, 'MALFORMED_JSON', '유효한 JSON 요청이 아닙니다.') from None
    if len(raw) > MAX_BODY:
        raise ManualSalesError(413, 'REQUEST_TOO_LARGE', '요청 내용이 너무 큽니다.')
    try:
        def constant(_):
            raise ValueError('invalid JSON constant')
        def unique_fields(pairs):
            result={}
            for key,value in pairs:
                if key in result:
                    raise ValueError('duplicate JSON field')
                result[key]=value
            return result
        return json.loads(raw.decode('utf-8'), parse_constant=constant, object_pairs_hook=unique_fields)
    except (ValueError, UnicodeError):
        raise ManualSalesError(400, 'MALFORMED_JSON', '유효한 JSON 요청이 아닙니다.') from None


def actor():
    # Authentication is performed solely by the injected existing require_admin.
    token = request.headers.get('X-Admin-Token', '').strip()
    if not token:
        raise ManualSalesError(401, 'ADMIN_AUTH_REQUIRED', '관리자 인증이 필요합니다.')
    return dict(session_id='admin-session:'+hashlib.sha256(token.encode('utf-8')).hexdigest(), is_personal_signature=False)


def query(allowed):
    if set(request.args)-set(allowed) or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise ManualSalesError(422, 'VALIDATION_ERROR', '조회 조건을 확인하세요.', 'query')


def paging(default=20):
    def number(name, fallback, low, high):
        value = request.args.get(name, str(fallback))
        if not re.fullmatch(r'\d{1,7}', value):
            raise ManualSalesError(422, 'VALIDATION_ERROR', '페이지 조회 범위를 확인하세요.', name)
        return integer(int(value), name, low, high)
    return number('limit', default, 1, 100), number('offset', 0, 0, 1000000)


def register_manual_sales(app, require_admin):
    """Call once after defining the application's existing require_admin decorator."""
    if not callable(require_admin):
        raise TypeError('existing require_admin decorator is required')
    if 'manual_sales' in app.blueprints:
        raise ValueError('manual_sales already registered')
    api = Blueprint('manual_sales', __name__, url_prefix='/api/manual-sales')

    def protect(fn):
        @wraps(fn)
        def safe(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ManualSalesError as exc:
                result = dict(success=False, code=exc.code, message=exc.message, **exc.details)
                if exc.field:
                    result['field'] = exc.field
                return jsonify(result), exc.status
            except Exception:
                return jsonify(success=False, code='MANUAL_SALES_INTERNAL_ERROR', message='판매기록 처리 중 오류가 발생했습니다.'), 500
        return require_admin(safe)

    @api.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'X-Admin-Token'
        return response

    @api.get('/status')
    @protect
    def status():
        return jsonify(dict(success=True, **configuration(current_app,probe=True)))

    @api.get('/products')
    @protect
    def products():
        query({'q','limit'})
        limit, _ = paging()
        needle = (text(request.args.get('q', ''), 'q', 100) or '').casefold()
        ledger = service();catalog = ledger.catalog()
        items = [dict(product_code=k, product_name=v) for k,v in sorted(catalog.items()) if needle in k.casefold() or needle in v.casefold()]
        return jsonify(success=True, items=items[:limit], limit=limit, catalog_configured=ledger.product_provider is not None)

    @api.get('')
    @protect
    def listing():
        ledger = service();query({'limit','offset','status','date_from','date_to'})
        limit, offset = paging();state = request.args.get('status')
        if state is not None and state not in ('submitted','cancelled'):
            raise ManualSalesError(422, 'VALIDATION_ERROR', '조회 상태를 확인하세요.', 'status')
        start = calendar(request.args['date_from'], 'date_from') if 'date_from' in request.args else None
        end = calendar(request.args['date_to'], 'date_to') if 'date_to' in request.args else None
        if start and end and start > end:
            raise ManualSalesError(422, 'VALIDATION_ERROR', '조회 시작일과 종료일을 확인하세요.', 'date_from')
        return jsonify(ledger.store.listing(limit, offset, state, start, end))

    @api.get('/idempotency/<key>')
    @protect
    def idempotency_result(key):
        return jsonify(service().store.idempotency(idem_key(key)))

    @api.post('/idempotency/<key>/resolve')
    @protect
    def resolve_pending(key):
        ledger = service()
        key = idem_key(key)
        obj(body(), set(), 'body')
        return jsonify(ledger.store.resolve(key, actor()))

    @api.get('/<sale_id>')
    @protect
    def detail(sale_id):
        return jsonify(service().store.get(uuid_value(sale_id, 'sale_id')))

    @api.get('/<sale_id>/history')
    @protect
    def history(sale_id):
        ledger = service();query({'limit','offset'});limit, offset = paging(50)
        return jsonify(ledger.store.history(uuid_value(sale_id, 'sale_id'), limit, offset))

    def mutation(operation, sale_id=None):
        ledger = service()
        result, code, replayed = ledger.command(operation, body(), request.headers.get('Idempotency-Key'), actor(), sale_id)
        response = jsonify(result);response.status_code = code
        response.headers['Idempotency-Replayed'] = 'true' if replayed else 'false'
        return response

    @api.post('')
    @protect
    def create():
        return mutation('create')

    @api.patch('/<sale_id>')
    @protect
    def update(sale_id):
        return mutation('update', sale_id)

    @api.post('/<sale_id>/cancel')
    @protect
    def cancel(sale_id):
        return mutation('cancel', sale_id)

    app.register_blueprint(api)
    return api
