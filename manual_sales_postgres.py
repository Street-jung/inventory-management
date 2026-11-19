"""Explicit PostgreSQL adapter. Never migrates, provisions, or falls back."""
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
try:
    from .manual_sales_common import ManualSalesError
    from .manual_sales_operations import LedgerOperations
except ImportError:
    from manual_sales_common import ManualSalesError
    from manual_sales_operations import LedgerOperations

MIGRATION = Path(__file__).with_name('migrations')/'manual_sales_postgres_v1.sql'


def migration_sha():
    return hashlib.sha256(MIGRATION.read_text(encoding='utf-8').replace('\r\n','\n').encode('utf-8')).hexdigest()


def driver():
    try:
        import psycopg
        return psycopg
    except ImportError:
        raise ManualSalesError(503,'POSTGRES_DRIVER_UNAVAILABLE','PostgreSQL 드라이버가 설치되지 않았습니다.') from None


def driver_available():
    return importlib.util.find_spec('psycopg') is not None


class PostgresSettings:
    def __init__(self, migration=False, url_env=None):
        name=url_env or ('MANUAL_SALES_MIGRATION_URL' if migration else 'MANUAL_SALES_DATABASE_URL')
        if name not in ('MANUAL_SALES_DATABASE_URL','MANUAL_SALES_MIGRATION_URL','MANUAL_SALES_RESTORE_URL'):
            raise ManualSalesError(503,'STORAGE_NOT_CONFIGURED','허용되지 않은 연결 설정입니다.')
        self._dsn=os.environ.get(name,'')
        try:
            parsed=urlsplit(self._dsn)
            if parsed.scheme not in ('postgres','postgresql') or not parsed.hostname or not parsed.username or not parsed.path.strip('/') or parsed.query or parsed.fragment:
                raise ValueError()
            from urllib.parse import unquote
            if not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}',unquote(parsed.path[1:])):raise ValueError()
            if parsed.port is not None and not 1<=parsed.port<=65535:raise ValueError()
            profile=os.environ.get('MANUAL_SALES_PG_CONNECTION','')
            self.options=dict(connect_timeout=int(os.environ.get('MANUAL_SALES_PG_CONNECT_TIMEOUT','3')),
                              application_name='manual-sales-migration' if migration else 'manual-sales-app')
            if not 1<=self.options['connect_timeout']<=10:raise ValueError()
            if profile=='render_internal':
                if not parsed.password or not (os.environ.get('RENDER') or os.environ.get('RENDER_SERVICE_ID')):raise ValueError()
                self.options['sslmode']='require'
            elif profile=='external':
                if not parsed.password:raise ValueError()
                try:ipaddress.ip_address(parsed.hostname)
                except ValueError:pass
                else:raise ValueError()
                cert=os.environ.get('MANUAL_SALES_PG_SSLROOTCERT','system')
                if cert!='system' and (not Path(cert).is_absolute() or not Path(cert).is_file()):raise ValueError()
                self.options.update(sslmode='verify-full',sslrootcert=cert)
            elif profile=='local_test':
                if os.environ.get('MANUAL_SALES_PG_TEST_ONLY')!='1' or os.environ.get('RENDER') or os.environ.get('RENDER_SERVICE_ID') or parsed.hostname not in ('localhost','127.0.0.1','::1'):raise ValueError()
                self.options['sslmode']='disable'
            else:raise ValueError()
        except (ValueError,TypeError,OSError):
            raise ManualSalesError(503,'STORAGE_NOT_CONFIGURED','PostgreSQL 연결 설정을 확인하세요.') from None

    def connect(self):
        # No exception/DSN is forwarded to application logs or responses.
        # Explicit BEGIN below must be the first transaction, without an implicit BEGIN.
        return driver().connect(self._dsn,autocommit=True,**self.options)

    def libpq_environment(self):
        """Secret child-process env only; never print it or pass URLs as CLI arguments."""
        from urllib.parse import unquote
        parsed=urlsplit(self._dsn)
        env=os.environ.copy()
        # Ambient libpq settings cannot redirect a reviewed target.
        for key in tuple(env):
            if key.startswith('PG'):env.pop(key)
        env.update(PGHOST=parsed.hostname,PGPORT=str(parsed.port or 5432),PGUSER=unquote(parsed.username),
                   PGPASSWORD=unquote(parsed.password or ''),PGDATABASE=unquote(parsed.path[1:]),
                   PGSSLMODE=self.options['sslmode'],PGCONNECT_TIMEOUT=str(self.options['connect_timeout']),
                   PGAPPNAME='manual-sales-synthetic-recovery')
        if 'sslrootcert' in self.options:env['PGSSLROOTCERT']=self.options['sslrootcert']
        return env


class Row:
    def __init__(self, values, names):
        self.values=values;self.names=names
    def __getitem__(self,key):
        return self.values[key] if isinstance(key,int) else self.values[self.names.index(key)]


class Cursor:
    def __init__(self,cursor):self.cursor=cursor
    @property
    def rowcount(self):return self.cursor.rowcount
    def _row(self,row):
        return None if row is None else Row(row,[col.name for col in self.cursor.description])
    def fetchone(self):return self._row(self.cursor.fetchone())
    def fetchall(self):return [self._row(row) for row in self.cursor.fetchall()]


class Connection:
    """Only shared, static operation SQL uses qmark placeholders; parameters stay bound."""
    def __init__(self,raw):self.raw=raw
    def execute(self,sql,parameters=()):
        return Cursor(self.raw.execute(sql.replace('?','%s'),parameters))


class PostgresManualSalesStore(LedgerOperations):
    def __init__(self, settings=None):self.settings=settings or PostgresSettings()

    def _check_schema(self,raw):
        row=raw.execute('SELECT version,migration_sha FROM manual_sales.schema_meta WHERE singleton=1').fetchone()
        if row is None or row[0]!=1 or row[1]!=migration_sha():
            raise ManualSalesError(503,'STORAGE_SCHEMA_NOT_READY','원장 스키마 준비가 완료되지 않았습니다.')

    def _check_permissions(self,raw):
        availability=raw.execute("SELECT pg_is_in_recovery(),current_setting('default_transaction_read_only')='on'").fetchone()
        if any(availability):
            raise ManualSalesError(503,'STORAGE_PERMISSIONS','쓰기 가능한 주 원장 연결이 필요합니다.')
        row=raw.execute("SELECT r.rolsuper,r.rolcreatedb,r.rolcreaterole,n.nspowner=r.oid,has_schema_privilege(current_user,'manual_sales','CREATE'),pg_has_role(current_user,n.nspowner,'MEMBER') FROM pg_roles r CROSS JOIN pg_namespace n WHERE r.rolname=current_user AND n.nspname='manual_sales'").fetchone()
        if row is None or any(row):
            raise ManualSalesError(503,'STORAGE_ROLE_UNSAFE','앱 전용 최소권한 역할이 필요합니다.')
        required={'schema_meta':'SELECT','sales':'SELECT,INSERT,UPDATE','events':'SELECT,INSERT',
                  'idempotency':'SELECT,INSERT','idempotency_abandonments':'SELECT,INSERT',
                  'request_keys':'SELECT,INSERT','online_links':'SELECT,INSERT,UPDATE','reflection_queue':'SELECT,INSERT,UPDATE'}
        for table,privileges in required.items():
            # has_table_privilege comma-list means ANY, so check every privilege separately.
            for privilege in privileges.split(','):
                if not raw.execute('SELECT has_table_privilege(current_user,%s,%s)',('manual_sales.'+table,privilege)).fetchone()[0]:
                    raise ManualSalesError(503,'STORAGE_PERMISSIONS','원장 앱 권한이 준비되지 않았습니다.')
        if not raw.execute("SELECT has_column_privilege(current_user,'manual_sales.request_keys','key','UPDATE')").fetchone()[0]:
            raise ManualSalesError(503,'STORAGE_PERMISSIONS','요청 키 잠금 권한이 준비되지 않았습니다.')
        for table in ('events','idempotency','idempotency_abandonments'):
            if raw.execute('SELECT has_table_privilege(current_user,%s,%s)',('manual_sales.'+table,'UPDATE,DELETE')).fetchone()[0]:
                raise ManualSalesError(503,'STORAGE_ROLE_UNSAFE','감사기록 변경 권한은 앱에 허용되지 않습니다.')

    @contextmanager
    def transaction(self, readonly=False, snapshot=False):
        raw=None
        try:
            raw=self.settings.connect()
            raw.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY' if snapshot else 'BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY' if readonly else 'BEGIN ISOLATION LEVEL READ COMMITTED')
            raw.execute('SET LOCAL search_path=manual_sales,pg_catalog,pg_temp')
            raw.execute("SET LOCAL lock_timeout='3s'")
            raw.execute("SET LOCAL statement_timeout='5s'")
            self._check_schema(raw);self._check_permissions(raw)
            yield Connection(raw)
            raw.commit()  # A commit failure is never acknowledged or automatically retried.
        except ManualSalesError:
            if raw is not None:
                try:raw.rollback()
                except Exception:pass
            raise
        except Exception as exc:
            if raw is not None:
                try:raw.rollback()
                except Exception:pass
            code=getattr(exc,'sqlstate',None)
            reason='STORAGE_BUSY' if code in ('55P03','40001','40P01','57014') else 'STORAGE_SCHEMA_NOT_READY' if code in ('42P01','3F000') else 'STORAGE_PERMISSIONS' if code=='42501' else 'STORAGE_UNAVAILABLE'
            raise ManualSalesError(503,reason,'PostgreSQL 원장을 사용할 수 없습니다.') from None
        finally:
            if raw is not None:
                try:raw.close()
                except Exception:pass

    def _lock_key(self,conn,key):
        conn.execute('INSERT INTO request_keys(key) VALUES(?) ON CONFLICT(key) DO NOTHING',(key,))
        conn.execute('SELECT key FROM request_keys WHERE key=? FOR UPDATE',(key,)).fetchone()

    def _lock_sale(self,conn,sale_id):
        conn.execute('SELECT sale_id FROM sales WHERE sale_id=? FOR UPDATE',(sale_id,)).fetchone()

    def health(self):
        try:
            with self.transaction(readonly=True):pass
            return dict(connection_health='healthy',schema_ready=True,write_ready=True,reason=None)
        except ManualSalesError as exc:
            schema=False if exc.code=='STORAGE_SCHEMA_NOT_READY' else None
            return dict(connection_health='unavailable' if exc.code=='STORAGE_UNAVAILABLE' else 'healthy' if exc.code in ('STORAGE_SCHEMA_NOT_READY','STORAGE_PERMISSIONS','STORAGE_ROLE_UNSAFE') else 'not_checked',schema_ready=schema,write_ready=False,reason=exc.code)
