"""Typed public status sidecar. No financial generation mutation or auto-activation."""
import base64
from datetime import datetime,timezone,timedelta,date
import hashlib,json,re
from pathlib import Path
from .free_automation_io import read,write,now,lock
from .generation_transport import HTTPGenerationTransport,OutcomeUnknown

MAX_BYTES=24576
CHANNELS=['coupang_growth','coupang_wing','smartstore','cafe24','gaemi_inventory','bank','hometax','b2b']
STATUSES=['not_collected','authentication_required','collection_failed','partial','collected_zero','collected','awaiting_ego']
OUTCOMES=['running','dry_run','awaiting_sources','candidate_ready','published_verified','failed','unknown','already_completed','recovered','retry_exhausted','halted']
PUBLICATIONS=['not_attempted','candidate_only','publishing','published','failed','unknown']
REASONS=['none','authentication_required','source_missing','source_partial','validation_failed','publication_failed','outcome_unknown','quota_exceeded','not_collected']
def field(t,**kw):return dict(t=t,**kw)
def obj(**fields):return field('object',fields=fields)
def nullable(t,**kw):return field(t,nullable=True,**kw)
REV=nullable('revision');TIME=nullable('time');DAY=nullable('date');COUNT=nullable('int',minimum=0,maximum=10000000)
SCHEMA=obj(version=field('int',minimum=1,maximum=1),producer_id=field('string',pattern='[a-z0-9_-]{1,40}'),sequence=field('int',minimum=1,maximum=9007199254740991),reported_at=field('time'),attempted_at=TIME,reason=field('enum',values=REASONS),requested_date=field('date'),run_slot=field('enum',values=['morning','afternoon','manual']),data_revision=REV,candidate_revision=REV,publication_status=field('enum',values=PUBLICATIONS),run_status=field('enum',values=OUTCOMES),last_verified=field('object',nullable=True,fields=dict(revision=field('revision'),requested_date=field('date'),source_as_of=field('date'),verified_at=field('time'))),channels=field('map',keys=CHANNELS,item=obj(status=field('enum',values=STATUSES),source_as_of=DAY,attempted_at=TIME,success_at=TIME,processed_count=COUNT,included_count=COUNT,excluded_count=COUNT,pending_count=COUNT,error_code=field('enum',values=REASONS),business_zero_verified=field('bool'))))

def instant(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})',value):raise ValueError('invalid_status_time')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('invalid_status_time')
    return result

def check(value,spec):
    if value is None and spec.get('nullable'):return
    t=spec['t']
    if t=='object':
        if not isinstance(value,dict) or set(value)!=set(spec['fields']):raise ValueError('invalid_status_fields')
        for k,s in spec['fields'].items():check(value[k],s)
    elif t=='map':
        if not isinstance(value,dict) or not set(value)<=set(spec['keys']):raise ValueError('invalid_status_channel')
        for v in value.values():check(v,spec['item'])
    elif t=='int':
        if type(value) is not int or not spec['minimum']<=value<=spec['maximum']:raise ValueError('invalid_status_count')
    elif t=='bool':
        if type(value) is not bool:raise ValueError('invalid_status_bool')
    elif t=='enum':
        if not isinstance(value,str) or value not in spec['values']:raise ValueError('invalid_status_enum')
    elif t=='revision':
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('invalid_status_revision')
    elif t=='date':
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) or date.fromisoformat(value).isoformat()!=value:raise ValueError('invalid_status_date')
    elif t=='time':instant(value)
    elif t=='string':
        if not isinstance(value,str) or not re.fullmatch(spec['pattern'],value):raise ValueError('invalid_status_string')
    else:raise ValueError('invalid_status_schema')

def encode(doc):
    check(doc,SCHEMA)
    blob=json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    if len(blob)>MAX_BYTES:raise ValueError('status_too_large')
    return blob

def decode(blob):
    if len(blob)>MAX_BYTES:raise ValueError('status_too_large')
    doc=json.loads(blob)
    if encode(doc)!=blob:raise ValueError('noncanonical_status_document')
    return doc

def project(state,sequence,producer='pc-primary'):
    last=state.get('last_success');verified=None
    if last and last.get('external_status_verified') is True:
        verified={k:last[k] for k in ('revision','requested_date','source_as_of','verified_at')}
    channels={}
    for name,v in state.get('channels',{}).items():
        if name not in CHANNELS:continue
        status=v.get('status','not_collected')
        reason='authentication_required' if status=='authentication_required' else 'validation_failed' if status=='collection_failed' else 'source_partial' if status=='partial' else 'source_missing' if status in ('not_collected','awaiting_ego') else 'none'
        channels[name]=dict(status=status,source_as_of=v.get('source_as_of'),attempted_at=v.get('attempted_at'),success_at=v.get('success_at'),error_code=reason,business_zero_verified=False,**{k:v.get(k) for k in ('processed_count','included_count','excluded_count','pending_count')})
    reason='quota_exceeded' if state.get('error_code') in ('cumulative_upload_budget_exhausted','quota_budget_exceeded','upload_budget_invalid') else 'outcome_unknown' if state['run_status']=='unknown' else 'validation_failed' if state['run_status']=='failed' else 'source_missing' if state['run_status']=='awaiting_sources' else 'none'
    doc=dict(version=1,producer_id=producer,sequence=sequence,reported_at=now(),attempted_at=state.get('attempted_at'),reason=reason,requested_date=state['requested_date'],run_slot=state.get('run_slot','manual'),data_revision=verified['revision'] if verified else None,candidate_revision=state.get('candidate_revision'),publication_status=state.get('publication_status','not_attempted'),run_status=state['run_status'],last_verified=verified,channels=channels)
    encode(doc);return doc

def queue(store,state):
    """Caller holds the pipeline lock. This writes a local outbox only."""
    store=Path(store);counter=store/'status_sequence.json';sequence=read(counter)['sequence']+1 if counter.exists() else 1
    doc=project(state,sequence);write(counter,dict(sequence=sequence));write(store/'status_outbox.json',doc)
    return doc

class StatusHTTP:
    def __init__(self,*args,**kwargs):self.client=HTTPGenerationTransport(*args,**kwargs)
    def get(self):
        result=self.client.call('automation_status_get')
        if result is None:return None
        blob=base64.b64decode(result['content_base64'],validate=True)
        if len(blob)!=result['bytes'] or hashlib.sha256(blob).hexdigest()!=result['id']:raise ValueError('status_integrity_failed')
        return dict(id=result['id'],document=decode(blob))
    def context(self):
        result=self.client.call('automation_status_context')
        if not isinstance(result,dict) or set(result)!={'status_id','data_revision'}:raise ValueError('invalid_status_context')
        check(result['status_id'],REV);check(result['data_revision'],REV);return result
    def put(self,doc,expected):
        raw=encode(doc);identity=hashlib.sha256(raw).hexdigest()
        try:self.client.call('automation_status_put',dict(id=identity,bytes=len(raw),content_base64=base64.b64encode(raw).decode(),expected_status_id=expected))
        except OutcomeUnknown:pass
        try:result=self.get()
        except Exception:raise OutcomeUnknown('status outcome unresolved; keep same outbox') from None
        if result and result['id']==identity:return dict(id=identity,confirmed_by_read=True)
        raise OutcomeUnknown('status outcome unresolved; keep same outbox') from None

def sync(reader,cache,data_revision,*,at=None,ttl_seconds=21600):
    cache=Path(cache);at=at or datetime.now(timezone.utc)
    outcome='unavailable'
    try:
        remote=reader.get()
        if remote is None:raise ValueError('missing_status')
        doc=remote['document'];encode(doc);age=(at-instant(doc['reported_at'])).total_seconds()
        if not -300<=age<=ttl_seconds:outcome='stale';raise ValueError('status_stale')
        if doc['data_revision']!=data_revision:outcome='different_revision';raise ValueError('status_revision_mismatch')
        current=read(cache/'status_document.json') if (cache/'status_document.json').exists() else None
        if current and (current['producer_id']!=doc['producer_id'] or doc['sequence']<current['sequence'] or instant(doc['reported_at'])<instant(current['reported_at'])):outcome='regression';raise ValueError('status_regression')
        write(cache/'status_document.json',doc);outcome='received'
    except Exception:pass
    delivery=dict(outcome=outcome,checked_at=at.isoformat(),data_revision=data_revision,retained=outcome!='received')
    write(cache/'status_delivery.json',delivery);return delivery

def view(cache,data_revision,*,at=None):
    """Recheck at request time; an old success cache never becomes a fresh success."""
    cache=Path(cache);at=at or datetime.now(timezone.utc)
    try:
        doc=read(cache/'status_document.json');encode(doc)
        delivery=read(cache/'status_delivery.json')
        age=(at-instant(doc['reported_at'])).total_seconds()
        usable=doc['data_revision']==data_revision and -300<=age<=21600
        return dict(document=doc,display_current=usable and delivery['outcome']=='received',delivery=delivery)
    except Exception:return dict(document=None,display_current=False,delivery=dict(outcome='unavailable'))

def frontend_view(cache,data_revision,*,at=None):
    result=view(cache,data_revision,at=at);doc=result['document']
    if doc is None:return dict(version=1,available=False,channels={},run_status='not_collected',last_success=None)
    usable=result['display_current'];last=doc['last_verified']
    published=usable and doc['publication_status']=='published' and doc['run_status'] in ('published_verified','recovered','already_completed') and last is not None and last['revision']==data_revision and last['requested_date']==doc['requested_date'] and last['source_as_of']==doc['requested_date']
    channels=json.loads(json.dumps(doc['channels']))
    public_run=doc['run_status'] if usable else 'failed'
    if not published and public_run in ('published_verified','already_completed','recovered'):public_run='awaiting_sources'
    if not usable:
        for record in channels.values():record.update(status='partial',error_code='status_transport_retained',business_zero_verified=False)
    return dict(version=1,available=True,requested_date=doc['requested_date'],attempted_at=doc['attempted_at'],reported_at=doc['reported_at'],data_revision=doc['data_revision'],run_slot=doc['run_slot'],error_code=doc['reason'] if usable else 'status_transport_retained',run_status=public_run,publication_status='published_verified' if published else 'failed' if not usable else 'not_attempted',
        last_success=dict(last,external_status_verified=published) if last else None,channels=channels,delivery=result['delivery'],observation_only=not published)

def send_outbox(store,writer,budget_bytes):
    """Explicit reviewed caller only; shared cumulative byte ledger, no auto-send."""
    store=Path(store)
    with lock(store):
        doc=read(store/'status_outbox.json');raw=encode(doc);identity=hashlib.sha256(raw).hexdigest()
        last=writer.get()
        if last and last['id']==identity:
            from .operations_retention import settle
            if (store/'upload_budget.json').exists():settle(store,'status:'+identity,confirmed_bytes=len(raw))
            return dict(id=identity,already_sent=True)
        context=writer.context()
        if doc['data_revision']!=context['data_revision']:
            # Observing current is not verifying a successful publication. Keep last_verified.
            doc['data_revision']=context['data_revision'];raw=encode(doc);write(store/'status_outbox.json',doc)
        from .operations_retention import reserve,settle
        policy=budget_bytes if isinstance(budget_bytes,dict) else dict(max_total_upload_bytes=budget_bytes)
        if not isinstance(budget_bytes,dict) and (type(budget_bytes) is not int or budget_bytes<=0):raise ValueError('status_upload_budget_exceeded')
        key='status:'+hashlib.sha256(raw).hexdigest()
        try:reserve(store,key,2*len(raw),policy)
        except ValueError as exc:
            if str(exc)=='cumulative_upload_budget_exhausted':raise ValueError('status_upload_budget_exceeded') from None
            raise
        result=writer.put(doc,context['status_id']);settle(store,key,confirmed_bytes=len(raw));return result
