"""Prepared private server-log probe; no HTTP route, network or filesystem writes."""
import hashlib
import json
import logging
import os
import re
from pathlib import Path

_done = False

def file_state(path, dashboard=False):
    value = {'exists': None, 'readable': None}
    try:
        value['exists'] = path.is_file()
        if not value['exists']:
            return value
        raw = path.read_bytes()
        value['readable'] = True
        if dashboard:
            meta = json.loads(raw).get('metadata', {})
            for key, pattern in [('report_date', r'\d{4}-\d{2}-\d{2}'),
                                 ('generated_at', r'\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+')]:
                text = meta.get(key)
                value[key] = text if isinstance(text, str) and re.fullmatch(pattern, text) else None
        else:
            value.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    except Exception:
        value['readable'] = False
    return value

def snapshot(app, base_dir, cache, generation_configured):
    base, cache = Path(base_dir), Path(cache)
    configured = app.config.get('INVENTORY_RAW_DIR')
    candidates = [('configured', Path(configured) if configured else None),
                  ('parent', base.parent / 'raw_data/inventory'),
                  ('base', base / 'raw_data/inventory'),
                  ('pc', Path('C:/AI/경영관리/raw_data/inventory'))]
    raw = {'configured': bool(configured), 'configured_exists': None, 'selected': 'none',
           'selected_exists': False, 'has_stock_file': False}
    for label, folder in candidates:
        try:
            present = folder is not None and folder.is_dir()
            if label == 'configured': raw['configured_exists'] = present
            if not present: continue
            raw.update(selected=label, selected_exists=True)
            raw['has_stock_file'] = any(p.is_file() and p.suffix.lower() in ('.csv', '.xls', '.xlsx')
                and any(m in p.name.lower() for m in ('3pl', 'gaemi', '개미', 'coupang', '쿠팡')) for p in folder.iterdir())
            break
        except Exception:
            raw.update(selected='unobserved', selected_exists=None, has_stock_file=None)
            break
    candidate = app.config.get('CHANNEL_WHITELIST_FILE')
    return dict(kind='mapping_seed_runtime', generation_configured=bool(generation_configured),
        reader_initialized=getattr(app, '_generation_reader', None) is not None,
        cache_dashboard=file_state(cache / 'web_dashboard/dashboard_data.json', dashboard=True),
        cache_mapping=file_state(cache / 'master_data/오키드상품정보.xlsx'),
        whitelist_configured=bool(candidate),
        configured_mapping=file_state(Path(candidate)) if candidate else None, raw=raw,
        explicit_json=bool(os.environ.get('DASHBOARD_DATA_FILE') or
            (app.config.get('DASHBOARD_DATA_FILE') if app.config.get('FIXED_RELEASE_DATA') else None)),
        explicit_pnl=bool(os.environ.get('PNL_OUTPUT_DIR') or
            (app.config.get('PNL_OUTPUT_DIR') if app.config.get('FIXED_RELEASE_DATA') else None)))

def log_once(app, base_dir, cache, generation_configured):
    global _done
    if _done: return
    try:
        logging.getLogger(__name__).warning('Mapping seed runtime %s',
            json.dumps(snapshot(app, base_dir, cache, generation_configured), sort_keys=True))
        _done = True
    except Exception:
        pass
