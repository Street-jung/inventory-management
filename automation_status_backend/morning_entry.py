"""Offline morning rehearsal; explicit selection, opt-in local publication."""
import argparse
from contextlib import contextmanager
from datetime import date,datetime,timezone
import json
import hashlib
import os
from pathlib import Path
import sys
try:
    from .common import yesterday,read_json,write_json
except ImportError:
    from common import yesterday,read_json,write_json


@contextmanager
def store_lock(store):
    store.mkdir(parents=True,exist_ok=True)
    lock=store/'.morning.lock'
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try:
        os.write(fd,str(os.getpid()).encode());os.close(fd)
        yield
    finally:
        lock.unlink()


def run(bundle,store,target,publish_local=False):
    try:
        from . import operations_snapshot as ops
    except ImportError:
        import operations_snapshot as ops
    b=ops.verify_bundle(bundle)
    bundle_digest=ops.sha(bundle)
    with store_lock(store):
        previous_summary=read_json(store/'daily_summary.json') if (store/'daily_summary.json').exists() else {}
        last_success=previous_summary.get('last_success')
        summary=dict(requested_date=str(target),source_as_of=b['period']['end'],candidate_id=None,
                     current_id=Path(read_json(store/'current.json').get('directory','')).name if (store/'current.json').exists() else None,
                     publication_status='candidate_only',last_success=last_success)
        def save_summary():
            summary['notification_key']=hashlib.sha256(json.dumps(dict(bundle=bundle_digest,date=str(target),
                source_as_of=b['period']['end'],status=summary['publication_status']),sort_keys=True).encode()).hexdigest()
            write_json(store/'daily_summary.json',summary)
            write_json(store/'telegram_preview.json',dict(send=False,summary=summary))
        state=dict(requested_date=str(target),source_as_of=b['period']['end'],candidate=None,current=None,
                   external_actions=False,stage='build',status='running')
        def log():
            with (store/'morning_events.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(dict(state,at=datetime.now(timezone.utc).isoformat()),ensure_ascii=False)+'\n')
        try:
            log();candidate=ops.build(bundle,target,store);state['candidate']=str(candidate.resolve())
            summary['candidate_id']=candidate.name
            state['stage']='verify';ops.verify_snapshot(candidate);log()
            data=read_json(candidate/'dashboard_data.json')
            summary.update(
                refresh_status='미수집일 포함' if data['source_selection']['missing_dates'] else '검증된 원천 기간',
                unreflected_channels=[k for k,v in data['sales_profit']['source_status'].items() if v['aggregation']['status']!='included_provisional'],
                action_needed='새 날짜 원천 확보 및 총괄 검수' if data['source_selection']['missing_dates'] else '잠정 지표 제약 확인')
            write_json(store/'candidate_summary.json',summary)
            if publish_local:
                state['stage']='publish';log();ops.publish(candidate,store)
            state['stage']='resolve'
            if (store/'current.json').exists():state['current']=str(ops.resolve(store))
            summary['current_id']=Path(state['current']).name if state['current'] else None
            if publish_local:
                summary['publication_status']='published'
                summary['last_success']=dict(current_id=summary['current_id'],requested_date=str(target),source_as_of=b['period']['end'])
            save_summary()
            state.update(stage='complete',status='success');log()
            return state
        except Exception as exc:
            summary['publication_status']='failed'
            save_summary()
            state.update(status='failed',error_code=type(exc).__name__)
            log();raise


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if '--free-automation' in args:
        args.remove('--free-automation')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from scripts.free_automation import main as free_main
        return free_main(args)
    if '--daily-generation' in args:
        args.remove('--daily-generation')
        try:
            from .daily_generation import main as daily_main
        except ImportError:
            from daily_generation import main as daily_main
        return daily_main(args)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-bundle',type=Path,default=os.getenv('DASHBOARD_SOURCE_BUNDLE'))
    p.add_argument('--snapshot-store',type=Path,default=os.getenv('DASHBOARD_SNAPSHOT_STORE'))
    p.add_argument('--date',type=date.fromisoformat,default=yesterday())
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--publish-local',action='store_true')
    a=p.parse_args(argv)
    if not a.source_bundle or not a.snapshot_store:p.error('source-bundle and snapshot-store required; no legacy fallback')
    try:
        if a.dry_run:
            try:
                from .operations_snapshot import plan
            except ImportError:
                from operations_snapshot import plan
            result=plan(a.source_bundle,a.date)
            result.update(entrypoint='safe_morning',publish=False,store=str(a.snapshot_store))
        else:result=run(a.source_bundle,a.snapshot_store,a.date,a.publish_local)
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps(dict(status='failed',stage='selection_or_execution',requested_date=str(a.date),
                              source_as_of=None,candidate=None,current=None,error_code=type(exc).__name__),ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
