"""Offline selected-source snapshots. Immutable releases, one atomic pointer."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
try:
    from .common import ROOT, read_json, write_json
except ImportError:
    from common import ROOT, read_json, write_json


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal(intake, evidence, output):
    try:
        from .browser_intake import coupang
    except ImportError:
        from browser_intake import coupang
    _, audit=coupang(intake)
    paths=[p for folder in (intake,evidence,ROOT/'master_data') for p in folder.rglob('*') if p.is_file()]
    paths.append(ROOT/'config/food_filter_rules.json')
    # Exclude file-password containers: they are not consumed by this pipeline.
    paths=[p.resolve() for p in paths if p.suffix!='.dpapi']
    bundle=dict(version=1,intake_root=str(intake.resolve()),evidence_root=str(evidence.resolve()),
        period=audit['period'],captured_at=audit['captured_at'],basis=audit['basis'],
        revenue=audit['revenue'],quantity=audit['option_quantity'],
        source_hashes={str(p):sha(p) for p in paths})
    write_json(output,bundle)
    return bundle


def verify_bundle(path):
    b=read_json(path)
    if b['version']!=1 or b['basis']!='coupang_sales_analysis':raise ValueError('unsupported bundle')
    if b.get('multichannel_policy'):
        if str(Path(b['multichannel_policy']).resolve()) not in b['source_hashes']:raise ValueError('multichannel policy not pinned')
        for name in b['source_hashes']:
            meta=Path(str(name)+'.meta.json')
            if meta.exists() and str(meta.resolve()) not in b['source_hashes']:raise ValueError('consumed metadata not pinned')
    # A selected replacement directory may not acquire unpinned files after seal.
    for field in ('review_intake_root','bank_file','hometax_root'):
        if not b.get(field):continue
        selected=Path(b[field]).resolve()
        paths=[selected] if selected.is_file() else [p for p in selected.rglob('*') if p.is_file() and p.suffix!='.dpapi']
        if not paths or any(str(p.resolve()) not in b['source_hashes'] for p in paths):
            raise ValueError('auxiliary source not pinned in bundle')
    for name,digest in b['source_hashes'].items():
        if sha(Path(name))!=digest:raise ValueError('bundle source changed: '+Path(name).name)
    return b


def plan(bundle, target):
    b=verify_bundle(bundle)
    from web_dashboard.cloud_storage import load_cloud_env,CloudStore
    load_cloud_env(ROOT)
    return dict(mode='offline_selected_bundle',requested_sales_date=str(target),period=b['period'],
        source_as_of=b['period']['end'],source_captured_at=b['captured_at'],
        missing_target=not b['period']['start']<=str(target)<=b['period']['end'],
        cloud_configured=bool(CloudStore().configured),external_actions=False,
        stages=['verify pinned source hashes','generate in unique temporary directory',
                'verify totals, scope, null policies and Excel rows','recheck source hashes',
                'retain candidate; explicit publish only'],
        legacy_batch=['optional dependency install','Coupang/WMS/API collection','reconciliation','Excel/JSON canonical writes',
                      'cloud backup --require-cloud','git_sync copies JSON only, commits and pushes'],
        morning_entrypoint='아침자동실행.bat -> morning_entry.py; no legacy follow-up')


def validate_output(folder,bundle,target):
    from openpyxl import load_workbook
    from decimal import Decimal
    d=read_json(folder/'dashboard_data.json');s=d['sales_profit'];month=target.strftime('%Y-%m')
    if d['metadata']['report_date']!=str(target):raise ValueError('wrong target date')
    for r in s['daily_series']:
        if not bundle['period']['start']<=r['date']<=min(str(target),bundle['period']['end']):raise ValueError('out of source range')
        if sum(c['revenue'] for c in r['channels'])!=r['revenue']:raise ValueError('channel total mismatch')
    totals=[sum(r['revenue'] for r in s['daily_series']),sum(r['revenue'] for r in s['transactions']),sum(r['revenue'] for r in s.get('product_ranking',[]))]
    if month==bundle['period']['start'][:7]:
        if any(v!=s['monthly']['revenue'] for v in totals):raise ValueError('selected view totals differ')
        if str(target)>=bundle['period']['end'] and totals[0]!=bundle['revenue']:raise ValueError('bundle total differs')
        if s['monthly']['contribution_profit'] is not None or d['profit_rollup']['profit'] is not None:raise ValueError('unverified profit')
    if str(target)>bundle['period']['end'] and s['daily_kpi']['revenue'] is not None:raise ValueError('missing date invented')
    for row in d['monthly_series']:
        p=folder/f"{row['month']}_식품부문_손익결산서.xlsx"
        book=load_workbook(p,read_only=True,data_only=True)
        try:
            # Read original detail amounts, not formula caches.
            amount=sum((Decimal(str(r[7])) for r in book['매출상세'].iter_rows(min_row=2,values_only=True) if r[7] is not None),Decimal(0))
            if amount!=Decimal(str(row['revenue'] or 0)):raise ValueError('Excel/JSON mismatch')
        finally:book.close()
    return d


def build(bundle, target, store):
    b=verify_bundle(bundle);store=Path(store)
    input_paths=[p for name in ('sales','bank','hometax','inventory') for p in (ROOT/'raw_data'/name).rglob('*') if p.is_file()]
    inputs={str(p.resolve()):sha(p) for p in input_paths}
    # Selected intake originals/copies are processed inputs too. Preservation
    # must retain their digests so PC-only retention can prove actual use.
    inputs.update(b['source_hashes'])
    stage=store/'candidates'/uuid.uuid4().hex
    command=[sys.executable,str(ROOT/'scripts/run_daily_pipeline.py'),'--date',str(target),'--skip-fetch','--skip-cloud',
        '--intake-root',b['intake_root'],'--evidence-root',b['evidence_root'],'--output-dir',str(stage)]
    for field, flag in (('review_intake_root','--review-intake-root'), ('bank_file','--bank-file'), ('hometax_root','--hometax-root')):
        if b.get(field):command.extend([flag,b[field]])
    stage.mkdir(parents=True)
    child_env=os.environ.copy()
    child_env['PYTHONIOENCODING']='utf-8'
    # The inner explicit intake invocation must not recursively select the
    # outer bundle through persistent scheduler environment variables.
    for key in ('DASHBOARD_SOURCE_BUNDLE','DASHBOARD_SNAPSHOT_STORE'):
        child_env.pop(key,None)
    with (stage/'build.log').open('w',encoding='utf-8') as log:
        subprocess.run(command,cwd=ROOT,stdout=log,stderr=log,check=True,env=child_env)
    d=validate_output(stage,b,target)
    d['metadata']['sales_as_of']=min(str(target),b['period']['end'])
    d['source_selection']=dict(bundle_sha256=sha(bundle),period=b['period'],source_as_of=b['period']['end'],
        source_captured_at=b['captured_at'],requested_sales_date=str(target),
        missing_dates=[str(date.fromisoformat(b['period']['end'])+timedelta(days=n)) for n in range(1,max(0,(target-date.fromisoformat(b['period']['end'])).days)+1)],
        fallback_allowed=False)
    if b.get('daily_collection'):
        d['daily_collection']=b['daily_collection']
    if b.get('multichannel_policy'):
        try:from .multichannel_review import apply
        except ImportError:from multichannel_review import apply
        d=apply(stage,d,b)
        write_json(stage/'dashboard_data.json',d)
        validate_output(stage,b,target)
    write_json(stage/'dashboard_data.json',d)
    if b.get('inventory_sources'):
        try:from .inventory_intake import attach
        except ImportError:from inventory_intake import attach
        attach(stage,b,target)
    verify_bundle(bundle)
    if any(sha(Path(p))!=h for p,h in inputs.items()):raise ValueError('financial inputs changed during build')
    write_json(stage/'input_hashes.json',inputs)
    write_json(stage/'snapshot.json',dict(bundle_sha256=sha(bundle),target=str(target),
        source_as_of=b['period']['end'],source_captured_at=b['captured_at'],validated=True,
        files={p.name:sha(p) for p in stage.iterdir() if p.suffix in ('.json','.xlsx') and p.name!='snapshot.json'}))
    return stage


def verify_snapshot(folder):
    manifest=read_json(folder/'snapshot.json')
    if manifest.get('validated') is not True:raise ValueError('snapshot not validated')
    for name,h in manifest['files'].items():
        p=(folder/name).resolve()
        if p.parent!=folder.resolve() or sha(p)!=h:raise ValueError('snapshot hash mismatch')
    if 'dashboard_data.json' not in manifest['files']:raise ValueError('missing dashboard')
    return manifest


def publish(candidate,store,allow_older=False):
    store=store.resolve();candidate=candidate.resolve()
    if not candidate.is_relative_to(store/'candidates'):raise ValueError('candidate outside store')
    m=verify_snapshot(candidate)
    pointer=store/'current.json';old=read_json(pointer) if pointer.exists() else None
    if old and old['directory']==str(candidate.relative_to(store)):return old
    if old and not allow_older:
        current=resolve(store)
        if m['target'] < verify_snapshot(current)['target']:
            raise ValueError('stale candidate; use explicit rollback')
        previous_manifest=verify_snapshot(current)
        if (m['source_as_of'],m['source_captured_at']) < (previous_manifest['source_as_of'],previous_manifest['source_captured_at']):
            raise ValueError('older source bundle; use explicit rollback')
    # The pointer is the only commit point. A failed replace leaves the old
    # JSON and all Excel files reachable together; no rollback rewriting.
    state=dict(directory=str(candidate.relative_to(store)),manifest_sha256=sha(candidate/'snapshot.json'),
               previous=old['directory'] if old else None,bundle_sha256=m['bundle_sha256'])
    write_json(pointer,state)
    return state


def resolve(store):
    state=read_json(store/'current.json');folder=(store/state['directory']).resolve()
    if not folder.is_relative_to((store/'candidates').resolve()):raise ValueError('invalid snapshot pointer')
    if sha(folder/'snapshot.json')!=state['manifest_sha256']:raise ValueError('invalid snapshot manifest')
    verify_snapshot(folder)
    return folder


def rollback(store):
    state=read_json(store/'current.json');previous=state.get('previous')
    if not previous:raise ValueError('no previous snapshot')
    return publish(store/previous,store,allow_older=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['seal','plan','build','publish','rollback','resolve'])
    p.add_argument('--bundle',type=Path);p.add_argument('--intake-root',type=Path);p.add_argument('--evidence-root',type=Path)
    p.add_argument('--store',type=Path);p.add_argument('--candidate',type=Path);p.add_argument('--date',type=date.fromisoformat)
    a=p.parse_args()
    if a.action=='seal':result=seal(a.intake_root,a.evidence_root,a.bundle)
    elif a.action=='plan':result=plan(a.bundle,a.date)
    elif a.action=='build':result=str(build(a.bundle,a.date,a.store))
    elif a.action=='publish':result=publish(a.candidate,a.store)
    elif a.action=='rollback':result=rollback(a.store)
    else:result=str(resolve(a.store))
    print(json.dumps(result,ensure_ascii=False,default=str))


if __name__=='__main__':main()
