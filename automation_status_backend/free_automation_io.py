"""Atomic state, crash-releasing OS lock, bounded non-sensitive event log."""
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path


def now():return datetime.now(timezone.utc).isoformat()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_bytes(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if Path(temp).exists():Path(temp).unlink()


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if Path(temp).exists():Path(temp).unlink()


@contextmanager
def lock(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    stream=(folder/'.automation.lock').open('a+b')
    try:
        stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close();raise RuntimeError('automation_already_running') from None
    try:yield
    finally:
        stream.seek(0)
        if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()


def event(folder,value,max_bytes=1024*1024,backups=4):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);path=folder/'events.jsonl'
    if path.exists() and path.stat().st_size>=max_bytes:
        oldest=folder/f'events.{backups}.jsonl'
        if oldest.exists():oldest.unlink()
        for n in range(backups-1,0,-1):
            old=folder/f'events.{n}.jsonl'
            if old.exists():os.replace(old,folder/f'events.{n+1}.jsonl')
        os.replace(path,folder/'events.1.jsonl')
    with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(dict(value,at=now()),ensure_ascii=False)+'\n')
