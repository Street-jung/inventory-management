"""Versioned file backups shared by the offline pipeline and Render (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import csv
import io
import re
from pathlib import Path, PurePosixPath
import time
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone


def mapping_log(diagnostic):
    """Server-side only; diagnostics must never turn a successful restore into failure."""
    try:
        logging.getLogger(__name__).warning('Cloud mapping diagnostic %s', json.dumps(diagnostic, sort_keys=True))
    except Exception:
        pass


def load_cloud_env(root):
    """Read simple .env assignments without logging secrets or requiring dotenv."""
    path = Path(root) / 'config/.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if key.isidentifier():
                os.environ.setdefault(key, value.strip('"\''))


def safe_path(root, relative):
    parts = PurePosixPath(relative)
    if parts.is_absolute() or '\\' in relative or ':' in relative or '..' in parts.parts:
        raise ValueError('Invalid backup path')
    target = (Path(root) / str(parts)).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise ValueError('Backup path escapes root')
    return target


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_inventory_sheets(path):
    """Standalone Render fallback when only web_dashboard is deployed."""
    data = Path(path).read_bytes()
    if data.startswith(b'PK'):
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            return [(s.title, list(s.values)) for s in book]
        finally:
            book.close()
    if data.startswith(b'\xd0\xcf\x11\xe0'):
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        return [(s.name, [[xlrd.xldate.xldate_as_datetime(c.value, book.datemode)
                          if c.ctype == xlrd.XL_CELL_DATE else c.value for c in s.row(i)]
                         for i in range(s.nrows)]) for s in book.sheets()]
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('cp949')
    return [('CSV', list(csv.reader(io.StringIO(text))))]


def normalized_code(value):
    if value is None:
        return ''
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value).strip()


def inventory_allowlist(path):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(book['채널코드맵핑'].values)
    finally:
        book.close()
    expected = ('판매자관리코드', '개미창고 상품코드', '개미창고 옵션명', '쿠팡 옵션 ID')
    header = next((i for i, r in enumerate(rows[:20]) if tuple(str(v).strip() for v in r[:4]) == expected), None)
    if header is None:
        raise ValueError('Invalid channel mapping headers')
    gaemi, coupang = set(), {}
    for row in rows[header + 1:]:
        if not row[0]:
            continue
        code = normalized_code(row[0]).upper()
        if row[1]:
            gaemi.add(normalized_code(row[1]))
        for option in re.split(r'[,;\n]+', normalized_code(row[3])):
            option = option.strip()
            if not option:
                continue
            if option in coupang and coupang[option] != code:
                raise ValueError('Conflicting option mapping')
            coupang[option] = code
    if not gaemi or not coupang:
        raise ValueError('Empty channel whitelist')
    return dict(path=str(path), gaemi=gaemi, coupang=coupang)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CloudRequestError(RuntimeError):
    """Allowlisted diagnostic only; never retain a response, URL or exception text."""
    def __init__(self, reason, http_status=None):
        super().__init__('Cloud request failed: ' + reason)
        self.reason = reason
        self.http_status = http_status


def request_failure(exc):
    if isinstance(exc, CloudRequestError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        return CloudRequestError('authentication_rejected' if exc.code in (401, 403) else 'http_error', exc.code)
    if isinstance(exc, TimeoutError) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)):
        return CloudRequestError('timeout')
    if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        return CloudRequestError('invalid_json_response')
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return CloudRequestError('connection_failed')
    return CloudRequestError('request_failed')


class CloudStore:
    def __init__(self, mode=None, folder=None, url=None, token=None, timeout=90):
        self.url = url if url is not None else os.environ.get('GOOGLE_APPS_SCRIPT_URL', '').strip()
        self.token = token if token is not None else os.environ.get('GOOGLE_APPS_SCRIPT_TOKEN', '').strip()
        self.folder = Path(folder or os.environ.get('CLOUD_DRIVE_DIR', '.'))
        selected = mode or os.environ.get('CLOUD_SYNC_MODE', 'auto').lower()
        self.mode = ('gas' if self.url and self.token else 'local' if os.environ.get('CLOUD_DRIVE_DIR') else 'off') if selected == 'auto' else selected
        if self.mode not in ('gas', 'local', 'off'):
            mapping_log(dict(kind='cloud_mapping_recovery', stage='configuration', reason='invalid_mode'))
            raise ValueError('CLOUD_SYNC_MODE must be auto/gas/local/off')
        if self.mode == 'gas' and not (self.url.startswith('https://') and self.token):
            mapping_log(dict(kind='cloud_mapping_recovery', stage='configuration', reason='missing_or_invalid_configuration'))
            raise ValueError('Apps Script HTTPS URL and token are required')
        if self.mode == 'local' and folder is None and not os.environ.get('CLOUD_DRIVE_DIR'):
            mapping_log(dict(kind='cloud_mapping_recovery', stage='configuration', reason='local_path_missing'))
            raise ValueError('CLOUD_DRIVE_DIR is required')
        self.timeout = timeout

    @property
    def configured(self):
        return self.mode != 'off'

    def call(self, action, payload=None):
        body = json.dumps(dict(action=action, token=self.token, payload=payload), ensure_ascii=False).encode('utf-8')
        for attempt in range(3):
            try:
                req = urllib.request.Request(self.url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    result = json.load(response)
                if not isinstance(result, dict) or not result.get('success'):
                    # Inspect only for classification. Never retain or log remote text.
                    message = result.get('error', '') if isinstance(result, dict) else ''
                    message = message.lower() if isinstance(message, str) else ''
                    reason = ('authentication_rejected' if message.startswith('unauthorized') else
                              'unsupported_action' if message.startswith(('unknown action', 'unknown backup action')) else
                              'object_missing' if message == 'object not found' else
                              'bridge_rejected' if isinstance(result, dict) else 'invalid_response')
                    raise CloudRequestError(reason)
                return result
            except Exception as exc:
                if attempt == 2:
                    raise request_failure(exc) from None
                time.sleep(attempt + 1)

    def manifest(self, revision=None):
        if revision is not None and not re.fullmatch(r'[a-f0-9]{64}', revision):
            raise ValueError('Invalid manifest revision')
        if self.mode == 'local':
            path = self.folder / 'manifests' / (revision + '.json') if revision else self.folder / 'latest.json'
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if self.mode == 'gas':
            return self.call('backup_manifest', dict(revision=revision) if revision else None).get('data')
        return None

    def put(self, content):
        sha = digest(content)
        if self.mode == 'local':
            path = self.folder / 'objects' / sha
            if not path.exists():
                atomic_write(path, content)
            if digest(path.read_bytes()) != sha:
                raise ValueError('Backup object checksum mismatch')
        elif self.mode == 'gas':
            if len(content) > 32 * 1024 * 1024:
                raise ValueError('GAS file exceeds 32 MiB; use local Drive sync for this archive')
            self.call('backup_put', dict(sha256=sha, content_base64=base64.b64encode(content).decode('ascii')))
        else:
            raise ValueError('Cloud backup is not configured')
        return sha

    def get(self, sha):
        if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('Invalid object hash')
        if self.mode == 'local':
            content = (self.folder / 'objects' / sha).read_bytes()
        else:
            content = base64.b64decode(self.call('backup_get', dict(sha256=sha))['content_base64'], validate=True)
        if digest(content) != sha:
            raise ValueError('Cloud object checksum mismatch')
        return content

    def publish(self, manifest, previous_revision):
        if self.mode == 'gas':
            return self.call('backup_publish', dict(manifest=manifest, previous_revision=previous_revision))
        # This lock protects overlapping scheduled jobs on the same sync mount.
        self.folder.mkdir(parents=True, exist_ok=True)
        lock = self.folder / '.publish.lock'
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            previous = self.manifest()
            if (previous or {}).get('revision') != previous_revision:
                if (previous or {}).get('revision') == manifest['revision']:
                    return
                raise ValueError('Backup manifest changed concurrently; retry the pipeline')
            if previous and previous.get('report_date', '') > manifest['report_date']:
                raise ValueError('Refusing to replace newer dashboard with older report')
            if previous and previous.get('dashboard_generated_at') and manifest.get('dashboard_generated_at'):
                if datetime.fromisoformat(previous['dashboard_generated_at']) > datetime.fromisoformat(manifest['dashboard_generated_at']):
                    raise ValueError('Refusing older dashboard generation')
            encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
            atomic_write(self.folder / 'manifests' / (manifest['revision'] + '.json'), encoded)
            atomic_write(self.folder / 'latest.json', encoded)
        finally:
            os.close(handle)
            lock.unlink(missing_ok=True)


def collect_files(root):
    root = Path(root)
    candidates = [root / 'web_dashboard/dashboard_data.json',
                  root / 'web_dashboard/inventory_master_store.json',
                  root / 'web_dashboard/manual_transactions.json']
    for folder in ('raw_data', 'output_excel', 'master_data'):
        candidates.extend((root / folder).rglob('*'))
    return sorted({p for p in candidates if p.is_file() and not p.is_symlink()
                   and (p.relative_to(root).parts[0] == 'raw_data' or
                        p.suffix.lower() in ('.json', '.csv', '.xlsx', '.xls', '.xlsm', '.tsv'))
                   and p.suffix.lower() not in ('.tmp', '.part')
                   and p.name.lower() != '.env' and not p.name.startswith('~$')})


def backup(root, store, dry_run=False):
    root = Path(root).resolve()
    dashboard = json.loads((root / 'web_dashboard/dashboard_data.json').read_text(encoding='utf-8'))
    report_date = dashboard['metadata']['report_date']
    previous = store.manifest() if store.configured and not dry_run else None
    entries = dict((previous or {}).get('files', {}))  # Keep removed historical inputs.
    uploaded = unchanged = total_bytes = 0
    for path in collect_files(root):
        relative = path.relative_to(root).as_posix()
        safe_path(root, relative)
        content = path.read_bytes()
        sha = digest(content)
        entry = dict(sha256=sha, size=len(content), mtime_ns=path.stat().st_mtime_ns)
        if entries.get(relative, {}).get('sha256') == sha:
            unchanged += 1
        else:
            if not dry_run:
                store.put(content)
            uploaded += 1
            total_bytes += len(content)
        entries[relative] = entry
    created = datetime.now(timezone.utc).isoformat()
    manifest = dict(schema_version=1, report_date=report_date,
                    dashboard_generated_at=dashboard['metadata'].get('generated_at'), created_at=created, files=entries)
    manifest['revision'] = digest(json.dumps(manifest, sort_keys=True).encode('utf-8'))
    if not dry_run:
        store.publish(manifest, (previous or {}).get('revision'))
    return dict(mode=store.mode, dry_run=dry_run, files=len(entries), uploaded=uploaded,
                unchanged=unchanged, uploaded_bytes=total_bytes, revision=manifest['revision'])


def restore_latest(root, store):
    """Existing restore with server-log-only diagnostics; no additional remote calls."""
    trace = dict(stage='backup_manifest', object_kind='none', manifest_status='not_checked')
    failure = None
    try:
        result = _restore_latest(root, store, trace)
        return result
    except Exception as exc:
        failure = exc
        raise
    finally:
        # Mapping verification is local and diagnostic only. Restore selection,
        # all-object-before-write behavior and consumer bindings remain unchanged.
        mapping = dict(mapping_status='not_checked', mapping_sha_verified=False,
                       mapping_parse_verified=False, mapping_content_sha256=None,
                       gaemi_keys=None, coupang_keys=None)
        entry = trace.pop('mapping_entry', None)
        if trace['manifest_status'] == 'received':
            mapping['mapping_status'] = 'not_in_manifest' if entry is None else 'cache_missing'
            if entry is not None:
                try:
                    path = Path(root) / 'master_data/오키드상품정보.xlsx'
                    if path.is_file():
                        content = path.read_bytes()
                        if digest(content) != entry['sha256'] or len(content) != entry['size']:
                            mapping['mapping_status'] = 'cache_integrity_mismatch'
                        else:
                            mapping.update(mapping_sha_verified=True, mapping_status='mapping_invalid',
                                           mapping_content_sha256=digest(content))
                            allowed = inventory_allowlist(path)
                            mapping.update(mapping_status='verified', mapping_parse_verified=True,
                                           gaemi_keys=len(allowed['gaemi']), coupang_keys=len(allowed['coupang']))
                except Exception:
                    # Diagnostic failure must not alter the existing restore outcome.
                    if mapping['mapping_status'] != 'mapping_invalid':
                        mapping['mapping_status'] = 'cache_check_failed'
        diagnostic = dict(kind='cloud_mapping_recovery', **trace, **mapping,
                          restore_outcome='failed' if failure else result['status'],
                          reason=(failure.reason if isinstance(failure, CloudRequestError) else
                                  'validation_failed' if isinstance(failure, (ValueError, KeyError, TypeError)) else
                                  'cache_io_failed' if isinstance(failure, OSError) else
                                  'restore_failed' if failure else 'none'),
                          http_status=failure.http_status if isinstance(failure, CloudRequestError) else None)
        mapping_log(diagnostic)


def _restore_latest(root, store, trace):
    """Refresh dashboard/reports/mapping/raw stock, preserving live admin state."""
    root = Path(root)
    manifest = store.manifest()
    if not manifest:
        trace['manifest_status'] = 'empty'
        return dict(status='empty', restored=0)
    trace['stage'] = 'manifest_validation'
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        trace['manifest_status'] = 'invalid'
        raise ValueError('Invalid cloud manifest shape')
    trace['manifest_status'] = 'received'
    files = manifest.get('files', {})
    trace['mapping_entry'] = files.get('master_data/오키드상품정보.xlsx')
    dashboard_key = 'web_dashboard/dashboard_data.json'
    if dashboard_key not in files:
        raise ValueError('Cloud manifest has no dashboard')
    local = root / dashboard_key
    if local.exists():
        try:
            current = json.loads(local.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            current = {}
        if current.get('metadata', {}).get('report_date', '') > manifest.get('report_date', ''):
            return dict(status='local_newer', restored=0)
        generated = current.get('metadata', {}).get('generated_at')
        if generated and manifest.get('dashboard_generated_at'):
            if datetime.fromisoformat(generated) > datetime.fromisoformat(manifest['dashboard_generated_at']):
                return dict(status='local_newer', restored=0)
    selected = {dashboard_key}
    selected.update(p for p in files if p.startswith('master_data/') and p.endswith('.xlsx'))
    selected.update(p for p in files if p.startswith('output_excel/') and p.endswith(('.xlsx', '.csv')))
    for marker in ('3pl', '개미', 'coupang'):
        stock = [p for p in files if p.startswith('raw_data/inventory/') and marker in p.lower()
                 and p.endswith(('.csv', '.xlsx', '.xls'))]
        if stock:
            selected.add(max(stock, key=lambda p: (PurePosixPath(p).name[:10], files[p].get('mtime_ns', 0))))
    pending = []
    for relative in sorted(selected):
        trace['stage'] = 'backup_get'
        trace['object_kind'] = ('whitelist' if relative == 'master_data/오키드상품정보.xlsx' else
                                'master_other' if relative.startswith('master_data/') else
                                'report' if relative.startswith('output_excel/') else
                                'inventory' if relative.startswith('raw_data/inventory/') else 'dashboard')
        path = safe_path(root, relative)
        entry = files[relative]
        if path.exists() and digest(path.read_bytes()) == entry['sha256']:
            continue
        content = store.get(entry['sha256'])
        if len(content) != entry['size']:
            raise ValueError('Cloud object length mismatch')
        if relative == dashboard_key:
            parsed = json.loads(content)
            if not isinstance(parsed.get('sales_profit'), dict) or parsed.get('metadata', {}).get('report_date') != manifest['report_date']:
                raise ValueError('Invalid cloud dashboard')
        pending.append((relative, path, content))
    # Validate every object before writes. Publish dashboard last.
    trace['stage'] = 'cache_write'
    for relative, path, content in sorted(pending, key=lambda row: row[0] == dashboard_key):
        atomic_write(path, content)
    trace['stage'] = 'complete'
    trace['object_kind'] = 'none'
    return dict(status='synced', restored=len(pending), revision=manifest['revision'],
                report_date=manifest['report_date'], created_at=manifest['created_at'])


def restore_archive(destination, store, revision=None):
    """Recover original filenames into an empty directory, including historical raw files."""
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Archive restore destination must be empty')
    manifest = store.manifest(revision)
    if not manifest:
        raise ValueError('Backup manifest not found')
    # Validate all paths before writing any files.
    targets = [(safe_path(destination, p), entry) for p, entry in manifest['files'].items()]
    for path, entry in targets:
        content = store.get(entry['sha256'])
        if len(content) != entry['size']:
            raise ValueError('Backup size mismatch')
        atomic_write(path, content)
    return dict(restored=len(targets), revision=manifest['revision'], destination=str(destination))
