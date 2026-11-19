"""Filesystem-only cloud protocol simulator. No network adapter is enabled."""
import hashlib
import json
from pathlib import Path
import uuid
import re
from datetime import date,datetime,timezone
try:
    from .common import read_json,write_json
    from .operations_snapshot import verify_snapshot,sha
    from .morning_entry import store_lock
except ImportError:
    from common import read_json,write_json
    from operations_snapshot import verify_snapshot,sha
    from morning_entry import store_lock


def current(root):
    p=root/'current.json'
    return read_json(p)['revision'] if p.exists() else None


def safe_revision(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid revision')
    return value


class FilesystemTransport:
    def __init__(self,root):self.root=Path(root).resolve()
    def put(self,revision,name,data):
        safe_revision(revision)
        if Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('invalid object path')
        p=(self.root/'generations'/revision/name).resolve()
        if not p.is_relative_to(self.root/'generations'):raise ValueError('object escaped root')
        p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists():
            if p.read_bytes()!=data:raise ValueError('immutable object conflict')
        else:
            temp=p.with_name(uuid.uuid4().hex[:8]+'.tmp')
            try:
                temp.write_bytes(data);temp.replace(p)
            finally:temp.unlink(missing_ok=True)
    def get(self,revision,name):
        safe_revision(revision)
        if Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('invalid object path')
        p=(self.root/'generations'/revision/name).resolve()
        if not p.is_relative_to(self.root/'generations'):raise ValueError('object escaped root')
        return p.read_bytes()


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def temporal_axes(meta):
    """Strict calendar dates and aware ISO capture time, compared in UTC."""
    result={}
    for key in ('requested_date','source_as_of'):
        value=meta[key]
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            raise ValueError('invalid ISO date: '+key)
        result[key]=date.fromisoformat(value)
    value=meta['captured_at']
    if not isinstance(value,str) or 'T' not in value:raise ValueError('invalid ISO captured_at')
    captured=datetime.fromisoformat(value.replace('Z','+00:00'))
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError('captured_at requires timezone')
    result['captured_at']=captured.astimezone(timezone.utc)
    return result


def require_no_regression(new,old):
    incoming,previous=temporal_axes(new),temporal_axes(old)
    for key in incoming:
        if incoming[key]<previous[key]:raise ValueError('generation time regression: '+key)


def payload(candidate):
    verify_snapshot(candidate)
    d=read_json(candidate/'dashboard_data.json')
    names=['dashboard_data.json']+[f"{r['month']}_식품부문_손익결산서.xlsx" for r in d['monthly_series']]
    if len(names)!=len(set(names)):raise ValueError('duplicate payload file')
    for name in names:
        if Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('invalid payload path')
    files={name:(candidate/name).read_bytes() for name in names}
    meta=dict(schema=d['schema_version'],requested_date=d['metadata']['report_date'],
        source_as_of=d['source_selection']['source_as_of'],captured_at=d['source_selection']['source_captured_at'])
    temporal_axes(meta)
    return files,meta


def verify_generation(transport,revision):
    raw=transport.get(revision,'manifest.json')
    if hashlib.sha256(raw).hexdigest()!=revision:raise ValueError('manifest digest mismatch')
    m=json.loads(raw)
    if m['schema']!='1.5':raise ValueError('unsupported schema')
    temporal_axes(m)
    data={}
    for name,record in m['files'].items():
        blob=transport.get(revision,name)
        if len(blob)!=record['bytes'] or hashlib.sha256(blob).hexdigest()!=record['sha256']:raise ValueError('object hash mismatch')
        data[name]=blob
    d=json.loads(data['dashboard_data.json'])
    expected={'dashboard_data.json'}|{f"{r['month']}_식품부문_손익결산서.xlsx" for r in d['monthly_series']}
    if set(data)!=expected:raise ValueError('incomplete or extra generation payload')
    if (d['schema_version'],d['metadata']['report_date'],d['source_selection']['source_as_of'],d['source_selection']['source_captured_at'])!=(m['schema'],m['requested_date'],m['source_as_of'],m['captured_at']):raise ValueError('metadata mismatch')
    return m,data


def publish(candidate,transport,expected_previous):
    files,meta=payload(Path(candidate));root=transport.root
    with store_lock(root):
        old=current(root)
        records={k:dict(bytes=len(v),sha256=hashlib.sha256(v).hexdigest()) for k,v in files.items()}
        if old:
            existing,_=verify_generation(transport,old)
            if existing['files']==records and all(existing[k]==v for k,v in meta.items()):return old
        if old!=expected_previous:raise ValueError('previous revision conflict')
        if old:require_no_regression(meta,existing)
        m=dict(meta,previous_revision=old,files=records)
        raw=encoded(m);revision=hashlib.sha256(raw).hexdigest()
        for name,blob in files.items():transport.put(revision,name,blob)
        transport.put(revision,'manifest.json',raw)
        verify_generation(transport,revision)
        write_json(root/'current.json',dict(revision=revision,previous_revision=old,rollback=False))
        return revision


def rollback(transport,revision,expected_previous):
    safe_revision(revision)
    with store_lock(transport.root):
        old=current(transport.root)
        if old!=expected_previous:raise ValueError('previous revision conflict')
        verify_generation(transport,revision)
        write_json(transport.root/'current.json',dict(revision=revision,previous_revision=old,rollback=True))


def consume(transport,cache):
    cache=Path(cache);pointer=read_json(transport.root/'current.json');rev=pointer['revision']
    with store_lock(cache):
        m,data=verify_generation(transport,rev)
        old=current(cache)
        if old and old!=rev:
            local,_=verify_generation(FilesystemTransport(cache),old)
            if pointer.get('rollback') is not True:require_no_regression(m,local)
        # An interrupted cache never becomes current. Files are immutable and
        # verified before pointer switch, just like the fake remote.
        local=FilesystemTransport(cache)
        for name,blob in data.items():local.put(rev,name,blob)
        local.put(rev,'manifest.json',transport.get(rev,'manifest.json'))
        verify_generation(local,rev)
        if read_json(transport.root/'current.json')!=pointer:raise ValueError('remote changed while consuming')
        write_json(cache/'current.json',pointer)
        return cache/'generations'/rev
