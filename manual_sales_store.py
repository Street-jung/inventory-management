"""Transactional local-development SQLite adapter; no production fallback."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
try:
    from .manual_sales_common import ManualSalesError, encoded, now
    from .manual_sales_operations import LedgerOperations
except ImportError:
    from manual_sales_common import ManualSalesError, encoded, now
    from manual_sales_operations import LedgerOperations


class SQLiteManualSalesStore(LedgerOperations):
    APP_ID = 0x4D534C31
    RESOLUTION_SCHEMA = (
        'CREATE TABLE idempotency_abandonments (key TEXT PRIMARY KEY NOT NULL, abandoned_at TEXT NOT NULL, actor TEXT NOT NULL)',
        "CREATE TRIGGER immutable_abandonments_update BEFORE UPDATE ON idempotency_abandonments BEGIN SELECT RAISE(ABORT,'immutable abandonment'); END",
        "CREATE TRIGGER immutable_abandonments_delete BEFORE DELETE ON idempotency_abandonments BEGIN SELECT RAISE(ABORT,'immutable abandonment'); END",
        "CREATE TRIGGER exclusive_committed_key BEFORE INSERT ON idempotency WHEN EXISTS(SELECT 1 FROM idempotency_abandonments WHERE key=NEW.key) BEGIN SELECT RAISE(ABORT,'key abandoned'); END",
        "CREATE TRIGGER exclusive_abandoned_key BEFORE INSERT ON idempotency_abandonments WHEN EXISTS(SELECT 1 FROM idempotency WHERE key=NEW.key) BEGIN SELECT RAISE(ABORT,'key committed'); END",
    )
    SCHEMA = (
        'CREATE TABLE IF NOT EXISTS sales (sale_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL CHECK(status IN (\'submitted\',\'cancelled\')), sale_date TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, sale_id TEXT NOT NULL REFERENCES sales(sale_id), revision INTEGER NOT NULL, type TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, before_document TEXT, after_document TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(sale_id,revision))',
        'CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, operation TEXT NOT NULL, request_hash TEXT NOT NULL, sale_id TEXT NOT NULL REFERENCES sales(sale_id), response TEXT NOT NULL, http_status INTEGER NOT NULL, created_at TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS online_links (channel TEXT NOT NULL, order_key TEXT NOT NULL, item_key TEXT NOT NULL, sale_id TEXT NOT NULL REFERENCES sales(sale_id), line_id TEXT NOT NULL, PRIMARY KEY(channel,order_key,item_key))',
        'CREATE TABLE IF NOT EXISTS reflection_queue (sale_id TEXT NOT NULL REFERENCES sales(sale_id), revision INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(sale_id,revision))',
        'CREATE INDEX IF NOT EXISTS sales_date_idx ON sales(sale_date,status)',
        "CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END",
        "CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END",
    )

    def __init__(self, path, timeout=5):
        self.path = Path(path)
        self.timeout = timeout
        if not self.path.is_absolute() or self.path.suffix.lower() not in ('.sqlite3', '.sqlite', '.db') or not self.path.parent.is_dir() or self.path.is_dir() or self.path.is_symlink():
            raise ManualSalesError(503, 'STORAGE_NOT_CONFIGURED', '로컬 판매기록 저장 경로가 설정되지 않았습니다.')

    @contextmanager
    def transaction(self):
        conn = None
        try:
            conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            app_id = conn.execute('PRAGMA application_id').fetchone()[0]
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if (app_id == 0 and tables) or app_id not in (0, self.APP_ID) or version not in (0, 1, 2):
                raise ManualSalesError(503, 'STORAGE_UNAVAILABLE', '판매기록 저장소 형식을 확인할 수 없습니다.')
            if app_id == 0:
                for statement in self.SCHEMA:
                    conn.execute(statement)
                conn.execute(f'PRAGMA application_id={self.APP_ID}')
                version = 1
            elif version not in (1, 2):
                raise ManualSalesError(503, 'STORAGE_UNAVAILABLE', '판매기록 저장소 버전을 확인할 수 없습니다.')
            if version == 1:
                for statement in self.RESOLUTION_SCHEMA:
                    conn.execute(statement)
                conn.execute('PRAGMA user_version=2')
            yield conn
            conn.commit()
        except ManualSalesError:
            if conn is not None:
                conn.rollback()
            raise
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.rollback()
            busy = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
            raise ManualSalesError(503, 'STORAGE_BUSY' if busy else 'STORAGE_UNAVAILABLE', '판매기록 저장소를 사용할 수 없습니다.') from None
        except (sqlite3.Error, OSError, ValueError, TypeError):
            if conn is not None:
                conn.rollback()
            raise ManualSalesError(503, 'STORAGE_UNAVAILABLE', '판매기록 저장소를 사용할 수 없습니다.') from None
        finally:
            if conn is not None:
                conn.close()

